"""Memory manager that unifies persistence, consolidation, and retrieval."""

from __future__ import annotations

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
