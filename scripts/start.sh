#!/bin/bash
# AI Team 전체 시스템 시작
# 사용법: ./scripts/start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SESSION="ai-team"

echo "=== AI Team 시작 ==="

# .env 파일 확인
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "❌ .env 파일이 없습니다. .env.example을 참고하여 생성해주세요."
    echo "   cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env"
    exit 1
fi

# 이미 실행 중인지 확인
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "⚠️  ai-team 세션이 이미 실행 중입니다."
    echo "   접속: tmux attach -t $SESSION"
    echo "   종료 후 재시작하려면: ./scripts/stop.sh && ./scripts/start.sh"
    exit 0
fi

# 로그 디렉토리
mkdir -p "$PROJECT_DIR/logs"

# tmux 세션 생성: 첫 번째 윈도우는 Slack Bot
tmux new-session -d -s "$SESSION" -n "bot" -x 220 -y 50

# Slack Bot 시작
# leader 윈도우 생성: Claude 팀리더
tmux new-window -t "$SESSION" -n "leader"
tmux send-keys -t "$SESSION:leader" \
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude --dangerously-skip-permissions --teammate-mode tmux" Enter

# Claude 시작 대기
sleep 5

# pipe-pane으로 리더 출력 로깅
tmux pipe-pane -t "$SESSION:leader" -o "cat >> $PROJECT_DIR/logs/leader_output.log"

# Slack Bot 시작 (bot 윈도우)
tmux send-keys -t "$SESSION:bot" "cd $PROJECT_DIR/slack_bot && python3 app.py" Enter

echo "✅ AI Team 시작됨"
echo ""
echo "📌 사용법:"
echo "   접속:   tmux attach -t $SESSION"
echo "   상태:   ./scripts/status.sh"
echo "   종료:   ./scripts/stop.sh"
echo ""
echo "💡 Slack #ai-team 채널에서 메시지를 보내면 팀리더가 작업을 시작합니다."
echo "   팀리더가 에이전트팀을 구성하면 tmux pane이 자동으로 분할됩니다."
