"""Codex processing service."""

import json
import logging
import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from d_brain.services.session import SessionStore

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 1200  # 20 minutes
DEFAULT_MCP_STARTUP_TIMEOUT = 30


class CodexProcessor:
    """Service for triggering Codex CLI processing."""

    def __init__(
        self,
        vault_path: Path,
        ticktick_api_token: str = "",
        ticktick_api_domain: str = "",
        codex_command: str | None = None,
    ) -> None:
        self.vault_path = Path(vault_path)
        self.ticktick_api_token = ticktick_api_token
        self.ticktick_api_domain = ticktick_api_domain
        self.project_root = self.vault_path.parent
        self._mcp_config_path = (self.project_root / "mcp-config.json").resolve()
        self.skills_root = self._detect_skills_root()

        configured_cmd = codex_command or os.environ.get("CODEX_CLI_COMMAND") or "codex"
        self._codex_commands: list[str] = [configured_cmd]

        if os.name == "nt" and not configured_cmd.lower().endswith(".cmd"):
            self._codex_commands.append(f"{configured_cmd}.cmd")

    def _detect_skills_root(self) -> Path:
        """Pick the preferred skills root (.codex first, then .claude fallback)."""
        codex_root = self.vault_path / ".codex"
        if codex_root.exists():
            return codex_root

        claude_root = self.vault_path / ".claude"
        if claude_root.exists():
            return claude_root

        return codex_root

    def _load_skill_content(self) -> str:
        """Load dbrain-processor skill content for inclusion in prompt."""
        skill_dir = self.skills_root / "skills" / "dbrain-processor"
        codex_skill = skill_dir / "SKILL-CODEX.md"
        legacy_skill = skill_dir / "SKILL.md"
        if codex_skill.exists():
            return codex_skill.read_text(encoding="utf-8")
        if legacy_skill.exists():
            return legacy_skill.read_text(encoding="utf-8")
        return ""

    def _load_ticktick_reference(self) -> str:
        """Load TickTick reference for inclusion in prompt."""
        references_dir = self.skills_root / "skills" / "dbrain-processor" / "references"
        ticktick_ref = references_dir / "ticktick.md"
        legacy_ref = references_dir / "todoist.md"
        if ticktick_ref.exists():
            return ticktick_ref.read_text(encoding="utf-8")
        if legacy_ref.exists():
            return legacy_ref.read_text(encoding="utf-8")
        return ""

    def _get_session_context(self, user_id: int) -> str:
        """Get today's session context for prompt enrichment."""
        if user_id == 0:
            return ""

        session = SessionStore(self.vault_path)
        today_entries = session.get_today(user_id)
        if not today_entries:
            return ""

        lines = ["=== TODAY'S SESSION ==="]
        for entry in today_entries[-10:]:
            ts = entry.get("ts", "")[11:16]  # HH:MM from ISO
            entry_type = entry.get("type", "unknown")
            text = entry.get("text", "")[:80]
            if text:
                lines.append(f"{ts} [{entry_type}] {text}")
        lines.append("=== END SESSION ===\n")
        return "\n".join(lines)

    @staticmethod
    def _toml_string(value: str) -> str:
        """Encode string for `codex -c key=value` TOML parsing."""
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _mcp_overrides(self) -> list[str]:
        """Translate mcp-config.json into Codex `-c mcp_servers.*` overrides."""
        if not self._mcp_config_path.exists():
            return []

        try:
            config = json.loads(self._mcp_config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse mcp-config.json: %s", exc)
            return []

        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            return []

        overrides: list[str] = []

        for server_name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                continue

            server_type = server_cfg.get("type", "stdio")
            if server_type != "stdio":
                logger.info("Skipping unsupported MCP server type '%s' for %s", server_type, server_name)
                continue

            command = server_cfg.get("command")
            if not isinstance(command, str) or not command.strip():
                continue

            overrides.extend(
                [
                    "-c",
                    f"mcp_servers.{server_name}.command={self._toml_string(command)}",
                ]
            )

            args = server_cfg.get("args", [])
            if isinstance(args, list):
                args_toml = "[" + ",".join(self._toml_string(str(arg)) for arg in args) + "]"
                overrides.extend(["-c", f"mcp_servers.{server_name}.args={args_toml}"])

            startup_timeout = server_cfg.get(
                "startup_timeout_sec",
                DEFAULT_MCP_STARTUP_TIMEOUT,
            )
            if isinstance(startup_timeout, int):
                overrides.extend(
                    [
                        "-c",
                        f"mcp_servers.{server_name}.startup_timeout_sec={startup_timeout}",
                    ]
                )

            env_cfg = server_cfg.get("env")
            if isinstance(env_cfg, dict):
                for env_key, env_value in env_cfg.items():
                    if not isinstance(env_key, str):
                        continue
                    overrides.extend(
                        [
                            "-c",
                            (
                                f"mcp_servers.{server_name}.env."
                                f"{env_key}={self._toml_string(str(env_value))}"
                            ),
                        ]
                    )

        return overrides

    def _run_codex(self, prompt: str) -> subprocess.CompletedProcess[str]:
        """Execute Codex CLI and return raw subprocess result."""
        env = os.environ.copy()
        if self.ticktick_api_token:
            env["TICKTICK_API_TOKEN"] = self.ticktick_api_token
            env["API_TOKEN"] = self.ticktick_api_token
        if self.ticktick_api_domain:
            env["TICKTICK_API_DOMAIN"] = self.ticktick_api_domain
            env["API_DOMAIN"] = self.ticktick_api_domain

        args = [
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            *self._mcp_overrides(),
            prompt,
        ]

        last_file_error: FileNotFoundError | None = None

        for command in self._codex_commands:
            try:
                return subprocess.run(
                    [command, *args],
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=DEFAULT_TIMEOUT,
                    check=False,
                    env=env,
                )
            except FileNotFoundError as exc:
                last_file_error = exc
                continue

        raise last_file_error or FileNotFoundError("Codex CLI not found")

    def _html_to_markdown(self, html: str) -> str:
        """Convert Telegram HTML to Obsidian Markdown."""
        import re

        text = html
        text = re.sub(r"<b>(.*?)</b>", r"**\1**", text)
        text = re.sub(r"<i>(.*?)</i>", r"*\1*", text)
        text = re.sub(r"<code>(.*?)</code>", r"`\1`", text)
        text = re.sub(r"<s>(.*?)</s>", r"~~\1~~", text)
        text = re.sub(r"</?u>", "", text)
        text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"[\2](\1)", text)

        return text

    def _save_weekly_summary(self, report_html: str, week_date: date) -> Path:
        """Save weekly summary to vault/summaries/YYYY-WXX-summary.md."""
        year, week, _ = week_date.isocalendar()
        filename = f"{year}-W{week:02d}-summary.md"
        summary_path = self.vault_path / "summaries" / filename

        content = self._html_to_markdown(report_html)

        frontmatter = f"""---
date: {week_date.isoformat()}
type: weekly-summary
week: {year}-W{week:02d}
---

"""
        summary_path.write_text(frontmatter + content, encoding="utf-8")
        logger.info("Weekly summary saved to %s", summary_path)
        return summary_path

    def _update_weekly_moc(self, summary_path: Path) -> None:
        """Add link to new summary in MOC-weekly.md."""
        moc_path = self.vault_path / "MOC" / "MOC-weekly.md"
        if moc_path.exists():
            content = moc_path.read_text(encoding="utf-8")
            link = f"- [[summaries/{summary_path.name}|{summary_path.stem}]]"
            if summary_path.stem not in content:
                content = content.replace(
                    "## Previous Weeks\n",
                    f"## Previous Weeks\n\n{link}\n",
                )
                moc_path.write_text(content, encoding="utf-8")
                logger.info("Updated MOC-weekly.md with link to %s", summary_path.stem)

    def process_daily(self, day: date | None = None) -> dict[str, Any]:
        """Process daily file with Codex."""
        if day is None:
            day = date.today()

        daily_file = self.vault_path / "daily" / f"{day.isoformat()}.md"

        if not daily_file.exists():
            logger.warning("No daily file for %s", day)
            return {
                "error": f"No daily file for {day}",
                "processed_entries": 0,
            }

        skill_content = self._load_skill_content()

        prompt = f"""Today is {day}. Run daily second-brain processing.

=== SKILL INSTRUCTIONS ===
{skill_content}
=== END SKILL ===

FIRST ACTION:
- Verify TickTick MCP connectivity with a lightweight read tool call.

RULES:
- Use available mcp__ticktick__* tools directly.
- Never claim MCP is unavailable without retries.
- If a tool fails, include the exact error in the report.

OUTPUT FORMAT:
- Return ONLY raw HTML for Telegram (parse_mode=HTML).
- No Markdown, no code fences, no tables.
- Start with: <b>Processing report for {day}</b>
- Allowed tags: <b>, <i>, <code>, <s>, <u>"""

        try:
            result = self._run_codex(prompt)

            if result.returncode != 0:
                logger.error("Codex processing failed: %s", result.stderr)
                return {
                    "error": result.stderr.strip() or "Codex processing failed",
                    "processed_entries": 0,
                }

            output = result.stdout.strip()
            return {
                "report": output,
                "processed_entries": 1,
            }

        except subprocess.TimeoutExpired:
            logger.error("Codex processing timed out")
            return {
                "error": "Processing timed out",
                "processed_entries": 0,
            }
        except FileNotFoundError:
            logger.error("Codex CLI not found")
            return {
                "error": "Codex CLI not installed",
                "processed_entries": 0,
            }
        except Exception as exc:
            logger.exception("Unexpected error during processing")
            return {
                "error": str(exc),
                "processed_entries": 0,
            }

    def execute_prompt(self, user_prompt: str, user_id: int = 0) -> dict[str, Any]:
        """Execute arbitrary prompt with Codex."""
        today = date.today()

        ticktick_ref = self._load_ticktick_reference()
        session_context = self._get_session_context(user_id)

        prompt = f"""You are the d-brain personal assistant.

CONTEXT:
- Current date: {today}
- Vault path: {self.vault_path}

{session_context}=== TICKTICK REFERENCE ===
{ticktick_ref}
=== END REFERENCE ===

FIRST ACTION:
- Verify TickTick MCP connectivity with a lightweight read tool call.

RULES:
- Use mcp__ticktick__* tools directly when needed.
- If a tool fails, include the exact error in the report.

USER REQUEST:
{user_prompt}

OUTPUT FORMAT:
- Return ONLY raw HTML for Telegram (parse_mode=HTML)
- No Markdown, no code fences, no tables
- Start with emoji and <b>header</b>
- Allowed tags: <b>, <i>, <code>, <s>, <u>
- Be concise (Telegram max length 4096)"""

        try:
            result = self._run_codex(prompt)

            if result.returncode != 0:
                logger.error("Codex execution failed: %s", result.stderr)
                return {
                    "error": result.stderr.strip() or "Codex execution failed",
                    "processed_entries": 0,
                }

            return {
                "report": result.stdout.strip(),
                "processed_entries": 1,
            }

        except subprocess.TimeoutExpired:
            logger.error("Codex execution timed out")
            return {"error": "Execution timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("Codex CLI not found")
            return {"error": "Codex CLI not installed", "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during execution")
            return {"error": str(exc), "processed_entries": 0}

    def generate_weekly(self) -> dict[str, Any]:
        """Generate weekly digest with Codex."""
        today = date.today()

        prompt = f"""Today is {today}. Generate a weekly digest.

FIRST ACTION:
- Verify TickTick MCP connectivity with a lightweight read tool call.

RULES:
- Use TickTick MCP task/project tools directly.
- If a tool fails, include the exact error in the report.

WORKFLOW:
1. Read weekly context from vault/daily and goals/3-weekly.md.
2. Fetch task status/progress from TickTick MCP.
3. Summarize wins, blockers, and next focus.
4. Return a concise weekly digest.

OUTPUT FORMAT:
- Return ONLY raw HTML for Telegram (parse_mode=HTML)
- No Markdown, no code fences, no tables
- Start with: <b>Weekly digest</b>
- Allowed tags: <b>, <i>, <code>, <s>, <u>
- Keep under Telegram limit (4096 chars)"""

        try:
            result = self._run_codex(prompt)

            if result.returncode != 0:
                logger.error("Weekly digest failed: %s", result.stderr)
                return {
                    "error": result.stderr.strip() or "Weekly digest failed",
                    "processed_entries": 0,
                }

            output = result.stdout.strip()

            try:
                summary_path = self._save_weekly_summary(output, today)
                self._update_weekly_moc(summary_path)
            except Exception as exc:
                logger.warning("Failed to save weekly summary: %s", exc)

            return {
                "report": output,
                "processed_entries": 1,
            }

        except subprocess.TimeoutExpired:
            logger.error("Weekly digest timed out")
            return {"error": "Weekly digest timed out", "processed_entries": 0}
        except FileNotFoundError:
            logger.error("Codex CLI not found")
            return {"error": "Codex CLI not installed", "processed_entries": 0}
        except Exception as exc:
            logger.exception("Unexpected error during weekly digest")
            return {"error": str(exc), "processed_entries": 0}


# Backward compatibility for existing imports.
ClaudeProcessor = CodexProcessor
