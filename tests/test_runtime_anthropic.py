"""Native Messages integration using scripted responses and MockTransport only."""

import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from src.agent.runtime_behaviour import load_behaviour
from src.agent_runtime.adapter import parse_response
from src.agent_runtime.anthropic import HttpAnthropicAdapter
from src.agent_runtime.context import ContextPolicy
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.providers import (GLM_SUBSCRIPTION_ANTHROPIC as ROUTE,
    GLM_ANTHROPIC_BASE_URL, provider_parameters, subscription_credentials)
from src.harness_contracts.usage import reported_total_tokens
from test_agent_runtime import MESSAGES, runtime
from test_runtime_recovery_edges import InjectedCrash


def native_response(*calls, text=None, stop=None, thinking=True, usage=None):
    blocks = [{"type": "thinking", "thinking": "Service-visible thought", "signature": "opaque-signature"}] if thinking else []
    blocks += [{"type": "tool_use", "id": cid, "name": name, "input": args} for cid, name, args in calls]
    if text is not None:
        blocks.append({"type": "text", "text": text})
    return {"id": "msg-fixture", "type": "message", "role": "assistant", "model": "glm-5.3-flash",
        "content": blocks, "stop_reason": stop or ("tool_use" if calls else "end_turn"),
        "usage": usage if usage is not None else {"input_tokens": 10, "cache_read_input_tokens": 20,
                                                  "cache_creation_input_tokens": 30, "output_tokens": 40}}


def native_engine(tmp_path, responses, **kwargs):
    engine = runtime(tmp_path, responses, **kwargs)
    engine.model = "glm-5.3-flash"
    engine.parameters = provider_parameters(ROUTE, output_tokens=32000)
    engine.versions = engine.versions.model_copy(update={"remote_model": engine.versions.remote_model.model_copy(
        update={"route_id": ROUTE, "remote_alias": engine.model})})
    return engine


def test_two_native_turns_tool_images_signatures_caching_accounting_and_reader(tmp_path):
    first = native_response(("v", "view", {}), ("e", "error", {}))
    engine = native_engine(tmp_path, [first, native_response(text="Done")],
        limits=RunLimits(model_calls=3, tool_calls=3, seconds=60, tokens=500000))
    engine.context_policy = ContextPolicy()
    with engine.store as store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed" and receipt["tool_calls"] == 2
        assert receipt["reported_tokens"] == 200
        assert receipt["usage_accounting"]["reported_cache_read_tokens"] == 40
        assert receipt["usage_accounting"]["reported_cache_write_tokens"] == 60
        assert receipt["usage_accounting"]["reported_output_tokens"] == 80
        assert receipt["usage_accounting"]["billing_modes"] == ["subscription"]
        assert receipt["estimated_cost_cny"] is None and receipt["billing_usd"] is None
        bodies = [json.loads(w) for w in engine.adapter.requests]
        for body in bodies:
            assert body["system"][-1]["text"] == MESSAGES[0]["content"]
            assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
            assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
            assert body["thinking"] == {"type": "adaptive", "display": "omitted"}
            assert body["output_config"] == {"effort": "medium"}
            assert body["context_management"]["edits"][0]["keep"] == "all"
            assert "n" not in body and "reasoning_effort" not in body and body["stream"] is False
            assert all("input_schema" in t and "function" not in t for t in body["tools"])
        assistant = next(m for m in bodies[1]["messages"] if m["role"] == "assistant")
        assert assistant["content"] == first["content"]
        blocks = [b for m in bodies[1]["messages"] for b in m["content"]]
        results = [b for b in blocks if b["type"] == "tool_result"]
        assert [b["tool_use_id"] for b in results] == ["v", "e"]
        assert results[1]["is_error"] is True
        images = [b for b in blocks if b["type"] == "image"]
        assert len(images) == 1
        requests = [e.payload for e in store.events if e.payload.event_type == "adapter_request"]
        assert base64.b64decode(images[0]["source"]["data"]) == store.get_bytes(requests[1].images[0].sent)
        for request, wire in zip(requests, engine.adapter.requests, strict=True):
            assert store.capture_bytes(request.final_request_body) == wire
            assert request.wire_sha256 == hashlib.sha256(wire).hexdigest()
        presentations = [e.payload for e in store.events if e.payload.event_type == "tool_presentation"]
        assert len(presentations) == 2
        assert all(p.protocol_conversion == "anthropic_messages_v1" for p in presentations)
        store.validate()
        report = load_behaviour(store.path)
        assert report["summary"]["tool_calls"] == 2
        assert all(step["delivered_to_model"] for step in report["invocations"][0]["steps"])


def test_truncated_native_write_batch_never_executes_and_r2_repairs(tmp_path):
    bad = native_response(("unsafe", "save", {"value": 1}), stop="max_tokens")
    bad["content"].append({"type": "tool_use", "id": "partial", "name": "save", "input": '{"value":'})
    engine = native_engine(tmp_path, [bad, native_response(text="Recovered")])
    with engine.store:
        receipt = asyncio.run(engine.run(MESSAGES))
        assert receipt["status"] == "completed" and receipt["tool_calls"] == 0
        assert receipt["model_calls"] == 2 and receipt["reported_tokens"] == 200
        second = json.loads(engine.adapter.requests[1])
        assert not any(m["role"] == "assistant" for m in second["messages"])
        assert "previous response exceeded" in json.dumps(second)
        truncations = [e for e in engine.store.events if e.payload.event_type == "response_truncation"]
        assert len(truncations) == 1


@pytest.mark.parametrize("status,error,retry,expected", [
    (503, "overloaded_error", True, "completed"),
    (429, "rate_limit_error", True, "completed"),
    (429, "insufficient_quota", False, "quota_exhausted"),
    (401, "authentication_error", False, "permission_denied"),
    (400, "invalid_request_error", False, "invalid_request"),
])
def test_native_http_failures_use_c1_rules_without_credentials(tmp_path, status, error, retry, expected):
    sent = []
    def handle(request):
        sent.append(request)
        assert request.url.path == "/api/anthropic/v1/messages" and request.url.query == b"beta=true"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "context-management" in request.headers["anthropic-beta"]
        if len(sent) == 1:
            return httpx.Response(status, json={"type": "error", "error": {"type": error, "message": "failure private-test-key"},
                "request_id": "req-native", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 3}},
                headers={"request-id": "req-native"})
        return httpx.Response(200, json=native_response(text="OK"))
    engine = native_engine(tmp_path, [], limits=RunLimits(model_calls=3, tool_calls=0,
        seconds=30, tokens=500000, retry_backoff_seconds=0.001))
    engine.adapter = HttpAnthropicAdapter(base_url=GLM_ANTHROPIC_BASE_URL, api_key="private-test-key",
                                          transport=httpx.MockTransport(handle))
    async def run():
        try:
            return await engine.run(MESSAGES)
        finally:
            await engine.adapter.close()
    with engine.store:
        result = asyncio.run(run())
        assert result["status"] == expected and len(sent) == (2 if retry else 1)
        assert result["reported_tokens"] == (103 if retry else 3)
        failure = next(e.payload.model_failure for e in engine.store.events
                       if e.payload.event_type == "run_lifecycle" and e.payload.model_failure)
        assert failure.http_status == status and failure.service_error_type == error
        assert failure.request_id == "req-native" and failure.usage_received
    # Include the lock file after closing its Windows-exclusive handle.
    for f in engine.store.directory.rglob("*"):
        if f.is_file():
            assert b"private-test-key" not in f.read_bytes()


def test_explicit_native_credentials_and_capture_parameters(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "must-not-use")
    path = tmp_path / "private.env"
    path.write_text(f"GLM_API_KEY=file-value\nGLM_ANTHROPIC_BASE_URL={GLM_ANTHROPIC_BASE_URL}\nGLM_BASE_URL=https://wrong.invalid\n")
    assert subscription_credentials(path, provider=ROUTE) == (GLM_ANTHROPIC_BASE_URL, "file-value")
    with pytest.raises(ValueError, match="explicit"):
        subscription_credentials(provider=ROUTE)
    path.write_text("GLM_API_KEY=file-value\nGLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4\n")
    with pytest.raises(ValueError, match="GLM_ANTHROPIC_BASE_URL"):
        subscription_credentials(path, provider=ROUTE)
    parameters = provider_parameters(ROUTE, output_tokens=32000)
    assert parameters["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert parameters["output_config"] == {"effort": "medium"}
    assert parameters["max_tokens"] == 32000
    assert provider_parameters(ROUTE, output_tokens=32000, reasoning_effort="low")["output_config"] == {"effort": "low"}


def test_usage_cache_components_not_double_counted_and_missing_stays_missing():
    assert reported_total_tokens({"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11, "cache_creation": {"ephemeral_5m_input_tokens": 11}}) == 23
    assert reported_total_tokens({"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": -1}) is None
    assert reported_total_tokens({"cache_read_input_tokens": 7}) is None


def test_opaque_thinking_roundtrips_without_becoming_public_text(tmp_path):
    raw = native_response(text="Done", thinking=False)
    raw["content"].insert(0, {"type": "redacted_thinking", "data": "encrypted-data"})
    engine = native_engine(tmp_path, [])
    with engine.store:
        parsed = parse_response(raw, "fixture", engine.store)
        assert parsed.assistant_message["anthropic_content"] == raw["content"]
        assert parsed.event_payload.thinking[0].kind == "unavailable"
        assert parsed.event_payload.visible_text == ("Done",)


@pytest.mark.parametrize("truncated", [False, True])
def test_native_response_and_signature_recovery_from_durable_journal(tmp_path, truncated):
    first = native_response(("v", "view", {}), stop="max_tokens" if truncated else "tool_use")
    limits = RunLimits(model_calls=3, tool_calls=2, seconds=60, tokens=500000)
    engine = native_engine(tmp_path, [first], limits=limits)
    engine.context_policy = ContextPolicy()
    def crash(boundary, _):
        if boundary == "after_response":
            raise InjectedCrash("after_response")
    engine.fault_hook = crash
    with engine.store:
        with pytest.raises(InjectedCrash):
            asyncio.run(engine.run(MESSAGES))
    resumed = native_engine(tmp_path, [native_response(text="Done")], limits=limits, tools=engine.tools)
    resumed.context_policy = ContextPolicy()
    with resumed.store:
        result = asyncio.run(resumed.run(MESSAGES, resume=True))
        assert result["status"] == "completed" and result["model_calls"] == 2
        assert result["reported_tokens"] == 200
        assert len(engine.tools.calls) == (0 if truncated else 1)
        body = json.loads(resumed.adapter.requests[0])
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        if truncated:
            assert assistants == [] and result["truncations"] == 1
        else:
            assert assistants[0]["content"] == first["content"]
        resumed.store.validate()


@pytest.mark.parametrize("damage", ["duplicate", "bad_input", "unknown_block", "wrong_finish"])
def test_native_malformed_batches_never_execute_any_tool(tmp_path, damage):
    raw = native_response(("call", "save", {"value": 1}))
    if damage == "duplicate":
        raw["content"].append(copy.deepcopy(raw["content"][-1]))
    elif damage == "bad_input":
        raw["content"][-1]["input"] = "{}"
    elif damage == "unknown_block":
        raw["content"].append({"type": "unknown_extension"})
    else:
        raw["stop_reason"] = "end_turn"
    engine = native_engine(tmp_path, [raw])
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] != "completed" and result["tool_calls"] == 0
        assert not engine.tools.calls


def test_native_large_result_compaction_records_actual_wire_content(tmp_path):
    from test_agent_runtime import Tools
    class LargeTools(Tools):
        async def call_tool(self, name, arguments):
            return {"content": [{"type": "text", "text": "large-result " * 5000}]}
    engine = native_engine(tmp_path, [native_response(("v", "view", {})), native_response(text="Done")],
        tools=LargeTools(tmp_path / "tools"), limits=RunLimits(model_calls=3, tool_calls=2, seconds=60, tokens=500000))
    engine.context_policy = ContextPolicy()
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed"
        record = load_behaviour(engine.store.path)
        step = record["invocations"][0]["steps"][0]
        assert step["delivered_to_model"]
        visible = step["model_visible_result"]
        wire = visible["wire_tool_result"]["content"]
        assert visible["tool_message"]["content"] == "".join(b["text"] for b in wire)
