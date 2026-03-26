# TickTick MCP Setup

This project now uses TickTick instead of Todoist for task management.

## 1. Set environment variables

Add these values to `.env`:

```bash
TICKTICK_API_TOKEN=your_ticktick_token
TICKTICK_API_DOMAIN=
```

Notes:
- `TICKTICK_API_TOKEN` is required.
- `TICKTICK_API_DOMAIN` is optional (use it for non-default domains, for example Dida365).

## 2. Configure MCP server

Project MCP config is in `mcp-config.json` and uses:

- command: `npx`
- args: `-y @ticktick/mcp-server`

No extra manual `codex mcp add` step is required for daily processing scripts.

## 3. Verify in Codex

Run a quick test from project root:

```bash
codex exec --skip-git-repo-check "Use TickTick MCP tools to read projects and return a short status."
```

If the first run fails due MCP startup latency, run it again after ~10-30 seconds.

## 4. Troubleshooting

- Check token is present in `.env`.
- Ensure Node.js and `npx` are installed.
- Retry once after MCP warmup.
