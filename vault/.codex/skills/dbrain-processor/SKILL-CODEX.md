# dbrain-processor (Codex + TickTick)

Use this minimal guidance for daily processing.

## Goal
Turn raw daily entries into:
- actionable TickTick tasks,
- structured notes in the vault,
- a concise Telegram HTML report.

## Rules
- Prefer TickTick MCP tools for task operations.
- Avoid duplicate task creation (search before create when needed).
- Keep outputs concise and factual.
- If a tool fails, include exact error in report JSON/HTML.

## Output constraints
- For JSON phases: return valid JSON only.
- For Telegram report: return HTML only using safe tags (`<b>`, `<i>`, `<code>`, `<s>`, `<u>`).
