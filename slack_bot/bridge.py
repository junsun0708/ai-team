"""
Claude Team Leader <-> tmux 브릿지 (capture-pane 기반)

tmux capture-pane으로 렌더링된 깨끗한 텍스트를 주기적으로 캡처하여
ANSI 이스케이프/인코딩 문제 없이 응답을 안정적으로 추출합니다.

1질문 1답변: 입력 전송 후 프롬프트(❯)가 다시 나타나면 응답 완료로 판단,
그 사이의 텍스트에서 최종 응답을 추출하여 콜백으로 전달합니다.
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
# 프롬프트 재등장 후 추가 대기 (짧은 후속 출력 대비)
SETTLE_DELAY = 5
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


# ── capture-pane 기반 출력 캡처 ──────────────────────────────

def _capture_pane() -> str:
    """tmux capture-pane으로 현재 화면 + 스크롤백 버퍼의 텍스트를 가져온다."""
    result = _run(
        f"tmux capture-pane -t {LEADER_TARGET} -p -S -{CAPTURE_HISTORY}",
        check=False
    )
    return result.stdout if result.returncode == 0 else ""


# ── 노이즈 필터 ──────────────────────────────────────────────

_NOISE_PATTERNS = re.compile('|'.join([
    r'^❯\s*$',                            # 빈 프롬프트
    r'^❯\s',                              # 프롬프트 + 입력
    r'^[─━═]{3,}',                        # 구분선
    r'bypass\s*permissions',
    r'shift\+tab\s*to\s*cycle',
    r'esc\s*to\s*interrupt',
    r'Claude Code (v|has)',
    r'^▐▛|^▝▜|^\s*▘▘',                   # 로고
    r'Opus \d|Claude Max|Sonnet',
    r'~/a-projects',
    r'^\$\s',
    r'^\s*⧉\s+Selected',
    r'^\[Slack 요청',                      # 입력 에코
    r'^[✢✶✻✽✲✱✴✵●\*·•]+\s*(Searching|Reading|Writing|Editing|Running|Loading|Connecting|Fetching|Checking|Processing)',
    r'^\s*\d+\s*[│┃|]',                   # 코드 라인 번호
    r'^⎿',                                # 도구 UI
    r'^⏵',                                # 권한 모드 표시줄
    r'^Tip:\s',                            # UI 팁
    r'Press Shift\+Enter',
    r'ctrl\+o to expand',
    r'Searching for \d+ pattern',
    r'^multi-?line message',
    r'tokens\)$',                          # 토큰 카운트 줄
    r'^\s*\d+k?\s*tokens',                # 토큰 표시
    r'^[✢✶✻✽✲✱✴✵●\*·•]\s+\S+…',          # 스피너 줄: * Forming…, · Thinking…
    r'\(thinking\)',                        # (thinking) 표시
    r'^\s*\S+…\s*\(',                      # Xxxing… (시간/토큰 정보)
    r'Churned for',                        # Churned for 4m 4s
]), re.IGNORECASE)

# 프롬프트 패턴
_PROMPT_RE = re.compile(r'❯')

# 스피너 단어 패턴 (Xxxing…, Xxx-xxxing…)
_SPINNER_WORD_RE = re.compile(r'^[A-Z][a-z-]*ing[…\.]*$')


def _extract_response(text: str) -> str:
    """
    capture-pane 텍스트에서 마지막 응답을 추출한다.

    전략:
    1. 줄 단위로 분할
    2. 마지막 프롬프트(❯) 바로 위의 텍스트 블록이 응답
    3. 그 위의 프롬프트(입력 에코)까지가 응답 영역
    4. 노이즈 필터링
    """
    lines = text.split('\n')

    # 마지막 프롬프트(빈 프롬프트 = 입력 대기 상태) 위치 찾기 (뒤에서부터)
    last_prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped == '❯' or re.match(r'^❯\s*$', stripped):
            last_prompt_idx = i
            break

    if last_prompt_idx < 0:
        return ""

    # 입력 에코 프롬프트 찾기 (마지막 프롬프트 위로 올라가며)
    first_prompt_idx = -1
    for i in range(last_prompt_idx - 1, -1, -1):
        if _PROMPT_RE.search(lines[i]):
            first_prompt_idx = i
            break

    if first_prompt_idx < 0:
        # 프롬프트가 하나뿐이면 맨 위부터 응답으로 간주
        first_prompt_idx = 0
        response_lines = lines[first_prompt_idx:last_prompt_idx]
    else:
        response_lines = lines[first_prompt_idx + 1:last_prompt_idx]

    # 노이즈 필터링
    clean_lines = []
    for line in response_lines:
        stripped = line.strip()
        if not stripped:
            if clean_lines and clean_lines[-1] != '':
                clean_lines.append('')
            continue

        # 노이즈 패턴 매칭
        if _NOISE_PATTERNS.search(stripped):
            continue

        # 스피너 단어 필터
        if _SPINNER_WORD_RE.match(stripped):
            continue

        clean_lines.append(stripped)

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

    1질문 1답변 방식:
    - 입력 전송 시 reset_offset()으로 응답 대기 시작
    - 주기적으로 capture-pane을 실행하여 화면 텍스트 확인
    - 빈 프롬프트(❯)가 나타나면 응답 완료로 판단
    - 응답 텍스트를 추출하여 콜백으로 전달
    """

    def __init__(self, on_output: Callable[[str], None]):
        self.on_output = on_output
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._waiting_for_response = False
        self._wait_start_time: Optional[float] = None
        self._last_capture = ""  # 이전 캡처와 비교용
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
            self._last_capture = ""
        logger.info("응답 대기 시작")

    def _is_prompt_ready(self, text: str) -> bool:
        """캡처된 텍스트에서 빈 프롬프트(❯)가 있는지 확인.

        Claude Code UI는 ❯ 아래에 구분선(──)과 bypass permissions 줄이 있으므로
        마지막 줄이 아닌, 하단 근처에서 빈 프롬프트를 찾는다.
        """
        lines = text.rstrip().split('\n')
        # 하단 10줄 이내에서 빈 프롬프트 찾기
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

            # capture-pane으로 현재 화면 가져오기
            captured = _capture_pane()
            if not captured:
                continue

            # 프롬프트(❯) 감지 - 빈 프롬프트 = 응답 완료
            if self._is_prompt_ready(captured):
                if prompt_seen_at is None:
                    prompt_seen_at = time.time()
                    logger.debug("프롬프트 감지, settle 대기 중...")
                elif time.time() - prompt_seen_at >= SETTLE_DELAY:
                    # settle 완료 → 응답 추출
                    response = _extract_response(captured)
                    if response:
                        # 슬랙 메시지 길이 제한
                        if len(response) > 3800:
                            response = response[:1800] + "\n\n... (중략) ...\n\n" + response[-1800:]
                        try:
                            self.on_output(response)
                            logger.info(f"응답 전송 완료 ({len(response)} chars)")
                        except Exception as e:
                            logger.error(f"on_output 콜백 오류: {e}")
                    else:
                        logger.warning("프롬프트 감지했으나 추출된 응답 없음")
                        logger.debug(f"캡처 텍스트 마지막 20줄:\n{chr(10).join(captured.split(chr(10))[-20:])}")

                    with self._lock:
                        self._waiting_for_response = False
                    prompt_seen_at = None
            else:
                # 아직 작업 중 (프롬프트 안 나옴)
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
