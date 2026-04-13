# AI Team - Slack 연동 Claude Agent Teams

Slack 비공개 채널 `#ai-team`에서 팀리더에게 작업을 지시하면,
Claude Agent Teams가 tmux split panes로 자동 구성되어 작업을 수행합니다.

## 아키텍처

```
[Slack #ai-team] → [Slack Bot (Socket Mode)] → [Claude 팀리더 (tmux)]
                                                      │
                                                      ├── teammate-1 (pane)
                                                      ├── teammate-2 (pane)
                                                      └── teammate-N (pane)
```

- **Slack Bot**: `slack_bolt` Socket Mode로 상시 구동
- **Claude 팀리더**: tmux 세션에서 interactive 모드로 실행, `--teammate-mode tmux`
- **브릿지**: `tmux capture-pane` 기반으로 Claude 응답을 캡처하여 Slack에 전달
- **하위 에이전트**: 팀리더가 자동 생성/관리, tmux split pane으로 표시

## 사전 준비

### 1. Slack App 생성

1. https://api.slack.com/apps 에서 새 앱 생성
2. **Socket Mode** 활성화 → App-Level Token 생성 (`xapp-...`)
3. **OAuth & Permissions**에서 Bot Token Scopes 추가:
   - `channels:history`, `channels:read`
   - `chat:write`
   - `groups:history`, `groups:read` (비공개 채널용)
   - `users:read`
   - `app_mentions:read`
4. **Event Subscriptions** 활성화, Subscribe to bot events:
   - `message.channels`
   - `message.groups` (비공개 채널용)
   - `app_mention`
5. 앱을 워크스페이스에 설치
6. `#ai-team` 비공개 채널에 봇 초대: `/invite @앱이름`

### 2. 환경변수 설정

```bash
cd ~/a-projects/ai-team
cp .env.example .env
# .env 파일에 토큰 입력
```

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
AI_TEAM_CHANNEL=ai-team
WORK_DIR=/home/jyh/a-projects
```

### 3. 의존성 설치

```bash
pip install -r requirements.txt
```

## 사용법

### 시작

```bash
./scripts/start.sh
```

### 상태 확인

```bash
./scripts/status.sh
```

### tmux 세션 접속 (모니터링)

```bash
# 전체 세션 접속
./scripts/attach.sh

# 특정 윈도우 접속
./scripts/attach.sh leader   # 팀리더
./scripts/attach.sh bot      # Slack 봇
```

접속 후 `Ctrl+B D`로 세션에서 분리(detach)할 수 있습니다.

### 종료

```bash
./scripts/stop.sh
```

## Slack 채널 사용법

### 작업 지시

`#ai-team` 채널에 메시지를 보내면 팀리더가 작업을 시작합니다:

```
가격 비교 웹스크래퍼 만들어줘. Python으로, ~/a-projects/price-scraper에
```

```
~/a-projects/my-api 프로젝트의 인증 모듈을 리뷰해줘. 보안 취약점 위주로.
```

### 특수 명령어

| 명령어 | 설명 |
|--------|------|
| `상태` 또는 `!status` | 팀 상태 확인 |
| `재시작` 또는 `!restart` | 팀리더 세션 재시작 |

## 파일 구조

```
ai-team/
├── .env                  # Slack 토큰 (git 미추적)
├── .env.example          # 환경변수 템플릿
├── .gitignore
├── CLAUDE.md             # 팀리더용 지침
├── README.md
├── requirements.txt
├── slack_bot/
│   ├── __init__.py
│   ├── app.py            # Slack Bot 메인
│   └── bridge.py         # Claude ↔ tmux 브릿지 (capture-pane 기반)
├── scripts/
│   ├── start.sh          # 시스템 시작
│   ├── stop.sh           # 시스템 종료
│   ├── status.sh         # 상태 확인
│   └── attach.sh         # tmux 접속
└── logs/                 # 런타임 로그
```
