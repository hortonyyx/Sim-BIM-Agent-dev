import gzip
import json

import pytest

from src.run_trace.deepseek import read_deepseek
from src.run_trace.wire import enrich_wire


def write_wire(folder, turn, request, response):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{turn:02}-out.json.gz").write_bytes(gzip.compress(json.dumps(request).encode()))
    (folder / f"{turn:02}-reply.json").write_text(json.dumps(response), encoding="utf-8")


def test_wire_merges_full_requests_without_secrets_or_private_reasoning(tmp_path):
    directory = tmp_path / "wire" / "arm"
    schema = {"type": "function", "function": {"name": "measure", "parameters": {"type": ["object", "null"]}}}
    messages = [
        {"role": "system", "content": "Measure the room."},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,SElEREVO"}}]},
        {"role": "assistant", "reasoning_content": "private inner voice", "content": "I will measure."},
        {"role": "tool", "content": '{"authorization":"Bearer SecretFixture", "thinking_text":"secret scratchpad"}\nremaining 15'},
    ]
    request = {"messages": messages, "tools": [schema], "reasoning_effort": "high", "thinking": {"type": "enabled"},
               "max_tokens": 32000, "Authorization": "Basic SecretFixture"}
    usage = {"completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 2}}
    answer = {"id": "r1", "usage": usage, "choices": [{"index": 0, "finish_reason": "tool_calls",
              "message": {"content": "Use the scale.", "reasoning_content": "secret scratchpad",
                          "tool_calls": [{"id": "call-1", "function": {"name": "measure", "arguments": '{"length":2}'}}]}}]}
    for turn in (1, 2):
        write_wire(directory, turn, request, answer)
    existing = [{"id": "host-1", "kind": "model", "turn": 1, "status": "completed", "detail": {"trace": "kept"}}]
    merged = enrich_wire(existing, directory.parent, arm="arm")
    assert len(merged) == 2 and merged[0]["id"] == "host-1"
    assert "wire" not in existing[0]["detail"]
    assert merged[1]["id"] == "wire-arm-2"
    rendered = json.dumps(merged)
    for prohibited in ("secret scratchpad", "private inner voice", "SElEREVO", "SecretFixture"):
        assert prohibited not in rendered
    for item in merged:
        wire = item["detail"]["wire"]
        assert len(wire["request"]["messages"]) == 4
        assert wire["request"]["tools"] == [schema]
        assert wire["request"]["parameters"]["thinking"] == {"type": "enabled"}
        assert wire["response"]["choices"][0]["message"]["tool_calls"][0]["id"] == "call-1"
        assert wire["response"]["usage"] == usage
        assert wire["host_delivery"]["status"] == "unverified"


def test_gateway_response_after_headless_timeout_is_distinct_from_host_receipt(tmp_path):
    control = tmp_path / "control"
    directory = control / "wire" / "dsh"
    session = tmp_path / "dsh" / "session"
    session.mkdir(parents=True)
    completed = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    for turn in (1, 2):
        write_wire(directory, turn, {"model": "fixture"}, {"id": f"r{turn}", "choices": [], "usage": completed})
    rows = [{"arm":"dsh","turn":n,"event":kind,"time":at} for n, kind, at in
            [(1, "request", 1), (1, "response", 2), (2, "request", 3), (2, "response", 15)]]
    (control / "relay.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    host = [{"type":"status","phase":"step_start","turn":1,"step":1},
            {"type":"status","phase":"step_end","turn":1,"step":1,"usage":{"totalTokens":10,"outputTokens":3}},
            {"type":"status","phase":"step_start","turn":1,"step":2},
            {"type":"status","phase":"turn_end","turn":1,"reason":{"error":{"code":"TIMEOUT"}}}]
    (session / "events.jsonl").write_text("\n".join(map(json.dumps, host)), encoding="utf-8")
    (session / "execution.json").write_text('{"ended":10}', encoding="utf-8")
    merged = enrich_wire([{"kind":"model","turn":2,"status":"failed","detail":{"failure":"HOST_TIMEOUT"}}], directory)
    row = next(event for event in merged if event["turn"] == 2)
    assert row["status"] == "timeout" and row["start"] == 3 and row["end"] == 10
    assert "未收到" in row["summary"]
    assert row["detail"]["wire"]["timing"]["end"] == 15
    assert row["detail"]["wire"]["host_delivery"]["status"] == "not_received"
    assert next(event for event in merged if event["turn"] == 1)["detail"]["wire"]["host_delivery"]["status"] == "received"


def test_responses_preserves_actual_tool_diagnostics_and_assembled_stream(tmp_path):
    wire = tmp_path / "control/wire/dsh"
    request = {"model":"fixture", "reasoning":{"effort":"high"}, "input":[
        {"type":"reasoning", "encrypted_content":"PRIVATE"},
        {"type":"function_call_output", "call_id":"old", "output":"value is not lossless JSON"}]}
    response = {"id":"r1", "error":None, "status":"completed", "output":[],
                "usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10}}
    write_wire(wire, 1, request, response)
    item = {"type":"function_call", "call_id":"next", "name":"inspect_parametric_plan", "arguments":"{}"}
    (wire / "01-assembled-reply.json").write_text(json.dumps({**response,"output":[item]}))
    events = enrich_wire([], wire)
    data = events[0]["detail"]["wire"]
    assert events[0]["status"] == "completed"
    assert "inspect_parametric_plan" in events[0]["summary"]
    assert data["request"]["protocol"] == "Responses"
    assert "input" not in data["request"]["parameters"]
    assert data["request"]["input"][1]["output"] == "value is not lossless JSON"
    assert data["response"]["output"] == [item]
    assert data["response"]["usage"] == response["usage"]
    assert "PRIVATE" not in json.dumps(events)


def test_partial_responses_keeps_proposals_without_claiming_completion(tmp_path):
    wire = tmp_path / "wire/dsh"
    wire.mkdir(parents=True)
    (wire / "01-out.json.gz").write_bytes(gzip.compress(b'{"input":[]}'))
    streamed = {"event":{"type":"response.output_item.done", "output_index":0,
                         "item":{"type":"function_call", "name":"build_bim"}},"time":2}
    (wire / "01-events.jsonl.gz").write_bytes(gzip.compress(json.dumps(streamed).encode()))
    event, = enrich_wire([], wire)
    assert event["status"] == "incomplete"
    partial = event["detail"]["wire"]["partial_stream"]
    assert partial["output_items_fully_streamed"][0]["name"] == "build_bim"
    assert partial["usage"] is None and not partial["terminal_response_saved"]


def test_local_finish_confirmation_is_not_counted_as_provider_request(tmp_path):
    wire = tmp_path / "control/wire/dsh"
    write_wire(wire, 1, {"input":[]}, {"id":"r1","status":"completed","output":[]})
    (tmp_path / "control/dsh-relay-finish.json").write_text('{}')
    host = {"kind":"model","turn":2,"detail":{"usage":{"totalTokens":0},
            "visible_text":["Verified whole-building handoff saved."]}}
    events = enrich_wire([host], wire)
    assert events[0]["kind"] == "status" and events[0]["turn"] is None
    assert events[0]["detail"]["local_completion"]


@pytest.mark.parametrize("provider_usage", [None,
    {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}])
def test_failed_dsh_usage_requires_provider_evidence_and_keeps_host_record(tmp_path, provider_usage):
    session = tmp_path / "private/dsh/session"
    session.mkdir(parents=True)
    placeholder = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    headless = [
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": placeholder},
        {"type": "status", "phase": "turn_end", "turn": 1,
         "reason": {"kind": "error", "error": {"code": "TRANSPORT"}}},
    ]
    log = session / "events.jsonl"
    log.write_text("\n".join(map(json.dumps, headless)), encoding="utf-8")
    original_log = log.read_bytes()
    wire = tmp_path / "control/wire/dsh"
    wire.mkdir(parents=True)
    (wire / "01-out.json.gz").write_bytes(gzip.compress(b'{"input":[]}'))
    if provider_usage is None:
        partial = {"event": {"type": "response.created", "response": {"id": "r1"}}, "time": 2}
        (wire / "01-events.jsonl.gz").write_bytes(gzip.compress(json.dumps(partial).encode()))
    else:
        (wire / "01-reply.json").write_text(json.dumps(
            {"id": "r1", "status": "completed", "output": [], "usage": provider_usage}), encoding="utf-8")
    rows = read_deepseek(session)
    merged = enrich_wire(rows, wire, journal=session)
    detail = next(row for row in merged if row["kind"] == "model")["detail"]
    assert detail["usage"] == provider_usage
    assert detail["usage_status"] == ("unknown" if provider_usage is None else "reported")
    assert detail["host_usage"] == {"source": "DeepSeek Harness", "value": placeholder}
    assert next(row for row in rows if row["kind"] == "model")["detail"]["usage"] == placeholder
    assert log.read_bytes() == original_log
