"""Offline lifecycle checks use real saved mesh handoffs and native receipts."""
import hashlib
import json

import pytest

from src.dsh_runtime.handoff import attest_local_finish
from src.dsh_runtime.lifecycle_evidence import (
    attest_host_finish, verify_recovery_state, work_snapshot,
)
from tests.test_mesh_runtime_completion import mesh_run, _candidate, _handoff


def _events(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _end(reason=None):
    return {"type": "status", "phase": "turn_end", "turn": 1,
            "reason": reason or {"kind": "completed"}}


def _pair(name="inputs", cid="call_1", result="{}", **extra):
    return [
        {"type": "tool_call", "callId": cid, "tool": "mcp__sim_bim__" + name,
         "input": {"candidate": "candidate_01"}},
        {"type": "tool_result", "callId": cid, "status": "completed", "result": result, **extra},
    ]


@pytest.fixture
def saved_finish(mesh_run):
    candidate = _candidate(mesh_run)
    _, result = _handoff(mesh_run, candidate)
    return mesh_run, result


def test_snapshot_covers_all_durable_files_and_ignores_runtime_caches(tmp_path):
    run = tmp_path / "bim"
    for name in ("inputs.json", "reports/result.json", ".harness_tmp/budget.json", "__pycache__/cache.pyc"):
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    snapshot = work_snapshot(run)
    assert snapshot == {name: hashlib.sha256(name.encode()).hexdigest()
                        for name in ("inputs.json", "reports/result.json")}
    (run / ".harness_tmp/budget.json").write_bytes(b"new budget")
    assert work_snapshot(run) == snapshot


def test_snapshot_refuses_a_link_even_to_a_file_inside_the_run(tmp_path):
    run = tmp_path / "bim"
    run.mkdir()
    target = run / "inputs.json"
    target.write_bytes(b"fixture")
    try:
        (run / "linked.json").symlink_to(target)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    with pytest.raises(ValueError, match="linked_evidence_path"):
        work_snapshot(run)


@pytest.mark.parametrize("reason", [{"kind": "completed"},
    {"kind": "error", "error": {"code": "TIMEOUT"}},
    {"kind": "error", "error": {"code": "PROVIDER_ERROR"}}])
def test_closed_native_turn_and_unchanged_work_can_resume(tmp_path, reason):
    run = tmp_path / "bim"
    run.mkdir()
    (run / "inputs.json").write_bytes(b"fixture")
    log = _events(tmp_path / "events.jsonl", _pair() + [_end(reason)])
    proof = verify_recovery_state(run, work_snapshot(run), log)
    assert proof["safe_to_resume"] and proof["closed_tools"]
    assert proof["tool_count"] == 1


def test_pure_text_turn_and_schema_rejection_are_safe_closed_states(tmp_path):
    run = tmp_path / "bim"
    run.mkdir()
    log = _events(tmp_path / "events.jsonl", [{"type": "text", "text": "done"}, _end()])
    assert verify_recovery_state(run, {}, log)["safe_to_resume"]
    rows = _pair("finish_bim", result="Error: schema requires candidate")
    rows[1]["status"] = "error"
    _events(log, rows + [_end()])
    assert verify_recovery_state(run, {}, log)["safe_to_resume"]
    assert attest_host_finish(run, log) is None


@pytest.mark.parametrize("change", ["add", "modify", "delete"])
def test_changed_disk_state_never_replays_checkpoint(tmp_path, change):
    run = tmp_path / "bim"
    run.mkdir()
    original = run / "source.json"
    original.write_bytes(b"original")
    checkpoint = work_snapshot(run)
    log = _events(tmp_path / "events.jsonl", [_end()])
    if change == "add":
        (run / "new.json").write_bytes(b"new")
    elif change == "modify":
        original.write_bytes(b"new")
    else:
        original.unlink()
    result = verify_recovery_state(run, checkpoint, log)
    assert not result["safe_to_resume"]
    assert result["reason"] == "work_snapshot_changed" and result["closed_tools"]


@pytest.mark.parametrize("case", ["missing", "orphan", "duplicate_call", "duplicate_result", "unknown_tool", "torn", "duplicate_json", "abort", "process_abort", "no_end", "later_activity"])
def test_ambiguous_execution_and_abort_never_resume(tmp_path, case):
    run = tmp_path / "bim"
    run.mkdir()
    pair = _pair()
    rows = pair + [_end()]
    if case == "missing":
        rows = [pair[0], _end()]
    elif case == "orphan":
        rows = [pair[1], _end()]
    elif case == "duplicate_call":
        rows = [pair[0], *pair, _end()]
    elif case == "duplicate_result":
        rows = [*pair, pair[1], _end()]
    elif case == "unknown_tool":
        rows = _pair("write_unregistered_file") + [_end()]
    elif case == "abort":
        rows[-1] = _end({"kind": "aborted"})
    elif case == "process_abort":
        rows[-1] = _end({"kind": "error", "error": {"code": "ABORT_ERR"}})
    elif case == "no_end":
        rows = pair
    elif case == "later_activity":
        rows += [{"type": "status", "phase": "step_start"}]
    log = _events(tmp_path / "events.jsonl", rows)
    if case == "torn":
        with log.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"tool_call"')
    elif case == "duplicate_json":
        log.write_text('{"type":"text","type":"tool_call"}\n', encoding="utf-8")
    assert not verify_recovery_state(run, {}, log)["safe_to_resume"]


def test_actual_finish_survives_host_stream_failure_and_later_read_only_attempt(saved_finish, tmp_path):
    run, result = saved_finish
    first = _events(tmp_path / "attempt-1/events.jsonl",
                    _pair("finish_bim", "call_finish|fc_finish", json.dumps(result)))
    second_rows = _pair("inspect_candidate", "call_read")
    second_rows += [{"type": "tool_call", "callId": "call_image", "tool": "mcp__sim_bim_context__read_context_image", "input": {}},
                    {"type": "tool_result", "callId": "call_image", "status": "completed", "result": "image"},
                    _end({"kind": "error", "error": {"code": "TIMEOUT"}})]
    second = _events(tmp_path / "attempt-2/events.jsonl", second_rows)
    proof = attest_host_finish(run, [first, second], 1)
    assert proof and proof["call_id"] == "call_finish"
    assert proof["native_call_id"] == "call_finish|fc_finish"
    assert proof["architectural_quality"] == "unrated"
    assert attest_host_finish(run, [first, second], 2) is None


@pytest.mark.parametrize("case", ["later_write", "unknown_tool", "missing_result", "failed_finish", "stale_artifact", "stale_mesh_evidence", "wrong_candidate", "overlapping_write"])
def test_finish_requires_current_files_and_last_completed_write(saved_finish, tmp_path, case):
    run, result = saved_finish
    rows = _pair("finish_bim", "call_finish", json.dumps(result))
    if case == "later_write":
        rows += _pair("record_inference", "call_write")
    elif case == "unknown_tool":
        rows += _pair("unknown_read_or_write", "call_unknown")
    elif case == "missing_result":
        rows = rows[:1]
    elif case == "failed_finish":
        rows[1]["status"] = "error"
    elif case == "stale_artifact":
        (run / "delivery.html").write_bytes(b"replacement")
    elif case == "stale_mesh_evidence":
        (run / "mesh_overlays/overlay_001.png").write_bytes(b"replacement")
    elif case == "wrong_candidate":
        rows[0]["input"]["candidate"] = "candidate_02"
    elif case == "overlapping_write":
        write = _pair("record_inference", "call_write")
        rows = [write[0], *rows, write[1]]
    log = _events(tmp_path / "events.jsonl", rows + [_end()])
    assert attest_host_finish(run, log) is None


def _saved_report(run, result):
    raw = json.dumps(result).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = run / "tool_reports" / f"finish_bim_{digest}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    return {"details_file": path.relative_to(run).as_posix(), "details_sha256": digest}


@pytest.mark.parametrize("truncated", [False, True])
def test_precise_hash_bound_report_recovers_full_finish(saved_finish, tmp_path, truncated):
    run, result = saved_finish
    bound = _saved_report(run, result)
    text = json.dumps(bound)
    if truncated:
        text = text[:-1] + ', "large_detail": "cut off'
    else:
        text += "\nExecution time: 0.1 seconds"
    log = _events(tmp_path / "events.jsonl", _pair("finish_bim", result=text, truncated=truncated))
    assert attest_host_finish(run, log)


@pytest.mark.parametrize("case", ["hash_mismatch", "escape", "missing_binding", "nested_binding", "invalid_bound_report"])
def test_report_lookup_never_guesses_old_reports_or_bypasses_bad_binding(saved_finish, tmp_path, case):
    run, result = saved_finish
    bound = _saved_report(run, result)
    recorded = {**attest_local_finish(run, "candidate_01", result), "call_id": "call_1"}
    if case == "hash_mismatch":
        bound["details_sha256"] = "0" * 64
    elif case == "escape":
        bound["details_file"] = "../" + bound["details_file"]
    elif case == "missing_binding":
        bound = {"candidate": "candidate_01"}
    elif case == "nested_binding":
        bound = {"notes": bound}
    elif case == "invalid_bound_report":
        bound = _saved_report(run, {"candidate": "candidate_01"})
    text = json.dumps(bound)
    log = _events(tmp_path / "events.jsonl", _pair("finish_bim", result=text, truncated=True))
    # Existing reports alone cannot establish a result; only the explicit same-call
    # relay proof may compensate for a genuinely missing display reference.
    assert attest_host_finish(run, log) is None
    if case in {"hash_mismatch", "escape", "invalid_bound_report"}:
        assert attest_host_finish(run, log, recorded_proof=recorded) is None


@pytest.mark.parametrize("case", ["valid", "wrong_call", "later_write", "unknown_tool", "stale", "not_truncated", "error_text", "wrong_candidate", "reused_provider_call"])
def test_recorded_relay_proof_is_revalidated_and_bound_to_actual_native_finish(saved_finish, tmp_path, case):
    run, result = saved_finish
    proof = {**attest_local_finish(run, "candidate_01", result), "call_id": "call_finish"}
    rows = _pair("finish_bim", "call_finish|fc_finish", '{"candidate": "candidate_01", "large": "cut', truncated=True)
    if case == "wrong_call":
        proof["call_id"] = "call_old"
    elif case == "later_write":
        rows += _pair("record_inference", "call_write")
    elif case == "unknown_tool":
        rows += _pair("unknown_operation", "call_unknown")
    elif case == "stale":
        (run / "candidate_01/viewer.html").write_bytes(b"changed")
    elif case == "not_truncated":
        rows[1]["truncated"] = False
    elif case == "error_text":
        rows[1]["result"] = "Error: finish failed after validation"
    elif case == "wrong_candidate":
        rows[1]["result"] = '{"candidate": "candidate_02", "large": "cut'
    elif case == "reused_provider_call":
        rows = _pair("inputs", "call_finish|fc_old") + rows
    log = _events(tmp_path / "events.jsonl", rows)
    assert bool(attest_host_finish(run, log, recorded_proof=proof)) == (case == "valid")
