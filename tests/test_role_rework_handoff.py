"""Rework preserves verified drafts, scoped permissions and required review."""

from reader_delivery_fixtures import REVIEW, compact_elevation, accept_elevation

import asyncio
import base64
import copy
import hashlib
import json
from contextlib import asynccontextmanager

import pytest

from src.agent.runtime_roles.plan_format import audit_plan_replacement
from src.agent.runtime_roles.trial import PlanTrial, canonical_plan_sha256
from src.agent.runtime_roles.trial import _expanded_rework_targets, normalize_rework_targets
from src.agent.runtime_roles.session import envelope
from src.agent_runtime.adapter import ScriptedAdapter
from src.harness_contracts import MissingCapture, ToolExecutionPayload, ToolInvocationPayload
from tests.test_role_session import dispatch, environment  # noqa: F401
from tests.test_role_trial import Tools, example
from test_agent_runtime import response


@pytest.mark.parametrize(("raw", "expected"), [
    (["plan.openings"], ["plan.openings"]),
    (["openings"], ["plan.openings"]),
    (["openings:W1"], ["plan.openings:W1"]),
])
def test_rework_target_opening_spellings_are_normalized_without_broadening(raw, expected):
    assert normalize_rework_targets(raw) == expected


def test_openings_collection_scope_preserves_every_other_plan_object():
    before = example()
    after = copy.deepcopy(before)
    after["openings"].append({**copy.deepcopy(after["openings"][0]), "id": "W_NEW"})
    allowed = _expanded_rework_targets(["plan.openings"], before, after)
    audit_plan_replacement(before, after, allowed_targets=allowed)
    after["partitions"][0]["points"][0][0] += 1
    with pytest.raises(ValueError, match="outside.*plan.partitions"):
        audit_plan_replacement(before, after, allowed_targets=allowed)


def test_unknown_rework_target_lists_three_supported_examples():
    with pytest.raises(ValueError) as raised:
        normalize_rework_targets(["doors:W1"])
    message = str(raised.value)
    for example_target in ("plan.openings", "openings", "openings:W1"):
        assert example_target in message


def test_rework_session_keeps_full_submission_review_before_any_adapter_request(environment, monkeypatch):
    store, make = environment
    session = make()
    task = session._task({**dispatch("plan-first"), "role_id": "plan_reader", "target": "F1"})
    child = session.registry.admit(task)
    workspace = child.task_directory / "bim/trial_workspace"
    (workspace / "images").mkdir(parents=True)
    (workspace / "images/north.png").write_bytes((session.run_directory / "images/north.png").read_bytes())
    (workspace / "inputs.json").write_bytes((session.run_directory / "inputs.json").read_bytes())
    prior = PlanTrial(Tools(workspace=workspace), image_name="north.png", workspace=workspace,
                      receipt_directory=workspace / "trial_receipts")
    value = example()
    receipt = asyncio.run(prior.run(value))
    # An accepted delivery can retain an earlier successful geometry while also
    # reviewing a warning discovered in a later, unsuccessful trial.
    review = [*receipt.get("topology_issues", []), {"issue_id": "later-failed-trial", "divider": "P2"}]
    compiled = prior.numeric_plan(receipt)
    session.registry.save(task, status="completed", artifact={"plan": compiled, "evidence": [], "unresolved": []},
                          validation={**receipt, "validation_passed": True, "topology_issues": review})
    captured = {}

    @asynccontextmanager
    async def isolated_trial(directory, image_name, *, root):
        captured["trial"] = PlanTrial(Tools(), image_name=image_name)
        yield captured["trial"]

    monkeypatch.setattr("src.agent.runtime_roles.trial.PlanTrialSession", isolated_trial)

    def stop_at_adapter_boundary(*args):
        trial = captured["trial"]
        assert trial.baseline() == (compiled, canonical_plan_sha256(compiled))
        assert trial.allowed_rework_targets == ["plan.openings:D1"]
        assert trial.inherited_topology_issues == review
        raise ValueError("handoff checked without requesting an adapter")

    session.adapter_factory = stop_at_adapter_boundary
    result = asyncio.run(session.delegate_many([{
        **dispatch("plan-next"), "role_id": "plan_reader", "target": "F1", "previous_task_id": "plan-first",
        "issues": ["correct only D1"], "rework_targets": ["plan.openings:D1"],
    }]))["results"][0]
    assert result["status"] == "failed" and "handoff checked" in result["reason"]
    assert not any(event.payload.event_type == "adapter_request" for event in store.all_events)


class RecoveryTools(Tools):
    """Tools remain unused: plan trials compile the actual 2D reading locally."""


def _append_trial(child, receipt, *, outcome=None, audit=True):
    outcome = outcome or ("succeeded" if receipt["status"] == "passed" else "failed")
    from src.agent.runtime_roles.trial_feedback import passed_receipt
    shown = passed_receipt(receipt) if audit and receipt["status"] == "passed" and "receipt_audit" not in receipt else receipt
    captured = child.capture(envelope(shown, error=outcome != "succeeded"))
    return child.append(ToolExecutionPayload(
        call_id=f"trial-{len(child.events)}", tool_name="trial_plan_bim", full_arguments={"plan": {}},
        raw_result=captured, shown_result=captured, repeatability="non_idempotent_write",
        operation_key=f"trial-{len(child.events)}", outcome=outcome,
        applied_write_id=f"write-{len(child.events)}" if outcome == "succeeded" else None))


def _passed_unsubmitted(session, *, reason="context_recovery_required", profile=False, topology=False):
    task = session._task(dispatch("prior", role_id="plan_reader", target="F1"))
    child = session.registry.admit(task)
    workspace = child.task_directory / "bim/trial_workspace"
    (workspace / "images").mkdir(parents=True)
    for relative in ("inputs.json", "images/north.png"):
        (workspace / relative).write_bytes((session.run_directory / relative).read_bytes())
    profiles = workspace / "profiles"
    value = example()
    if profile:
        profiles.mkdir()
        evidence = {"name": "north.png", "image_sha256": task["input_sha256"], "axis": "x",
                    "candidates": [{"id": "C01", "pixels": [value["x_anchors"][0][0], 1], "peak": 1}]}
        (profiles / "profile_001.json").write_text(json.dumps(evidence), encoding="utf-8")
        value["x_anchors"][0][0] = {"profile": "profile_001", "candidate": "C01"}
    prior = PlanTrial(RecoveryTools(workspace=workspace), image_name="north.png", workspace=workspace,
                      receipt_directory=workspace / "trial_receipts", profile_directory=profiles)
    failed = copy.deepcopy(value)
    failed["openings"][0].update(p1=[4, 3], p2=[4, 4])
    rejected = asyncio.run(prior.run(failed))
    assert rejected["status"] == "failed"
    earlier = _append_trial(child, rejected)
    passed = asyncio.run(prior.run(value))
    assert passed["status"] == "passed", passed
    if topology:
        passed["topology_issues"] = [{"issue_id": "retained-review", "divider": "wall-A", "opening": "D1",
                                      "look_box": [5, 2, 7, 5], "kind": "unsupported_declared_divider"}]
        (workspace / passed["receipt_file"]).write_text(json.dumps(passed), encoding="utf-8")
    current = _append_trial(child, passed)
    session.registry.save(task, status="failed", reason=reason, runtime={"status": reason})
    return task, child, prior, value, passed, earlier, current


def _rework_arguments(**extra):
    return dispatch("next", role_id="plan_reader", target="F1", previous_task_id="prior",
                    issues=["Continue from the saved passed draft; check D1 if needed."],
                    rework_targets=["plan.openings:D1"], **extra)


def _isolated_recovery_trials(monkeypatch, captured):
    @asynccontextmanager
    async def isolated_trial(directory, image_name, *, root):
        workspace = directory / "trial_workspace"
        (workspace / "images").mkdir(parents=True)
        for relative in ("inputs.json", f"images/{image_name}"):
            (workspace / relative).write_bytes((directory / relative).read_bytes())
        trial = PlanTrial(RecoveryTools(workspace=workspace), image_name=image_name, workspace=workspace,
                          receipt_directory=workspace / "trial_receipts", profile_directory=workspace / "profiles")
        captured["trial"] = trial
        yield trial
    monkeypatch.setattr("src.agent.runtime_roles.trial.PlanTrialSession", isolated_trial)


def _operation(collection="openings", identity="D1", **changes):
    return {"op": "update", "collection": collection, "id": identity, "changes": changes,
            "bbox": [0, 0, 20, 20], "reason": "Located current-task correction",
            "source_refs": ["north.png: original"]}


@pytest.mark.parametrize(("reason", "local_edit"), [
    ("context_recovery_required", False), ("invalid_settlement_evidence", True),
])
def test_passed_unsubmitted_rework_reads_retrials_and_explicitly_submits(environment, monkeypatch,
                                                                       reason, local_edit):
    store, make = environment
    session = make()
    task, child, prior, original, passed, earlier, current = _passed_unsubmitted(
        session, reason=reason, profile=True)
    handoff = session._failure_handoff("prior")
    assert handoff["editable_draft"]["status"] == "passed"
    assert handoff["editable_draft"]["submission_accepted"] is False
    assert handoff["editable_draft"]["event_id"] == current.event_id
    assert handoff["editable_draft"]["compiled_numeric_plan_sha256"] == passed["compiled_numeric_plan_sha256"]
    assert handoff["tool_failures"] == []  # The pre-pass host error is resolved.
    assert handoff["runtime_status"] == reason
    assert session.registry.records["prior"].get("artifact") is None
    assert not (child.task_directory / "bim/reader_submission.json").exists()
    captured = {}
    _isolated_recovery_trials(monkeypatch, captured)
    revised = copy.deepcopy(original)
    if local_edit:
        revised["openings"][0]["z"] = [0, 2.2]
    trial_arguments = ({"operations": [_operation(z=[0, 2.2])]} if local_edit else {"plan": original})
    adapter = ScriptedAdapter([
        response(("read", "read_plan_draft", {"field": "plan"})),
        response(("premature", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
        response(("trial", "trial_plan_bim", trial_arguments)),
        response(("submit", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
        response(text="Submission acknowledged."),
    ])
    session.adapter_factory = lambda *args: adapter
    result = asyncio.run(session.delegate_many([_rework_arguments()]))["results"][0]
    assert result["status"] == "completed", result
    next_child = session.registry.child("next")
    executions = {event.payload.call_id: event.payload for event in next_child.events
                  if event.payload.event_type == "tool_execution"}
    read = next_child.resolve(executions["read"].raw_result)["structuredContent"]
    assert read["inherited"] and read["status"] == "passed" and read["plan"] == original
    assert read["validation_passed"] and read["submission_accepted"] is False
    assert "regularization" not in read["plan"]  # Keep the original declaration, not expanded numeric audit.
    assert executions["premature"].outcome == "failed"
    assert "successful isolated trial in this task" in str(next_child.resolve(executions["premature"].raw_result))
    assert executions["trial"].outcome == executions["submit"].outcome == "succeeded"
    trial = captured["trial"]
    assert trial.reference_validation_passed and trial.allowed_rework_targets == ["plan.openings:D1"]
    assert trial.inherited_reference_ids() == {"profile_001"}
    assert trial.load_plan(trial.receipts[-1]) == revised
    assert session.registry.read("next")["plan"] == trial.numeric_plan(trial.receipts[-1])
    assert result["validation"]["validation_passed"]
    assert (next_child.task_directory / "bim/reader_submission.json").is_file()
    assert prior.load_plan(prior.receipts[-1]) == original
    assert len(adapter.requests) == 4


def test_passed_rework_rejects_out_of_scope_ops_then_preserves_unchanged_submit(environment, monkeypatch):
    _, make = environment
    session = make()
    _, _, prior, original, passed, _, _ = _passed_unsubmitted(session)
    captured = {}
    _isolated_recovery_trials(monkeypatch, captured)
    adapter = ScriptedAdapter([
        response(("outside", "trial_plan_bim", {"operations": [
            _operation("partitions", original["partitions"][0]["id"], points=[[1, 1], [2, 2]])]})),
        response(("read", "read_plan_draft", {"field": "plan"})),
        response(("trial", "trial_plan_bim", {"plan": original})),
        response(("submit", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
        response(text="Submission acknowledged."),
    ])
    session.adapter_factory = lambda *args: adapter
    result = asyncio.run(session.delegate_many([_rework_arguments()]))["results"][0]
    assert result["status"] == "completed", result
    child = session.registry.child("next")
    executions = {event.payload.call_id: event.payload for event in child.events
                  if event.payload.event_type == "tool_execution"}
    rejected = child.resolve(executions["outside"].raw_result)["structuredContent"]
    assert rejected["draft_unchanged"] and "outside" in str(rejected)
    assert child.resolve(executions["read"].raw_result)["structuredContent"]["plan"] == original
    assert len(captured["trial"].receipts) == 1
    assert captured["trial"].receipts[0]["plan_sha256"] == passed["plan_sha256"]
    assert prior.verified_plan(passed["plan_sha256"])[0] == original


@pytest.mark.parametrize("product", ["declaration", "numeric", "source", "profile", "image", "receipt", "event",
                                     "topology", "orientation"])
def test_passed_unsubmitted_recovery_rejects_changed_products(environment, product):
    _, make = environment
    session = make()
    _, child, prior, _, passed, _, _ = _passed_unsubmitted(session, profile=True, topology=True)
    if product == "event":
        changed = {**passed, "reading_product_sha256": "0" * 64}
        _append_trial(child, changed)
    elif product == "image":
        path = prior.workspace / "images/north.png"
        path.write_bytes(path.read_bytes() + b"tampered")
        manifest = json.loads((prior.workspace / "inputs.json").read_bytes())
        manifest["images"]["north.png"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (prior.workspace / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        relative = {"declaration": passed["input_plan_file"], "numeric": passed["compiled_numeric_plan_file"],
                    "source": passed["reading_product_file"],
                    "profile": passed["measurement_profiles"][0]["file"],
                    "receipt": passed["receipt_file"], "topology": passed["receipt_file"],
                    "orientation": passed["receipt_file"]}[product]
        path = prior.workspace / relative
        if product in {"receipt", "topology", "orientation"}:
            changed = json.loads(path.read_bytes())
            if product == "receipt":
                changed["reading_product_sha256"] = "0" * 64
            elif product == "topology":
                changed["topology_issues"] = []
            else:
                changed["axis_orientation"] = {"world_north_toward": "image_bottom",
                                                "world_east_toward": "image_left"}
            path.write_text(json.dumps(changed), encoding="utf-8")
        else:
            path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        session._task(_rework_arguments())


def test_compact_passed_event_binds_complete_receipt_and_rejects_missing_identity(environment):
    from src.agent.runtime_roles.trial_feedback import passed_receipt
    _, make = environment
    session = make()
    _, child, _, _, passed, _, _ = _passed_unsubmitted(session)
    compact = passed_receipt(passed)
    _append_trial(child, compact)
    admitted = session._task(_rework_arguments())
    assert admitted["previous_task_handoff"]["editable_draft"]["expected_receipt_sha256"] == compact["receipt_audit"]["sha256"]
    incomplete = {key: value for key, value in compact.items() if key != "receipt_audit"}
    _append_trial(child, incomplete, audit=False)
    refused = session._task(_rework_arguments())["previous_task_handoff"]
    assert "editable_draft" not in refused
    assert "receipt audit" in refused["draft_recovery"]["reason"]


@pytest.mark.parametrize("mutation", ["path", "hash", "projection", "encoding"])
def test_compact_trial_audit_cannot_redirect_or_rewrite_recovery(environment, mutation):
    from src.agent.runtime_roles.trial_feedback import passed_receipt
    _, make = environment
    session = make()
    _, child, _, _, passed, _, _ = _passed_unsubmitted(session)
    compact = passed_receipt(passed)
    if mutation == "path":
        compact["receipt_file"] = compact["receipt_audit"]["receipt_file"] = "../outside.json"
    elif mutation == "hash":
        compact["receipt_audit"]["sha256"] = "0" * 64
    elif mutation == "projection":
        compact["plan_sha256"] = "0" * 64
    else:
        compact["receipt_audit"]["hash_encoding"] = "unsupported"
    _append_trial(child, compact, audit=False)
    if mutation == "encoding":
        handoff = session._task(_rework_arguments())["previous_task_handoff"]
        assert "editable_draft" not in handoff
        assert "receipt audit" in handoff["draft_recovery"]["reason"]
    else:
        with pytest.raises(ValueError, match="escapes|hash mismatch|projection differs"):
            session._task(_rework_arguments())


def test_passed_receipt_is_reverified_after_admission_before_adapter(environment, monkeypatch):
    store, make = environment
    session = make()
    _, _, prior, _, passed, _, _ = _passed_unsubmitted(session, topology=True)

    @asynccontextmanager
    async def tamper_after_admission(directory, image_name, *, root):
        saved = prior.workspace / passed["receipt_file"]
        changed = json.loads(saved.read_bytes())
        changed["topology_issues"] = []
        saved.write_text(json.dumps(changed), encoding="utf-8")
        yield PlanTrial(RecoveryTools(), image_name=image_name)

    monkeypatch.setattr("src.agent.runtime_roles.trial.PlanTrialSession", tamper_after_admission)
    result = asyncio.run(session.delegate_many([_rework_arguments()]))["results"][0]
    assert result["status"] == "failed" and "receipt" in result["reason"]
    assert not any(event.payload.event_type == "adapter_request" for event in store.all_events)


def test_inherited_validation_is_readable_and_still_requires_current_submission_decisions(environment, monkeypatch):
    _, make = environment
    session = make()
    _, _, _, original, passed, _, _ = _passed_unsubmitted(session, topology=True)
    handoff = session._failure_handoff("prior")
    assert handoff["editable_draft"]["topology_decisions_required"] == 1
    captured = {}
    _isolated_recovery_trials(monkeypatch, captured)
    decision = {"issue_id": "retained-review", "decision": "retain_opening", "bbox": [5, 2, 7, 5],
                "basis": "The located original has a door within the same physical divider."}
    adapter = ScriptedAdapter([
        response(("review", "read_plan_draft", {"field": "report:/topology_issues"})),
        response(("trial", "trial_plan_bim", {"plan": original})),
        response(("missing-review", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
        response(("submit", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest", "topology_decisions": [decision]})),
        response(text="Submission acknowledged."),
    ])
    session.adapter_factory = lambda *args: adapter
    result = asyncio.run(session.delegate_many([_rework_arguments()]))["results"][0]
    assert result["status"] == "completed", result
    child = session.registry.child("next")
    executions = {event.payload.call_id: event.payload for event in child.events
                  if event.payload.event_type == "tool_execution"}
    assert executions["review"].outcome == "succeeded"
    report = child.resolve(executions["review"].raw_result)["structuredContent"]
    assert report["value"] == passed["topology_issues"]
    assert executions["missing-review"].outcome == "failed"
    assert "retained-review" in str(child.resolve(executions["missing-review"].raw_result))
    assert result["validation"]["topology_issues"] == passed["topology_issues"]
    assert result["validation"]["topology_decisions"] == [decision]


def test_passed_unsubmitted_requires_scope_and_matching_admitted_image(environment):
    _, make = environment
    session = make()
    _passed_unsubmitted(session)
    no_scope = _rework_arguments()
    no_scope.pop("rework_targets")
    with pytest.raises(ValueError, match="explicit rework_targets"):
        session._task(no_scope)
    with pytest.raises(ValueError, match="same role, original image and target"):
        session._task({**_rework_arguments(), "target": "F2"})
    session.registry.records["prior"]["input_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="same admitted original image hash"):
        session._task(_rework_arguments())


@pytest.mark.parametrize("unknown", ["invocation", "execution"])
def test_latest_unknown_write_blocks_passed_draft_recovery(environment, unknown):
    _, make = environment
    session = make()
    _, child, _, _, _, _, _ = _passed_unsubmitted(session)
    fields = dict(call_id="unsettled", tool_name="trial_plan_bim", full_arguments={"plan": {}},
                  repeatability="non_idempotent_write", operation_key="unsettled")
    child.append(ToolInvocationPayload(**fields))
    if unknown == "execution":
        child.append(ToolExecutionPayload(**fields, raw_result=MissingCapture(reason="unknown"),
                                          shown_result=MissingCapture(reason="unknown"), outcome="unknown"))
    handoff = session._failure_handoff("prior")
    assert "editable_draft" not in handoff
    assert handoff["draft_recovery"]["status"] == "unavailable"


def test_later_editable_failure_remains_baseline_after_earlier_pass(environment):
    _, make = environment
    session = make()
    _, child, prior, original, passed, _, _ = _passed_unsubmitted(session)
    changed = copy.deepcopy(original)
    changed["openings"][0].update(p1=[4, 3], p2=[4, 4])
    latest = asyncio.run(prior.run(changed))
    _append_trial(child, latest)
    handoff = session._failure_handoff("prior")
    assert handoff["editable_draft"]["status"] == "failed"
    assert handoff["editable_draft"]["plan_sha256"] == latest["plan_sha256"] != passed["plan_sha256"]
