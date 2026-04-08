#!/bin/bash
# AI Team 전체 시스템 종료
# 사용법: ./scripts/stop.sh

SESSION="ai-team"

echo "=== AI Team 종료 ==="

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "실행 중인 ai-team 세션이 없습니다."
    exit 0
fi

# Claude 세션들에 종료 신호
echo "Claude 세션 종료 중..."
# leader 윈도우가 있으면 /exit 전송
if tmux list-windows -t "$SESSION" -F '#{window_name}' 2>/dev/null | grep -q "leader"; then
    tmux send-keys -t "$SESSION:leader" '/exit' Enter 2>/dev/null || true
    sleep 3
fi

# tmux 세션 종료
tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "✅ AI Team 종료됨"
