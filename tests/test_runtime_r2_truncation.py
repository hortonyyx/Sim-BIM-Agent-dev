"""Recover the actual migration failure without echoing or executing partial output."""

import asyncio
import json
from pathlib import Path

import pytest

from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore

from test_agent_runtime import MESSAGES, response, runtime
from test_runtime_child_tasks import _child
from test_runtime_recovery_edges import InjectedCrash, _summary_response


ROOT = Path(__file__).resolve().parents[1]


def truncated(*, calls=False):
    raw = response(text="partial", reasoning=True)
    raw["choices"][0]["finish_reason"] = "length"
    if calls:
        raw["choices"][0]["message"]["tool_calls"] = [
            {"id": "whole-but-rejected", "type": "function", "function": {
                "name": "save", "arguments": '{"value": 7}'}},
            {"id": "partial", "type": "function", "function": {
                "name": "save", "arguments": '{"value":'}}]
    return raw


@pytest.mark.parametrize("context", [False, True])
def test_truncated_response_is_settled_and_recovers_without_echo(tmp_path, context):
    raw = truncated()
    raw["usage"] = {"prompt_tokens": 47036, "completion_tokens": 16384, "total_tokens": 63420,
                    "completion_tokens_details": {"reasoning_tokens": 16377}}
    assert raw["usage"]["total_tokens"] == 63_420
    engine = runtime(tmp_path, [raw, response(("fresh", "save", {"value": 8})), response(text="Done")])
    engine.parameters["max_tokens"] = 16_384
    if context:
        engine.context_policy = ContextPolicy()
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed"
        assert receipt["model_calls"] == 3 and receipt["truncations"] == 1
        assert receipt["reported_tokens"] == 63_480
        assert receipt["task_budget"]["settlements"][0]["actual"]["tokens"] == 63_420
        assert engine.tools.calls == [("save", {"value": 8})]
        event = next(e for e in engine.store.events if e.payload.event_type == "response_truncation")
        assert event.payload.thinking_characters == len(raw["choices"][0]["message"]["reasoning_content"])
        assert event.payload.visible_characters == len(raw["choices"][0]["message"]["content"])
        assert event.payload.reported_reasoning_tokens == 16_377
        assert event.payload.has_tool_calls is False
        assert event.payload.action == "continue"
        sent = json.loads(engine.adapter.requests[1])
        assert engine._truncation_prompt() in sent["messages"]
        assert "reasoning_content" not in json.dumps(sent)
        assert raw["choices"][0]["message"]["reasoning_content"] not in json.dumps(sent, ensure_ascii=False)
        captured = next(e.payload for e in engine.store.events if e.payload.event_type == "model_response")
        assert engine.store.resolve(captured.raw_response) == raw
        engine.store.validate()


@pytest.mark.parametrize("max_consecutive,max_total,expected", [(0, 3, 1), (1, 3, 2), (2, 1, 2)])
def test_recovery_limits_stop_with_incomplete_response(tmp_path, max_consecutive, max_total, expected):
    limits = RunLimits(model_calls=5, tool_calls=2, seconds=30, tokens=100_000,
        max_consecutive_truncations=max_consecutive, max_total_truncations=max_total)
    engine = runtime(tmp_path, [truncated()] * 5, limits=limits)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "incomplete_response"
        assert receipt["model_calls"] == expected
        assert receipt["reported_tokens"] == expected * 30
        records = [e.payload for e in engine.store.events if e.payload.event_type == "response_truncation"]
        assert records[-1].action == "stop"
        assert records[-1].total_count == expected


def test_successful_turn_resets_consecutive_but_not_total_bound(tmp_path):
    limits = RunLimits(model_calls=6, tool_calls=3, seconds=30, tokens=100_000,
        max_consecutive_truncations=1, max_total_truncations=2)
    engine = runtime(tmp_path, [truncated(), response(("a", "error", {})),
        truncated(), response(("b", "error", {})), truncated()], limits=limits)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "incomplete_response" and receipt["model_calls"] == 5
        records = [e.payload for e in engine.store.events if e.payload.event_type == "response_truncation"]
        assert [r.consecutive_count for r in records] == [1, 1, 1]
        assert [r.total_count for r in records] == [1, 2, 3]


def test_partial_batch_is_never_exposed_or_executed(tmp_path):
    engine = runtime(tmp_path, [truncated(calls=True), response(text="Done")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed" and engine.tools.calls == []
        events = engine.store.events
        assert next(e.payload for e in events if e.payload.event_type == "model_response").tool_calls == ()
        truncation = next(e.payload for e in events if e.payload.event_type == "response_truncation")
        assert truncation.has_tool_calls and truncation.tool_call_count == 2
        assert b"whole-but-rejected" not in engine.adapter.requests[1]
        assert b'"tool_calls"' not in engine.adapter.requests[1]


def test_recovery_consumes_child_and_root_budgets(tmp_path):
    root_limits = RunLimits(model_calls=5, tool_calls=4, seconds=30, tokens=200_000)
    child_limits = RunLimits(model_calls=2, tool_calls=2, seconds=30, tokens=100_000)
    with EventStore(tmp_path / "root", run_id="root", task_id="parent", budget_limit=root_limits.ledger_limit()) as store:
        child = _child(store, tmp_path, "child", [truncated(), response(text="Done")], limits=child_limits)
        receipt = asyncio.run(child.run(MESSAGES))
        assert receipt["status"] == "completed"
        assert receipt["root_budget_available"]["calls"] == 3
        assert receipt["task_budget_available"]["calls"] == 0
        assert receipt["task_budget_available"]["tokens"] == 99_940
        assert all(e.task_id == "child" for e in store.all_events)
        store.validate()


def test_total_recovery_cap_is_shared_between_sibling_tasks(tmp_path):
    root_limits = RunLimits(model_calls=6, tool_calls=0, seconds=30, tokens=200_000,
        max_total_truncations=1)
    child_limits = RunLimits(model_calls=3, tool_calls=0, seconds=30, tokens=100_000,
        max_total_truncations=1)
    with EventStore(tmp_path / "root", run_id="root", task_id="parent", budget_limit=root_limits.ledger_limit()) as store:
        first = _child(store, tmp_path, "first", [truncated(), response(text="Done")], limits=child_limits)
        assert asyncio.run(first.run(MESSAGES))["status"] == "completed"
        second = _child(store, tmp_path, "second", [truncated(), response(text="forbidden")], limits=child_limits)
        receipt = asyncio.run(second.run(MESSAGES))
        assert receipt["status"] == "incomplete_response" and receipt["model_calls"] == 1
        assert receipt["truncations"] == 1 and receipt["root_truncations"] == 2
        record = next(e.payload for e in second.store.events if e.payload.event_type == "response_truncation")
        assert record.consecutive_count == 1 and record.total_count == 2


@pytest.mark.parametrize("boundary", ["after_response", "after_checkpoint"])
def test_resume_reconstructs_one_prompt_and_never_double_settles(tmp_path, boundary):
    engine = runtime(tmp_path, [truncated(calls=True)])

    def crash(name, current):
        if name == boundary and any(e.payload.event_type == "response_truncation" for e in current.store.events):
            raise InjectedCrash(name)

    engine.fault_hook = crash
    with engine.store, pytest.raises(InjectedCrash):
        asyncio.run(engine.run(MESSAGES))
    resumed = runtime(tmp_path, [response(text="Recovered")], tools=engine.tools)
    with resumed.store:
        receipt = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert receipt["status"] == "completed" and receipt["reported_tokens"] == 60
        assert receipt["truncations"] == 1 and receipt["tool_calls"] == 0
        assert len(receipt["task_budget"]["settlements"]) == 2
        sent = json.loads(resumed.adapter.requests[0])
        assert sent["messages"].count(resumed._truncation_prompt()) == 1
        assert "reasoning_content" not in json.dumps(sent)


def test_recovery_cannot_bypass_call_budget_or_unknown_usage(tmp_path):
    limits = RunLimits(model_calls=1, tool_calls=2, seconds=30, tokens=100_000)
    engine = runtime(tmp_path, [truncated(), response(text="forbidden")], limits=limits)
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "model_budget_exhausted" and receipt["model_calls"] == 1
    other = tmp_path / "unknown"
    other.mkdir()
    raw = truncated()
    del raw["usage"]
    engine = runtime(other, [raw, response(text="forbidden")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "token_usage_unavailable" and receipt["model_calls"] == 1
        assert receipt["truncations"] == 1


@pytest.mark.parametrize("boundary", [None, "after_response", "after_checkpoint"])
def test_domain_truncation_continues_tools_and_preserves_settlement_on_resume(tmp_path, boundary):
    def completed(current):
        if (current.tools.directory / "saved.json").is_file():
            return {"origin": "domain_tool", "artifact": "saved.json"}

    responses = [truncated(calls=True), response(("complete-save", "save", {"value": 8}))]
    engine = runtime(tmp_path, responses)
    engine.domain_completion = completed
    if boundary:
        def crash(name, current):
            if name == boundary and any(e.payload.event_type == "response_truncation" for e in current.store.events):
                raise InjectedCrash(name)
        engine.fault_hook = crash
        with engine.store, pytest.raises(InjectedCrash):
            asyncio.run(engine.run(MESSAGES))
        engine = runtime(tmp_path, responses[1:], tools=engine.tools)
        engine.domain_completion = completed
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES, resume=bool(boundary)))
        assert receipt["status"] == "completed" and receipt["answer"] is None
        assert receipt["model_calls"] == len(receipt["task_budget"]["settlements"]) == 2
        assert receipt["tool_calls"] == receipt["truncations"] == 1
        assert receipt["reported_tokens"] == 60
        assert engine.tools.calls == [("save", {"value": 8})]
        sent = json.loads(engine.adapter.requests[-1])
        assert sent["messages"].count(engine._truncation_prompt()) == 1
        assert engine._truncation_prompt() != engine._truncation_prompt(purpose="context_summary")
        assert "whole-but-rejected" not in json.dumps(sent)
        engine.store.validate()


@pytest.mark.parametrize("crash_boundary", [None, "after_response", "after_checkpoint"])
@pytest.mark.parametrize("domain", [False, True])
def test_summary_truncation_recovers_with_its_own_prompt_and_budget(tmp_path, crash_boundary, domain):
    limits = RunLimits(model_calls=5, tool_calls=2, seconds=30, tokens=100_000, summary_every=1)
    policy = ContextPolicy(preserve_initial_messages=2)

    def repaired_summary(body):
        assert body.get("tools", []) == []
        assert body["messages"][-1] == engine._truncation_prompt(purpose="context_summary")
        assert "reasoning_content" not in json.dumps(body)
        return _summary_response({**body, "messages": body["messages"][:-1]})

    ending = response(("saved", "save", {"value": 8})) if domain else response(text="Done")
    completion = (lambda current: {"origin": "domain_tool"}
        if (current.tools.directory / "saved.json").is_file() else None) if domain else None
    engine = runtime(tmp_path, [response(("once", "view", {})), truncated(),
        repaired_summary, ending], limits=limits)
    engine.context_policy = policy
    engine.domain_completion = completion
    if crash_boundary:
        def crash(name, current):
            if name == crash_boundary and any(e.payload.event_type == "response_truncation"
                                              for e in current.store.events):
                raise InjectedCrash(name)
        engine.fault_hook = crash
        with engine.store, pytest.raises(InjectedCrash):
            asyncio.run(engine.run(MESSAGES))
        engine = runtime(tmp_path, [repaired_summary, ending],
            limits=limits, tools=engine.tools)
        engine.context_policy = policy
        engine.domain_completion = completion
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES, resume=bool(crash_boundary)))
        assert receipt["status"] == "completed", receipt
        assert receipt["model_calls"] == 4 and receipt["truncations"] == 1
        assert receipt["reported_tokens"] == 120
        assert engine.tools.calls == [("view", {})] + ([("save", {"value": 8})] if domain else [])
        assert [e.payload.logical_purpose for e in engine.store.events
                if e.payload.event_type == "adapter_request"] == [
            "primary_task", "context_summary", "context_summary", "primary_task"]
        assert engine._truncation_prompt() not in engine.messages
        engine.store.validate()
