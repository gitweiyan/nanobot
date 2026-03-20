"""Structured memory persistence and retrieval primitives."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import ensure_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


class StructuredMemoryStore:
    """JSONL-backed store for structured memory items and events."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.items_file = self.memory_dir / "items.jsonl"
        self.events_file = self.memory_dir / "events.jsonl"
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    rows.append(data)
        return rows

    def _write_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)

    def read_items(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.items_file)

    def read_events(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.events_file)

    def append_event(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("id", "evt_" + hashlib.sha1(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest()[:12])
        event.setdefault("created_at", _now_iso())
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def _build_item_id(scope: str, kind: str, content: str) -> str:
        digest = hashlib.sha1(f"{scope}|{kind}|{_normalize_text(content)}".encode("utf-8")).hexdigest()[:16]
        return f"mem_{digest}"

    def upsert_item(
        self,
        *,
        scope: str,
        kind: str,
        content: str,
        confidence: float = 0.6,
        status: str = "active",
        source_ref: str | None = None,
    ) -> dict[str, Any] | None:
        text = (content or "").strip()
        if not text:
            return None
        item_id = self._build_item_id(scope, kind, text)
        now = _now_iso()

        items = self.read_items()
        found: dict[str, Any] | None = None
        for item in items:
            if item.get("id") == item_id:
                found = item
                break

        if found is None:
            found = {
                "id": item_id,
                "scope": scope,
                "kind": kind,
                "content": text,
                "confidence": confidence,
                "status": status,
                "source_refs": [source_ref] if source_ref else [],
                "created_at": now,
                "updated_at": now,
                "last_used_at": None,
            }
            items.append(found)
        else:
            found["status"] = status
            found["confidence"] = max(float(found.get("confidence") or 0), confidence)
            found["updated_at"] = now
            refs = found.setdefault("source_refs", [])
            if source_ref and source_ref not in refs:
                refs.append(source_ref)

        self._write_jsonl(self.items_file, items)
        return found

    def touch_items(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        touched = set(item_ids)
        now = _now_iso()
        items = self.read_items()
        changed = False
        for item in items:
            if item.get("id") in touched:
                item["last_used_at"] = now
                changed = True
        if changed:
            self._write_jsonl(self.items_file, items)

    @staticmethod
    def extract_candidate_lines(markdown: str, limit: int = 120) -> list[str]:
        """Extract compact fact-like lines from markdown memory text."""
        out: list[str] = []
        for raw in (markdown or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#") or line.startswith("---"):
                continue
            if line.startswith("*This file is automatically updated"):
                continue
            if line.startswith("- "):
                line = line[2:].strip()
            line = line.strip("`").strip()
            if len(line) < 5:
                continue
            out.append(line)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _score_item(item: dict[str, Any], query: str) -> float:
        content = str(item.get("content") or "")
        if not content:
            return 0.0
        q = _normalize_text(query)
        c = _normalize_text(content)
        if not q:
            return float(item.get("confidence") or 0.0)
        q_terms = [t for t in q.split(" ") if t]
        if not q_terms:
            return float(item.get("confidence") or 0.0)
        overlap = sum(1 for t in q_terms if t in c)
        recency_boost = 0.1 if item.get("last_used_at") else 0.0
        return overlap + float(item.get("confidence") or 0.0) + recency_boost

    def search_items(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        items = [i for i in self.read_items() if i.get("status", "active") == "active"]
        if not items:
            return []
        ranked = sorted(
            items,
            key=lambda i: (self._score_item(i, query), i.get("updated_at", "")),
            reverse=True,
        )
        return ranked[: max(1, limit)]
