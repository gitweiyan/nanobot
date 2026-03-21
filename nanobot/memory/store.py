"""Structured memory persistence and retrieval primitives.

P0 changes in this file:
  1. Per-workspace threading.Lock guards every read-modify-write cycle on
     items.jsonl. Two concurrent consolidations from different sessions can
     no longer interleave and silently drop writes.
  2. mtime-based in-process read cache. The file is re-read only when its
     mtime changes. Write methods call _write_jsonl which atomically replaces
     the file and immediately invalidates the cache.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from nanobot.utils.helpers import ensure_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


class StructuredMemoryStore:
    """JSONL-backed store for structured memory items and events."""

    # Class-level registries keyed by the canonical items_file path.
    # threading.Lock is safe inside asyncio — acquisitions are fast (in-memory
    # ops + atomic file replace) so they never meaningfully block the event loop.
    _file_locks:  ClassVar[dict[Path, threading.Lock]] = {}
    _cache:       ClassVar[dict[Path, list[dict[str, Any]]]] = {}
    _cache_mtime: ClassVar[dict[Path, float]] = {}

    def __init__(self, workspace: Path):
        self.memory_dir   = ensure_dir(workspace / "memory")
        self.items_file   = self.memory_dir / "items.jsonl"
        self.events_file  = self.memory_dir / "events.jsonl"
        self.memory_file  = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

        # One lock per unique items_file path, shared across all instances
        # that point at the same workspace.
        if self.items_file not in self._file_locks:
            self._file_locks[self.items_file] = threading.Lock()
        self._lock = self._file_locks[self.items_file]

    # ------------------------------------------------------------------
    # Markdown helpers (MEMORY.md is a generated human-readable view,
    # not a source of truth — see manager._rebuild_memory_md)
    # ------------------------------------------------------------------

    def read_long_term(self) -> str:
        return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    # ------------------------------------------------------------------
    # JSONL low-level I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
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
        """Atomic replace via tmp file, then invalidate the cache entry."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)
        # Invalidate so the next read_items() picks up the new mtime.
        self._cache.pop(path, None)
        self._cache_mtime.pop(path, None)

    # ------------------------------------------------------------------
    # Cached item reads
    # ------------------------------------------------------------------

    def read_items(self) -> list[dict[str, Any]]:
        """Return all items via an mtime-based in-process cache.

        Shared across instances for the same workspace: a write from one
        MemoryStore is immediately visible to another within the same process.
        Returns a shallow copy so callers cannot mutate the cached list.
        """
        mtime = self.items_file.stat().st_mtime if self.items_file.exists() else 0.0
        cached_mtime = self._cache_mtime.get(self.items_file)
        if cached_mtime == mtime and self.items_file in self._cache:
            return list(self._cache[self.items_file])
        rows = self._parse_jsonl(self.items_file)
        self._cache[self.items_file] = rows
        self._cache_mtime[self.items_file] = mtime
        return list(rows)

    def read_events(self) -> list[dict[str, Any]]:
        return self._parse_jsonl(self.events_file)

    def list_items(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item for item in self.read_items()
            if (not scope  or item.get("scope")  == scope)
            and (not kind   or item.get("kind")   == kind)
            and (not status or item.get("status") == status)
        ]

    def append_event(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault(
            "id",
            "evt_" + hashlib.sha1(
                json.dumps(event, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12],
        )
        event.setdefault("created_at", _now_iso())
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Write operations — all protected by the per-workspace lock
    # ------------------------------------------------------------------

    @staticmethod
    def _build_item_id(scope: str, kind: str, content: str) -> str:
        digest = hashlib.sha1(
            f"{scope}|{kind}|{_normalize_text(content)}".encode("utf-8")
        ).hexdigest()[:16]
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

        with self._lock:
            items = self.read_items()
            found: dict[str, Any] | None = next(
                (i for i in items if i.get("id") == item_id), None
            )
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

    def update_item_status(self, item_ids: list[str], status: str) -> int:
        if not item_ids:
            return 0
        ids = set(item_ids)
        now = _now_iso()

        with self._lock:
            items = self.read_items()
            updated = 0
            for item in items:
                if item.get("id") in ids and item.get("status") != status:
                    item["status"] = status
                    item["updated_at"] = now
                    updated += 1
            if updated:
                self._write_jsonl(self.items_file, items)

        return updated

    def touch_items(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        touched = set(item_ids)
        now = _now_iso()

        with self._lock:
            items = self.read_items()
            changed = any(item.get("id") in touched for item in items)
            if not changed:
                return
            for item in items:
                if item.get("id") in touched:
                    item["last_used_at"] = now
            self._write_jsonl(self.items_file, items)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_candidate_lines(markdown: str, limit: int = 120) -> list[str]:
        """Extract compact fact-like lines from markdown.

        Fallback only — used when the LLM did not provide structured_items.
        Prefer the structured_items path in manager.apply_consolidation().
        """
        out: list[str] = []
        for raw in (markdown or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("#", "---", "*This file is automatically updated")):
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
        """Lightweight relevance score — no external dependencies.

        Components (all additive):
          phrase_score  2.0 if the full normalised query appears verbatim in content
          term_score    0-1 normalised fraction of query terms found in content
          confidence    stored confidence value (0-1)
          recency       small bonus for recently used / recently updated items
        """
        content = str(item.get("content") or "")
        if not content:
            return 0.0

        q = _normalize_text(query)
        c = _normalize_text(content)
        confidence = float(item.get("confidence") or 0.0)

        if not q:
            return confidence + (0.1 if item.get("last_used_at") else 0.0)

        phrase_score = 2.0 if q in c else 0.0

        q_terms = [t for t in q.split() if len(t) > 1]
        term_score = (
            sum(1 for t in q_terms if t in c) / len(q_terms)
            if q_terms else 0.0
        )

        recency = 0.15 if item.get("last_used_at") else (0.05 if item.get("updated_at") else 0.0)

        return phrase_score + term_score + confidence + recency

    def search_items(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        active = [i for i in self.read_items() if i.get("status", "active") == "active"]
        if not active:
            return []
        ranked = sorted(
            active,
            key=lambda i: (self._score_item(i, query), i.get("updated_at", "")),
            reverse=True,
        )
        return ranked[: max(1, limit)]

    def find_items_for_query(
        self,
        query: str,
        *,
        statuses: tuple[str, ...] = ("active",),
        scope: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        allowed = set(statuses)
        candidates = [
            item for item in self.read_items()
            if item.get("status", "active") in allowed
            and (not scope or item.get("scope") == scope)
        ]
        ranked = sorted(
            candidates,
            key=lambda i: (self._score_item(i, q), i.get("updated_at", "")),
            reverse=True,
        )
        return [i for i in ranked if self._score_item(i, q) > 0][: max(1, limit)]