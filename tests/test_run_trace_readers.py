"""Recorded evidence must distinguish returned errors, proposals and observed execution."""
import hashlib
import json

from src.run_trace.readers import read_events


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")


def native_event(n, event_type, **fields):
    return {"event_id": str(n), "task_id": "coordinator", "occurred_at": {"kind": "known", "value": 100 + n},
            "payload": {"event_type": event_type, **fields}}


def test_native_merges_call_lifecycle_and_retains_verified_input_without_private_reasoning(tmp_path):
    body = {"messages": [{"role": "assistant", "content": "public", "reasoning_content": "PRIVATE_THOUGHT"}],
            "thinking": {"type": "enabled"}, "authorization": "Bearer test_private_credential"}
    data = json.dumps(body).encode()
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / digest).write_bytes(data)
    cap = {"kind": "blob", "blob": {"kind": "sha256", "sha256": digest, "uri": "blobs/" + digest,
                                   "media_type": "application/json"}}
    result = {"kind": "inline", "value": {"isError": False, "content": [
        {"type": "text", "text": '{"status":"rejected","error":"Missing host","source_geometry_ready":false}'}]}}
    refs = [{"call_id": "a", "tool_name": "build_bim", "full_arguments": {"plan": 1}},
            {"call_id": "b", "tool_name": "inputs", "full_arguments": {}}]
    write_rows(tmp_path / "events.jsonl", [
        native_event(1, "adapter_request", final_request_body=cap),
        native_event(2, "model_response", request_event_id="1", tool_calls=refs, visible_text=["public"]),
        native_event(3, "tool_invocation", **refs[0]),
        native_event(4, "tool_execution", outcome="succeeded", raw_result=result, invocation_event_id="3", **refs[0]),
        native_event(5, "tool_presentation", tool_execution_event_id="4", request_event_id="6"),
        native_event(6, "adapter_request", final_request_body={"kind": "inline", "value": {}}),
        native_event(7, "run_lifecycle", action="failure", model_failure={"request_event_id": "6", "category": "timeout"}),
    ])
    rows = read_events(tmp_path, "native")
    tools = [r for r in rows if r["kind"] == "tool"]
    models = [r for r in rows if r["kind"] == "model"]
    assert len(rows) == len({r["id"] for r in rows})
    assert [(r["turn"], r["status"]) for r in tools] == [(1, "failed"), (1, "unconfirmed")]
    assert (tools[0]["start"], tools[0]["end"]) == (103, 104)
    assert tools[0]["detail"]["execution_outcome"] == "succeeded"
    assert tools[0]["summary"] == "Missing host"
    assert tools[0]["detail"]["presentations"][0]["request_event_id"] == "6"
    assert tools[1]["start"] is None and tools[1]["end"] is None
    assert models[0]["detail"]["final_request_body"]["messages"][0]["content"] == "public"
    assert models[0]["detail"]["final_request_body"]["thinking"] == {"type": "enabled"}
    assert "PRIVATE_THOUGHT" not in str(rows) and "test_private_credential" not in str(rows)
    assert models[1]["status"] == "failed" and models[1]["end"] == 107
    (tmp_path / "blobs" / digest).write_bytes(b"{}")
    bad = read_events(tmp_path, "native")[0]["detail"]["final_request_body"]
    assert "unavailable" in bad


def test_deepseek_full_journal_supplies_timing_and_distinguishes_empty_step_end(tmp_path):
    headless = [
        {"type": "session", "sessionId": "session-example"},
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "thinking", "text": "PRIVATE_THOUGHT"},
        {"type": "tool_call", "callId": "a", "tool": "mcp__sim_bim__inputs", "input": {}},
        {"type": "tool_result", "callId": "a", "status": "completed", "result": "cut", "truncated": True},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {"totalTokens": 21}},
        {"type": "status", "phase": "step_start", "turn": 1, "step": 2},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 2,
         "usage": {"totalTokens": 0, "outputTokens": 0, "inputTokens": 0}},
        {"type": "status", "phase": "turn_end", "turn": 1,
         "reason": {"kind": "error", "error": {"code": "TIMEOUT"}}},
    ]
    write_rows(tmp_path / "events.jsonl", headless)
    path = tmp_path / "dsh-home/sessions/sample/session-example"
    path.mkdir(parents=True)
    def record(t, when, **data):
        return {"type": t, "time": when, "data": {"turn": 1, **data}}
    write_rows(path / "session.v4.jsonl", [
        record("user/message", 500, content=[{"type": "text", "text": "initial task"}]),
        record("step/start", 1000, step=1),
        record("request/header", 1010, header={"config": {"maxTokens": 17}, "tools": ["example"]}),
        record("request/context", 1011, provider="test-provider", model="test-model"),
        record("assistant/message", 3000, step=1, message={"content": [
            {"type": "reasoning", "text": "PRIVATE_THOUGHT"}, {"type": "text", "text": "public"}]}),
        record("tool/call", 3100, step=1, callId="a", arguments={}),
        record("tool/result", 3200, step=1, message={"toolCallId": "a", "isError": False,
               "content": [{"type": "text", "text": "full result"}]}),
        record("step/start", 4000, step=2), record("assistant/attempt", 9000, step=2),
        record("turn/end", 9100),
    ])
    rows = read_events(tmp_path, "deepseek")
    assert "PRIVATE_THOUGHT" not in str(rows)
    model = [r for r in rows if r["kind"] == "model"]
    assert [(r["start"], r["end"], r["status"]) for r in model] == [(1, 3, "completed"), (4, 9, "failed")]
    assert model[-1]["detail"]["failure"]["error"]["code"] == "TIMEOUT"
    assert model[0]["detail"]["host_request_header"] == model[1]["detail"]["host_request_header"]
    assert model[1]["detail"]["host_request_header"]["config"] == {"maxTokens": 17}
    assert model[1]["detail"]["host_request_context"]["model"] == "test-model"
    tool = next(r for r in rows if r["kind"] == "tool")
    assert tool["detail"]["result"][0]["text"] == "full result" and not tool["detail"]["truncated"]
    assert tool["turn"] == 1 and (tool["start"], tool["end"]) == (3.1, 3.2)
    (path / "session.v4.jsonl").unlink()
    fallback = read_events(tmp_path, "deepseek")
    assert any(r["id"] == "headless-only" for r in fallback)
    assert all(r["start"] is None and r["end"] is None for r in fallback)
    assert next(r for r in fallback if r["id"] == "model-2")["status"] == "failed"
