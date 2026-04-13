"""
AI Team Slack Bot (실시간 양방향)

Slack #ai-team 채널 ↔ Claude 팀리더 실시간 대화:
- 채널 메시지 → Claude에 전달
- Claude 출력 → 슬랙 스레드에 실시간 전달
- 스레드 답글 → Claude에 입력 (질문 답변, 확인 등)
"""

import os
import sys
import logging
import threading
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from bridge import (
    send_input, is_session_alive, is_leader_ready,
    get_team_status, stop_leader_session, OutputMonitor,
)

# 환경변수 로드
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), '..', 'logs', 'slack_bot.log'),
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger("ai-team-bot")

# Slack App 초기화
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# === 활성 대화 추적 ===
# Claude 출력을 어떤 슬랙 스레드로 보낼지 추적
_active_channel: str | None = None
_active_thread: str | None = None
_lock = threading.Lock()
_monitor: OutputMonitor | None = None
_slack_client = None  # Slack WebClient 참조


def _post_to_slack(text: str):
    """Claude 출력을 현재 활성 슬랙 스레드에 전송"""
    with _lock:
        channel = _active_channel
        thread_ts = _active_thread

    if not channel or not thread_ts or not _slack_client:
        logger.warning("활성 스레드 없음 — Claude 출력을 전달할 수 없습니다.")
        return

    try:
        # 긴 메시지 분할
        if len(text) <= 3900:
            _slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text
            )
        else:
            chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
            for i, chunk in enumerate(chunks):
                prefix = f"({i+1}/{len(chunks)})\n" if len(chunks) > 1 else ""
                _slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts, text=f"{prefix}{chunk}"
                )
    except Exception as e:
        logger.error(f"슬랙 전송 실패: {e}")


def _set_active_thread(channel_id: str, thread_ts: str):
    """활성 대화 스레드 설정"""
    with _lock:
        global _active_channel, _active_thread
        _active_channel = channel_id
        _active_thread = thread_ts


def _is_ai_team_channel(channel_name: str) -> bool:
    target = os.environ.get("AI_TEAM_CHANNEL", "ai-team")
    return channel_name == target


def _get_channel_name(client, channel_id: str) -> str:
    try:
        info = client.conversations_info(channel=channel_id)
        return info["channel"]["name"]
    except Exception:
        return ""


def _get_user_name(client, user_id: str) -> str:
    try:
        info = client.users_info(user=user_id)
        return info["user"]["real_name"]
    except Exception:
        return user_id


@app.event("message")
def handle_message(event, say, client):
    """채널/스레드 메시지 처리"""
    global _slack_client
    _slack_client = client

    # 봇 자신의 메시지 무시
    if event.get("bot_id") or event.get("subtype"):
        return

    channel_id = event["channel"]
    user_id = event.get("user", "unknown")
    text = event.get("text", "").strip()
    ts = event["ts"]
    thread_ts = event.get("thread_ts")  # None이면 최상위 메시지

    if not text:
        return

    # 채널 확인
    channel_name = _get_channel_name(client, channel_id)
    if not _is_ai_team_channel(channel_name):
        return

    logger.info(f"메시지 수신 - user: {user_id}, thread: {thread_ts}, text: {text[:100]}")

    # === 특수 명령어 ===
    text_lower = text.lower().strip()
    if text_lower in ("상태", "!status", "/status"):
        status = get_team_status()
        say(text=f"```\n{status}\n```", thread_ts=thread_ts or ts)
        return

    if text_lower in ("재시작", "!restart", "/restart"):
        say(text=":arrows_counterclockwise: 팀리더 세션을 재시작합니다...", thread_ts=thread_ts or ts)
        _restart_leader()
        say(text=":white_check_mark: 팀리더 세션이 재시작되었습니다.", thread_ts=thread_ts or ts)
        return

    # === 스레드 답글 → Claude에 입력 전달 ===
    if thread_ts is not None:
        # 스레드 안의 답글 = Claude에게 보내는 응답 (예: "y", "진행해", 추가 지시)
        logger.info(f"스레드 답글 → Claude 입력: {text[:100]}")

        # 활성 스레드 갱신 (답글이 온 스레드로)
        _set_active_thread(channel_id, thread_ts)

        try:
            if _monitor:
                _monitor.reset_offset()
            send_input(text)
        except Exception as e:
            say(text=f":x: 전달 실패: {e}", thread_ts=thread_ts)
        return

    # === 새 작업 (최상위 메시지) ===
    user_name = _get_user_name(client, user_id)

    # 이 메시지의 ts를 스레드 루트로 사용
    _set_active_thread(channel_id, ts)

    say(text=":robot_face: 작업을 접수했습니다. 팀리더에게 전달합니다.", thread_ts=ts)

    # Claude에게 전달
    try:
        prompt = f"[Slack 요청 - {user_name}] {text}"

        # 모니터 오프셋 리셋 (입력 에코 무시)
        if _monitor:
            _monitor.reset_offset()

        send_input(prompt)
    except Exception as e:
        logger.error(f"Claude 전달 실패: {e}", exc_info=True)
        say(
            text=f":x: 팀리더에게 전달할 수 없습니다.\n```\n{str(e)[:500]}\n```\n"
                 "`tmux attach -t ai-team`으로 확인해주세요.",
            thread_ts=ts
        )


@app.event("app_mention")
def handle_mention(event, say, client):
    """@멘션도 동일 처리"""
    handle_message(event, say, client)


def _restart_leader():
    """리더 세션 재시작"""
    global _monitor
    if _monitor:
        _monitor.stop()
    stop_leader_session()

    # start.sh가 담당하므로 여기선 안내만
    logger.warning("리더 세션 종료됨. start.sh로 재시작 필요.")


def main():
    global _monitor

    log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(log_dir, exist_ok=True)

    if not is_session_alive():
        logger.error("ai-team tmux 세션이 없습니다. start.sh로 시작해주세요.")
        sys.exit(1)

    # 출력 모니터 시작 — Claude 출력을 슬랙에 실시간 전달
    _monitor = OutputMonitor(on_output=_post_to_slack)
    _monitor.start()

    logger.info("AI Team Slack Bot 시작 (실시간 양방향 모드)")

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
