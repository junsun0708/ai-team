"""
AI Team Slack Bot

Slack #ai-team 비공개 채널에서 메시지를 수신하여
Claude 팀리더에게 전달하고 결과를 슬랙에 회신합니다.

Socket Mode로 동작하므로 별도 웹서버가 필요 없습니다.
"""

import os
import sys
import logging
import threading
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from bridge import send_message, start_leader_session, is_session_alive, get_team_status

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

# 진행 중인 작업 추적 (중복 요청 방지)
_active_tasks: dict[str, bool] = {}
_lock = threading.Lock()


def _is_ai_team_channel(channel_name: str) -> bool:
    """ai-team 채널인지 확인"""
    target = os.environ.get("AI_TEAM_CHANNEL", "ai-team")
    return channel_name == target


@app.event("message")
def handle_message(event, say, client):
    """채널 메시지 처리"""
    # 봇 자신의 메시지 무시
    if event.get("bot_id") or event.get("subtype"):
        return

    channel_id = event["channel"]
    user_id = event.get("user", "unknown")
    text = event.get("text", "").strip()
    thread_ts = event.get("thread_ts") or event["ts"]

    if not text:
        return

    # 채널 정보 조회
    try:
        channel_info = client.conversations_info(channel=channel_id)
        channel_name = channel_info["channel"]["name"]
    except Exception:
        channel_name = ""

    if not _is_ai_team_channel(channel_name):
        return

    logger.info(f"메시지 수신 - user: {user_id}, text: {text[:100]}")

    # 특수 명령어 처리
    if text.lower() in ("/status", "!status", "상태"):
        status = get_team_status()
        say(text=f"```\n{status}\n```", thread_ts=thread_ts)
        return

    if text.lower() in ("/restart", "!restart", "재시작"):
        say(text=":arrows_counterclockwise: 팀리더 세션을 재시작합니다...", thread_ts=thread_ts)
        from bridge import stop_leader_session
        stop_leader_session()
        start_leader_session()
        say(text=":white_check_mark: 팀리더 세션이 재시작되었습니다.", thread_ts=thread_ts)
        return

    # 중복 작업 방지
    task_key = f"{user_id}:{thread_ts}"
    with _lock:
        if _active_tasks.get(task_key):
            say(
                text=":hourglass_flowing_sand: 이전 작업이 진행 중입니다. 완료 후 새 작업을 요청해주세요.",
                thread_ts=thread_ts
            )
            return
        _active_tasks[task_key] = True

    # 작업 수신 확인
    say(text=":robot_face: 작업을 접수했습니다. 팀리더에게 전달 중...", thread_ts=thread_ts)

    # 백그라운드 스레드에서 Claude 작업 처리
    def _process():
        try:
            # 유저 정보 조회
            try:
                user_info = client.users_info(user=user_id)
                user_name = user_info["user"]["real_name"]
            except Exception:
                user_name = user_id

            # Claude 팀리더에게 전달
            prompt = f"[Slack 요청 - {user_name}] {text}"
            response = send_message(prompt)

            # 응답을 슬랙에 전송 (긴 응답은 분할)
            if len(response) <= 3900:
                say(text=response, thread_ts=thread_ts)
            else:
                # 3900자씩 분할 전송
                chunks = [response[i:i+3900] for i in range(0, len(response), 3900)]
                for i, chunk in enumerate(chunks):
                    prefix = f"({i+1}/{len(chunks)})\n" if len(chunks) > 1 else ""
                    say(text=f"{prefix}{chunk}", thread_ts=thread_ts)

        except Exception as e:
            logger.error(f"작업 처리 실패: {e}", exc_info=True)
            say(
                text=f":x: 작업 처리 중 오류가 발생했습니다.\n```\n{str(e)[:500]}\n```\n"
                     f"`tmux attach -t ai-team`으로 직접 확인해주세요.",
                thread_ts=thread_ts
            )
        finally:
            with _lock:
                _active_tasks.pop(task_key, None)

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()


@app.event("app_mention")
def handle_mention(event, say, client):
    """@멘션 처리 - 일반 메시지와 동일하게 처리"""
    # app_mention은 message 이벤트와 동일하게 처리
    handle_message(event, say, client)


def main():
    # 로그 디렉토리 확인
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(log_dir, exist_ok=True)

    # leader 세션은 start.sh가 먼저 띄움 — 여기선 확인만
    if not is_session_alive():
        logger.error("ai-team tmux 세션이 없습니다. start.sh로 시작해주세요.")
        sys.exit(1)

    logger.info("AI Team Slack Bot 시작")

    # Socket Mode로 실행
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
