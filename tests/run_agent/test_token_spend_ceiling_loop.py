"""Integration proof for the per-run token spend ceiling gate.

Unit tests in ``tests/agent/test_token_spend_ceiling.py`` cover the helper
predicates. This drives the real ``AIAgent.run_conversation`` tool loop with
a mocked provider and proves the gate actually STOPS the loop: once the
run's fresh-token spend (non-cached input + output) crosses
``agent.token_spend_ceiling``, the crossing request completes (its tool
writes land) and no further provider call is made — mirroring the
background-review input-budget gate it was modeled on.

Harness copied from ``tests/run_agent/test_background_review_input_budget.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(prompt_tokens: int, completion_tokens: int = 2000) -> SimpleNamespace:
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call()],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def _final_response() -> SimpleNamespace:
    message = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _tool_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _make_loop_agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[_tool_definition()]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=256_000),
        patch("agent.context_compressor.get_model_context_length", return_value=256_000),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=10,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1

    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 999_999_999
    compressor.context_length = 1_000_000_000
    compressor.last_prompt_tokens = -1
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.return_value = False
    compressor.should_compress_info.return_value = (False, None)
    compressor.should_compress_preflight.return_value = False
    compressor.should_defer_preflight_to_real_usage.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    compressor.select_context.return_value = None
    compressor.get_automatic_compaction_status_message.return_value = ""
    agent.compression_enabled = False
    agent.context_compressor = compressor

    def _fake_execute_tool_calls(assistant_message, messages, *_args):
        tool_call = assistant_message.tool_calls[0]
        messages.append(
            {
                "role": "tool",
                "name": tool_call.function.name,
                "tool_call_id": tool_call.id,
                "content": "ok",
            }
        )

    agent._execute_tool_calls = _fake_execute_tool_calls
    return agent


def _run_with_responses(agent: AIAgent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("do some tool work")


def test_ceiling_stops_tool_loop_before_next_provider_call():
    """Fresh-token spend crossing the ceiling halts the loop before the
    next provider call; the crossing request still completes."""
    agent = _make_loop_agent()
    agent.token_spend_ceiling = 100_000

    responses = [
        _tool_response(60_000, 2_000),   # fresh ~62k
        _tool_response(60_000, 2_000),   # cumulative fresh ~124k -> ceiling crossed
        _tool_response(60_000, 2_000),   # must never be consumed
        _final_response(),
    ]
    result = _run_with_responses(agent, responses)

    create = agent.client.chat.completions.create
    fresh = agent.session_input_tokens + agent.session_output_tokens
    assert create.call_count == 2, (
        f"expected loop to stop after crossing the ceiling, but "
        f"{create.call_count} provider calls were made "
        f"(ceiling {agent.token_spend_ceiling}, fresh {fresh})"
    )
    assert fresh > agent.token_spend_ceiling
    assert result["completed"] is False


def test_no_ceiling_leaves_tool_loop_unbounded():
    """Without a ceiling (every normal agent) all scripted responses run."""
    agent = _make_loop_agent()
    assert getattr(agent, "token_spend_ceiling", None) is None

    responses = [
        _tool_response(60_000, 2_000),
        _tool_response(60_000, 2_000),
        _tool_response(60_000, 2_000),
        _final_response(),
    ]
    result = _run_with_responses(agent, responses)

    assert agent.client.chat.completions.create.call_count == 4
    assert result["completed"] is True
    assert result["final_response"] == "done"


def test_env_override_arms_gate_without_config(monkeypatch):
    """HERMES_TOKEN_SPEND_CEILING arms the gate even when config left it off."""
    agent = _make_loop_agent()
    assert getattr(agent, "token_spend_ceiling", None) is None
    monkeypatch.setenv("HERMES_TOKEN_SPEND_CEILING", "100000")

    responses = [
        _tool_response(60_000, 2_000),
        _tool_response(60_000, 2_000),   # crosses 100k
        _tool_response(60_000, 2_000),   # never consumed
        _final_response(),
    ]
    result = _run_with_responses(agent, responses)

    assert agent.client.chat.completions.create.call_count == 2
    assert result["completed"] is False
