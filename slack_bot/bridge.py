"""
Claude Team Leader <-> tmux 브릿지 (pipe-pane 기반)

tmux pipe-pane으로 전체 출력을 파일에 스트리밍하여
화면 스크롤과 관계없이 모든 응답을 안정적으로 캡처합니다.

1질문 1답변: 입력 전송 후 프롬프트(❯)가 다시 나타나면 응답 완료로 판단,
그 사이의 마지막 텍스트 응답을 추출하여 콜백으로 전달합니다.
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
PIPE_LOG = LOG_DIR / "pipe_output.log"

# 폴링 간격 (초)
POLL_INTERVAL = 3
# 프롬프트 재등장 후 추가 대기 (짧은 후속 출력 대비)
SETTLE_DELAY = 3
# 최대 응답 대기 시간 (분)
MAX_WAIT_MINUTES = 30


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


# ── pipe-pane 관리 ──────────────────────────────────────────────

def _start_pipe_pane():
    """tmux pipe-pane으로 출력을 파일에 스트리밍 시작."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 기존 pipe-pane 종료
    _run(f"tmux pipe-pane -t {LEADER_TARGET}", check=False)
    # 새 pipe-pane 시작 (append 모드)
    _run(f"tmux pipe-pane -t {LEADER_TARGET} -o 'cat >> {PIPE_LOG}'", check=False)
    logger.info(f"pipe-pane 시작: {PIPE_LOG}")


def _stop_pipe_pane():
    """pipe-pane 종료."""
    _run(f"tmux pipe-pane -t {LEADER_TARGET}", check=False)
    logger.info("pipe-pane 종료")


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
    r'(Marinating|Manifesting|Osmosing|Thinking|Warming|Nebulizing|Crystallizing|Synthesizing|Percolating|Transmuting|Alchemizing|Conjuring|Distilling)',
    r'^[✢✶✻✽✲✱✴✵●\*·•]+\s*(Searching|Reading|Writing|Editing|Running|Loading|Connecting|Fetching|Checking|Processing)',  # 스피너 + 도구 상태
    r'^\s*\d+\s*[│┃|]',                   # 코드 라인 번호
    r'^⎿',                                # 모든 도구 UI
    r'^⏵',                                # 권한 모드 표시줄
    r'^Tip:\s',                            # UI 팁
    r'Press Shift\+Enter',                 # UI 팁 변형
    r'ctrl\+o to expand',                  # UI 팁
    r'Searching for \d+ pattern',          # 검색 상태
    r'^multi-?line message',               # 팁 끝부분
]), re.IGNORECASE)

# ANSI/터미널 이스케이프 시퀀스를 모두 제거하는 정규식
_ANSI_RE = re.compile(
    r'\x1b'           # ESC
    r'(?:'
    r'\[[0-9;?]*[a-zA-Z]'   # CSI sequences: \e[...X (일반 + DEC private mode)
    r'|\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC sequences: \e]...BEL
    r'|\([\w]'        # Character set: \e(B 등
    r'|[=>ABCDHM78]'  # 단일 문자 시퀀스
    r')'
)

# 스피너 잔해 감지: 짧은 줄에 스피너 문자가 포함
_SPINNER_CHARS = set('✢✶✻✽✲✱✴✵●·•*')
_SPINNER_JUNK_RE = re.compile(r'^[✢✶✻✽✲✱✴✵●·•\*\s]{0,3}\w{0,4}$')

# 프롬프트 패턴: ❯ 가 줄 시작에 나타남
_PROMPT_RE = re.compile(r'❯')


def _strip_ansi(text: str) -> str:
    """모든 ANSI/터미널 이스케이프 시퀀스를 제거한다."""
    return _ANSI_RE.sub('', text)


def _is_spinner_junk(line: str) -> bool:
    """스피너 애니메이션 잔해인지 판별한다.

    pipe-pane은 터미널 프레임을 그대로 캡처하므로
    'ebu', '*ebli', '✢ulzi', '·zg' 같은 스피너 조각이 남는다.
    한글/영문 의미 있는 텍스트가 아닌 짧은 조각을 필터링한다.
    """
    if len(line) <= 6:
        # 짧은 줄: 스피너 문자 포함 여부 또는 의미 없는 조각
        if any(c in _SPINNER_CHARS for c in line):
            return True
        if _SPINNER_JUNK_RE.match(line):
            return True
        # 의미 있는 짧은 한글은 보존 (예: "확인", "완료")
        if re.match(r'^[\uac00-\ud7af]{1,3}$', line):
            return False
        # 영문 단어가 아닌 짧은 조각
        if len(line) <= 3 and not re.match(r'^[a-zA-Z]+$', line):
            return True
    return False


def _extract_final_response(text: str) -> str:
    """
    pipe-pane 출력에서 마지막 텍스트 응답을 추출한다.

    전략:
    1. 전체 텍스트에서 ANSI 이스케이프를 먼저 제거
    2. 줄 단위로 순회하며 노이즈/스피너 잔해를 필터링
    3. 마지막 프롬프트(❯) 직전까지의 의미 있는 텍스트 블록을 반환
    """
    # 1단계: ANSI 이스케이프 전체 제거
    text = _strip_ansi(text)

    lines = text.split('\n')

    # 마지막 프롬프트 위치 찾기 (뒤에서부터)
    last_prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if _PROMPT_RE.search(lines[i]):
            last_prompt_idx = i
            break

    if last_prompt_idx < 0:
        return ""

    # 입력 에코(첫 프롬프트) 위치 찾기
    first_prompt_idx = -1
    for i in range(len(lines)):
        if _PROMPT_RE.search(lines[i]):
            first_prompt_idx = i
            break

    # 첫 프롬프트와 마지막 프롬프트 사이가 응답 영역
    if first_prompt_idx == last_prompt_idx:
        return ""

    response_lines = lines[first_prompt_idx + 1:last_prompt_idx]

    # 2단계: 노이즈 필터링
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

        # 스피너 잔해 필터링
        if _is_spinner_junk(stripped):
            continue

        # 선행 스피너/상태 문자 제거 (●, ✶ 등)
        cleaned = re.sub(r'^[✢✶✻✽✲✱✴✵●\*·•]+\s*', '', stripped)
        # 제어 문자 정리
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
        cleaned = cleaned.strip()

        if cleaned:
            clean_lines.append(cleaned)

    # 앞뒤 빈줄 제거
    while clean_lines and clean_lines[0] == '':
        clean_lines.pop(0)
    while clean_lines and clean_lines[-1] == '':
        clean_lines.pop()

    result = '\n'.join(clean_lines).strip()

    return result


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


# ── 출력 모니터 (pipe-pane 기반) ──────────────────────────────

class OutputMonitor:
    """
    pipe-pane 기반 출력 모니터.

    1질문 1답변 방식:
    - 입력 전송 시 mark_input_sent()로 현재 파일 위치를 기록
    - 이후 프롬프트(❯)가 다시 나타나면 응답 완료로 판단
    - mark ~ 프롬프트 사이의 텍스트에서 최종 응답을 추출하여 콜백
    """

    def __init__(self, on_output: Callable[[str], None]):
        self.on_output = on_output
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._waiting_for_response = False
        self._input_file_pos = 0  # 입력 시점의 파일 위치
        self._lock = threading.Lock()

    def start(self):
        if self._running:
            return
        _start_pipe_pane()
        # 파일 초기 위치 기록
        if PIPE_LOG.exists():
            self._input_file_pos = PIPE_LOG.stat().st_size
        else:
            self._input_file_pos = 0
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("OutputMonitor 시작됨 (pipe-pane 모드)")

    def stop(self):
        self._running = False
        _stop_pipe_pane()
        logger.info("OutputMonitor 중지됨")

    def reset_offset(self):
        """입력 전송 직후 호출 - 응답 대기 시작."""
        time.sleep(1)  # 입력 에코가 파일에 기록될 시간
        with self._lock:
            if PIPE_LOG.exists():
                self._input_file_pos = PIPE_LOG.stat().st_size
            self._waiting_for_response = True
        logger.info(f"응답 대기 시작 (file pos: {self._input_file_pos})")

    def _read_new_output(self) -> str:
        """마지막 mark 이후의 새 출력을 읽는다."""
        if not PIPE_LOG.exists():
            return ""
        try:
            with open(PIPE_LOG, 'r', errors='replace') as f:
                f.seek(self._input_file_pos)
                return f.read()
        except Exception as e:
            logger.error(f"파일 읽기 오류: {e}")
            return ""

    def _loop(self):
        prompt_seen_at = None

        while self._running:
            time.sleep(POLL_INTERVAL)

            with self._lock:
                if not self._waiting_for_response:
                    prompt_seen_at = None
                    continue

            new_output = self._read_new_output()
            if not new_output:
                continue

            # 프롬프트(❯) 감지
            if _PROMPT_RE.search(new_output):
                if prompt_seen_at is None:
                    prompt_seen_at = time.time()
                    logger.debug("프롬프트 감지, settle 대기 중...")
                elif time.time() - prompt_seen_at >= SETTLE_DELAY:
                    # settle 완료 → 응답 추출
                    response = _extract_final_response(new_output)
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

                    with self._lock:
                        self._waiting_for_response = False
                        if PIPE_LOG.exists():
                            self._input_file_pos = PIPE_LOG.stat().st_size
                    prompt_seen_at = None
            else:
                # 프롬프트 아직 안 나왔으면 리셋
                prompt_seen_at = None

            # 타임아웃 체크
            # (너무 오래 걸리면 중간 결과라도 전달)


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
