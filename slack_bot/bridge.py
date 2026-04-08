"""
Claude Team Leader <-> tmux 브릿지 (실시간 스트리밍)

tmux 세션 안에서 실행 중인 Claude 팀리더와 양방향 통신합니다.
- 입력: tmux load-buffer + paste-buffer로 메시지 전달
- 출력: pipe-pane 로그 파일을 실시간 모니터링, idle 감지 시 콜백 호출
"""

import os
import re
import subprocess
import tempfile
import time
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TMUX_SESSION = "ai-team"
LEADER_WINDOW = "leader"
LEADER_TARGET = f"{TMUX_SESSION}:{LEADER_WINDOW}"
LOG_DIR = Path(__file__).parent.parent / "logs"
LEADER_LOG = LOG_DIR / "leader_output.log"
WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/a-projects"))

# 출력이 멈춘 후 이 시간(초)이 지나면 버퍼를 슬랙으로 전송
FLUSH_TIMEOUT = 5
# 폴링 간격 (초)
POLL_INTERVAL = 1


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


def stop_leader_session():
    """팀리더 세션 종료"""
    if not is_session_alive():
        logger.info("실행 중인 세션이 없습니다.")
        return
    _run(f"tmux send-keys -t {LEADER_TARGET} '/exit' Enter", check=False)
    time.sleep(3)
    _run(f"tmux kill-session -t {TMUX_SESSION}", check=False)
    logger.info("ai-team 세션 종료됨")


def _get_log_size() -> int:
    try:
        return LEADER_LOG.stat().st_size
    except FileNotFoundError:
        return 0


def _read_log_from(offset: int) -> str:
    try:
        with open(LEADER_LOG, "r", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except FileNotFoundError:
        return ""


def _clean_ansi(text: str) -> str:
    """ANSI escape 시퀀스 및 제어 문자 제거"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text


def _clean_output(raw: str) -> str:
    """원시 출력을 정리하여 슬랙에 보낼 텍스트 추출"""
    cleaned = _clean_ansi(raw)
    lines = cleaned.split('\n')
    result_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in ('>', '❯', '$', '%'):
            continue
        result_lines.append(line)

    result = '\n'.join(result_lines).strip()

    # 슬랙 메시지 길이 제한
    if len(result) > 3800:
        result = result[:1800] + "\n\n... (중략) ...\n\n" + result[-1800:]

    return result


def send_input(message: str):
    """
    Claude 팀리더에게 입력을 보냅니다 (비차단).
    새 작업 지시, 질문에 대한 답변, 'y' 확인 등 모두 이 함수로 전송.
    """
    if not is_session_alive() or not is_leader_ready():
        raise RuntimeError("ai-team 세션이 없습니다. ./scripts/start.sh로 시작해주세요.")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(message)
        tmp_path = f.name
    try:
        _run(f"tmux load-buffer -b ai-team-input '{tmp_path}'")
        _run(f"tmux paste-buffer -b ai-team-input -t {LEADER_TARGET}")
        _run(f"tmux send-keys -t {LEADER_TARGET} Enter")
    finally:
        os.unlink(tmp_path)

    logger.info(f"입력 전송됨: {message[:100]}...")


class OutputMonitor:
    """
    리더 pane 출력을 실시간 모니터링합니다.

    출력이 감지되면 버퍼에 쌓아두고,
    FLUSH_TIMEOUT 동안 새 출력이 없으면 (idle)
    버퍼를 정리하여 on_output 콜백으로 전달합니다.

    → 슬랙에 실시간으로 Claude 출력이 전달됩니다.
    """

    def __init__(self, on_output: Callable[[str], None]):
        self.on_output = on_output
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._log_offset = 0

    def start(self):
        """모니터링 시작"""
        if self._running:
            return
        self._log_offset = _get_log_size()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("OutputMonitor 시작됨")

    def stop(self):
        """모니터링 중지"""
        self._running = False
        logger.info("OutputMonitor 중지됨")

    def reset_offset(self):
        """새 메시지 전송 후 오프셋 리셋 (입력 에코 건너뛰기)"""
        time.sleep(0.5)
        self._log_offset = _get_log_size()

    def _loop(self):
        buffer = ""
        idle_start = None

        while self._running:
            time.sleep(POLL_INTERVAL)

            current_size = _get_log_size()

            if current_size > self._log_offset:
                # 새 출력 있음
                new_content = _read_log_from(self._log_offset)
                self._log_offset = current_size
                buffer += new_content
                idle_start = None

            elif buffer:
                # 출력 멈춤 + 버퍼에 데이터 있음
                if idle_start is None:
                    idle_start = time.time()
                elif time.time() - idle_start >= FLUSH_TIMEOUT:
                    # idle 시간 초과 → 버퍼 플러시
                    cleaned = _clean_output(buffer)
                    if cleaned:
                        try:
                            self.on_output(cleaned)
                        except Exception as e:
                            logger.error(f"on_output 콜백 오류: {e}")
                    buffer = ""
                    idle_start = None


def get_team_status() -> str:
    """현재 팀 상태 조회"""
    if not is_session_alive():
        return "ai-team 세션이 실행되고 있지 않습니다."

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
