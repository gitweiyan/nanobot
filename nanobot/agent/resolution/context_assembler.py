"""Context assembler: builds priority-ordered system prompt blocks.

Replaces PriorityResolver. Design principles:
- Code handles: prompt assembly order, irreversible side-effect guards.
- LLM handles: semantic constraint enforcement, preference conflict resolution.
- No regex-based intent detection. No rule libraries. No preference key inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.memory import MemoryManager


@dataclass
class ToolAuthResult:
    allowed: bool
    reason: str


class ContextAssembler:
    """Assembles system prompt blocks in priority order and guards irreversible tool calls."""

    PRIORITY_FILES = [
        ("[P1] SOUL",   "SOUL.md"),
        ("[P2] AGENTS", "AGENTS.md"),
        ("[P3] TOOLS",  "TOOLS.md"),
    ]

    _PROTECTED_WRITE_ROOTS = (Path("/system"),)

    def __init__(
        self,
        workspace: Path,
        memory: MemoryManager,
        *,
        memory_trust_threshold: float = 0.85,
    ):
        self.workspace = workspace
        self.memory = memory
        self.memory_trust_threshold = memory_trust_threshold

    def configure(
        self,
        *,
        memory_trust_threshold: float | None = None,
        # kept for drop-in compatibility with old PriorityResolver callers
        enforce_tool_and_role_intersection: bool | None = None,
    ) -> None:
        if memory_trust_threshold is not None:
            self.memory_trust_threshold = memory_trust_threshold

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def build_priority_blocks(self, *, memory_query: str | None = None) -> list[str]:
        """Return system prompt blocks in P1 → P5 priority order.

        Physical position in the prompt is itself the priority signal —
        the LLM weights earlier content more heavily (primacy effect).
        No further enforcement logic is needed here.
        """
        blocks: list[str] = []

        for label, filename in self.PRIORITY_FILES:
            content = self._read_file(filename)
            if content:
                blocks.append(f"# {label}\n\n{content.strip()}")

        memory_ctx = self.memory.get_relevant_context(memory_query)
        if memory_ctx:
            blocks.append(f"# [P4] RELEVANT MEMORY\n\n{memory_ctx}")

        user_md = self._read_file("USER.md")
        if user_md:
            blocks.append(f"# [P5] USER\n\n{self._render_user_block(user_md)}")

        return blocks

    def _render_user_block(self, user_md: str) -> str:
        """Merge USER.md with high-confidence memory preferences.

        Rather than inferring preference keys in Python, we pass both sources
        to the LLM with a single natural-language override instruction and let
        it resolve conflicts semantically.
        """
        trusted = self._trusted_memory_preferences()
        parts = [user_md.strip()]

        if trusted:
            items = "\n".join(
                f"- {item['content']} (confidence={float(item.get('confidence', 0)):.2f})"
                for item in trusted
                if str(item.get("content", "")).strip()
            )
            if items:
                parts.append(
                    "## Memory-derived preferences\n"
                    "The following preferences were inferred from past conversations. "
                    "They take precedence over USER.md when they conflict:\n"
                    + items
                )

        return "\n\n".join(parts)

    def _trusted_memory_preferences(self) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta, timezone

        items = self.memory.store.list_items(scope="user", kind="preference", status="active")
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        trusted = []

        for item in items:
            confidence = float(item.get("confidence") or 0.0)
            if confidence < self.memory_trust_threshold:
                continue
            updated_raw = str(item.get("updated_at") or "")
            try:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(updated_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except ValueError:
                continue
            trusted.append(item)

        trusted.sort(
            key=lambda i: (float(i.get("confidence") or 0.0), str(i.get("updated_at") or "")),
            reverse=True,
        )
        return trusted

    # ------------------------------------------------------------------
    # Tool authorization — only irreversible path writes are blocked here.
    # Semantic tool-usage rules (e.g. "prefer cron over exec") live in
    # TOOLS.md and are enforced by the LLM, not by regex in Python.
    # ------------------------------------------------------------------

    def authorize_tool_call(self, tool_name: str, arguments: Any) -> ToolAuthResult:
        """Block writes to protected system paths. All other policy is in TOOLS.md."""
        if tool_name in ("write_file", "edit_file"):
            raw_path = self._extract_path(arguments)
            if raw_path:
                resolved = self._resolve_path(raw_path)
                if self._is_protected_path(resolved):
                    return ToolAuthResult(
                        allowed=False,
                        reason="system_path_write_forbidden",
                    )
        return ToolAuthResult(allowed=True, reason="allowed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_file(self, name: str) -> str:
        path = self.workspace / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _resolve_path(self, raw: str) -> Path:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve(strict=False)

    @staticmethod
    def _is_protected_path(path: Path) -> bool:
        for root in ContextAssembler._PROTECTED_WRITE_ROOTS:
            root_resolved = root.resolve(strict=False)
            if path == root_resolved or root_resolved in path.parents:
                return True
        return False

    @staticmethod
    def _extract_path(arguments: Any) -> str:
        if isinstance(arguments, list) and arguments:
            arguments = arguments[0]
        if isinstance(arguments, dict):
            val = arguments.get("path")
            return val if isinstance(val, str) else ""
        return ""