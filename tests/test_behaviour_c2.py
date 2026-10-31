"""The same behaviour measures for both transports, including domain failures."""
import asyncio
import copy
import gzip
import json
from pathlib import Path

import pytest

from src.agent.runtime_behaviour import load_behaviour, tool_result_data, write_behaviour_report, summarise, render_timeline


def test_metadata_requires_complete_json_in_native_transport_wrappers():
    data = {"name": "North.png", "display_scale_actual": [2, 2], "original_size": [900, 600]}
    text = json.dumps(data)
    forms = [text, {"content": [{"type": "text", "text": text}]},
             {"structuredContent": data, "content": [{"type": "text", "text": text}]}]
    assert all(tool_result_data(value) == data for value in forms)
    for incomplete in ('{"candidate":"candidate_04","source_geometry_ready":true,"evidence":[',
                       text + '\nadditional text'):
        assert tool_result_data(incomplete) == {}


def test_saved_claims_domain_outcomes_and_unanswered_calls_are_not_conflated(tmp_path):
    run = tmp_path / "run"
    (run / "claims").mkdir(parents=True)
    claim = {"candidate": "candidate_01", "objects": [{"kind": "window", "id": "W1"}],
             "values": {"z": [1, 2]}, "reason": "own chain"}
    saved = {"id": "claim_0001", "claim": claim, "sources": [{"image": "West.png"}],
             "resolved_values": {"z": [1, 2]}}
    (run / "claims/claim_0001.json").write_text(json.dumps(saved))
    steps = []
    def add(tool, data, *, error=False, args=None):
        steps.append(dict(index=len(steps)+1, t_call=len(steps), tool=tool, arguments=args or {},
                          result_text=json.dumps(data)+'\n剩余 1 分钟。', result_data=data, is_error=error,
                          outcome="failed" if error else "succeeded",
                          model_text_before="", thinking_tokens_before=0))
    add("view_image", {"name": "West.png", "display_scale_actual": [2, 2]}, args={"name": "West.png"})
    add("build_plan_bim", {"status": "error", "source_geometry_ready": False})
    add("revise_plan_bim", {"candidate": "candidate_01", "source_geometry_ready": True,
        "saved_candidate": "candidate_01", "save_effects": {"created_candidates": ["candidate_01"], "geometry_applied": True, "audit_written": True}})
    add("record_claim", {"id": "claim_0001", "sources": [{"image": "wrong-return.png"}]},
        args={"claim_json": json.dumps(claim)})
    add("revise_bim", {"status": "error"}, error=True)
    add("build_bim", {"candidate": "candidate_02", "source_geometry_ready": False})
    (run / "candidate_02").mkdir()
    (run / "candidate_02/report.json").write_text('{"source_geometry_ready": false}')
    # Reading a failed candidate is a successful inspection, not another failed build.
    add("inspect_candidate", {"candidate": "candidate_02", "report": {"source_geometry_ready": False}})
    record = dict(run="fixture", receipt={}, invocations=[dict(invocation=1, stream="archive", steps=steps)])
    loaded = {**record, "source_format": "event_envelope_jsonl", "_source_root": str(run), "gaps": []}
    summary = summarise(loaded)
    assert (summary["call_errors"], summary["domain_failures"], summary["usable_source_drafts"]) == (1, 2, 1)
    assert summary["first_build_attempt_s"] == 1 and summary["first_draft_s"] == 2
    assert summary["full_views_before_first_build_attempt"] == 1
    assert summary["height_provenance_claims"][0]["sources"] == ["West.png"]
    assert summary["height_provenance_claims"][0]["evidence_capture"] == "saved_claim_file"
    assert summary["claims_from_saved_files"] == 1
    assert "调用报错 | 领域未成功 | 可用源稿" in render_timeline({**loaded, "summary": summary})


def test_expanded_old_archive_cannot_bypass_native_journal_verification(tmp_path):
    archive = tmp_path / "record.json.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as stream:
        json.dump({"run": "fixture", "receipt": {}, "invocations": []}, stream)
    with pytest.raises(ValueError, match="verified native event-log reference"):
        load_behaviour(archive)


def _projected_transaction_record(tmp_path, *, root_projection=False):
    from scripts.tool_scripts.bim_agent_replies import compact_reply

    run = tmp_path / "run"
    (run / "claims").mkdir(parents=True)
    entries = [
        {"status": "confirmed", "recorded": True, "claim_id": "claim_0001"},
        {"status": "applied", "recorded": True, "claim_id": "claim_0002", "result_candidate": "candidate_02"},
        {"status": "failed", "recorded": False, "error": "invalid height evidence"},
    ]
    for entry in entries:
        entry["diagnostic"] = "retained full evidence " * 600
    full = {"id": "transaction_001", "status": "partial", "candidate": "candidate_01",
        "saved_candidate": "candidate_02", "save_effects": {"created_candidates": ["candidate_02"],
            "geometry_applied": True, "audit_written": True}, "entries": entries}
    for i in (1, 2):
        claim = {"id": f"claim_{i:04d}", "claim": {"candidate": "candidate_01",
            "objects": [{"id": f"W{i}"}], "values": {"z": [1, 2]}, "reason": "visible height"},
            "sources": [{"image": "North.png"}], "resolved_values": {"z": [1, 2]}}
        (run / "claims" / f"claim_{i:04d}.json").write_text(json.dumps(claim), encoding="utf8")
    visible = compact_reply(run, "claim_transaction", full)
    assert isinstance(visible["entries"], dict)
    if root_projection:
        visible = {key: visible[key] for key in ("status", "candidate", "saved_candidate", "save_effects",
                                                "details_file", "details_sha256", "details_read")}
        visible["report_view"] = {"reference": {"report_file": visible["details_file"],
            "report_sha256": visible["details_sha256"], "json_pointer": ""}}
    record = {}
    def save():
        record.clear()
        record.update({"run": "fixture", "source_format": "event_envelope_jsonl",
            "_source_root": str(run), "gaps": [], "receipt": {}, "invocations": [{"invocation": 1,
            "stream": "events", "steps": [{"index": 1, "t_call": 7, "tool": "claim_transaction",
                "arguments": {}, "result_data": visible, "result_text": json.dumps(visible), "is_error": False,
                "model_text_before": "", "thinking_tokens_before": 0}]}]})
    save()
    return run, record, visible, full, save


@pytest.mark.parametrize("root_projection", [False, True])
def test_projected_transaction_entries_report_preserves_partial_commits(tmp_path, root_projection):
    run, record, visible, full, _ = _projected_transaction_record(tmp_path, root_projection=root_projection)
    summary = summarise(record)
    record["summary"] = summary
    assert summary["tool_calls"] == 1
    assert summary["claim_transactions"] == {"calls": 1, "entries": 3,
        "status_counts": {"confirmed": 1, "applied": 1, "failed": 1}, "recorded_claims": 2, "applied_entries": 1}
    assert (summary["call_errors"], summary["domain_failures"], summary["usable_source_drafts"]) == (0, 1, 1)
    assert summary["claims_from_saved_files"] == 2 and summary["candidate_revisions"] == 1
    step = record["invocations"][0]["steps"][0]
    assert step["result_data"] == visible
    assert step["transaction_entries_source"] == {"file": visible["details_file"], "sha256": visible["details_sha256"]}
    assert "claim_transaction" in render_timeline(record)
    assert json.loads((run / visible["details_file"]).read_bytes()) == full


@pytest.mark.parametrize("damage,match", [
    ("hash", "hash mismatch"), ("escape", "tool_reports directory"),
    ("missing_reference", "lack a saved report reference"), ("status", "differs from returned status"),
    ("malformed_entries", "complete entries list"),
])
def test_projected_transaction_entries_refuse_unbound_or_changed_evidence(tmp_path, damage, match):
    import hashlib

    run, record, visible, full, save = _projected_transaction_record(tmp_path)
    path = run / visible["details_file"]
    if damage == "hash":
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "escape":
        outside = run.parent / path.name
        outside.write_bytes(path.read_bytes())
        visible["details_file"] = "../" + outside.name
    elif damage == "missing_reference":
        visible.pop("details_sha256")
    elif damage == "status":
        visible["status"] = "completed"
    else:
        full["entries"] = {"count": 3}
        raw = json.dumps(full).encode()
        digest = hashlib.sha256(raw).hexdigest()
        target = path.with_name("claim_transaction_" + digest + ".json")
        target.write_bytes(raw)
        visible.update(details_file=target.relative_to(run).as_posix(), details_sha256=digest)
    save()
    with pytest.raises(ValueError, match=match):
        summarise(record)


def test_projected_failed_transaction_without_entries_keeps_its_error(tmp_path):
    import hashlib
    from src.agent.runtime_behaviour import summarise

    run = tmp_path / "run"
    folder = run / "tool_reports"
    folder.mkdir(parents=True)
    full = {"status": "error", "error": "claim parsing failed before a transaction result"}
    raw = json.dumps(full).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = folder / ("claim_transaction_" + digest + ".json")
    path.write_bytes(raw)
    data = {"status": "error", "report_view": {"reference": {}},
        "details_file": path.relative_to(run).as_posix(), "details_sha256": digest}
    step = {"index": 1, "tool": "claim_transaction", "t_call": 1, "is_error": True,
        "result_data": data, "arguments": {}, "delivered_to_model": True}
    record = {"run": "fixture", "source_format": "event_envelope_jsonl", "gaps": [],
        "_source_root": str(run), "invocations": [{"steps": [step]}]}
    summary = summarise(record)
    assert summary["call_errors"] == 1 and summary["domain_failures"] == 0
    assert summary["claim_transactions"]["entries"] == 0
    assert step["result_data"] == data and step["call_error"]
    assert step["transaction_entries_source"]["sha256"] == digest


@pytest.fixture
def responses_run(tmp_path):
    from test_agent_runtime import MESSAGES
    from test_runtime_responses_integration import engine_for, native

    engine = engine_for(tmp_path, [native(("v", "view", {}), ("e", "error", {})), native(text="Done")])
    with engine.store as store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
        yield store


def _responses_evidence(store):
    events = [event.model_dump(mode="json") for event in store.events]
    presentation = next(event for event in events if event["payload"]["event_type"] == "tool_presentation")
    by_id = {event["event_id"]: event for event in events}
    request = by_id[presentation["payload"]["request_event_id"]]
    from src.agent.runtime_behaviour import _captured

    visible = _captured(presentation["payload"]["shown_result"], store.directory)
    body = _captured(request["payload"]["final_request_body"], store.directory)
    return events, presentation, request, visible, body


def _write_responses_evidence(store, events, presentation, request, visible, body):
    from src.agent_runtime.store import json_bytes
    import hashlib

    presentation["payload"]["shown_result"] = store.capture(visible, force_blob=True).model_dump(mode="json")
    request["payload"]["final_request_body"] = store.capture(body, force_blob=True).model_dump(mode="json")
    request["payload"]["wire_sha256"] = hashlib.sha256(json_bytes(body)).hexdigest()
    path = store.path
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


def test_responses_report_accepts_native_metadata_and_reopens_exact_images(responses_run, tmp_path):
    store = responses_run
    record = load_behaviour(store.path)
    steps = record["invocations"][0]["steps"]
    assert len(steps) == 2 and all(step["delivered_to_model"] for step in steps)
    assert [step["outcome"] for step in steps] == ["succeeded", "failed"]
    for step in steps:
        visible, prepared = step["model_visible_result"], step["prepared_result"]
        assert visible["tool_message"] == prepared["tool_message"]
        assert visible["image_blocks"] == prepared["image_blocks"]
        assert "wire_tool_result" in visible and visible != prepared
    assert sum(block["type"] == "input_image"
               for block in steps[0]["model_visible_result"]["wire_image_blocks"]) == 1
    out = tmp_path / "report"
    write_behaviour_report(store.path, out)
    assert load_behaviour(out)["invocations"] == record["invocations"]
    assert (out / "timeline.md").is_file()


@pytest.mark.parametrize("damage,match", [
    ("wire_text", "absent from the actual request"),
    ("logical_text", "differs from native content"),
    ("wire_image", "images differ from native content"),
    ("missing_image", "images are absent from the actual request"),
    ("wrong_call", "absent from the actual request"),
    ("wrong_request", "lacks its native request"),
    ("changed_text", "lacks its prior context decision"),
    ("forged_context", "lacks its prior context decision"),
    ("changed_image", "lacks its prior context decision"),
])
def test_responses_report_rejects_content_and_image_tampering(responses_run, damage, match):
    store = responses_run
    events, presentation, request, visible, body = _responses_evidence(store)
    result = visible["wire_tool_result"]
    if damage == "wire_text":
        result["output"][0]["text"] = "forged observation"
    elif damage == "logical_text":
        visible["tool_message"]["content"] = "forged observation"
    elif damage == "wire_image":
        next(block for block in visible["wire_image_blocks"] if block["type"] == "input_image")[
            "image_url"] = "data:image/png;base64,Zm9yZ2Vk"
    elif damage == "missing_image":
        for item in body["input"]:
            if isinstance(item.get("content"), list):
                item["content"] = [block for block in item["content"] if block.get("type") != "input_image"]
    elif damage == "wrong_call":
        visible["tool_message"]["tool_call_id"] = "different-call"
    elif damage == "wrong_request":
        presentation["payload"]["request_event_id"] = "absent-request"
    elif damage in {"changed_text", "forged_context"}:
        visible["tool_message"]["content"] = "forged observation"
        result["output"][0]["text"] = "forged observation"
        for item in body["input"]:
            if item.get("type") == "function_call_output" and item.get("call_id") == "v":
                item["output"] = copy.deepcopy(result["output"])
        if damage == "forged_context":
            presentation["payload"]["context_event_id"] = presentation["payload"]["tool_execution_event_id"]
    else:
        import base64
        import io
        from PIL import Image

        png = io.BytesIO()
        Image.new("RGB", (4, 4), (0, 0, 255)).save(png, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode("ascii")
        next(block for block in visible["image_blocks"] if block["type"] == "image_url")[
            "image_url"]["url"] = url
        next(block for block in visible["wire_image_blocks"] if block["type"] == "input_image")[
            "image_url"] = url
        for item in body["input"]:
            for block in item.get("content", []):
                if block.get("type") == "input_image":
                    block["image_url"] = url
    path = _write_responses_evidence(store, events, presentation, request, visible, body)
    with pytest.raises(ValueError, match=match):
        load_behaviour(path)


@pytest.mark.parametrize("context_location", ["before", "after", "other_task", "before_execution", "unrelated_image"])
def test_responses_report_accepts_image_removal_only_with_prior_context(responses_run, context_location):
    store = responses_run
    events, presentation, request, visible, body = _responses_evidence(store)
    image = request["payload"]["images"][0]["sent"]
    visible["image_blocks"] = []
    visible["wire_image_blocks"] = []
    for item in body["input"]:
        if isinstance(item.get("content"), list):
            item["content"] = [block for block in item["content"] if block.get("type") != "input_image"]
    context = {**copy.deepcopy(request), "event_id": "context-remove-image", "payload": {
        "event_type": "context", "action": "remove_image", "reason": "fixture image eviction", "image": image}}
    insertion = events.index(request) + (context_location == "after")
    if context_location == "before_execution":
        insertion = next(index for index, event in enumerate(events)
                         if event["event_id"] == presentation["payload"]["tool_execution_event_id"])
    if context_location == "unrelated_image":
        context["payload"]["image"] = store.put_bytes(b"different image", "image/png").model_dump(mode="json")
    events.insert(insertion, context)
    if context_location == "other_task":
        context["task_id"] = "another-reader"
    for index, event in enumerate(events):
        event["sequence"] = index
    presentation["payload"]["context_event_id"] = context["event_id"]
    path = _write_responses_evidence(store, events, presentation, request, visible, body)
    if context_location == "before":
        step = load_behaviour(path)["invocations"][0]["steps"][0]
        assert step["delivered_to_model"] and step["model_visible_result"]["image_blocks"] == []
        assert step["prepared_result"]["image_blocks"]
    else:
        match = ("not explained by their context decisions" if context_location == "unrelated_image"
                 else "lacks its prior context decision")
        with pytest.raises(ValueError, match=match):
            load_behaviour(path)


@pytest.mark.parametrize("context_location", ["before_execution", "after_execution"])
def test_responses_report_rejects_text_rewrite_with_unrelated_valid_context(responses_run, context_location):
    store = responses_run
    events, presentation, request, visible, body = _responses_evidence(store)
    visible["tool_message"]["content"] = "forged observation"
    visible["wire_tool_result"]["output"] = [{"type": "input_text", "text": "forged observation"}]
    for item in body["input"]:
        if item.get("type") == "function_call_output" and item.get("call_id") == "v":
            item["output"] = copy.deepcopy(visible["wire_tool_result"]["output"])
    context = {**copy.deepcopy(request), "event_id": "unrelated-context", "payload": {
        "event_type": "context", "action": "retain_image", "reason": "unrelated retention",
        "image": request["payload"]["images"][0]["sent"]}}
    execution_index = next(index for index, event in enumerate(events)
                           if event["event_id"] == presentation["payload"]["tool_execution_event_id"])
    events.insert(execution_index if context_location == "before_execution" else events.index(request), context)
    for index, event in enumerate(events):
        event["sequence"] = index
    presentation["payload"]["context_event_id"] = context["event_id"]
    path = _write_responses_evidence(store, events, presentation, request, visible, body)
    with pytest.raises(ValueError, match="context decision"):
        load_behaviour(path)


@pytest.mark.parametrize("damage", [None, "wrong_original_hash", "unrelated_execution"])
def test_responses_report_rejects_retired_text_reference_even_with_valid_hash(responses_run, damage):
    from src.agent.runtime_behaviour import _captured
    import hashlib
    from src.agent_runtime.store import json_bytes

    store = responses_run
    events, presentation, request, visible, body = _responses_evidence(store)
    execution_id = presentation["payload"]["tool_execution_event_id"]
    execution = next(event for event in events if event["event_id"] == execution_id)
    original = _captured(execution["payload"]["shown_result"], store.directory)["tool_message"]
    content_sha = hashlib.sha256(json_bytes(original.get("content"))).hexdigest() if damage != "wrong_original_hash" else "0" * 64
    reference = {"content_sha256": content_sha, "full_result_history_id": "history-full-result",
                 "reason": "exact repeated large tool result"}
    text = json.dumps({"context_reference": reference}, sort_keys=True, separators=(",", ":"))
    visible["tool_message"]["content"] = text
    visible["wire_tool_result"]["output"] = [{"type": "input_text", "text": text}]
    for item in body["input"]:
        if item.get("type") == "function_call_output" and item.get("call_id") == "v":
            item["output"] = copy.deepcopy(visible["wire_tool_result"]["output"])
    source_id = execution_id if damage != "unrelated_execution" else "other-execution"
    summary = store.put_json({"kind": "large_result_references", "replacements": [{
        "event_id": source_id, "content_sha256": content_sha,
        "full_result_history_id": reference["full_result_history_id"]}]})
    context = {**copy.deepcopy(request), "event_id": "reference-context", "payload": {
        "event_type": "context", "action": "compact", "reason": "exact repeated large tool result",
        "summary": summary.model_dump(mode="json"), "replaced_event_ids": [source_id]}}
    events.insert(events.index(request), context)
    for index, event in enumerate(events):
        event["sequence"] = index
    presentation["payload"]["context_event_id"] = context["event_id"]
    path = _write_responses_evidence(store, events, presentation, request, visible, body)
    with pytest.raises(ValueError, match="not explained by its context decisions"):
        load_behaviour(path)
