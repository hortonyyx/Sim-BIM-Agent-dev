"""Offline full runtime, native Responses history, controls and billing isolation."""

import asyncio
import base64
import copy
import io
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from src.agent.runtime_entry import parser, runtime_model_profile
from src.agent.runtime_roles.config import load_roles
from src.agent_runtime.connections import resolve_connection, validate_connection_model
from src.agent_runtime.context import ContextPolicy, StateEntry
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.providers import CHATGPT_SUBSCRIPTION as ROUTE, provider_parameters
from test_agent_runtime import MESSAGES, runtime
from test_runtime_recovery_edges import InjectedCrash


def native(*calls, text=None, incomplete=False, usage=True):
    output = [{"type": "reasoning", "id": "rs_fixture", "summary": [],
               "encrypted_content": "opaque-retained-state"}]
    output += [{"type": "function_call", "id": "fc_" + cid, "call_id": cid,
                "namespace": "runtime", "name": name, "arguments": json.dumps(args),
                "status": "completed"} for cid, name, args in calls]
    if text is not None:
        output.append({"type": "message", "id": "msg_fixture", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    raw = {"object": "response", "id": "resp_fixture", "model": "gpt-6-astra",
           "status": "incomplete" if incomplete else "completed", "output": output}
    if incomplete:
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
    if usage:
        raw["usage"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
            "input_tokens_details": {"cached_tokens": 12}, "output_tokens_details": {"reasoning_tokens": 6}}
    return raw


def engine_for(tmp_path, responses, **kwargs):
    engine = runtime(tmp_path, responses, **kwargs)
    engine.model = "gpt-6-astra"
    engine.parameters = provider_parameters(ROUTE, output_tokens=32000, reasoning_effort="low")
    engine.versions = engine.versions.model_copy(update={"remote_model":
        engine.versions.remote_model.model_copy(update={"route_id": ROUTE, "remote_alias": engine.model})})
    engine.context_policy = ContextPolicy()
    return engine


def test_responses_round_trip_runtime_tools_images_and_exact_journal(tmp_path):
    engine = engine_for(tmp_path, [native(("v", "view", {}), ("s", "save", {"value": 8})), native(text="Done")])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["tool_calls"] == 2
        assert result["reported_tokens"] == 60
        assert result["usage_accounting"]["reported_cache_read_tokens"] == 24
        assert result["usage_accounting"]["billing_modes"] == ["subscription"]
        assert result["usage_accounting"]["budget_charge_tokens"] == 60
        assert result["estimated_cost_cny"] is None and result["billing_usd"] is None
        assert result["subscription_output_policy"]["service_output_cap"] is None
        bodies = [json.loads(w) for w in engine.adapter.requests]
        assert all(b["stream"] is True and b["store"] is False for b in bodies)
        assert all(not ({"messages", "max_tokens", "max_output_tokens", "temperature", "previous_response_id"} & b.keys()) for b in bodies)
        assert any(i.get("encrypted_content") == "opaque-retained-state" for i in bodies[1]["input"])
        assert {i["call_id"] for i in bodies[1]["input"] if i.get("type") == "function_call_output"} == {"v", "s"}
        requests = [e.payload for e in engine.store.events if e.payload.event_type == "adapter_request"]
        assert len(requests[1].images) == 1
        for event, wire in zip(requests, engine.adapter.requests, strict=True):
            assert engine.store.capture_bytes(event.final_request_body) == wire
        shown = [e.payload for e in engine.store.events if e.payload.event_type == "tool_presentation"]
        assert len(shown) == 2 and all(p.protocol_conversion == "responses_v1" for p in shown)
        engine.store.validate()


@pytest.mark.parametrize("truncated", [False, True])
def test_native_checkpoint_restart_preserves_items_and_does_not_repeat_write(tmp_path, truncated):
    first = native(("save_once", "save", {"value": 1}), incomplete=truncated)
    limits = RunLimits(model_calls=3, tool_calls=3, seconds=90, tokens=500000)
    engine = engine_for(tmp_path, [first], limits=limits)
    def crash(boundary, _):
        if boundary == "after_response":
            raise InjectedCrash(boundary)
    engine.fault_hook = crash
    with engine.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(engine.run(MESSAGES))
    resumed = engine_for(tmp_path, [native(text="Recovered")], limits=limits, tools=engine.tools)
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "completed" and result["model_calls"] == 2
        assert len(engine.tools.calls) == (0 if truncated else 1)
        body = json.loads(resumed.adapter.requests[0])
        assert (first["output"][0] in body["input"]) is not truncated
        assert result["truncations"] == int(truncated)
        resumed.store.validate()


@pytest.mark.parametrize("state_change", ["unchanged", "changed", "cleared"])
@pytest.mark.parametrize("recovery", [None, "durable_response", "checkpoint"])
def test_sent_state_is_a_stable_prefix_with_current_semantics_after_recovery(tmp_path, state_change, recovery):
    """State belongs before its response, including a response replayed after a crash."""
    limits = RunLimits(model_calls=3, tool_calls=2, seconds=90, tokens=500000)
    replies = [native(("write_once", "save", {"value": 1})),
               native(("read_once", "view", {})), native(text="Done")]

    def configured(responses, tools=None):
        engine = engine_for(tmp_path, responses, limits=limits, tools=tools)
        engine.context_policy = ContextPolicy(append_state_history=True)

        def update(active, event, raw):
            previous = next((s for s in active.context.state if s.key == "probe-state"), None)
            if previous is None:
                active.context.set_state(StateEntry(key="probe-state", category="constraint", value=1,
                    epistemic_status="computed", source_refs=(active._event_source(event),), revision=1))
            elif state_change != "unchanged":
                active.context.set_state(previous.model_copy(update={"revision": 2,
                    "value": 2 if state_change == "changed" else 1,
                    "active": state_change != "cleared"}))
        engine.context_update = update
        return engine

    engine = configured(replies)
    if recovery:
        def crash(boundary, active):
            responses = sum(e.payload.event_type == "model_response" for e in active.store.events)
            if ((recovery == "durable_response" and boundary == "after_response" and responses == 2)
                    or (recovery == "checkpoint" and boundary == "after_checkpoint"
                        and len(active.tools.calls) == 2 and not active.pending_response_id)):
                raise InjectedCrash("state snapshot recovery")
        engine.fault_hook = crash
    with engine.store:
        if recovery:
            with pytest.raises(InjectedCrash):
                asyncio.run(engine.run(MESSAGES))
        else:
            result = asyncio.run(engine.run(MESSAGES))
    if recovery:
        engine = configured([replies[-1]], tools=engine.tools)
        with engine.store:
            result = asyncio.run(engine.run(MESSAGES, resume=True))

    assert result["status"] == "completed"
    assert result["model_calls"] == 3 and result["tool_calls"] == len(engine.tools.calls) == 2
    # Re-open solely for verified evidence and projection checks, no live calls.
    from src.agent_runtime.store import EventStore
    with EventStore(tmp_path / "run", run_id="run-test", task_id="task", budget_limit=limits.ledger_limit()) as store:
        requests = [e for e in store.events if e.payload.event_type == "adapter_request"]
        bodies = [store.resolve(e.payload.final_request_body) for e in requests]
        for previous, current in zip(bodies, bodies[1:]):
            assert current["input"][:len(previous["input"])] == previous["input"]

        def states(body):
            texts = [block.get("text", "") for item in body["input"] if item.get("role") == "user"
                     for block in item.get("content", []) if block.get("type") == "input_text"]
            return [json.loads(text[text.index("["):]) for text in texts
                    if text.startswith("Current runtime state (machine generated;")]

        assert states(bodies[0]) == []
        assert len(states(bodies[1])) == 1 and states(bodies[1])[0][0]["value"] == 1
        final_states = states(bodies[2])
        assert len(final_states) == (1 if state_change == "unchanged" else 2)
        assert final_states[-1] == ([] if state_change == "cleared" else [{
            "key": "probe-state", "value": 2 if state_change == "changed" else 1,
            "epistemic_status": "computed", "revision": 2 if state_change == "changed" else 1}])
        # A checkpoint reload and repeated request replay cannot add snapshots.
        from src.agent_runtime.context import ContextManager
        context = ContextManager.load(store, engine.context.dump(), policy=engine.context_policy)
        before = context.full_history()
        for request in requests:
            context.commit_request_state(request)
        assert context.full_history() == before
        first = context.project(consume_retrievals=False)
        second = context.project(consume_retrievals=False)
        assert first.messages == second.messages and context.full_history() == before
        assert first.messages == before[0]  # unchanged latest state needs no new tail
        store.validate()


def test_missing_usage_stops_without_executing_write_or_fallback(tmp_path):
    engine = engine_for(tmp_path, [native(("x", "save", {"value": 1}), usage=False)])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "token_usage_unavailable"
        assert result["model_calls"] == 1 and result["tool_calls"] == 0
        assert result["fallback"] is False and not result["usage_complete"]


def test_image_response_without_usage_keeps_hold_and_does_not_execute_write(tmp_path):
    engine = engine_for(tmp_path, [native(("view_once", "view", {})),
        native(("must_not_execute", "save", {"value": 1}), usage=False)])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "token_usage_unavailable"
        assert result["model_calls"] == 2 and result["tool_calls"] == 1
        assert not (tmp_path / "tools/saved.json").exists()
        missing = [e.payload.settlement for e in engine.store.events
            if e.payload.event_type == "budget" and e.payload.action == "settle"
            and e.payload.settlement.usage.kind == "missing"]
        assert len(missing) == 1 and missing[0].actual.tokens is None
        assert missing[0].image_tokens_estimate > 0
        assert missing[0].reported_usage_includes_image_tokens is False
        assert missing[0].additional_image_tokens == 0
        assert result["usage_complete"] is False and result["fallback"] is False
        engine.store.validate()


@pytest.mark.parametrize("observed_usage", [None,
    {"input_tokens": 500},
    {"input_tokens": 500, "output_tokens": 50, "total_tokens": 550},
])
@pytest.mark.parametrize("token_limit", [None, 500000])
def test_image_transport_retry_preserves_unknown_usage_and_executes_only_complete_tools(tmp_path, observed_usage, token_limit):
    from src.agent_runtime.responses import HttpResponsesAdapter
    from src.agent_runtime.budget import RuntimeBudget

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            partial = native(("never_execute", "save", {"value": -1}), usage=False)
            partial["status"] = "in_progress"
            if observed_usage is not None:
                partial["usage"] = observed_usage
            yield ("data: " + json.dumps({"type": "response.in_progress", "response": partial}) + "\n\n").encode()
            raise httpx.ReadError("offline interrupted stream")

    async def token():
        return "offline-token"

    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 2:
            return httpx.Response(200, stream=InterruptedStream())
        raw = (native(("view_once", "view", {})) if len(seen) == 1 else
               native(("save_once", "save", {"value": 8})) if len(seen) == 3 else
               native(text="Recovered"))
        return httpx.Response(200, text="data: " + json.dumps({"type": "response.completed", "response": raw}) + "\n\n")

    limits = RunLimits(model_calls=5, tool_calls=2, seconds=90, tokens=token_limit,
        max_model_retries=1, retry_backoff_seconds=0.001)
    engine = engine_for(tmp_path, [], limits=limits)
    engine.adapter = HttpResponsesAdapter(token_provider=token, transport=httpx.MockTransport(handler))
    async def scenario():
        try:
            return await engine.run(MESSAGES)
        finally:
            await engine.adapter.close()

    with engine.store:
        result = asyncio.run(scenario())
        assert result["status"] == "completed"
        assert result["model_calls"] == len(seen) == 4
        assert result["tool_calls"] == len(engine.tools.calls) == 2
        assert (tmp_path / "tools/saved.json").read_text() == '{"value": 8}'
        assert result["reported_tokens"] == 90 and result["usage_complete"] is False
        assert result["fallback"] is False
        failures = [e for e in engine.store.events if e.payload.event_type == "run_lifecycle"
                    and e.payload.model_failure]
        assert len(failures) == 1 and failures[0].payload.reason == "transport_error"
        assert failures[0].payload.model_failure.usage_received is False
        retries = [e for e in engine.store.events if e.payload.event_type == "run_lifecycle"
                   and e.payload.action == "retry"]
        assert len(retries) == 1 and retries[0].payload.attempt == 2
        settlements = [e.payload.settlement for e in engine.store.events
                       if e.payload.event_type == "budget" and e.payload.action == "settle"]
        unknown = [s for s in settlements if s.usage.kind == "missing"]
        assert len(settlements) == 4 and len(unknown) == 1
        assert unknown[0].actual.tokens is None and unknown[0].actual.calls == 1
        assert unknown[0].image_tokens_estimate > 0
        assert unknown[0].reported_usage_includes_image_tokens is False
        assert unknown[0].additional_image_tokens == 0
        held = next(r.amounts.tokens for r in engine.budget.ledger.reservations
                    if r.reservation_id == unknown[0].reservation_id)
        assert engine.budget.ledger.committed.tokens == held + 90
        restored = RuntimeBudget.from_events(limits.ledger_limit(), engine.store.events)
        assert restored.ledger == engine.budget.ledger and restored.fatal_reason is None
        evidence = [s for e in failures for s in e.source_refs if s.source_id == "observed-stream-usage"]
        assert len(evidence) == int(observed_usage is not None)
        if evidence:
            saved = json.loads(engine.store.get_bytes(evidence[0].blob))
            assert saved["completeness"] == "unknown"
            assert saved["usage"]["raw_usage"] == observed_usage
        engine.store.validate()


@pytest.mark.parametrize("token_limit,expected_status,expected_calls", [
    (None, "model_retries_exhausted:transport_error", 3),
    (50000, "token_budget_exhausted", 2),
])
def test_image_transport_unknown_hold_protects_limits_and_retries_are_bounded(
    tmp_path, token_limit, expected_status, expected_calls,
):
    limits = RunLimits(model_calls=6, tool_calls=2, seconds=90, tokens=token_limit,
        max_model_retries=1, retry_backoff_seconds=0.001)
    engine = engine_for(tmp_path, [native(("view_once", "view", {})),
        ConnectionError("offline failure 1"), ConnectionError("offline failure 2"),
        native(text="must not be sent")], limits=limits)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == expected_status
        assert len(engine.adapter.requests) == result["model_calls"] == expected_calls
        assert result["tool_calls"] == 1 and result["usage_complete"] is False
        ledger = engine.budget.ledger
        unknown = [s for s in ledger.settlements if s.usage.kind == "missing"]
        assert len(unknown) == expected_calls - 1
        assert all(s.actual.tokens is None and not s.reported_usage_includes_image_tokens for s in unknown)
        held = sum(r.amounts.tokens for r in ledger.reservations
                   if r.reservation_id in {s.reservation_id for s in unknown})
        assert ledger.committed.tokens == held + 30
        assert not ledger.releases and not ledger.reconciliations
        if token_limit is not None:
            assert ledger.committed.tokens <= token_limit
            assert engine.budget.available.tokens < engine.parameters["max_tokens"]
        engine.store.validate()


@pytest.mark.parametrize("raw_usage,total", [
    (None, None), ({"input_tokens": 100}, None),
    ({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, 120),
])
def test_image_http_failure_preserves_missing_partial_and_complete_usage(tmp_path, raw_usage, total):
    from src.agent_runtime.responses import HttpResponsesAdapter
    seen = []
    async def token():
        return "offline-token"
    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 2:
            body = {"error": {"type": "server_error"}}
            if raw_usage is not None:
                body["usage"] = raw_usage
            return httpx.Response(503, json=body)
        raw = native(("view_once", "view", {})) if len(seen) == 1 else native(text="Recovered")
        return httpx.Response(200, text="data: " + json.dumps({"type": "response.completed", "response": raw}) + "\n\n")
    limits = RunLimits(model_calls=4, tool_calls=1, seconds=90, tokens=500000,
        max_model_retries=1, retry_backoff_seconds=0.001)
    engine = engine_for(tmp_path, [], limits=limits)
    engine.adapter = HttpResponsesAdapter(token_provider=token, transport=httpx.MockTransport(handler))
    async def scenario():
        try:
            return await engine.run(MESSAGES)
        finally:
            await engine.adapter.close()
    with engine.store:
        result = asyncio.run(scenario())
        assert result["status"] == "completed" and len(seen) == 3
        settlements = [e for e in engine.store.events
                       if e.payload.event_type == "budget" and e.payload.action == "settle"]
        failed_settlement = settlements[1].payload.settlement
        assert failed_settlement.actual.tokens == total
        assert failed_settlement.image_tokens_estimate > 0
        assert failed_settlement.additional_image_tokens == 0
        assert failed_settlement.reported_usage_includes_image_tokens is (total is not None)
        if raw_usage is None:
            assert failed_settlement.usage.kind == "missing"
        else:
            assert failed_settlement.usage.raw_usage == raw_usage
        accounting_source = next(s for s in settlements[1].source_refs
                                 if s.source_id == "request-usage-accounting")
        accounting = json.loads(engine.store.get_bytes(accounting_source.blob))
        assert accounting["reported_usage_includes_image_tokens"] == failed_settlement.reported_usage_includes_image_tokens
        assert accounting["budget_charge_tokens"] == total
        assert accounting["estimated_cost_cny"] is None
        engine.store.validate()


def test_image_transport_resume_settles_unknown_once_without_repeating_tools(tmp_path):
    limits = RunLimits(model_calls=4, tool_calls=2, seconds=90, tokens=None,
        max_model_retries=1, retry_backoff_seconds=0.001)
    engine = engine_for(tmp_path, [native(("view_once", "view", {})),
        ConnectionError("offline failure")], limits=limits)
    record_failure = engine._record_model_failure
    def crash_after_failure(*args, **kwargs):
        record_failure(*args, **kwargs)
        raise InjectedCrash("after durable transport failure, before settlement")
    engine._record_model_failure = crash_after_failure
    with engine.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(engine.run(MESSAGES))
        requests = [e for e in engine.store.events if e.payload.event_type == "adapter_request"]
        assert len(requests[-1].payload.images) == 1
        interrupted_reservation = requests[-1].payload.reservation_id
        before = engine.store.path.read_bytes()
    resumed = engine_for(tmp_path, [native(text="Recovered")], limits=limits, tools=engine.tools)
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "completed" and result["model_calls"] == 3
        assert result["tool_calls"] == len(engine.tools.calls) == 1
        assert len(resumed.adapter.requests) == 1 and result["usage_complete"] is False
        unknown = [s for s in resumed.budget.ledger.settlements
                   if s.reservation_id == interrupted_reservation]
        assert len(unknown) == 1 and unknown[0].usage.kind == "missing"
        assert unknown[0].actual.tokens is None
        assert unknown[0].reported_usage_includes_image_tokens is False
        assert unknown[0].additional_image_tokens == 0
        assert resumed.store.path.read_bytes().startswith(before)
        resumed.store.validate()


def metered_native(*calls, input_tokens=1000, text=None):
    raw = native(*calls, text=text)
    raw["usage"].update(input_tokens=input_tokens, total_tokens=input_tokens + 10)
    return raw


@pytest.mark.parametrize("reject_again", [False, True])
def test_context_rejection_compacts_once_retains_unknown_usage_and_never_repeats_tools(tmp_path, reject_again):
    from src.agent_runtime.responses import HttpResponsesAdapter
    limits = RunLimits(model_calls=5, tool_calls=2, seconds=90, tokens=500000)
    engine = engine_for(tmp_path, [], limits=limits)
    seen = []

    async def token():
        return "offline-context-token"

    def handler(request):
        seen.append(json.loads(request.content))
        number = len(seen)
        if number in ({3, 4} if reject_again else {3}):
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        raw = (metered_native(("saved_once", "save", {"value": 8}), input_tokens=1000)
               if number == 1 else metered_native(("last_view", "view", {}), input_tokens=5000)
               if number == 2 else metered_native(text="Recovered", input_tokens=2000))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps({"type": "response.completed", "response": raw}) + "\n\n")

    engine.adapter = HttpResponsesAdapter(token_provider=token, transport=httpx.MockTransport(handler))

    async def run():
        try:
            return await engine.run(MESSAGES)
        finally:
            await engine.adapter.close()

    with engine.store:
        result = asyncio.run(run())
        assert result["status"] == ("context_recovery_required" if reject_again else "completed")
        assert len(seen) == 4 and result["model_calls"] == 4
        assert result["tool_calls"] == 2 and len(engine.tools.calls) == 2
        assert result["usage_complete"] is False and result["fallback"] is False
        assert len([e for e in engine.store.events if e.payload.event_type == "context"
                    and e.payload.action == "compact"]) == 1
        kept_calls = {item["call_id"] for item in seen[3]["input"] if item.get("type") == "function_call"}
        kept_results = {item["call_id"] for item in seen[3]["input"] if item.get("type") == "function_call_output"}
        assert kept_calls == kept_results == {"last_view"}
        assert any(item.get("type") == "reasoning" for item in seen[3]["input"])
        assert any(block.get("type") == "input_image" for item in seen[3]["input"]
                   for block in item.get("content", []))
        settlements = [e.payload.settlement for e in engine.store.events
                       if e.payload.event_type == "budget" and e.payload.action == "settle"]
        missing = [s for s in settlements if s.usage.kind == "missing"]
        assert len(missing) == (2 if reject_again else 1)
        assert all(s.actual.tokens is None for s in missing)
        assert all(not s.reported_usage_includes_image_tokens and s.additional_image_tokens == 0 for s in missing)
        held = sum(r.amounts.tokens for r in engine.task_budget.ledger.reservations
                   if r.reservation_id in {s.reservation_id for s in missing})
        known = sum(s.actual.tokens or 0 for s in settlements)
        assert engine.task_budget.ledger.committed.tokens >= held + known
        assert (tmp_path / "tools/saved.json").read_text() == '{"value": 8}'
        engine.store.validate()


def test_oversized_required_context_stops_recoverably_without_dispatch(tmp_path):
    limits = RunLimits(model_calls=2, tool_calls=0, seconds=90, tokens=500000, context_tokens=32001)
    engine = engine_for(tmp_path, [], limits=limits)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "context_recovery_required" and result["model_calls"] == 0
        evidence = [s for e in engine.store.events for s in e.source_refs
                    if s.source_id == "context-recovery"]
        assert len(evidence) == 1
        saved = json.loads(engine.store.get_bytes(evidence[0].blob))
        assert saved["recoverable"] is True and saved["estimate"]["output_token_limit"] == 32000
        assert len(engine.context.full_history()[0]) == 2
        engine.store.validate()


def test_auto_pressure_compacts_complete_old_groups_before_admission(tmp_path):
    limits = RunLimits(model_calls=3, tool_calls=2, seconds=90, tokens=None, context_tokens=100000)
    engine = engine_for(tmp_path, [metered_native(("old_save", "save", {"value": 2}), input_tokens=1000),
        metered_native(("recent_view", "view", {}), input_tokens=50000),
        metered_native(text="Done", input_tokens=2000)], limits=limits)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["model_calls"] == 3
        third = json.loads(engine.adapter.requests[-1])
        calls = {i["call_id"] for i in third["input"] if i.get("type") == "function_call"}
        replies = {i["call_id"] for i in third["input"] if i.get("type") == "function_call_output"}
        assert calls == replies == {"recent_view"}
        assert len(engine.tools.calls) == 2
        assert any(e.payload.event_type == "context" and e.payload.action == "compact"
                   for e in engine.store.events)
        engine.store.validate()


def test_auto_has_no_200k_cutoff_and_uses_usage_anchor(tmp_path):
    limits = RunLimits(model_calls=2, tool_calls=1, seconds=90, tokens=None)
    engine = engine_for(tmp_path, [metered_native(("see", "view", {}), input_tokens=220000),
                                 metered_native(text="Done", input_tokens=225000)], limits=limits)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["model_calls"] == 2
        decisions = [json.loads(engine.store.get_bytes(s.blob)) for e in engine.store.events
                     for s in e.source_refs if s.source_id == "request-budget-decision"]
        second = decisions[1]["token_estimate"]
        assert second["input_tokens_estimate"] >= 220000
        assert "observed_input_anchor=" in second["source"]
        assert not any(e.payload.event_type == "context" and e.payload.action == "compact" for e in engine.store.events)
        engine.store.validate()


def test_multiround_image_usage_lower_than_proxy_has_valid_budget_subset(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (1024, 1024), (20, 80, 120)).save(image, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    messages = [*MESSAGES, {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url, "detail": "high"}} for _ in range(4)]}]
    engine = engine_for(tmp_path, [metered_native(("view1", "view", {}), input_tokens=1000),
        metered_native(("view2", "view", {}), input_tokens=1100),
        metered_native(text="Done", input_tokens=1200)],
        limits=RunLimits(model_calls=3, tool_calls=2, seconds=90, tokens=None))
    with engine.store:
        result = asyncio.run(engine.run(messages))
        assert result["status"] == "completed" and result["model_calls"] == 3
        decisions = [json.loads(engine.store.get_bytes(s.blob)) for e in engine.store.events
                     for s in e.source_refs if s.source_id == "request-budget-decision"]
        for decision in decisions[1:]:
            token = decision["token_estimate"]
            image_budget = decision["image_budget_estimate"]
            assert token["input_tokens_upper_bound"] < token["image_tokens"]
            assert image_budget["raw_image_proxy_tokens"] == token["image_tokens"]
            assert image_budget["included_image_subset_tokens"] == token["input_tokens_upper_bound"]
            assert "not provider-reported" in image_budget["basis"]
            assert sum(item["tokens"] for item in token["images"]) == token["image_tokens"]
        assert result["reported_tokens"] == 3330 and result["usage_complete"] is True
        engine.store.validate()


def test_local_output_allowance_does_not_claim_server_enforcement(tmp_path):
    engine = engine_for(tmp_path, [native(text="Done")])
    engine.limits = engine.limits.model_copy(update={"near_limit": "reduce_output"})
    with engine.store, pytest.raises(ValueError, match="local only"):
        asyncio.run(engine.run(MESSAGES))
    assert not engine.adapter.requests


def test_subscription_rejects_unsupported_opaque_reasoning_trim(tmp_path):
    engine = engine_for(tmp_path, [])
    engine.reasoning_history = "current_tool_chain"
    with engine.store, pytest.raises(ValueError, match="reasoning_history=all"):
        asyncio.run(engine.run(MESSAGES))
    assert not engine.adapter.requests


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_subscription_rejects_unreviewed_reasoning_effort(effort):
    with pytest.raises(ValueError, match="reviewed ChatGPT models"):
        provider_parameters(ROUTE, output_tokens=32000, reasoning_effort=effort)


def test_subscription_refuses_currency_budget_even_in_direct_runtime(tmp_path):
    limits = RunLimits(model_calls=1, tool_calls=0, seconds=30, tokens=100000, money_usd=Decimal("1"))
    engine = engine_for(tmp_path, [], limits=limits)
    with engine.store, pytest.raises(ValueError, match="metered API"):
        asyncio.run(engine.run(MESSAGES))
    assert not engine.adapter.requests


def test_subscription_connection_ignores_api_keys_and_validates_catalog(monkeypatch, tmp_path):
    from src.agent_runtime import openai_subscription
    monkeypatch.setenv("OPENAI_API_KEY", "paid-key-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.invalid")
    created = []
    class Credentials:
        def __init__(self, directory):
            created.append(directory)
        async def get_access_token(self):
            return "subscription-only-token"
        async def list_models(self):
            return [SimpleNamespace(slug="gpt-6-astra")]
    monkeypatch.setattr(openai_subscription, "SubscriptionCredentials", Credentials)
    connection = resolve_connection(ROUTE, tmp_path / "must-not-read.env", chatgpt_auth_dir=tmp_path / "auth")
    assert created == [tmp_path / "auth"] and connection.api_key == ""
    assert connection.descriptor.billing_mode == "subscription"
    assert connection.descriptor.base_url == "https://api.openai.com/v1"
    asyncio.run(validate_connection_model(connection, "gpt-6-astra"))
    with pytest.raises(ValueError, match="catalog"):
        asyncio.run(validate_connection_model(connection, "not-granted"))


def test_single_and_role_entrypoints_admit_subscription_without_codex():
    args = parser().parse_args(["--out", "unused", "--provider", ROUTE, "--model", "gpt-6-astra"])
    assert args.credentials_file is None
    assert runtime_model_profile(ROUTE, args.model).context_window_tokens == 1050000
    config = {"provider": ROUTE, "model": "gpt-6-astra", "output_tokens": 32000, "reasoning_effort": "low"}
    roles = load_roles({name: copy.deepcopy(config) for name in ("coordinator", "plan_reader", "elevation_reader")})
    assert all(role.provider == ROUTE for role in roles.values())


@pytest.mark.parametrize("mode", ["single_model", "role_division"])
def test_subscription_configuration_needs_no_paid_credentials(tmp_path, mode):
    from test_role_configuration import case, load
    from src.agent.runtime_configuration import argv_for
    value = case(mode)
    value.update(provider=ROUTE, model="gpt-6-astra", reasoning_effort="low")
    value.pop("credentials_file")
    value["chatgpt_auth_dir"] = str(tmp_path / "auth")
    if mode == "role_division":
        value["roles"] = {name: {"provider": ROUTE, "model": "gpt-6-astra",
            "reasoning_effort": "low", "output_tokens": 32000}
            for name in ("coordinator", "plan_reader", "elevation_reader")}
    command = argv_for(load(tmp_path, value)["cases"][0])
    assert "--credentials-file" not in command and "--chatgpt-auth-dir" in command


def test_external_coordinator_rejects_subscription_before_credentials(tmp_path):
    from test_role_configuration import case, load
    value = case("external_coordinator_mcp")
    value.update(provider=ROUTE, model="gpt-6-astra", reasoning_effort="low")
    value.pop("credentials_file")
    with pytest.raises(ValueError, match="external_coordinator_mcp is not supported"):
        load(tmp_path, value)


def test_sse_transport_runtime_executes_only_completed_tool_batches(tmp_path):
    from src.agent_runtime.responses import HttpResponsesAdapter
    responses = [native(("v", "view", {})), native(text="HTTP stream done")]
    requests = []
    async def token():
        return "fake-private-oauth"
    def handler(request):
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer fake-private-oauth"
        requests.append(json.loads(request.content))
        body = {"type": "response.completed", "response": responses[len(requests) - 1]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="event: response.completed\ndata: " + json.dumps(body) + "\n\n")
    engine = engine_for(tmp_path, [])
    engine.adapter = HttpResponsesAdapter(token_provider=token, transport=httpx.MockTransport(handler))
    async def scenario():
        try:
            return await engine.run(MESSAGES)
        finally:
            await engine.adapter.close()
    with engine.store:
        result = asyncio.run(scenario())
        assert result["status"] == "completed" and result["tool_calls"] == 1
        assert len(requests) == 2
        engine.store.validate()
    for path in engine.store.directory.rglob("*"):
        if path.is_file():
            assert b"fake-private-oauth" not in path.read_bytes()


@pytest.mark.parametrize("terminal", ["timeout", "failed"])
def test_stream_partial_usage_retained_without_claiming_final_accounting(tmp_path, terminal):
    from src.agent_runtime.responses import HttpResponsesAdapter
    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            partial = native(("partial", "save", {"value": 5}))
            partial["status"] = "in_progress"
            yield ("data: " + json.dumps({"type": "response.in_progress", "response": partial}) + "\n\n").encode()
            if terminal == "timeout":
                await asyncio.Event().wait()
            else:
                yield b'data: {"type":"response.failed","response":{"object":"response","status":"failed","error":{"type":"server_error"}}}\n\n'
    async def token():
        return "fake-oauth"
    engine = engine_for(tmp_path, [])
    engine.request_timeout_seconds = 0.1
    # This checks partial-stream evidence, not retry policy. A 100 ms request
    # can cross the reservation tolerance depending on scheduler overhead.
    engine.limits = engine.limits.model_copy(update={"max_model_retries": 0})
    engine.adapter = HttpResponsesAdapter(token_provider=token, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=InterruptedStream())))
    async def scenario():
        try:
            return await engine.run(MESSAGES)
        finally:
            await engine.adapter.close()
    with engine.store:
        result = asyncio.run(scenario())
        assert result["status"] in ({"model_retries_exhausted:timeout", "time_budget_exhausted"}
                                    if terminal == "timeout" else {"service_error"})
        assert result["tool_calls"] == 0 and result["model_calls"] == 1
        failure = next(e.payload.model_failure for e in engine.store.events
                       if e.payload.event_type == "run_lifecycle" and e.payload.model_failure)
        assert failure.category == ("timeout" if terminal == "timeout" else "service_error")
        assert result["usage_complete"] is False
        evidence = [source for event in engine.store.events for source in event.source_refs
                    if source.source_id == "observed-stream-usage"]
        assert len(evidence) == 1
        observed = json.loads(engine.store.get_bytes(evidence[0].blob))
        assert observed["completeness"] == "unknown"
        assert observed["usage"]["raw_usage"]["total_tokens"] == 30
        engine.store.validate()
