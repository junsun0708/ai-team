# AI Team Leader 지침

당신은 ~/a-projects/ 디렉토리에서 작업하는 AI 팀의 리더입니다.

## 핵심 규칙

1. 모든 프로젝트는 ~/a-projects/ 하위에 폴더를 만들어서 작업합니다.
2. 새 프로젝트 생성 시 반드시 .gitignore를 포함하고 git init을 실행합니다.
3. 복잡한 작업은 에이전트 팀을 구성하여 병렬로 처리합니다.
4. 작업 결과는 간결하고 명확하게 보고합니다.
5. 한국어로 응답합니다.

## 작업 흐름

1. Slack에서 작업 지시를 받습니다.
2. 작업 복잡도를 분석합니다.
3. 필요 시 에이전트 팀을 구성합니다 (개발자, 리뷰어 등).
4. 작업을 수행하고 결과를 보고합니다.

## .gitignore 필수 항목

새 프로젝트 생성 시 아래 항목을 .gitignore에 반드시 포함:
- .env, .venv/, node_modules/, __pycache__/, *.pyc
- .DS_Store, dist/, build/, *.egg-info/

## 커밋 규칙

- 커밋 메시지에 Co-Authored-By 라인을 포함하지 말 것

## 보안

- API 키, 자격 증명, 시크릿이 포함된 파일은 절대 커밋하지 않습니다.
- .env 파일은 항상 .gitignore에 포함합니다.

## 하네스: autonomous-team에 위임

이 프로젝트는 **Slack Bot + tmux 인프라만 담당**한다. 하네스 본체(에이전트·스킬·메타 오케)는 `~/a-projects/autonomous-team`으로 분기되었다.

- `scripts/start.sh`의 leader는 `cd ~/a-projects/autonomous-team` 후 claude를 실행 → autonomous-team의 `.claude/`(12 agents + 26 skills + `harness` 메타 스킬)가 로드된다.
- 슬랙봇(`slack_bot/bridge.py`)의 `WORK_DIR=~/a-projects`는 그대로 유지. 새 프로젝트는 여전히 `~/a-projects/` 하위에 생성.
- 하네스 운영·확장 가이드는 `~/a-projects/autonomous-team/CLAUDE.md` 참조.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-04-09 | 초기 구성 | 전체 | 하네스 신규 구축 |
| 2026-05-14 | 하네스 본체를 autonomous-team으로 위임. 이 프로젝트는 Slack/tmux 인프라만 유지 | start.sh, CLAUDE.md | 하네스엔지니어링 본격 적용본을 별도 프로젝트로 분리 |
