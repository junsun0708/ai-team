"""
Claude Team Leader <-> tmux 브릿지

tmux 세션 안에서 실행 중인 Claude 팀리더와 통신합니다.
- 입력: tmux send-keys로 메시지 전달
- 출력: pipe-pane 로그 파일 모니터링 + idle 감지
"""

import os
import re
import subprocess
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TMUX_SESSION = "ai-team"
LEADER_WINDOW = "leader"
LEADER_TARGET = f"{TMUX_SESSION}:{LEADER_WINDOW}"
LOG_DIR = Path(__file__).parent.parent / "logs"
LEADER_LOG = LOG_DIR / "leader_output.log"
WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/a-projects"))

# idle 감지: 마지막 출력 후 이 시간(초)이 지나면 응답 완료로 판단
IDLE_TIMEOUT = 10
# 최대 대기 시간 (분)
MAX_WAIT_MINUTES = 30
# 폴링 간격 (초)
POLL_INTERVAL = 2


def _run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, check=check
    )


def is_session_alive() -> bool:
    """ai-team tmux 세션이 살아있는지 확인"""
    result = _run(f"tmux has-session -t {TMUX_SESSION}", check=False)
    return result.returncode == 0


def is_leader_ready() -> bool:
    """leader 윈도우가 존재하는지 확인"""
    result = _run(
        f"tmux list-windows -t {TMUX_SESSION} -F '#{{window_name}}'",
        check=False
    )
    return LEADER_WINDOW in result.stdout


def start_leader_session():
    """Claude 팀리더를 tmux 세션에서 시작"""
    if is_session_alive():
        logger.info("ai-team 세션이 이미 실행 중입니다.")
        return True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 로그 파일 초기화
    LEADER_LOG.write_text("")

    # tmux 세션 생성 (leader 윈도우)
    _run(
        f"tmux new-session -d -s {TMUX_SESSION} -n {LEADER_WINDOW} "
        f"-x 220 -y 50"
    )

    # 환경변수 설정 후 Claude 시작
    claude_cmd = (
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 "
        f"claude --dangerously-skip-permissions --teammate-mode tmux"
    )
    _run(f"tmux send-keys -t {LEADER_TARGET} '{claude_cmd}' Enter")

    # pipe-pane으로 출력 로깅
    _run(
        f"tmux pipe-pane -t {LEADER_TARGET} -o "
        f"'cat >> {LEADER_LOG}'"
    )

    # Claude 시작 대기
    time.sleep(5)

    logger.info("Claude 팀리더 세션 시작됨")
    return True


def stop_leader_session():
    """팀리더 세션 종료"""
    if not is_session_alive():
        logger.info("실행 중인 세션이 없습니다.")
        return

    # Claude에게 정리 요청
    _run(f"tmux send-keys -t {LEADER_TARGET} '/exit' Enter", check=False)
    time.sleep(3)

    # tmux 세션 종료
    _run(f"tmux kill-session -t {TMUX_SESSION}", check=False)
    logger.info("ai-team 세션 종료됨")


def _get_log_size() -> int:
    """로그 파일 현재 크기"""
    try:
        return LEADER_LOG.stat().st_size
    except FileNotFoundError:
        return 0


def _read_log_from(offset: int) -> str:
    """로그 파일에서 offset 이후 내용 읽기"""
    try:
        with open(LEADER_LOG, "r", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except FileNotFoundError:
        return ""


def _clean_ansi(text: str) -> str:
    """ANSI escape 시퀀스 제거"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    # 추가 제어 문자 제거
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text


def _extract_response(raw: str) -> str:
    """원시 출력에서 Claude 응답 텍스트 추출"""
    cleaned = _clean_ansi(raw)

    # 빈 줄, 프롬프트 기호 등 정리
    lines = cleaned.split('\n')
    result_lines = []
    for line in lines:
        stripped = line.strip()
        # 빈 줄이나 프롬프트만 있는 줄 스킵
        if not stripped or stripped in ('>', '❯', '$', '%'):
            continue
        # 입력된 명령어 줄 스킵 (보낸 메시지와 동일한 줄)
        result_lines.append(line)

    result = '\n'.join(result_lines).strip()

    # 너무 길면 앞뒤 요약
    if len(result) > 3000:
        result = result[:1500] + "\n\n... (중략) ...\n\n" + result[-1500:]

    return result


def send_message(message: str) -> str:
    """
    Claude 팀리더에게 메시지를 보내고 응답을 반환합니다.

    1. 현재 로그 위치 기록
    2. tmux send-keys로 메시지 전송
    3. 로그 파일 모니터링하며 idle 감지
    4. 응답 텍스트 추출 후 반환
    """
    if not is_session_alive():
        raise RuntimeError("ai-team tmux 세션이 없습니다. ./scripts/start.sh로 시작해주세요.")

    if not is_leader_ready():
        raise RuntimeError("leader 윈도우가 없습니다. ./scripts/start.sh로 재시작해주세요.")

    # 현재 로그 위치 기록
    log_offset = _get_log_size()

    # 메시지 전송: tmux send-keys -l (literal)로 특수문자 안전 전송
    # 임시파일에 쓴 뒤 tmux load-buffer + paste-buffer 사용
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(message)
        tmp_path = f.name
    try:
        _run(f"tmux load-buffer -b ai-team-input '{tmp_path}'")
        _run(f"tmux paste-buffer -b ai-team-input -t {LEADER_TARGET}")
        _run(f"tmux send-keys -t {LEADER_TARGET} Enter")
    finally:
        os.unlink(tmp_path)

    logger.info(f"메시지 전송됨: {message[:100]}...")

    # 응답 대기 (idle 감지)
    max_wait = MAX_WAIT_MINUTES * 60
    elapsed = 0
    last_size = log_offset
    idle_start = None

    while elapsed < max_wait:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        current_size = _get_log_size()

        if current_size > last_size:
            # 새 출력이 있으면 idle 타이머 리셋
            last_size = current_size
            idle_start = None
        else:
            # 출력이 멈춤
            if idle_start is None:
                idle_start = time.time()
            elif time.time() - idle_start >= IDLE_TIMEOUT:
                # idle 시간 초과 → 응답 완료
                break

    # 응답 추출
    raw_output = _read_log_from(log_offset)
    response = _extract_response(raw_output)

    if not response:
        response = "(응답을 캡처하지 못했습니다. `tmux attach -t ai-team`으로 직접 확인해주세요.)"

    logger.info(f"응답 수신됨 ({len(response)} chars)")
    return response


def get_team_status() -> str:
    """현재 팀 상태 조회"""
    if not is_session_alive():
        return "ai-team 세션이 실행되고 있지 않습니다."

    # tmux 윈도우/페인 목록
    result = _run(
        f"tmux list-panes -s -t {TMUX_SESSION} "
        f"-F '#{{window_name}}:#{{pane_index}} #{{pane_current_command}} #{{pane_width}}x#{{pane_height}}'",
        check=False
    )

    status_lines = ["## AI Team 상태\n"]
    status_lines.append(f"세션: {TMUX_SESSION}")
    status_lines.append(f"페인 목록:")

    if result.stdout:
        for line in result.stdout.strip().split('\n'):
            status_lines.append(f"  - {line}")
    else:
        status_lines.append("  (정보 없음)")

    return '\n'.join(status_lines)
