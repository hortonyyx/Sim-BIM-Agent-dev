"""Explicit new-run evidence imports keep identity, scope and submission gates."""

from reader_delivery_fixtures import REVIEW, compact_elevation, accept_elevation

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.agent.runtime_roles import continuation as domain
from src.agent.runtime_roles.session import RoleSession, envelope
from src.agent.runtime_roles.submission import ReaderSubmission
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.continuation import ReadOnlyRun
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore, json_bytes
from src.harness_contracts import ToolExecutionPayload
from tests.test_role_session import Frozen, ROOT, ROUTES, dispatch, elevation
from tests.test_role_rework_handoff import _passed_unsubmitted, _isolated_recovery_trials, _operation
from test_agent_runtime import response, versions


def _images(directory, source=None):
    run = directory / "bim"
    (run / "images").mkdir(parents=True)
    manifest = {"image_kind": "drawings", "floor_plan_images": ["north.png", "f2.png"], "images": {}}
    for name in ("north.png", "f2.png", "e_n.png", "e_s.png", "e_e.png", "e_w.png"):
        path = run / "images" / name
        if source is None:
            Image.new("RGB", (100, 100), "white").save(path)
        else:
            path.write_bytes((source / "bim/images" / name).read_bytes())
        manifest["images"][name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": [100, 100]}
    (run / "inputs.json").write_bytes(json_bytes(manifest))
    return run


def _session(store, run):
    return RoleSession(store=store, frozen=Frozen(run), routes=ROUTES, root=ROOT,
                       limits=RunLimits(model_calls=30, tool_calls=50, seconds=120, tokens=500_000),
                       adapter_factory=lambda *args: pytest.fail("Unexpected model request"))


def _facade(session, side, image):
    task = session._task(dispatch("e_" + side.lower(), image=image, target=side + "/F1"))
    child = session.registry.admit(task)
    value = elevation()
    value["orientation"] = side
    value["view_direction"] = {"North": "South", "South": "North", "East": "West", "West": "East"}[side]
    if side in {"South", "East"}:
        value["x_calibration"].update(world_start_m=0, world_end_m=10)
    submission = ReaderSubmission(role_id="elevation_reader", image_name=image, directory=child.task_directory / "bim",
                                  target=task["target"])
    accepted = submission.submit(value)
    captured = child.capture(envelope(accepted))
    child.append(ToolExecutionPayload(call_id="submit", tool_name="submit_elevation_reading",
        full_arguments=value, raw_result=captured, shown_result=captured,
        repeatability="non_idempotent_write", operation_key="submit-" + side,
        outcome="succeeded", applied_write_id="submit-" + side))
    result = submission.read()
    session.registry.save(task, status="completed", artifact=result["artifact"], validation=result["validation"])


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.runtime_roles.session.make_versions", lambda *args, **kwargs: versions())
    monkeypatch.setattr(domain, "verify_domain_compatibility", lambda *args: {
        "source_domain": "fixture-v1", "destination_domain": "fixture-v1", "changed_files": []})
    source_path = tmp_path / "source"
    source_run = _images(source_path)
    source_limits = RunLimits(model_calls=30, tool_calls=50, seconds=120, tokens=500_000)
    with EventStore(source_path, run_id="old", task_id="coordinator", budget_limit=source_limits.ledger_limit()) as store:
        session = _session(store, source_run)
        for side, image in (("North", "e_n.png"), ("South", "e_s.png"), ("East", "e_e.png"), ("West", "e_w.png")):
            _facade(session, side, image)
        task, child, prior, original, passed, _, current = _passed_unsubmitted(session, profile=True)
        store.write_json("versions.json", {"mode": "role_division"})
        facts = {"task": task, "workspace": prior.workspace, "original": original,
                 "passed": passed, "event": current, "child": child.task_directory}
    source = ReadOnlyRun(source_path)
    target_path = tmp_path / "target"
    target_run = _images(target_path, source_path)
    with EventStore(target_path, run_id="new", task_id="coordinator", budget_limit=source_limits.ledger_limit()) as store:
        yield SimpleNamespace(source=source, target=_session(store, target_run), facts=facts)


def test_four_submissions_and_passed_draft_import_idempotently_without_execution(continuation):
    c = continuation
    summary = domain.import_reader_evidence(c.source, c.target)
    assert set(summary["completed_ids"]) == {"e_north", "e_south", "e_east", "e_west"}
    assert [row["task_id"] for row in summary["editable_drafts"]] == ["prior"]
    assert not c.target.store.all_events
    for identity in summary["completed_ids"]:
        original = c.source.read_json("tasks/" + hashlib.sha256(identity.encode()).hexdigest() + "/reader_record.json")
        assert c.target.registry.records[identity]["delivered_at_ns"] == original["delivered_at_ns"]
    assert summary == domain.import_reader_evidence(c.source, c.target)
    assert not c.target.store.all_events
    assert c.source.verify_unchanged()
    assert not (c.source.directory / ".writer.lock").exists()
    record = c.target.registry.records["prior"]
    assert record["status"] == "failed" and record["artifact"] is None
    assert not (c.target.registry.child("prior").task_directory / "bim/reader_submission.json").exists()
    handoff = c.target._failure_handoff("prior")
    assert handoff["editable_draft"]["status"] == "passed"
    assert not handoff["editable_draft"]["submission_accepted"]
    assert not handoff["tool_failures"]
    with pytest.raises(ValueError, match="immutable"):
        c.target.registry.save(c.facts["task"], status="running")
    state = domain._manifest(c.target)
    assert any(name.endswith("trial_001.json") for name in state["source_files"])
    assert not any("trial_001" in name for name in state["files"])  # Only selected products were copied.


@pytest.mark.parametrize("local_edit", [False, True])
def test_imported_draft_reads_retrials_and_explicitly_submits(continuation, monkeypatch, local_edit):
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    captured = {}
    _isolated_recovery_trials(monkeypatch, captured)
    args = {"operations": [_operation(z=[0, 2.2])]} if local_edit else {"plan": c.facts["original"]}
    adapter = ScriptedAdapter([
        response(("read", "read_plan_draft", {"field": "plan"})),
        response(("premature", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
        response(("trial", "trial_plan_bim", args)),
        response(("submit", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
        response(text="Submission acknowledged."),
    ])
    c.target.adapter_factory = lambda *args: adapter
    result = asyncio.run(c.target.delegate_many([dispatch("new_plan", role_id="plan_reader", target="F1",
        previous_task_id="prior", issues=["Continue passed draft; verify before submission."],
        rework_targets=["plan.openings:D1"])]))["results"][0]
    assert result["status"] == "completed", result
    child = c.target.registry.child("new_plan")
    calls = {e.payload.call_id: e.payload for e in child.events if e.payload.event_type == "tool_execution"}
    read = child.resolve(calls["read"].raw_result)["structuredContent"]
    assert read["inherited"] and read["plan"] == c.facts["original"] and not read["submission_accepted"]
    assert calls["premature"].outcome == "failed"
    assert calls["trial"].outcome == calls["submit"].outcome == "succeeded"
    assert captured["trial"].inherited_reference_ids() == {"profile_001"}
    assert captured["trial"].allowed_rework_targets == ["plan.openings:D1"]
    assert c.source.verify_unchanged()


@pytest.mark.parametrize("product", ["receipt", "plan", "numeric", "reading_product", "profile", "image", "artifact"])
def test_source_tamper_rejects_before_any_target_import(continuation, product):
    c = continuation
    receipt = c.facts["passed"]
    paths = {"receipt": c.facts["workspace"] / receipt["receipt_file"],
             "plan": c.facts["workspace"] / receipt["input_plan_file"],
             "numeric": c.facts["workspace"] / receipt["compiled_numeric_plan_file"],
             "reading_product": c.facts["workspace"] / receipt["reading_product_file"],
             "profile": c.facts["workspace"] / receipt["measurement_profiles"][0]["file"],
             "image": c.source.directory / "bim/images/north.png",
             "artifact": c.source.directory / "tasks" / hashlib.sha256(b"e_north").hexdigest() / "reader_artifact.json"}
    path = paths[product]
    if product == "receipt":
        altered = json.loads(path.read_bytes())
        altered["topology_issues"] = [{"issue_id": "altered"}]
        path.write_bytes(json_bytes(altered))
    else:
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        domain.import_reader_evidence(c.source, c.target)
    assert not c.target.registry.records
    assert not (c.target.store.directory / domain.MANIFEST).exists()
    assert not (c.target.store.directory / domain.PENDING).exists()


def test_destination_tamper_and_partial_import_are_rejected(continuation):
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    path = c.target.registry.child("prior").task_directory / "bim/trial_workspace" / c.facts["passed"]["receipt_file"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        domain.import_reader_evidence(c.source, c.target)
    with pytest.raises(ValueError, match="changed"):
        c.target._recoverable_plan_trial_event("prior")
    (c.target.store.directory / domain.MANIFEST).unlink()
    c.target.store.write_json(domain.PENDING, {"interrupted": True})
    with pytest.raises(ValueError, match="incomplete"):
        domain.import_reader_evidence(c.source, c.target)


def test_unknown_write_does_not_import_an_earlier_passed_draft(continuation):
    c = continuation
    from src.harness_contracts import MissingCapture
    with EventStore(c.source.directory, run_id="old", task_id="coordinator",
                    budget_limit=c.source.budget_limit) as store:
        child = store.for_task("prior", parent_task_id="coordinator")
        child.append(ToolExecutionPayload(call_id="unknown", tool_name="trial_plan_bim", full_arguments={},
            raw_result=MissingCapture(reason="unknown write"), shown_result=MissingCapture(reason="unknown write"),
            repeatability="non_idempotent_write", operation_key="unknown", outcome="unknown"))
    summary = domain.import_reader_evidence(ReadOnlyRun(c.source.directory), c.target)
    assert not summary["editable_drafts"]
    assert len(summary["completed_ids"]) == 4
    assert "unknown" in summary["skipped"][0]["latest_trial"]["reason"]


@pytest.mark.parametrize("scope", [{"target": "F2"}, {"image": "f2.png"},
                                  {"role_id": "elevation_reader", "target": "North/F1"}])
def test_imported_rework_cannot_change_admitted_scope(continuation, scope):
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    with pytest.raises(ValueError, match="same|image|role|target"):
        c.target._task(dispatch("next", role_id="plan_reader", target="F1", previous_task_id="prior",
                               issues=["Continue"], rework_targets=["plan.openings:D1"]) | scope)
    assert not c.target.store.all_events


def test_import_keeps_required_ancestor_identity_without_old_execution(continuation):
    c = continuation
    with EventStore(c.source.directory, run_id="old", task_id="coordinator",
                    budget_limit=c.source.budget_limit) as store:
        old = _session(store, c.source.directory / "bim")
        ancestor = old._task(dispatch("ancestor", role_id="plan_reader", target="F1"))
        old.registry.save(ancestor, status="failed", reason="no usable declaration")
        task = {**old.registry.task("prior"), "previous_task_id": "ancestor"}
        old.registry.child("prior").write_json("reader_task.json", task)
        old.registry.save(task, status="failed", reason="service_error")
    domain.import_reader_evidence(ReadOnlyRun(c.source.directory), c.target)
    assert c.target.registry.task("prior")["previous_task_id"] == "ancestor"
    assert c.target.registry.task("ancestor") == ancestor
    assert not c.target.registry.child("ancestor").events
    assert c.target.registry.records["ancestor"]["artifact"] is None
    assert c.target._recoverable_plan_trial_event("ancestor") is None
    assert c.target._failure_handoff("prior")["editable_draft"]["status"] == "passed"


def test_completed_record_without_submission_is_not_imported(continuation):
    c = continuation
    path = c.source.directory / "tasks" / hashlib.sha256(b"e_north").hexdigest() / "bim/reader_submission.json"
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        domain.import_reader_evidence(c.source, c.target)
    assert not c.target.registry.records


def _compat_fixture(tmp_path, *, change="src/agent/runtime_roles/session.py", target="domain-fixture-b"):
    from src.agent_runtime.agent_registry import discover_files, REGISTRY_RELATIVE_PATH
    root = tmp_path / "repo"
    scope = {"domain": ["src/agent"], "runtime": []}
    for name in ("src/agent/geometry.py", "src/agent/runtime_roles/session.py", "src/agent/version_fingerprints.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old", encoding="utf-8")
    old = {"files": discover_files(root, "domain", scope=scope, registry_path=root / REGISTRY_RELATIVE_PATH)}
    path = root / change
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new", encoding="utf-8")
    new = {"files": discover_files(root, "domain", scope=scope, registry_path=root / REGISTRY_RELATIVE_PATH)}
    registry = {"schema_version": "agent-version-registry.v2", "current_domain_version": target,
                "domain_versions": {"domain-fixture-a": old, target: new}, "scope": scope}
    path = root / REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(registry))
    body = {"version_id": "domain-fixture-a", **old}
    ref = {"uri": "blobs/version", "sha256": hashlib.sha256(json_bytes(body)).hexdigest(), "media_type": "application/json"}
    source = SimpleNamespace(verified_versions=lambda: {"mode": "role_division", "domain_version": {
        "identifier": "domain-fixture-a", "evidence": {"blob": ref}}}, get_bytes=lambda _: json_bytes(body))
    return source, root


@pytest.mark.parametrize("change", ["src/agent/runtime_roles/session.py", "src/agent/runtime_roles/continuation.py",
                                    "src/agent/version_fingerprints.py"])
def test_reader_continuation_rejects_changed_domain_even_for_runtime_glue(tmp_path, change):
    source, root = _compat_fixture(tmp_path, change=change)
    with pytest.raises(ValueError, match="same domain version"):
        domain.verify_domain_compatibility(source, root)


@pytest.mark.parametrize(("change", "target"), [("src/agent/geometry.py", "domain-fixture-b"),
    ("src/agent/runtime_roles/unreviewed.py", "domain-fixture-b"),
    ("src/agent/runtime_roles/session.py", "domain-fixture-c")])
def test_geometry_new_files_and_unknown_versions_are_not_compatible(tmp_path, change, target):
    source, root = _compat_fixture(tmp_path, change=change, target=target)
    with pytest.raises(ValueError, match="same domain version"):
        domain.verify_domain_compatibility(source, root)


def test_same_version_requires_exact_registered_source_and_current_files(tmp_path):
    from src.agent_runtime.agent_registry import REGISTRY_RELATIVE_PATH
    source, root = _compat_fixture(tmp_path)
    path = root / REGISTRY_RELATIVE_PATH
    registry = json.loads(path.read_bytes())
    old = registry["domain_versions"]["domain-fixture-a"]
    registry["current_domain_version"] = "domain-fixture-a"
    path.write_bytes(json_bytes(registry))
    (root / "src/agent/runtime_roles/session.py").write_text("old", encoding="utf-8")
    assert domain.verify_domain_compatibility(source, root)["rule"] == "identical_release"
    (root / "src/agent/geometry.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="registered release"):
        domain.verify_domain_compatibility(source, root)


def test_source_version_evidence_cannot_claim_another_registered_snapshot(tmp_path):
    source, root = _compat_fixture(tmp_path)
    source.get_bytes = lambda _: json_bytes({"version_id": "domain-fixture-a", "files": {}})
    with pytest.raises(ValueError, match="immutable registered"):
        domain.verify_domain_compatibility(source, root)


def _anchor_imported_manifest(session):
    """A real offline Runtime request binds the source import, as entry does."""
    from src.agent_runtime.loop import Runtime
    from src.harness_contracts import VersionStamp
    from test_agent_runtime import Tools, role
    route = {"reader_continuation": {"manifest_sha256": hashlib.sha256(
        (session.store.directory / domain.MANIFEST).read_bytes()).hexdigest()}}
    version = versions().model_copy(update={"model_route": VersionStamp(
        identifier="fixture-continuation", evidence=session.store.source("fixture-route", route))})
    session.store.write_json("versions.json", version.model_dump(mode="json"))
    engine = Runtime(store=session.store, adapter=ScriptedAdapter([response(text="Offline fixture terminal.")]),
        tools=Tools(session.store.directory / "fixture-tools"), role=role(session.limits),
        model="test", parameters={"max_tokens": 256}, versions=version, limits=session.limits)
    asyncio.run(engine.run([{"role": "user", "content": "Offline fixture only."}]))


@pytest.mark.parametrize("submitted", [False, True])
def test_second_continuation_preserves_deliveries_and_prefers_native_plan_submission(continuation, monkeypatch, submitted):
    from src.harness_contracts import RunLifecyclePayload
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    if submitted:
        _isolated_recovery_trials(monkeypatch, {})
        adapter = ScriptedAdapter([
            response(("trial", "trial_plan_bim", {"plan": c.facts["original"]})),
            response(("submit", "submit_plan_reading", {"local_review": REVIEW, "trial_id": "latest"})),
            response(text="Submission acknowledged."),
        ])
        c.target.adapter_factory = lambda *args: adapter
        result = asyncio.run(c.target.delegate_many([dispatch("submitted_plan", role_id="plan_reader", target="F1",
            previous_task_id="prior", issues=["Continue saved draft"], rework_targets=["plan.openings:D1"])]))
        assert result["results"][0]["status"] == "completed", result
    _anchor_imported_manifest(c.target)
    source = ReadOnlyRun(c.target.store.directory)
    third = c.target.store.directory.parent / "third"
    run = _images(third, source.directory)
    with EventStore(third, run_id="third", task_id="coordinator", budget_limit=source.budget_limit) as store:
        session = _session(store, run)
        summary = domain.import_reader_evidence(source, session)
        assert len(summary["completed_ids"]) == (5 if submitted else 4)
        assert not store.all_events
        if submitted:
            assert not summary["editable_drafts"]
            assert "editable_draft" not in session._failure_handoff("prior")
        else:
            assert session._failure_handoff("prior")["editable_draft"]["status"] == "passed"
        body = domain._manifest(session)
        assert body["deliveries"]["e_north"]["submission"]["origin"] == "imported_submission"
        if submitted:
            assert session.registry.read("submitted_plan") == c.target.registry.read("submitted_plan")
            assert body["deliveries"]["submitted_plan"]["submission"]["origin"] == "native_submission"
        assert source.verify_unchanged() and c.source.verify_unchanged()


def test_second_continuation_rejects_broken_parent_manifest_proof(continuation):
    from src.harness_contracts import RunLifecyclePayload
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    _anchor_imported_manifest(c.target)
    path = c.target.store.directory / domain.MANIFEST
    value = json.loads(path.read_bytes())
    value["deliveries"]["e_north"]["submission"]["origin_event_id"] = "forged"
    path.write_bytes(json_bytes(value))
    source = ReadOnlyRun(c.target.store.directory)
    third = c.target.store.directory.parent / "third"
    run = _images(third, source.directory)
    with EventStore(third, run_id="third", task_id="coordinator", budget_limit=source.budget_limit) as store:
        session = _session(store, run)
        with pytest.raises(ValueError, match="manifest hash"):
            domain.import_reader_evidence(source, session)
        assert not session.registry.records and not (third / domain.PENDING).exists()


def test_second_continuation_rejects_rehashed_manifest_with_unchanged_approved_journal(continuation):
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    _anchor_imported_manifest(c.target)
    path = c.target.store.directory / domain.MANIFEST
    before = hashlib.sha256((c.target.store.directory / "events.jsonl").read_bytes()).hexdigest()
    value = json.loads(path.read_bytes())
    value.pop("manifest_blob")
    value["deliveries"]["e_north"]["submission"]["origin_event_id"] = "nonexistent-event"
    value["manifest_blob"] = c.target.store.put_json(value).model_dump(mode="json")
    path.write_bytes(json_bytes(value))
    assert hashlib.sha256((c.target.store.directory / "events.jsonl").read_bytes()).hexdigest() == before
    source = ReadOnlyRun(c.target.store.directory)
    with pytest.raises(ValueError, match="durable root route anchor"):
        domain._manifest(SimpleNamespace(store=source))


def test_unexecuted_import_is_not_a_durable_parent_run(continuation):
    from src.harness_contracts import RunLifecyclePayload
    c = continuation
    domain.import_reader_evidence(c.source, c.target)
    c.target.store.write_json("versions.json", versions().model_dump(mode="json"))
    c.target.store.append(RunLifecyclePayload(action="stop", reason="service_error"))
    source = ReadOnlyRun(c.target.store.directory)
    with pytest.raises(ValueError, match="durable root request"):
        domain._manifest(SimpleNamespace(store=source))


@pytest.fixture(params=[False, True], ids=["passed-unsubmitted-2d", "completed-2d"])
def planar_continuation(tmp_path, monkeypatch, request):
    """Real 2D compilation and submission, with no provider or source-BIM fixture."""
    from src.agent.runtime_roles.trial import PlanTrial
    from tests.test_role_rework_handoff import _append_trial

    submitted = request.param
    monkeypatch.setattr(domain, "verify_domain_compatibility", lambda *args: {
        "source_domain": "fixture-2d", "destination_domain": "fixture-2d", "changed_files": []})
    source_path = tmp_path / "source-2d"
    source_run = _images(source_path)
    limits = RunLimits(model_calls=30, tool_calls=50, seconds=120, tokens=500_000)
    with EventStore(source_path, run_id="old-2d", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        session = _session(store, source_run)
        task = session._task(dispatch("planar", role_id="plan_reader", target="F1"))
        child = session.registry.admit(task)
        workspace = child.task_directory / "bim/trial_workspace"
        (workspace / "images").mkdir(parents=True)
        for relative in ("inputs.json", "images/north.png"):
            (workspace / relative).write_bytes((source_run / relative).read_bytes())
        plan = {
            "reading_type": "plan", "floor_id": "F1",
            "x_anchors": [[0, 0], [99, 9.9]], "y_anchors": [[0, 9.9], [99, 0]],
            "footprint_pixels": [[10, 10], [90, 10], [90, 90], [10, 90]],
            "partitions": [{"id": "P1", "points": [[50, 10], [50, 90]]}],
            "openings": [
                {"id": "D1", "kind": "door", "p1": [50, 40], "p2": [50, 50]},
                {"id": "W1", "kind": "window", "p1": [20, 10], "p2": [30, 10]},
            ],
            "space_seeds": [{"id": "left", "point": [20, 30]}, {"id": "right", "point": [70, 30]}],
            "basis": "Offline planar continuation fixture; no independent quality claim.",
            "assumptions": [], "unresolved": [],
        }
        plan["wall_metadata"] = [
            {"target": target, "thickness_m": .1, "reference_side": "centerline",
             "evidence_status": "inferred", "basis": "Synthetic continuation fixture assumption",
             "source_refs": ["north.png:fixture"]}
            for target in ("footprint:*", "partition:*")
        ]
        trial = PlanTrial(None, image_name="north.png", workspace=workspace,
                          receipt_directory=workspace / "trial_receipts")
        receipt = asyncio.run(trial.run(plan))
        assert receipt["status"] == "passed", receipt
        assert receipt["reading_geometry_ready"] and not receipt["source_geometry_ready"]
        _append_trial(child, receipt)
        artifact = None
        if submitted:
            submission = ReaderSubmission(role_id="plan_reader", image_name="north.png", trial=trial,
                directory=child.task_directory / "bim", target="F1")
            arguments = {"trial_id": "latest", "local_review": REVIEW}
            accepted = submission.submit(arguments)
            captured = child.capture(envelope(accepted))
            child.append(ToolExecutionPayload(call_id="submit-planar", tool_name="submit_plan_reading",
                full_arguments=arguments, raw_result=captured, shown_result=captured,
                repeatability="non_idempotent_write", operation_key="submit-planar", outcome="succeeded",
                applied_write_id="submit-planar"))
            formal = submission.read()
            artifact = formal["artifact"]
            session.registry.save(task, status="completed", artifact=artifact, validation=formal["validation"])
        else:
            session.registry.save(task, status="failed", reason="offline interrupted before submission")
        store.write_json("versions.json", {"mode": "role_division"})
    source = ReadOnlyRun(source_path)
    target_path = tmp_path / "target-2d"
    target_run = _images(target_path, source_path)
    with EventStore(target_path, run_id="new-2d", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        yield SimpleNamespace(source=source, target=_session(store, target_run), workspace=workspace,
                              receipt=receipt, submitted=submitted, task=task, artifact=artifact)


def test_planar_continuation_copies_verified_reading_product_and_preserves_submission_boundary(planar_continuation):
    from src.agent.runtime_roles.plan_reading import product_identity
    from src.agent.runtime_roles.trial import PlanTrial

    c = planar_continuation
    product_file, product_sha = product_identity(c.receipt)
    assert product_file.endswith("/reading.json")
    original_product = (c.workspace / product_file).read_bytes()
    summary = domain.import_reader_evidence(c.source, c.target)
    assert summary["completed_ids"] == (["planar"] if c.submitted else [])
    assert [row["task_id"] for row in summary["editable_drafts"]] == ([] if c.submitted else ["planar"])
    workspace = c.target.registry.child("planar").task_directory / "bim/trial_workspace"
    copied = workspace / product_file
    assert copied.read_bytes() == original_product
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == product_sha
    assert not list(c.source.directory.rglob("source_model.json"))
    assert not list(c.target.store.directory.rglob("source_model.json"))
    manifest = domain._manifest(c.target)
    assert manifest["files"][copied.relative_to(c.target.store.directory).as_posix()] == product_sha
    assert manifest["source_files"][(c.workspace / product_file).relative_to(c.source.directory).as_posix()] == product_sha
    restored = PlanTrial(None, image_name="north.png", workspace=workspace,
                         receipt_directory=workspace / "trial_receipts")
    restored_plan, verified = restored.verified_plan(c.receipt["plan_sha256"])
    assert product_identity(verified) == (product_file, product_sha)
    assert restored_plan["reading_type"] == "plan" and "z_floor" not in restored_plan
    if c.submitted:
        assert c.target.registry.read("planar") == c.artifact
        formal = ReaderSubmission(role_id="plan_reader", image_name="north.png", trial=restored,
            directory=c.target.registry.child("planar").task_directory / "bim", target="F1").read()
        assert formal["artifact"] == c.artifact
    else:
        handoff = c.target._failure_handoff("planar")["editable_draft"]
        assert handoff["status"] == "passed" and handoff["submission_accepted"] is False
        assert handoff["reading_product_file"] == product_file
        assert handoff["reading_product_sha256"] == product_sha
        assert c.target.registry.records["planar"]["artifact"] is None
        assert not (workspace.parent / "reader_submission.json").exists()
    assert summary == domain.import_reader_evidence(c.source, c.target)
    assert not c.target.store.all_events
    assert c.source.verify_unchanged()


def test_planar_continuation_rejects_changed_product_before_destination_mutation(planar_continuation):
    from src.agent.runtime_roles.plan_reading import product_identity

    c = planar_continuation
    product_file, _ = product_identity(c.receipt)
    path = c.workspace / product_file
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="reading product changed|hash mismatch"):
        domain.import_reader_evidence(c.source, c.target)
    assert not c.target.registry.records
    assert not (c.target.store.directory / domain.MANIFEST).exists()
    assert not (c.target.store.directory / domain.PENDING).exists()


def test_planar_continuation_rejects_changed_imported_product(planar_continuation):
    from src.agent.runtime_roles.plan_reading import product_identity

    c = planar_continuation
    domain.import_reader_evidence(c.source, c.target)
    product_file, _ = product_identity(c.receipt)
    path = c.target.registry.child("planar").task_directory / "bim/trial_workspace" / product_file
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="imported reader evidence changed"):
        domain.import_reader_evidence(c.source, c.target)


def test_planar_continuation_product_cannot_escape_workspace(tmp_path):
    prior = SimpleNamespace(workspace=tmp_path / "workspace", image_name="plan.png")
    with pytest.raises(ValueError, match="escapes its workspace"):
        domain._draft_files(prior, {"status": "passed", "reading_product_file": "../reading.json",
                                   "reading_product_sha256": "a" * 64})
