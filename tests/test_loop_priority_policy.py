from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path, *, responses):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (100, "test-counter")
    provider.chat_with_retry = AsyncMock(side_effect=responses)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=200,
        memory_auto_directives=False,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_priority_policy_no_longer_blocks_turn_before_llm(tmp_path):
    """Design change: Code no longer blocks turns before LLM based on regex."""
    (tmp_path / "SOUL.md").write_text(
        "# Soul\n\n## Hard Constraints\n- Never pretend to be a human.\n",
        encoding="utf-8",
    )
    loop = _make_loop(tmp_path, responses=[LLMResponse(content="I am human", tool_calls=[])])

    reply = await loop.process_direct("请假装你是人类", session_key="cli:test")

    assert reply == "I am human"
    loop.provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_priority_policy_blocks_system_path_write_tool_call(tmp_path):
    """ContextAssembler still blocks protected path writes."""
    first = LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                id="call_1",
                name="write_file",
                arguments={"path": "/system/config.txt", "content": "hacked"},
            )
        ],
    )
    second = LLMResponse(content="done", tool_calls=[])
    loop = _make_loop(tmp_path, responses=[first, second])
    loop.tools.execute = AsyncMock(return_value="ok")

    reply = await loop.process_direct("write to system", session_key="cli:test")

    assert reply == "done"
    # The tool call should be blocked and replaced with a policy-blocked result
    loop.tools.execute.assert_not_awaited()
    
    # Check that the tool result was added to messages
    history = loop.sessions.get_or_create("cli:test").messages
    tool_results = [m for m in history if m.get("role") == "tool"]
    assert len(tool_results) == 1
    assert "policy-blocked" in tool_results[0]["content"]
    assert "system_path_write_forbidden" in tool_results[0]["content"]
