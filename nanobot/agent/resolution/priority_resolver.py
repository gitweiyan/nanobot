"""Priority resolver for SOUL > AGENTS > TOOLS > MEMORY > USER flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
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


@dataclass
class HardRule:
    """A parsed hard-constraint rule from SOUL.md."""

    rule_id: str
    source_line: str
    patterns: list[re.Pattern[str]]
    deny_message: str


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
    _ILLEGAL_ACTIVITY_PATTERNS = (
        re.compile(r"\b(hack|malware|ransomware|ddos|exploit)\b", re.IGNORECASE),
        re.compile(r"(制作|编写).*(木马|病毒|勒索软件|恶意软件)"),
        re.compile(r"(教我|指导).*(入侵|攻击)"),
    )
    _RULE_LIBRARY: dict[str, dict[str, Any]] = {
        "human_impersonation": {
            "aliases": ("human", "人类", "伪装", "假装", "identity"),
            "patterns": _PERSONA_IMPERSONATION_PATTERNS,
            "deny_message": "抱歉，这个请求与我的核心边界冲突，我不能执行。",
        },
        "illegal_activity": {
            "aliases": ("illegal", "违法", "犯罪", "abusive"),
            "patterns": _ILLEGAL_ACTIVITY_PATTERNS,
            "deny_message": "抱歉，我不能协助违法或明显有害的行为。",
        },
    }
    _EXPLICIT_RULE_ID_PATTERN = re.compile(r"\[rule:([a-z0-9_\-]+)\]", re.IGNORECASE)
    _HARD_RULE_LOOKBACK_DAYS = 90
    _PROTECTED_WRITE_ROOTS = (Path("/system"),)

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

    @staticmethod
    def _iter_constraint_lines(section: str) -> list[str]:
        lines: list[str] = []
        for raw in section.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("- ", "* ")):
                line = line[2:].strip()
            lines.append(line)
        return lines

    def _compile_hard_rules(self, soul_content: str) -> list[HardRule]:
        section = self._soul_hard_constraints(soul_content)
        if not section:
            return []

        rules: list[HardRule] = []
        seen_ids: set[str] = set()

        for line in self._iter_constraint_lines(section):
            line_low = line.lower()
            explicit_match = self._EXPLICIT_RULE_ID_PATTERN.search(line)
            explicit_id = explicit_match.group(1).lower() if explicit_match else None

            if explicit_id and explicit_id in self._RULE_LIBRARY:
                entry = self._RULE_LIBRARY[explicit_id]
                if explicit_id not in seen_ids:
                    rules.append(
                        HardRule(
                            rule_id=explicit_id,
                            source_line=line,
                            patterns=list(entry["patterns"]),
                            deny_message=str(entry["deny_message"]),
                        )
                    )
                    seen_ids.add(explicit_id)
                continue

            matched_any = False
            for rule_id, entry in self._RULE_LIBRARY.items():
                aliases = tuple(str(a).lower() for a in entry["aliases"])
                if any(alias in line_low for alias in aliases):
                    if rule_id in seen_ids:
                        matched_any = True
                        continue
                    rules.append(
                        HardRule(
                            rule_id=rule_id,
                            source_line=line,
                            patterns=list(entry["patterns"]),
                            deny_message=str(entry["deny_message"]),
                        )
                    )
                    seen_ids.add(rule_id)
                    matched_any = True

            if matched_any:
                continue

            fallback_id = "line_" + hashlib.sha1(line.encode("utf-8")).hexdigest()[:8]
            literal = re.escape(line[:80])
            if fallback_id not in seen_ids and literal:
                rules.append(
                    HardRule(
                        rule_id=fallback_id,
                        source_line=line,
                        patterns=[re.compile(literal, re.IGNORECASE)],
                        deny_message="抱歉，该请求触发了 SOUL 硬约束，无法执行。",
                    )
                )
                seen_ids.add(fallback_id)

        return rules

    @staticmethod
    def _extract_user_bullets(user_md: str) -> list[str]:
        lines: list[str] = []
        for raw in user_md.splitlines():
            stripped = raw.strip()
            if stripped.startswith("- "):
                value = stripped[2:].strip()
                if value:
                    lines.append(value)
        return lines

    @staticmethod
    def _infer_preference_key(text: str) -> str | None:
        low = text.lower()
        if any(k in low for k in ("concise", "brief", "详细", "简洁", "verbosity", "verbose")):
            return "response_verbosity"
        if any(k in low for k in ("中文", "english", "language", "语言")):
            return "language"
        if any(k in low for k in ("bullet", "list", "格式", "排版")):
            return "formatting"
        if any(k in low for k in ("timezone", "时区", "utc")):
            return "timezone"
        return None

    @staticmethod
    def _parse_iso_time(value: str | None) -> datetime | None:
        if not value:
            return None
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _is_recent_memory(self, item: dict[str, Any]) -> bool:
        updated = self._parse_iso_time(str(item.get("updated_at") or ""))
        if updated is None:
            return False
        return datetime.now(timezone.utc) - updated <= timedelta(days=self._HARD_RULE_LOOKBACK_DAYS)

    def _trusted_memory_preferences(self) -> list[dict[str, Any]]:
        items = self.memory.store.list_items(scope="user", kind="preference", status="active")
        trusted = []
        for item in items:
            confidence = float(item.get("confidence") or 0.0)
            if confidence < self.memory_trust_threshold:
                continue
            if not self._is_recent_memory(item):
                continue
            trusted.append(item)
        trusted.sort(
            key=lambda i: (
                float(i.get("confidence") or 0.0),
                str(i.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return trusted

    def _render_effective_user_block(self, user_md: str) -> str:
        """Merge USER.md with trusted memory preferences (memory can override by key)."""
        trusted = self._trusted_memory_preferences()
        if not trusted:
            return user_md.strip()

        user_bullets = self._extract_user_bullets(user_md)
        effective: dict[str, str] = {}
        passthrough: list[str] = []

        for bullet in user_bullets:
            key = self._infer_preference_key(bullet)
            if key:
                effective[key] = bullet
            else:
                passthrough.append(bullet)

        overrides: list[str] = []
        for item in trusted:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            key = self._infer_preference_key(content)
            confidence = float(item.get("confidence") or 0.0)
            if key:
                previous = effective.get(key)
                if previous and previous != content:
                    overrides.append(f"- {key}: USER.md='{previous}' -> Memory='{content}' (conf={confidence:.2f})")
                effective[key] = content
            else:
                passthrough.append(f"{content} (memory, conf={confidence:.2f})")

        out_lines = ["## Effective Preferences"]
        for key, value in effective.items():
            out_lines.append(f"- {key}: {value}")
        for line in passthrough:
            out_lines.append(f"- {line}")
        if overrides:
            out_lines.append("")
            out_lines.append("## Memory Overrides")
            out_lines.extend(overrides)
        return "\n".join(out_lines).strip()

    def build_priority_blocks(
        self,
        *,
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

        soul_lines: list[str] = []
        if soul_hard:
            soul_lines.append("## Hard Constraints\n" + soul_hard)
        if soul_style:
            soul_lines.append("## Style\n" + soul_style)
        if soul_lines:
            blocks.append("# [P1] SOUL\n" + "\n\n".join(soul_lines))

        if agents_md:
            blocks.append("# [P2] AGENTS\n" + agents_md.strip())
        if tools_md:
            blocks.append("# [P3] TOOLS\n" + tools_md.strip())

        if memory:
            blocks.append("# [P4] RELEVANT MEMORY\n" + memory)
        if user_md:
            blocks.append("# [P5] USER\n" + self._render_effective_user_block(user_md))

        return blocks

    def resolve_turn(self, user_input: str) -> TurnResolution:
        """Apply high-priority hard constraints before normal processing."""
        text = (user_input or "").strip()
        trace: list[str] = []
        if not text:
            return TurnResolution(blocked=False, trace=["no_input"])

        soul_md = self._read_file("SOUL.md")
        hard_rules = self._compile_hard_rules(soul_md)
        trace.append(f"hard_rules_loaded:{len(hard_rules)}")

        for rule in hard_rules:
            for pattern in rule.patterns:
                if pattern.search(text):
                    trace.append(f"blocked_by_soul_hard:{rule.rule_id}")
                    return TurnResolution(
                        blocked=True,
                        response=rule.deny_message,
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

    def _resolve_target_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.workspace / path)
        return path.resolve(strict=False)

    @staticmethod
    def _is_protected_write_path(path: Path) -> bool:
        for root in PriorityResolver._PROTECTED_WRITE_ROOTS:
            root_resolved = root.resolve(strict=False)
            if path == root_resolved or root_resolved in path.parents:
                return True
        return False

    @staticmethod
    def _check_exec_no_cron_shell(_tool_name: str, arguments: Any) -> str | None:
        command = PriorityResolver._extract_command(arguments).lower()
        if re.search(r"\bnanobot\s+cron\b", command):
            return "tools_policy:use_cron_tool_instead_of_exec"
        return None

    @staticmethod
    def _check_exec_dangerous_rmrf(_tool_name: str, arguments: Any) -> str | None:
        command = PriorityResolver._extract_command(arguments).lower()
        if re.search(r"\brm\s+-rf\b", command):
            return "tools_policy:dangerous_exec_command"
        return None

    def _check_write_protected_paths(self, _tool_name: str, arguments: Any) -> str | None:
        raw_path = self._extract_path(arguments)
        if not raw_path:
            return None
        resolved = self._resolve_target_path(raw_path)
        if self._is_protected_write_path(resolved):
            return "tools_policy:system_path_write_forbidden"
        return None

    def authorize_tool_call(self, tool_name: str, arguments: Any) -> dict[str, Any]:
        """Authorize tool call by role/tool intersection guardrails."""
        if not self.enforce_tool_and_role_intersection:
            return {"allowed": True, "reason": "intersection_disabled"}

        role_ok = True  # Reserved hook for future multi-role routing.
        policy_table: dict[str, list] = {
            "exec": [
                self._check_exec_no_cron_shell,
                self._check_exec_dangerous_rmrf,
            ],
            "write_file": [self._check_write_protected_paths],
            "edit_file": [self._check_write_protected_paths],
        }

        tool_ok = True
        reason = "allowed"
        for check in policy_table.get(tool_name, []):
            denied_reason = check(tool_name, arguments)
            if denied_reason:
                tool_ok = False
                reason = denied_reason
                break

        allowed = bool(role_ok and tool_ok)
        if not allowed and reason == "allowed":
            reason = "intersection_denied"
        return {"allowed": allowed, "reason": reason, "role_ok": role_ok, "tool_ok": tool_ok}
