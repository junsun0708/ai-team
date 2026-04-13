"""
Claude Team Leader <-> tmux 브릿지 (capture-pane 기반)

tmux capture-pane으로 렌더링된 깨끗한 텍스트를 주기적으로 캡처하여
Claude 응답 블록(● 로 시작)만 추출하여 Slack에 전달합니다.

단순 전략: 입력 전송 후 빈 프롬프트(❯)가 나타나면 응답 완료,
마지막 ● 블록의 텍스트를 그대로 추출.
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

# 폴링 간격 (초)
POLL_INTERVAL = 3
# 프롬프트 재등장 후 추가 대기
SETTLE_DELAY = 5
# 입력 후 최소 대기 시간 (에이전트 시작 시 잠깐 빈 프롬프트가 보이는 문제 방지)
MIN_WAIT_AFTER_INPUT = 10
# 최대 응답 대기 시간 (분)
MAX_WAIT_MINUTES = 30
# capture-pane 히스토리 라인 수
CAPTURE_HISTORY = 500


def _run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, check=check
    )


def is_session_alive() -> bool:
    result = _run(f"tmux has-session -t {TMUX_SESSION}", check=False)
    return result.returncode == 0


def is_leader_ready() -> bool:
    result = _run(
        f"tmux list-windows -t {TMUX_SESSION} -F '#{{window_name}}'",
        check=False
    )
    return LEADER_WINDOW in result.stdout


def stop_leader_session():
    if not is_session_alive():
        logger.info("실행 중인 세션이 없습니다.")
        return
    _run(f"tmux send-keys -t {LEADER_TARGET} '/exit' Enter", check=False)
    time.sleep(3)
    _run(f"tmux kill-session -t {TMUX_SESSION}", check=False)
    logger.info("ai-team 세션 종료됨")


# ── capture-pane ──────────────────────────────────────────────

def _capture_pane() -> str:
    """tmux capture-pane으로 현재 화면 + 스크롤백 텍스트를 가져온다."""
    result = _run(
        f"tmux capture-pane -t {LEADER_TARGET} -p -S -{CAPTURE_HISTORY}",
        check=False
    )
    return result.stdout if result.returncode == 0 else ""


# ── 응답 추출 (단순 전략) ─────────────────────────────────────

def _extract_response(text: str) -> str:
    """
    capture-pane 텍스트에서 마지막 Claude 응답을 추출한다.

    단순 전략:
    1. 마지막 빈 프롬프트(❯)를 찾는다 (응답 완료 지점)
    2. 그 위로 올라가며 마지막 ● 블록을 찾는다 (Claude 응답 시작)
    3. ● 블록의 텍스트를 추출한다
    """
    lines = text.split('\n')

    # 1. 마지막 빈 프롬프트 위치 (뒤에서부터)
    last_prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped == '❯' or re.match(r'^❯\s*$', stripped):
            last_prompt_idx = i
            break

    if last_prompt_idx < 0:
        return ""

    # 2. 입력 에코 프롬프트 찾기 (마지막 빈 프롬프트 위의 ❯ + 텍스트)
    input_prompt_idx = -1
    for i in range(last_prompt_idx - 1, -1, -1):
        stripped = lines[i].strip()
        if re.match(r'^❯\s+\S', stripped):  # ❯ + 텍스트 = 입력 에코
            input_prompt_idx = i
            break

    if input_prompt_idx < 0:
        return ""

    # 3. 입력 에코와 빈 프롬프트 사이에서 텍스트 응답 ● 찾기
    _TOOL_NAMES = re.compile(
        r'^●\s*(Bash|Read|Edit|Write|Glob|Grep|Agent|WebSearch|WebFetch|'
        r'Skill|Background|TaskCreate|TaskUpdate|ToolSearch)\b'
    )

    # 입력 에코 바로 다음부터 빈 프롬프트 전까지 범위에서 첫 번째 텍스트 ● 찾기
    response_start = -1
    for i in range(input_prompt_idx + 1, last_prompt_idx):
        stripped = lines[i].strip()
        if stripped.startswith('●') and not _TOOL_NAMES.match(stripped):
            response_start = i
            break

    if response_start < 0:
        return ""

    # 4. ● 응답 블록의 끝 찾기 (다음 ● 도구호출 전까지)
    response_end = last_prompt_idx
    for i in range(response_start + 1, last_prompt_idx):
        stripped = lines[i].strip()
        if stripped.startswith('●'):
            response_end = i
            break

    response_lines = lines[response_start:response_end]

    # 클리닝: ● 마커 제거, UI 잔해 제거
    clean_lines = []
    for line in response_lines:
        stripped = line.strip()
        if not stripped:
            if clean_lines and clean_lines[-1] != '':
                clean_lines.append('')
            continue

        # UI 요소 스킵
        if re.match(r'^[─━═]{3,}', stripped):
            continue
        if re.search(r'bypass\s*permissions|shift\+tab|esc\s*to\s*interrupt', stripped, re.I):
            continue
        if re.match(r'^⏵', stripped):
            continue
        # 스피너/처리시간 스킵
        if re.match(r'^[✢✶✻✽✲✱✴✵●\*·•]\s+\S+…', stripped):
            continue
        if re.search(r'Churned for|Brewed for|Fermenting', stripped):
            continue
        if re.match(r'^[A-Z][a-z-]*ing[…\.]*$', stripped):
            continue
        if re.search(r'tokens\)\s*$', stripped):
            continue

        # ● 마커 제거
        cleaned = re.sub(r'^●\s*', '', stripped)
        if cleaned:
            clean_lines.append(cleaned)

    # 앞뒤 빈줄 제거
    while clean_lines and clean_lines[0] == '':
        clean_lines.pop(0)
    while clean_lines and clean_lines[-1] == '':
        clean_lines.pop()

    return '\n'.join(clean_lines).strip()


# ── 입력 전송 ──────────────────────────────────────────────

def send_input(message: str):
    """Claude 팀리더에게 입력을 보냅니다."""
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


# ── 출력 모니터 (capture-pane 기반) ──────────────────────────

class OutputMonitor:
    """
    capture-pane 기반 출력 모니터.

    1질문 1답변:
    - 입력 전송 시 reset_offset()으로 응답 대기 시작
    - 주기적으로 capture-pane 실행하여 빈 프롬프트(❯) 확인
    - 빈 프롬프트 나타나면 마지막 ● 블록을 추출하여 콜백
    """

    def __init__(self, on_output: Callable[[str], None]):
        self.on_output = on_output
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._waiting_for_response = False
        self._wait_start_time: Optional[float] = None
        self._lock = threading.Lock()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("OutputMonitor 시작됨 (capture-pane 모드)")

    def stop(self):
        self._running = False
        logger.info("OutputMonitor 중지됨")

    def reset_offset(self):
        """입력 전송 직후 호출 - 응답 대기 시작."""
        with self._lock:
            self._waiting_for_response = True
            self._wait_start_time = time.time()
        logger.info("응답 대기 시작")

    def _is_prompt_ready(self, text: str) -> bool:
        """하단 10줄 이내에서 빈 프롬프트(❯)가 있는지 확인."""
        lines = text.rstrip().split('\n')
        for line in lines[-10:]:
            stripped = line.strip()
            if stripped == '❯' or re.match(r'^❯\s*$', stripped):
                return True
        return False

    def _loop(self):
        prompt_seen_at = None

        while self._running:
            time.sleep(POLL_INTERVAL)

            with self._lock:
                if not self._waiting_for_response:
                    prompt_seen_at = None
                    continue
                wait_start = self._wait_start_time

            # 타임아웃 체크
            if wait_start and (time.time() - wait_start) > MAX_WAIT_MINUTES * 60:
                logger.warning(f"응답 대기 타임아웃 ({MAX_WAIT_MINUTES}분)")
                with self._lock:
                    self._waiting_for_response = False
                prompt_seen_at = None
                continue

            captured = _capture_pane()
            if not captured:
                continue

            # 입력 후 최소 대기 시간 확인 (에이전트 실행 시 잠깐 빈 프롬프트가 보이는 문제 방지)
            elapsed = time.time() - wait_start if wait_start else 0
            if elapsed < MIN_WAIT_AFTER_INPUT:
                prompt_seen_at = None
                continue

            if self._is_prompt_ready(captured):
                if prompt_seen_at is None:
                    prompt_seen_at = time.time()
                    logger.debug("프롬프트 감지, settle 대기 중...")
                elif time.time() - prompt_seen_at >= SETTLE_DELAY:
                    response = _extract_response(captured)
                    if response:
                        if len(response) > 3800:
                            response = response[:1800] + "\n\n... (중략) ...\n\n" + response[-1800:]
                        try:
                            self.on_output(response)
                            logger.info(f"응답 전송 완료 ({len(response)} chars)")
                        except Exception as e:
                            logger.error(f"on_output 콜백 오류: {e}")
                        with self._lock:
                            self._waiting_for_response = False
                        prompt_seen_at = None
                    else:
                        # ● 블록 없음 = 아직 작업 중일 수 있음, 대기 계속
                        logger.debug("프롬프트 감지했으나 ● 블록 없음, 대기 계속")
                        prompt_seen_at = None
            else:
                prompt_seen_at = None


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
