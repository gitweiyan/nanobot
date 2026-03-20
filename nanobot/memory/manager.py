"""Memory manager that unifies persistence, consolidation, and retrieval."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nanobot.memory.store import StructuredMemoryStore


class MemoryManager:
    """Single entrypoint for memory read/write operations."""

    def __init__(self, workspace: Path):
        self.store = StructuredMemoryStore(workspace)

    def read_long_term(self) -> str:
        return self.store.read_long_term()

    def write_long_term(self, content: str) -> None:
        self.store.write_long_term(content)

    def append_history(self, entry: str) -> None:
        self.store.append_history(entry)

    def ingest_memory_markdown(
        self,
        memory_markdown: str,
        *,
        source_ref: str | None = None,
        scope: str = "project",
    ) -> int:
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
    ) -> None:
        """Persist consolidation output into both markdown and structured stores."""
        self.append_history(history_entry)
        current = self.read_long_term()
        if memory_update != current:
            self.write_long_term(memory_update)

        source_ref = None
        if source_messages:
            first = source_messages[0]
            ts = str(first.get("timestamp") or "")[:16]
            source_ref = ts or "consolidation"
        source_ref = source_ref or "consolidation"

        self.store.append_event(
            {
                "type": "consolidation",
                "summary": history_entry,
                "source_ref": source_ref,
                "message_count": len(source_messages or []),
            }
        )
        self.ingest_memory_markdown(memory_update, source_ref=source_ref)

    def record_raw_archive(self, messages: list[dict[str, Any]], summary: str) -> None:
        """Persist raw-archive fallback for auditability."""
        self.append_history(summary)
        self.store.append_event(
            {
                "type": "raw_archive",
                "summary": summary.splitlines()[0] if summary else "",
                "message_count": len(messages),
            }
        )

    def get_relevant_context(self, query: str | None = None, limit: int = 12) -> str:
        """Build compact memory context for prompt injection."""
        query = (query or "").strip()
        items = self.store.search_items(query, limit=limit)
        if not items:
            long_term = self.read_long_term()
            return f"## Long-term Memory\n{long_term}" if long_term else ""

        item_ids = [str(i.get("id")) for i in items if i.get("id")]
        self.store.touch_items(item_ids)

        lines = []
        for item in items:
            scope = item.get("scope", "project")
            kind = item.get("kind", "fact")
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"- ({scope}/{kind}) {content}")

        if not lines:
            long_term = self.read_long_term()
            return f"## Long-term Memory\n{long_term}" if long_term else ""
        return "## Relevant Memory\n" + "\n".join(lines)

    @staticmethod
    def _infer_kind(text: str) -> str:
        low = text.lower()
        if any(k in low for k in ("prefer", "preference", "i like", "i usually")):
            return "preference"
        if any(k in text for k in ("偏好", "喜欢", "习惯", "通常")):
            return "preference"
        if any(k in low for k in ("decision", "decided", "constraint", "must")):
            return "decision"
        return "fact"

    def remember_from_user_text(self, text: str, *, scope: str = "user") -> dict[str, Any] | None:
        content = (text or "").strip()
        if not content:
            return None
        kind = self._infer_kind(content)
        item = self.store.upsert_item(
            scope=scope,
            kind=kind,
            content=content,
            confidence=0.95,
            source_ref="user_directive",
        )
        if not item:
            return None

        # For explicit user preference updates, mark prior active preferences as conflicted.
        if kind == "preference":
            others = [
                it for it in self.store.list_items(scope=scope, kind="preference", status="active")
                if it.get("id") != item.get("id")
            ]
            if others:
                self.store.update_item_status([str(it.get("id")) for it in others if it.get("id")], "conflicted")
        self.store.append_event(
            {
                "type": "remember",
                "scope": scope,
                "kind": kind,
                "content": content,
                "memory_id": item.get("id"),
            }
        )
        return item

    def forget_by_query(self, query: str, *, scope: str = "user") -> int:
        matches = self.store.find_items_for_query(query, statuses=("active", "conflicted"), scope=scope, limit=50)
        ids = [str(i.get("id")) for i in matches if i.get("id")]
        count = self.store.update_item_status(ids, "deprecated")
        self.store.append_event(
            {
                "type": "forget",
                "scope": scope,
                "query": query,
                "matched": count,
            }
        )
        return count

    def render_memory_snapshot(self, *, limit: int = 20) -> str:
        active = self.store.list_items(status="active")
        if not active:
            return "我目前没有可用的结构化记忆。"
        ranked = sorted(active, key=lambda i: i.get("updated_at", ""), reverse=True)[: max(1, limit)]
        lines = ["当前可用记忆："]
        for item in ranked:
            scope = item.get("scope", "project")
            kind = item.get("kind", "fact")
            lines.append(f"- ({scope}/{kind}) {item.get('content', '')}")
        return "\n".join(lines)

    _REMEMBER_PATTERNS = (
        re.compile(r"^\s*remember(?:\s+that)?\s+(.+)$", re.IGNORECASE),
        re.compile(r"^\s*(?:请)?(?:帮我)?记住[:：]?\s*(.+)$"),
    )
    _FORGET_PATTERNS = (
        re.compile(r"^\s*(?:forget|remove memory|delete memory)\s+(.+)$", re.IGNORECASE),
        re.compile(r"^\s*(?:请)?(?:帮我)?忘记[:：]?\s*(.+)$"),
    )
    _SHOW_PATTERNS = (
        re.compile(r"^\s*(?:show|list)\s+(?:memory|memories)\s*$", re.IGNORECASE),
        re.compile(r"^\s*what do you remember\s*$", re.IGNORECASE),
        re.compile(r"^\s*(?:查看|显示|列出)?(?:我的)?记忆\s*$"),
        re.compile(r"^\s*你记得什么\s*\??\s*$"),
    )

    def handle_user_directive(self, text: str) -> str | None:
        raw = (text or "").strip()
        if not raw:
            return None
        for pat in self._SHOW_PATTERNS:
            if pat.match(raw):
                return self.render_memory_snapshot()

        for pat in self._REMEMBER_PATTERNS:
            m = pat.match(raw)
            if not m:
                continue
            content = (m.group(1) or "").strip()
            item = self.remember_from_user_text(content)
            if not item:
                return "未识别到可保存的记忆内容。"
            return f"已记住：{item.get('content')}"

        for pat in self._FORGET_PATTERNS:
            m = pat.match(raw)
            if not m:
                continue
            query = (m.group(1) or "").strip()
            if not query:
                return "请告诉我你想忘记什么。"
            count = self.forget_by_query(query)
            if count <= 0:
                return "没有找到可删除的匹配记忆。"
            return f"已忘记 {count} 条匹配记忆。"
        return None
