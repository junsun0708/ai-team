#!/bin/bash
# AI Team tmux 세션에 접속
# 사용법: ./scripts/attach.sh [윈도우이름]
#   ./scripts/attach.sh          → 세션 전체 접속
#   ./scripts/attach.sh leader   → leader 윈도우 접속
#   ./scripts/attach.sh bot      → bot 윈도우 접속

SESSION="ai-team"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "❌ ai-team 세션이 실행되고 있지 않습니다."
    echo "   시작: ./scripts/start.sh"
    exit 1
fi

if [ -n "$1" ]; then
    tmux attach -t "$SESSION:$1"
else
    tmux attach -t "$SESSION"
fi
