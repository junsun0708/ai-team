#!/bin/bash
# AI Team 상태 확인
# 사용법: ./scripts/status.sh

SESSION="ai-team"

echo "=== AI Team 상태 ==="
echo ""

# tmux 세션 확인
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "❌ ai-team 세션이 실행되고 있지 않습니다."
    echo "   시작: ./scripts/start.sh"
    exit 1
fi

echo "✅ ai-team 세션 실행 중"
echo ""

# 윈도우 목록
echo "📋 윈도우:"
tmux list-windows -t "$SESSION" -F '  [#{window_index}] #{window_name} (#{window_panes} panes)' 2>/dev/null

echo ""

# 페인 상세
echo "📋 페인 상세:"
tmux list-panes -s -t "$SESSION" \
    -F '  #{window_name}:#{pane_index} | #{pane_current_command} | #{pane_width}x#{pane_height} | #{?pane_active,ACTIVE,}' 2>/dev/null

echo ""

# Claude 팀 정보
TEAM_DIR="$HOME/.claude/teams"
if [ -d "$TEAM_DIR" ]; then
    echo "📋 Claude 팀:"
    for team_dir in "$TEAM_DIR"/*/; do
        if [ -f "$team_dir/config.json" ]; then
            team_name=$(basename "$team_dir")
            members=$(python3 -c "
import json
with open('$team_dir/config.json') as f:
    config = json.load(f)
members = config.get('members', [])
for m in members:
    print(f\"  - {m.get('name', 'unknown')} ({m.get('agentType', 'default')})\")
" 2>/dev/null)
            echo "  팀: $team_name"
            if [ -n "$members" ]; then
                echo "$members"
            fi
        fi
    done
else
    echo "  (활성 팀 없음)"
fi

echo ""
echo "💡 접속: tmux attach -t $SESSION"
echo "   특정 윈도우: tmux attach -t $SESSION:{윈도우이름}"
