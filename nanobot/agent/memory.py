"""Memory system for persistent agent memory.

P1 change in this file:
  Existing active items are summarised and injected into the consolidation
  prompt so the LLM knows what already exists and emits only genuinely new
  or updated structured_items — eliminating silent duplicate writes.
"""

from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.memory import MemoryManager
from nanobot.utils.helpers import estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": (
                            "A paragraph summarising key events/decisions/topics. "
                            "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search."
                        ),
                    },
                    "memory_update": {
                        "type": "string",
                        "description": (
                            "Full updated long-term memory as markdown. Include all existing "
                            "facts plus new ones. Return unchanged if nothing new."
                        ),
                    },
                    "structured_items": {
                        "type": "array",
                        "description": (
                            "Discrete facts, preferences, and decisions extracted from this conversation. "
                            "Only include items that are NEW or that UPDATE existing ones listed above. "
                            "Do not repeat items already present in the existing items list. "
                            "Omit this field entirely if nothing new was learned."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["fact", "preference", "decision"],
                                    "description": (
                                        "fact = general knowledge about the user or project; "
                                        "preference = user style, habit, or working preference; "
                                        "decision = an explicit choice or constraint the user committed to."
                                    ),
                                },
                                "scope": {
                                    "type": "string",
                                    "enum": ["user", "project"],
                                    "description": "user = about the person; project = about the work.",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "One concise sentence, max ~120 chars.",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": "0.0–1.0. Use 0.95 for explicit user statements, 0.7 for inferred facts.",
                                },
                            },
                            "required": ["kind", "scope", "content", "confidence"],
                        },
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice", "toolchoice", "does not support", 'should be ["none", "auto"]',
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Two-layer memory: items.jsonl (source of truth) + HISTORY.md (log)."""

    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        self.manager      = MemoryManager(workspace)
        self.memory_dir   = self.manager.store.memory_dir
        self.memory_file  = self.manager.store.memory_file
        self.history_file = self.manager.store.history_file
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        return self.manager.read_long_term()

    def write_long_term(self, content: str) -> None:
        self.manager.write_long_term(content)

    def append_history(self, entry: str) -> None:
        self.manager.append_history(entry)

    def get_memory_context(self, query: str | None = None) -> str:
        return self.manager.get_relevant_context(query)

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = (
                f" [tools: {', '.join(message['tools_used'])}]"
                if message.get("tools_used") else ""
            )
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] "
                f"{message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate messages into items.jsonl + HISTORY.md.

        P1: the consolidation prompt now includes a summary of existing active
        items so the LLM can avoid emitting duplicate structured_items.
        """
        if not messages:
            return True

        current_memory = self.read_long_term()

        # P1-1: inject existing items so the LLM knows what's already stored.
        existing_summary = self.manager.existing_items_summary(limit=40)

        prompt = (
            f"## Existing Structured Items (do not duplicate these)\n"
            f"{existing_summary}\n\n"
            f"## Current Long-term Memory\n"
            f"{current_memory or '(empty)'}\n\n"
            f"## Conversation to Process\n"
            f"{self._format_messages(messages)}"
        )

        system_prompt = (
            "You are a memory consolidation agent. Call the save_memory tool with your "
            "consolidation of the conversation.\n\n"
            "For structured_items: extract only items that are GENUINELY NEW or that "
            "UPDATE/CORRECT an existing item listed above. Do not repeat items already "
            "present in the existing list. Classify kind carefully:\n"
            "  preference — user style, habits, or working preferences\n"
            "  decision   — explicit commitments or constraints the user stated\n"
            "  fact       — everything else\n"
            "Omit structured_items entirely if nothing new was learned."
        )

        chat_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(response.content):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            for field in ("history_entry", "memory_update"):
                if field not in args or args[field] is None:
                    logger.warning("Memory consolidation: missing required field '%s'", field)
                    return self._fail_or_raw_archive(messages)

            entry  = _ensure_text(args["history_entry"]).strip()
            update = _ensure_text(args["memory_update"])

            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalisation")
                return self._fail_or_raw_archive(messages)

            raw_items = args.get("structured_items")
            structured_items: list[dict[str, Any]] | None = None
            if isinstance(raw_items, list) and raw_items:
                structured_items = [i for i in raw_items if isinstance(i, dict)]
                logger.debug(
                    "Memory consolidation: {} structured_items from LLM", len(structured_items)
                )

            # apply_consolidation handles items.jsonl write + MEMORY.md rebuild (P0-1).
            self.manager.apply_consolidation(
                history_entry=entry,
                memory_update=update,
                source_messages=messages,
                structured_items=structured_items,
            )

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True

        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary = (
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        self.manager.record_raw_archive(messages, summary)
        logger.warning("Memory consolidation degraded: raw-archived {} messages", len(messages))


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self, session: Session, tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None
        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)
        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        history = session.get_history(max_messages=0)
        channel, chat_id = (
            session.key.split(":", 1) if ":" in session.key else (None, None)
        )
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider, self.model, probe_messages, self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        if not messages:
            return True
        for _ in range(self.store._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            target = self.context_window_tokens // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key, estimated, self.context_window_tokens, source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(
                    session, max(1, estimated - target)
                )
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key, round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num, session.key, estimated, self.context_window_tokens,
                    source, len(chunk),
                )
                if not await self.consolidate_messages(chunk):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return