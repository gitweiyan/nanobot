"""Tests for explicit remember/forget/show-memory directives."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.memory import MemoryManager
from nanobot.providers.base import LLMResponse


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (20, "test-counter")
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=200,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_remember_directive_bypasses_llm_and_persists_item(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    text = "记住 我喜欢简洁技术回答"

    response = await loop.process_direct(text, session_key="cli:test")

    assert "已记住" in response
    loop.provider.chat_with_retry.assert_not_awaited()
    items = loop.context.memory.manager.store.read_items()
    assert any("简洁技术回答" in str(i.get("content")) for i in items)


@pytest.mark.asyncio
async def test_show_memory_directive_reads_structured_items(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    await loop.process_direct("记住 我喜欢中文回复", session_key="cli:test")

    response = await loop.process_direct("查看记忆", session_key="cli:test")

    assert "当前可用记忆" in response
    assert "我喜欢中文回复" in response


@pytest.mark.asyncio
async def test_forget_directive_deprecates_matched_items(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    await loop.process_direct("记住 我喜欢简洁回答", session_key="cli:test")

    response = await loop.process_direct("忘记 简洁回答", session_key="cli:test")

    assert "已忘记" in response
    deprecated = loop.context.memory.manager.store.list_items(status="deprecated")
    assert deprecated


def test_preference_update_marks_prior_preference_conflicted(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path)
    first = manager.remember_from_user_text("我喜欢简洁回答")
    second = manager.remember_from_user_text("我喜欢详细解释")

    assert first and second
    conflicted = manager.store.list_items(scope="user", kind="preference", status="conflicted")
    active = manager.store.list_items(scope="user", kind="preference", status="active")
    assert conflicted
    assert len(active) == 1
