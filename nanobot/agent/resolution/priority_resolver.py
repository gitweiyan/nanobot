"""Priority resolver for USER > SOUL > MEMORY > AGENTS > TOOLS flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from nanobot.memory import MemoryManager


@dataclass
class TurnResolution:
    """Decision result before model/tool execution."""

    blocked: bool
    response: str | None = None
    trace: list[str] = field(default_factory=list)


class PriorityResolver:
    """Resolve turn/tool decisions with explicit priority traces."""

    _PERSONA_IMPERSONATION_PATTERNS = (
        re.compile(r"pretend\s+.*human", re.IGNORECASE),
        re.compile(r"do\s+not\s+say\s+you\s+are\s+ai", re.IGNORECASE),
        re.compile(r"假装.*人类"),
        re.compile(r"伪装.*人类"),
        re.compile(r"你是人类"),
        re.compile(r"不要说你是ai", re.IGNORECASE),
    )

    def __init__(
        self,
        workspace: Path,
        memory: MemoryManager,
        *,
        memory_trust_threshold: float = 0.85,
        enforce_tool_and_role_intersection: bool = True,
    ):
        self.workspace = workspace
        self.memory = memory
        self.memory_trust_threshold = memory_trust_threshold
        self.enforce_tool_and_role_intersection = enforce_tool_and_role_intersection

    def configure(
        self,
        *,
        memory_trust_threshold: float | None = None,
        enforce_tool_and_role_intersection: bool | None = None,
    ) -> None:
        if memory_trust_threshold is not None:
            self.memory_trust_threshold = memory_trust_threshold
        if enforce_tool_and_role_intersection is not None:
            self.enforce_tool_and_role_intersection = enforce_tool_and_role_intersection

    def _read_file(self, name: str) -> str:
        path = self.workspace / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    @staticmethod
    def _extract_markdown_section(content: str, heading: str) -> str:
        if not content:
            return ""
        # Match "## Heading" until next "## "
        pattern = re.compile(
            rf"(?ims)^\s*##\s+{re.escape(heading)}\s*$\n(.*?)(?=^\s*##\s+|\Z)"
        )
        match = pattern.search(content)
        if not match:
            return ""
        return match.group(1).strip()

    def _soul_hard_constraints(self, soul_content: str) -> str:
        hard = self._extract_markdown_section(soul_content, "Hard Constraints")
        if hard:
            return hard
        # fallback to values section when hard section absent
        return self._extract_markdown_section(soul_content, "Values")

    def _soul_style(self, soul_content: str) -> str:
        style = self._extract_markdown_section(soul_content, "Style")
        if style:
            return style
        # fallback to personality + communication style
        parts: list[str] = []
        for heading in ("Personality", "Communication Style"):
            section = self._extract_markdown_section(soul_content, heading)
            if section:
                parts.append(f"### {heading}\n{section}")
        return "\n\n".join(parts).strip()

    def build_priority_blocks(
        self,
        *,
        user_intent: str | None = None,
        memory_query: str | None = None,
    ) -> list[str]:
        """Build system prompt blocks in declared priority order."""
        user_md = self._read_file("USER.md")
        soul_md = self._read_file("SOUL.md")
        agents_md = self._read_file("AGENTS.md")
        tools_md = self._read_file("TOOLS.md")

        soul_hard = self._soul_hard_constraints(soul_md)
        soul_style = self._soul_style(soul_md)
        memory = self.memory.get_relevant_context(memory_query)

        blocks: list[str] = []

        user_lines: list[str] = []
        if user_intent:
            user_lines.append(f"- Current User Intent: {user_intent}")
        if user_md:
            user_lines.append("\n## USER.md\n" + user_md.strip())
        if user_lines:
            blocks.append("# [P1] USER\n" + "\n".join(user_lines))

        soul_lines: list[str] = []
        if soul_hard:
            soul_lines.append("## Hard Constraints\n" + soul_hard)
        if soul_style:
            soul_lines.append("## Style\n" + soul_style)
        if soul_lines:
            blocks.append("# [P2] SOUL\n" + "\n\n".join(soul_lines))

        if memory:
            blocks.append("# [P3] RELEVANT MEMORY\n" + memory)
        if agents_md:
            blocks.append("# [P4] AGENTS\n" + agents_md.strip())
        if tools_md:
            blocks.append("# [P5] TOOLS\n" + tools_md.strip())

        return blocks

    def resolve_turn(self, user_input: str) -> TurnResolution:
        """Apply high-priority hard constraints before normal processing."""
        text = (user_input or "").strip()
        trace: list[str] = []
        if not text:
            return TurnResolution(blocked=False, trace=["no_input"])

        soul_md = self._read_file("SOUL.md")
        hard = self._soul_hard_constraints(soul_md).lower()

        if "伪装" in hard or "human" in hard or "人类" in hard:
            for pat in self._PERSONA_IMPERSONATION_PATTERNS:
                if pat.search(text):
                    trace.append("blocked_by_soul_hard:human_impersonation")
                    return TurnResolution(
                        blocked=True,
                        response="抱歉，这个请求与我的核心边界冲突，我不能执行。",
                        trace=trace,
                    )
        trace.append("allowed")
        return TurnResolution(blocked=False, trace=trace)

    @staticmethod
    def _extract_command(args: Any) -> str:
        if isinstance(args, list) and args:
            args = args[0]
        if isinstance(args, dict):
            cmd = args.get("command")
            return cmd if isinstance(cmd, str) else ""
        return ""

    @staticmethod
    def _extract_path(args: Any) -> str:
        if isinstance(args, list) and args:
            args = args[0]
        if isinstance(args, dict):
            path = args.get("path")
            return path if isinstance(path, str) else ""
        return ""

    def authorize_tool_call(self, tool_name: str, arguments: Any) -> dict[str, Any]:
        """Authorize tool call by role/tool intersection guardrails."""
        if not self.enforce_tool_and_role_intersection:
            return {"allowed": True, "reason": "intersection_disabled"}

        role_ok = True  # Reserved hook for future multi-role routing.
        tool_ok = True
        reason = "allowed"

        if tool_name == "exec":
            command = self._extract_command(arguments).lower()
            if re.search(r"\bnanobot\s+cron\b", command):
                tool_ok = False
                reason = "tools_policy:use_cron_tool_instead_of_exec"
            elif re.search(r"\brm\s+-rf\b", command):
                tool_ok = False
                reason = "tools_policy:dangerous_exec_command"
        elif tool_name in {"write_file", "edit_file"}:
            path = self._extract_path(arguments)
            if path.startswith("/system") or "/system/" in path:
                tool_ok = False
                reason = "tools_policy:system_path_write_forbidden"

        allowed = bool(role_ok and tool_ok)
        if not allowed and reason == "allowed":
            reason = "intersection_denied"
        return {"allowed": allowed, "reason": reason, "role_ok": role_ok, "tool_ok": tool_ok}
