"""Integration tests for structured memory manager flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.memory import MemoryManager
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _tool_response(history_entry: str, memory_update: str) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="call_1",
                name="save_memory",
                arguments={
                    "history_entry": history_entry,
                    "memory_update": memory_update,
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_consolidation_persists_structured_memory_files(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(
        return_value=_tool_response(
            history_entry="[2026-03-19 11:30] User prefers dark mode and uses FastAPI.",
            memory_update=(
                "# Long-term Memory\n\n"
                "## Preferences\n"
                "- User prefers dark mode.\n\n"
                "## Project Context\n"
                "- API stack is FastAPI + PostgreSQL.\n"
            ),
        )
    )

    result = await store.consolidate(
        [{"role": "user", "content": "remember my stack", "timestamp": "2026-03-19T11:30:00"}],
        provider,
        "test-model",
    )

    assert result is True
    assert (tmp_path / "memory" / "items.jsonl").exists()
    assert (tmp_path / "memory" / "events.jsonl").exists()
    assert "dark mode" in (tmp_path / "memory" / "items.jsonl").read_text(encoding="utf-8").lower()


def test_memory_manager_retrieves_relevant_items(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path)
    manager.store.upsert_item(
        scope="user",
        kind="preference",
        content="User prefers concise technical answers.",
        confidence=0.9,
    )
    manager.store.upsert_item(
        scope="project",
        kind="fact",
        content="Project uses FastAPI and PostgreSQL.",
        confidence=0.8,
    )

    context = manager.get_relevant_context("FastAPI database")
    assert "Relevant Memory" in context
    assert "FastAPI and PostgreSQL" in context


def test_context_builder_injects_query_relevant_memory(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path)
    manager.store.upsert_item(
        scope="user",
        kind="preference",
        content="User prefers Chinese responses.",
        confidence=0.9,
    )

    builder = ContextBuilder(tmp_path)
    messages = builder.build_messages(
        history=[],
        current_message="请用中文回复",
        channel="cli",
        chat_id="direct",
    )

    system_prompt = messages[0]["content"]
    assert "Relevant Memory" in system_prompt
    assert "User prefers Chinese responses." in system_prompt
