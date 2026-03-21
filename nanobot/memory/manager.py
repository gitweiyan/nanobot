"""Memory manager that unifies persistence, consolidation, and retrieval.

P0 changes in this file:
  1. MEMORY.md is no longer a source of truth. It is a generated human-readable
     view rebuilt from items.jsonl after every consolidation (_rebuild_memory_md).
     get_relevant_context() always uses search_items(); MEMORY.md is only injected
     as a token-capped fallback when items.jsonl is empty.

P1 changes:
  2. get_relevant_context() fallback is token-capped (max_chars parameter) to
     prevent a large MEMORY.md from flooding the prompt.
  3. existing_items_summary() is exposed so memory.py (the consolidation layer)
     can inject current items into the consolidation prompt, guiding the LLM to
     produce only genuinely new structured_items.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nanobot.memory.store import StructuredMemoryStore


def _now_iso_short() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()[:10]


class MemoryManager:
    """Single entrypoint for memory read/write operations."""

    def __init__(self, workspace: Path):
        self.store = StructuredMemoryStore(workspace)

    # ------------------------------------------------------------------
    # Low-level pass-throughs
    # ------------------------------------------------------------------

    def read_long_term(self) -> str:
        return self.store.read_long_term()

    def write_long_term(self, content: str) -> None:
        self.store.write_long_term(content)

    def append_history(self, entry: str) -> None:
        self.store.append_history(entry)

    # ------------------------------------------------------------------
    # P0-1: MEMORY.md is a generated view
    # ------------------------------------------------------------------

    def _rebuild_memory_md(self) -> None:
        """Regenerate MEMORY.md from the current active items in items.jsonl.

        Called at the end of every apply_consolidation so the file stays in
        sync. MEMORY.md is purely for human inspection; the agent always reads
        from items.jsonl via search_items / get_relevant_context.
        """
        active = self.store.list_items(status="active")
        if not active:
            self.store.write_long_term(
                f"# Memory\n\n*Updated: {_now_iso_short()}. No active items.*\n"
            )
            return

        by_kind: dict[str, list[str]] = {"preference": [], "decision": [], "fact": []}
        for item in sorted(active, key=lambda i: i.get("updated_at", ""), reverse=True):
            kind = item.get("kind", "fact")
            bucket = by_kind.get(kind, by_kind["fact"])
            scope = item.get("scope", "project")
            conf = float(item.get("confidence") or 0)
            bucket.append(f"- [{scope}] {item.get('content', '')}  (conf={conf:.2f})")

        sections: list[str] = [f"# Memory\n\n*Updated: {_now_iso_short()}*"]
        labels = [("preference", "Preferences"), ("decision", "Decisions"), ("fact", "Facts")]
        for key, heading in labels:
            lines = by_kind.get(key, [])
            if lines:
                sections.append(f"## {heading}\n\n" + "\n".join(lines))

        self.store.write_long_term("\n\n".join(sections) + "\n")

    # ------------------------------------------------------------------
    # P1-1: expose existing items for consolidation prompt injection
    # ------------------------------------------------------------------

    def existing_items_summary(self, limit: int = 40) -> str:
        """Return a compact text summary of current active items.

        Injected into the consolidation prompt so the LLM knows what already
        exists and can avoid emitting duplicate structured_items.
        """
        active = self.store.list_items(status="active")
        if not active:
            return "(none)"
        recent = sorted(active, key=lambda i: i.get("updated_at", ""), reverse=True)[:limit]
        lines = [
            f"- [{i.get('kind', 'fact')}|{i.get('scope', 'project')}] {i.get('content', '')}"
            for i in recent
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Consolidation persistence
    # ------------------------------------------------------------------

    def ingest_memory_markdown(
        self,
        memory_markdown: str,
        *,
        source_ref: str | None = None,
        scope: str = "project",
    ) -> int:
        """Fallback: extract items from markdown when LLM omitted structured_items.

        All items default to kind='fact'; LLM-provided structured_items are
        strictly preferred because they carry accurate kind/scope/confidence.
        """
        count = 0
        for line in self.store.extract_candidate_lines(memory_markdown):
            item = self.store.upsert_item(
                scope=scope,
                kind="fact",
                content=line,
                confidence=0.6,
                source_ref=source_ref,
            )
            if item:
                count += 1
        return count

    def apply_consolidation(
        self,
        *,
        history_entry: str,
        memory_update: str,
        source_messages: list[dict[str, Any]] | None = None,
        structured_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist consolidation output.

        Write path (P0-1):
          1. Append to HISTORY.md.
          2. Write structured items to items.jsonl (primary) or fall back to
             ingest_memory_markdown() (legacy providers that omit structured_items).
          3. Rebuild MEMORY.md from items.jsonl so it stays in sync.

        memory_update is still accepted for backward compatibility (providers
        that don't support the extended tool schema will still send it), but it
        is no longer written directly to MEMORY.md — the rebuild step handles that.
        """
        self.append_history(history_entry)

        source_ref: str = "consolidation"
        if source_messages:
            ts = str((source_messages[0].get("timestamp") or ""))[:16]
            if ts:
                source_ref = ts

        self.store.append_event(
            {
                "type": "consolidation",
                "summary": history_entry,
                "source_ref": source_ref,
                "message_count": len(source_messages or []),
                "structured_items_count": len(structured_items) if structured_items else 0,
            }
        )

        if structured_items:
            # Primary path: LLM classified items directly — accurate kind/scope/confidence.
            for raw in structured_items:
                content = str(raw.get("content") or "").strip()
                if not content:
                    continue
                kind = str(raw.get("kind") or "fact")
                if kind not in ("fact", "preference", "decision"):
                    kind = "fact"
                scope = str(raw.get("scope") or "project")
                if scope not in ("user", "project"):
                    scope = "project"
                confidence = min(1.0, max(0.0, float(raw.get("confidence") or 0.7)))
                self.store.upsert_item(
                    scope=scope,
                    kind=kind,
                    content=content,
                    confidence=confidence,
                    source_ref=source_ref,
                )
        else:
            # Fallback: parse markdown (lossy — all items become kind='fact').
            self.ingest_memory_markdown(memory_update, source_ref=source_ref)

        # P0-1: always rebuild MEMORY.md from the now-updated items.jsonl.
        self._rebuild_memory_md()

    def record_raw_archive(self, messages: list[dict[str, Any]], summary: str) -> None:
        self.append_history(summary)
        self.store.append_event(
            {
                "type": "raw_archive",
                "summary": summary.splitlines()[0] if summary else "",
                "message_count": len(messages),
            }
        )

    # ------------------------------------------------------------------
    # P1-2: token-capped retrieval
    # ------------------------------------------------------------------

    def get_relevant_context(
        self,
        query: str | None = None,
        limit: int = 12,
        max_fallback_chars: int = 2_000,
    ) -> str:
        """Build compact memory context for prompt injection.

        Always tries items.jsonl first (search_items). Falls back to MEMORY.md
        only when items.jsonl is empty, and caps the fallback at max_fallback_chars
        to prevent large files from flooding the prompt.
        """
        query = (query or "").strip()
        items = self.store.search_items(query, limit=limit)

        if not items:
            # Fallback: MEMORY.md (generated view — may be empty on first run).
            long_term = self.read_long_term()
            if not long_term:
                return ""
            truncated = long_term[:max_fallback_chars]
            suffix = "\n... (truncated — full memory in MEMORY.md)" if len(long_term) > max_fallback_chars else ""
            return f"## Long-term Memory\n{truncated}{suffix}"

        item_ids = [str(i.get("id")) for i in items if i.get("id")]
        self.store.touch_items(item_ids)

        lines = [
            f"- ({i.get('scope', 'project')}/{i.get('kind', 'fact')}) {str(i.get('content') or '').strip()}"
            for i in items
            if str(i.get("content") or "").strip()
        ]
        return ("## Relevant Memory\n" + "\n".join(lines)) if lines else ""

    # ------------------------------------------------------------------
    # User-facing memory ops
    # ------------------------------------------------------------------

    def remember_from_user_text(
        self,
        text: str,
        *,
        scope: str = "user",
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        content = (text or "").strip()
        if not content:
            return None
        resolved_kind = kind if kind in ("fact", "preference", "decision") else "fact"
        item = self.store.upsert_item(
            scope=scope,
            kind=resolved_kind,
            content=content,
            confidence=0.95,
            source_ref="user_directive",
        )
        if not item:
            return None
        if resolved_kind == "preference":
            others = [
                it for it in self.store.list_items(scope=scope, kind="preference", status="active")
                if it.get("id") != item.get("id")
            ]
            if others:
                self.store.update_item_status(
                    [str(it["id"]) for it in others if it.get("id")], "conflicted"
                )
        self.store.append_event(
            {
                "type": "remember",
                "scope": scope,
                "kind": resolved_kind,
                "content": content,
                "memory_id": item.get("id"),
            }
        )
        # Rebuild the view so MEMORY.md reflects the user's explicit addition.
        self._rebuild_memory_md()
        return item

    def forget_by_query(self, query: str, *, scope: str = "user") -> int:
        matches = self.store.find_items_for_query(
            query, statuses=("active", "conflicted"), scope=scope, limit=50
        )
        ids = [str(i["id"]) for i in matches if i.get("id")]
        count = self.store.update_item_status(ids, "deprecated")
        self.store.append_event(
            {"type": "forget", "scope": scope, "query": query, "matched": count}
        )
        if count:
            self._rebuild_memory_md()
        return count

    def render_memory_snapshot(self, *, limit: int = 20) -> str:
        active = self.store.list_items(status="active")
        if not active:
            return "我目前没有可用的结构化记忆。"
        ranked = sorted(active, key=lambda i: i.get("updated_at", ""), reverse=True)[: max(1, limit)]
        lines = ["当前可用记忆："] + [
            f"- ({i.get('scope', 'project')}/{i.get('kind', 'fact')}) {i.get('content', '')}"
            for i in ranked
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Directive parsing — slash commands only
    # (NL patterns removed: LLM handles "please remember X" naturally)
    # ------------------------------------------------------------------

    _REMEMBER_PATTERNS = (
        re.compile(r"^\s*/remember\s+(.+)$", re.IGNORECASE),
        re.compile(r"^\s*/记住\s+(.+)$"),
    )
    _FORGET_PATTERNS = (
        re.compile(r"^\s*/forget\s+(.+)$", re.IGNORECASE),
        re.compile(r"^\s*/忘记\s+(.+)$"),
    )
    _SHOW_PATTERNS = (
        re.compile(r"^\s*/memory(?:\s+show)?\s*$", re.IGNORECASE),
    )
    _MAX_DIRECTIVE_CHARS = 220

    def _eligible_directive_text(self, text: str) -> str | None:
        raw = (text or "").strip()
        if not raw or not raw.startswith("/"):
            return None
        if len(raw) > self._MAX_DIRECTIVE_CHARS or "\n" in raw or "\r" in raw:
            return None
        return raw

    def parse_user_directive(self, text: str) -> dict[str, Any]:
        raw = self._eligible_directive_text(text)
        if raw is None:
            return {"matched": False, "reason": "not_a_slash_command"}

        for pat in self._SHOW_PATTERNS:
            if pat.match(raw):
                return {"matched": True, "action": "show", "payload": {}, "reason": "show_pattern_matched", "raw": raw}

        for pat in self._REMEMBER_PATTERNS:
            m = pat.match(raw)
            if m:
                content = (m.group(1) or "").strip()
                if len(content) < 2:
                    return {"matched": False, "reason": "remember_content_too_short", "raw": raw}
                return {"matched": True, "action": "remember", "payload": {"content": content}, "reason": "remember_pattern_matched", "raw": raw}

        for pat in self._FORGET_PATTERNS:
            m = pat.match(raw)
            if m:
                query = (m.group(1) or "").strip()
                if not query:
                    return {"matched": False, "reason": "forget_query_empty", "raw": raw}
                return {"matched": True, "action": "forget", "payload": {"query": query}, "reason": "forget_pattern_matched", "raw": raw}

        return {"matched": False, "reason": "no_pattern_match", "raw": raw}

    def apply_user_directive(self, decision: dict[str, Any]) -> str:
        action  = str(decision.get("action") or "")
        reason  = str(decision.get("reason") or "")
        payload = decision.get("payload") or {}

        if action == "show":
            self.store.append_event({"type": "memory_inspect", "parser_reason": reason})
            return self.render_memory_snapshot()

        if action == "remember":
            content = str(payload.get("content") or "").strip()
            item = self.remember_from_user_text(content)
            if not item:
                return "未识别到可保存的记忆内容。"
            self.store.append_event(
                {"type": "memory_directive_applied", "action": "remember", "parser_reason": reason, "memory_id": item.get("id")}
            )
            return f"已记住：{item.get('content')}"

        if action == "forget":
            query = str(payload.get("query") or "").strip()
            if not query:
                return "请告诉我你想忘记什么。"
            count = self.forget_by_query(query)
            self.store.append_event(
                {"type": "memory_directive_applied", "action": "forget", "parser_reason": reason, "query": query, "matched": count}
            )
            return "没有找到可删除的匹配记忆。" if count <= 0 else f"已忘记 {count} 条匹配记忆。"

        return "未识别到可执行的记忆指令。"

    def handle_user_directive_with_meta(self, text: str) -> tuple[str | None, dict[str, Any]]:
        decision = self.parse_user_directive(text)
        if not decision.get("matched"):
            return None, decision
        return self.apply_user_directive(decision), decision

    def handle_user_directive(self, text: str) -> str | None:
        reply, _ = self.handle_user_directive_with_meta(text)
        return reply