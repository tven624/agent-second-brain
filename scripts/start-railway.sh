#!/usr/bin/env bash
set -euo pipefail

# Persist Codex auth in container on each boot (optional).
export CODEX_HOME="${CODEX_HOME:-/app/.codex}"
mkdir -p "$CODEX_HOME"

if [[ -n "${CODEX_AUTH_JSON_B64:-}" ]]; then
  printf "%s" "$CODEX_AUTH_JSON_B64" | base64 -d > "$CODEX_HOME/auth.json"
  chmod 600 "$CODEX_HOME/auth.json" || true
fi

exec uv run python -m d_brain
