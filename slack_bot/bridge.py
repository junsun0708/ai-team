"""
Claude Team Leader <-> tmux 브릿지 (capture-pane 기반)

tmux 세션 안에서 실행 중인 Claude 팀리더와 양방향 통신합니다.
- 입력: tmux load-buffer + paste-buffer로 메시지 전달
- 출력: tmux capture-pane으로 화면 캡처 → 이전 화면과 diff하여 Claude 응답만 추출
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
WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/a-projects"))

# 화면이 안정된 후 이 시간(초)이 지나면 응답으로 간주
FLUSH_TIMEOUT = 5
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


def stop_leader_session():
    """팀리더 세션 종료"""
    if not is_session_alive():
        logger.info("실행 중인 세션이 없습니다.")
        return
    _run(f"tmux send-keys -t {LEADER_TARGET} '/exit' Enter", check=False)
    time.sleep(3)
    _run(f"tmux kill-session -t {TMUX_SESSION}", check=False)
    logger.info("ai-team 세션 종료됨")


def _capture_pane() -> str:
    """tmux capture-pane으로 현재 화면 내용을 깨끗하게 가져온다."""
    result = _run(f"tmux capture-pane -t {LEADER_TARGET} -p", check=False)
    return result.stdout if result.returncode == 0 else ""


# Claude 응답 블록을 나타내는 마커들
_RESPONSE_MARKERS = re.compile(r'^[●◇◆⎿✓✗⚠]')
_NOISE_PATTERNS = re.compile('|'.join([
    r'^❯\s',                          # 프롬프트
    r'^[─━═]{3,}',                     # 구분선
    r'bypass\s+permissions',           # 상태바
    r'shift\+tab\s+to\s+cycle',
    r'esc\s+to\s+interrupt',
    r'Claude Code (v|has)',
    r'^▐▛|^▝▜|^\s*▘▘',               # 로고
    r'Opus \d|Claude Max',             # 모델 표시
    r'~/a-projects',                   # 경로 표시
    r'^\$\s',                          # 쉘 프롬프트
    r'^\s*⧉\s+Selected',              # IDE 연동
    r'^\[Slack 요청',                  # 입력 에코
    r'(Marinating|Manifesting|Osmosing|Thinking|Warming)',  # 스피너
    r'^[✢✶✻✽✲✱✴✵\*·]+\s*(Marinating|Manifesting|Osmosing|Thinking|Warming)',
]), re.IGNORECASE)


def _extract_responses(screen: str) -> list[str]:
    """capture-pane 화면에서 Claude 응답 블록들만 추출한다."""
    lines = screen.split('\n')
    responses = []
    current_block = []
    in_response = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_response and current_block:
                current_block.append('')
            continue

        # 노이즈 라인 스킵
        if _NOISE_PATTERNS.search(stripped):
            if in_response and current_block:
                # 응답 블록 끝
                responses.append('\n'.join(current_block).strip())
                current_block = []
                in_response = False
            continue

        # 응답 시작 마커 (● 로 시작하는 줄 = Claude 텍스트 응답)
        if stripped.startswith('●'):
            in_response = True
            # ● 마커 제거
            text = stripped[1:].strip()
            if text:
                current_block.append(text)
            continue

        # ⎿ 로 시작하는 줄 (도구 사용 UI) → 무시
        if stripped.startswith('⎿'):
            continue

        if in_response:
            current_block.append(stripped)

    if current_block:
        responses.append('\n'.join(current_block).strip())

    return [r for r in responses if r]


def send_input(message: str):
    """Claude 팀리더에게 입력을 보냅니다 (비차단)."""
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
    capture-pane 기반 출력 모니터.

    주기적으로 tmux 화면을 캡처하고, 이전 캡처와 비교하여
    새로운 Claude 응답이 감지되면 on_output 콜백으로 전달합니다.
    화면이 FLUSH_TIMEOUT 동안 변하지 않으면 응답 완료로 간주합니다.
    """

    def __init__(self, on_output: Callable[[str], None]):
        self.on_output = on_output
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_screen = ""
        self._last_responses: list[str] = []

    def start(self):
        if self._running:
            return
        # 현재 화면 스냅샷을 기준점으로 잡음
        self._last_screen = _capture_pane()
        self._last_responses = _extract_responses(self._last_screen)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("OutputMonitor 시작됨 (capture-pane 모드)")

    def stop(self):
        self._running = False
        logger.info("OutputMonitor 중지됨")

    def reset_offset(self):
        """새 메시지 전송 후 기준점 리셋."""
        time.sleep(1)
        self._last_screen = _capture_pane()
        self._last_responses = _extract_responses(self._last_screen)

    def _loop(self):
        stable_screen = ""
        stable_since = None

        while self._running:
            time.sleep(POLL_INTERVAL)

            current_screen = _capture_pane()
            if not current_screen:
                continue

            if current_screen == self._last_screen:
                # 화면 변화 없음
                if stable_screen and stable_since:
                    if time.time() - stable_since >= FLUSH_TIMEOUT:
                        # 안정됨 → 새 응답 확인
                        self._check_new_responses(stable_screen)
                        stable_screen = ""
                        stable_since = None
                continue

            # 화면 변화 감지 → 안정 타이머 리셋
            self._last_screen = current_screen
            stable_screen = current_screen
            stable_since = time.time()

    def _check_new_responses(self, screen: str):
        """새 화면에서 이전에 없던 응답을 찾아 전송한다."""
        current_responses = _extract_responses(screen)

        # 이전에 이미 전송한 응답과 비교
        new_responses = []
        for resp in current_responses:
            if resp not in self._last_responses:
                new_responses.append(resp)

        if new_responses:
            combined = '\n\n'.join(new_responses).strip()
            if combined:
                # 슬랙 메시지 길이 제한
                if len(combined) > 3800:
                    combined = combined[:1800] + "\n\n... (중략) ...\n\n" + combined[-1800:]
                try:
                    self.on_output(combined)
                    logger.info(f"응답 전송됨 ({len(combined)} chars)")
                except Exception as e:
                    logger.error(f"on_output 콜백 오류: {e}")

        self._last_responses = current_responses


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
