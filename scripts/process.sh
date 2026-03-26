#!/bin/bash
set -euo pipefail

# PATH for systemd (codex, uv, npx, node)
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | tail -1)/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="$PROJECT_DIR/vault"
ENV_FILE="$PROJECT_DIR/.env"

# Load environment variables
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# TickTick MCP server typically expects API_TOKEN / API_DOMAIN.
if [ -n "${TICKTICK_API_TOKEN:-}" ] && [ -z "${API_TOKEN:-}" ]; then
    export API_TOKEN="$TICKTICK_API_TOKEN"
fi
if [ -n "${TICKTICK_API_DOMAIN:-}" ] && [ -z "${API_DOMAIN:-}" ]; then
    export API_DOMAIN="$TICKTICK_API_DOMAIN"
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi

export TZ="${TZ:-UTC}"

TODAY=$(date +%Y-%m-%d)
CHAT_ID="$(echo "${ALLOWED_USER_IDS:-}" | tr -d '[] ' | cut -d',' -f1)"
CODEX_CLI="${CODEX_CLI_COMMAND:-codex}"

if [ -d "$VAULT_DIR/.codex" ]; then
    SKILLS_ROOT=".codex"
elif [ -d "$VAULT_DIR/.claude" ]; then
    SKILLS_ROOT=".claude"
else
    SKILLS_ROOT=".codex"
fi

echo "=== d-brain processing for $TODAY ==="
echo "Skills root: $SKILLS_ROOT"

DAILY_FILE="$VAULT_DIR/daily/$TODAY.md"
HANDOFF_FILE="$VAULT_DIR/.session/handoff.md"
GRAPH_FILE="$VAULT_DIR/.graph/vault-graph.json"

if [ ! -f "$DAILY_FILE" ]; then
    echo "ORIENT: daily/$TODAY.md not found - creating empty file"
    mkdir -p "$VAULT_DIR/daily"
    echo "# $TODAY" > "$DAILY_FILE"
fi

DAILY_SIZE=$(wc -c < "$DAILY_FILE" 2>/dev/null || echo "0")
if [ "$DAILY_SIZE" -lt 50 ]; then
    echo "ORIENT: daily/$TODAY.md is empty ($DAILY_SIZE bytes) - skipping AI processing"
    echo "ORIENT: running graph rebuild only"

    cd "$VAULT_DIR"
    uv run "$SKILLS_ROOT/skills/graph-builder/scripts/analyze.py" || echo "Graph rebuild failed (non-critical)"
    cd "$PROJECT_DIR"

    git add -A
    git commit -m "chore: process daily $TODAY" || true
    git push || true
    echo "=== Done (empty daily, graph-only) ==="
    exit 0
fi

if [ ! -f "$HANDOFF_FILE" ]; then
    echo "ORIENT: handoff.md not found - creating stub"
    mkdir -p "$VAULT_DIR/.session"
    cat > "$HANDOFF_FILE" <<EOF
---
updated: $(date -Iseconds)
---

## Last Session
(none)

## Observations
EOF
fi

if [ -f "$GRAPH_FILE" ]; then
    GRAPH_MTIME=$(stat -c %Y "$GRAPH_FILE" 2>/dev/null || stat -f %m "$GRAPH_FILE" 2>/dev/null || echo 0)
    GRAPH_AGE=$(( ($(date +%s) - GRAPH_MTIME) / 86400 ))
    if [ "$GRAPH_AGE" -gt 7 ]; then
        echo "ORIENT: vault-graph.json is $GRAPH_AGE days old (>7)"
    fi
fi

echo "ORIENT: daily=$DAILY_SIZE bytes, handoff=OK, graph=OK"

SESSION_DIR="$VAULT_DIR/.session"
mkdir -p "$SESSION_DIR"
CAPTURE_FILE="$SESSION_DIR/capture.json"
EXECUTE_FILE="$SESSION_DIR/execute.json"
REPORT=""

YEARLY_GOALS=$(ls "$VAULT_DIR/goals/1-yearly-"*.md 2>/dev/null | tail -1 || true)
if [ -n "${YEARLY_GOALS:-}" ]; then
    YEARLY_GOALS_NAME=$(basename "$YEARLY_GOALS")
else
    YEARLY_GOALS_NAME="1-yearly-YYYY.md"
fi

run_codex() {
    local prompt="$1"
    "$CODEX_CLI" exec --skip-git-repo-check --sandbox workspace-write \
        -c 'mcp_servers.ticktick.command="npx"' \
        -c 'mcp_servers.ticktick.args=["-y","@ticktick/mcp-server"]' \
        -c 'mcp_servers.ticktick.startup_timeout_sec=30' \
        "$prompt"
}

MCP_PROMPT="MCP can take 10-30 seconds to warm up.
If the first TickTick MCP call fails, retry after waiting.
Do not claim MCP is unavailable without retries."

cd "$VAULT_DIR"

echo "=== Phase 1: CAPTURE ==="
CAPTURE=$(run_codex "Today is $TODAY. Execute CAPTURE phase.
Read daily/$TODAY.md, goals/3-weekly.md, goals/2-monthly.md, goals/$YEARLY_GOALS_NAME.
Classify each entry into categories: task, idea, learning, reflection, crm, other.
Return ONLY valid JSON with arrays per category and short metadata." 2>&1) || true

echo "$CAPTURE" | grep -o '{.*}' | python3 -c '
import json
import sys
for line in sys.stdin:
    try:
        data = json.loads(line.strip())
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.exit(0)
    except Exception:
        pass
sys.stdout.write("{\"error\": \"failed to parse capture output\", \"raw\": \"see capture.log\"}")
' > "$CAPTURE_FILE" 2>/dev/null || echo '{"error": "capture failed"}' > "$CAPTURE_FILE"

echo "Capture saved: $(wc -c < "$CAPTURE_FILE") bytes"

if grep -q '"error"' "$CAPTURE_FILE"; then
    echo "WARN: capture phase had issues, falling back to monolith mode"
    REPORT=$(run_codex "Today is $TODAY. Execute daily processing according to dbrain-processor skill.
$MCP_PROMPT" 2>&1) || true
else
    echo "=== Phase 2: EXECUTE ==="
    EXECUTE=$(run_codex "Today is $TODAY. Execute EXECUTE phase.
Read .session/capture.json for input data.
Read business/_index.md and projects/_index.md for context.
Use available TickTick MCP tools to create/update tasks where relevant.
Save non-task entries into the vault in the most relevant locations.
Update CRM notes when client-related entries are present.
Return ONLY valid JSON with actions taken, created task IDs/links, and any errors.
$MCP_PROMPT" 2>&1) || true

    echo "$EXECUTE" | grep -o '{.*}' | python3 -c '
import json
import sys
for line in sys.stdin:
    try:
        data = json.loads(line.strip())
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        sys.exit(0)
    except Exception:
        pass
sys.stdout.write("{\"error\": \"failed to parse execute output\"}")
' > "$EXECUTE_FILE" 2>/dev/null || echo '{"error": "execute failed"}' > "$EXECUTE_FILE"

    echo "Execute saved: $(wc -c < "$EXECUTE_FILE") bytes"

    echo "=== Phase 3: REFLECT ==="
    REPORT=$(run_codex "Today is $TODAY. Execute REFLECT phase.
Read .session/capture.json and .session/execute.json for input data.
Read MEMORY.md, .session/handoff.md, .graph/health-history.json.
Generate concise HTML report, update MEMORY.md, and append key observations to .session/handoff.md.
Return ONLY RAW HTML (for Telegram)." 2>&1) || true
fi

cd "$PROJECT_DIR"

echo "=== Codex output ==="
echo "$REPORT"
echo "===================="

REPORT_CLEAN=$(echo "$REPORT" | sed '/<!--/,/-->/d')

echo "=== Rebuilding vault graph ==="
cd "$VAULT_DIR"
uv run "$SKILLS_ROOT/skills/graph-builder/scripts/analyze.py" || echo "Graph rebuild failed (non-critical)"

echo "=== Memory decay ==="
uv run "$SKILLS_ROOT/skills/agent-memory/scripts/memory-engine.py" decay . || echo "Memory decay failed (non-critical)"
cd "$PROJECT_DIR"

git add -A
git commit -m "chore: process daily $TODAY" || true
git push || true

if [ -n "$REPORT_CLEAN" ] && [ -n "$CHAT_ID" ]; then
    echo "=== Sending to Telegram ==="
    RESULT=$(curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d "chat_id=$CHAT_ID" \
        -d "text=$REPORT_CLEAN" \
        -d "parse_mode=HTML")

    if echo "$RESULT" | grep -q '"ok":false'; then
        echo "HTML failed: $RESULT"
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$CHAT_ID" \
            -d "text=$REPORT_CLEAN"
    fi
fi

echo "=== Done ==="
