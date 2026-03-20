---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
---

# Memory

## Structure

- `memory/items.jsonl` — Structured long-term memory items (source of truth for recall).
- `memory/events.jsonl` — Structured event history for consolidation/audit.
- `memory/MEMORY.md` — Human-readable projection of long-term memory.
- `memory/HISTORY.md` — Append-only legacy history log for grep/search compatibility.

## Search Past Events

Choose the search method based on file size:

- Small history files: use `read_file`, then search in-memory
- Large or long-lived history files: use the `exec` tool for targeted search

Examples:
- **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
- **Windows:** `findstr /i "keyword" memory\HISTORY.md`
- **Structured events:** search `memory/events.jsonl` with targeted commands

Prefer targeted command-line search for large history files.

## When to Update Memory

Prefer the unified memory pipeline (automatic consolidation + explicit user requests like "remember this").
Do **not** directly overwrite `memory/MEMORY.md` unless the user explicitly asks to edit that file.

Explicit directives supported by the memory manager:
- `记住 ...` / `remember ...` — store as high-confidence user memory
- `忘记 ...` / `forget ...` — deprecate matched memory items
- `查看记忆` / `show memory` — inspect current active memories

## Auto-consolidation

Old conversations are automatically consolidated when the session grows large.
Structured memory is stored in `items.jsonl`/`events.jsonl`, with `MEMORY.md` kept as a readable projection.
