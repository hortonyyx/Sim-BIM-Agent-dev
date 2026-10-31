"""Shared accounting, immutable references and recovery guards for D1."""

from reader_delivery_fixtures import compact_elevation, accept_elevation, elevation_responses

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from PIL import Image

from src.agent.runtime_roles.artifacts import ArtifactRegistry
from src.agent.runtime_roles.config import load_roles
from src.agent.runtime_roles.context_policy import effective_role_context
from src.agent.runtime_roles.feedback import reader_batch_reply
from src.agent.runtime_roles.readers import ELEVATION_READER_TOOL_NAMES
from src.agent.runtime_roles.elevation import merge_elevation_rework
from src.agent.runtime_roles.session import RoleSession, envelope
from src.agent_runtime.adapter import ScriptedAdapter
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from src.harness_contracts import MissingCapture, ToolExecutionPayload, ToolInvocationPayload

from test_agent_runtime import response, versions


ROOT = Path(__file__).resolve().parents[1]
ROUTES = load_roles({role: {"provider": "scripted", "model": "scripted-model",
    "reasoning_effort": "medium", "output_tokens": 4000}
    for role in ("coordinator", "plan_reader", "elevation_reader")}, allow_scripted=True)


def elevation():
    return {"orientation": "North", "view_direction": "South",
        "x_calibration": {"pixel_start": 0, "pixel_end": 100, "world_start_m": 10, "world_end_m": 0},
        "elevations": [{"id": "ground", "kind": "ground", "value_m": 0,
                        "evidence_type": "assumption", "bbox": [0, 0, 10, 10]}],
        "openings": [], "counts": [{"floor_id": "F1", "window_count": 0, "door_count": 0}],
        "unresolved": ["Scripted empty facade fixture, not drawing quality evidence."]}


def measured_elevation(*, eave=3.94, width=1.04, sill=1.02, head=2.24,
                       evidence_type="annotation"):
    return {"orientation": "North", "view_direction": "South",
        "x_calibration": {"pixel_start": 0, "pixel_end": 100,
                          "world_start_m": 10, "world_end_m": 0},
        "z_calibration": {"pixel_start": 0, "pixel_end": 100,
                          "world_start_m": 4, "world_end_m": 0},
        "elevations": [
            {"id": "ground", "kind": "ground", "value_m": 0,
             "evidence_type": "annotation", "bbox": [0, 90, 10, 99]},
            {"id": "eave", "kind": "eave", "value_m": eave,
             "evidence_type": "annotation", "bbox": [0, 1, 10, 5]},
        ],
        "openings": [{"id": "W1", "floor_id": "F1", "kind": "window",
            "x_px": [20, 30], "width_m": width, "sill_m": sill, "head_m": head,
            "evidence_type": evidence_type, "bbox": [15, 40, 35, 90]}],
        "counts": [{"floor_id": "F1", "window_count": 1, "door_count": 0}],
        "unresolved": []}


def dispatch(identity="north", **extra):
    return {"task_id": identity, "role_id": "elevation_reader", "image": "north.png",
            "target": "North/F1", "instructions": "Report this one facade.", **extra}


def submit_elevation(call_id="submit"):
    return response((call_id, "trial_elevation_reading", compact_elevation(elevation())))


def delivered_adapter():
    return ScriptedAdapter([submit_elevation(), accept_elevation])


class Frozen:
    def __init__(self, run):
        self.run_directory = run
        self.calls = []

    @staticmethod
    @asynccontextmanager
    async def task_session(*, directory, images, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "images").mkdir(exist_ok=True)
        manifest = {"images": {}}
        for name, (raw, size) in images.items():
            path = directory / "images" / name
            if path.is_file():
                assert path.read_bytes() == raw
            else:
                path.write_bytes(raw)
            manifest["images"][name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": list(size)}
        (directory / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        yield Frozen(directory)

    async def list_tools(self):
        return [{"name": name, "description": "test read tool", "inputSchema": {"type": "object"}}
                for name in ELEVATION_READER_TOOL_NAMES]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return envelope({"candidate": "candidate_01", "value": arguments})

    def repeatability(self, name):
        return "read_only"

    def snapshot_state(self):
        return {}

    def artifacts(self):
        return []

    def image_origins(self, raw):
        return {}


@pytest.fixture
def environment(tmp_path, request, monkeypatch):
    # These protocol tests exercise scripted readers against an actively edited
    # worktree. Agent registry sealing has its own tests and must not prevent the
    # submit/ack state machine from running before the new version is registered.
    monkeypatch.setattr("src.agent.runtime_roles.session.make_versions",
                        lambda *args, **kwargs: versions())
    run = tmp_path / "bim"
    (run / "images").mkdir(parents=True)
    image = run / "images/north.png"
    Image.new("RGB", (100, 100), "white").save(image)
    manifest = {"images": {"north.png": {"sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}}
    (run / "inputs.json").write_text(json.dumps(manifest))
    limits = RunLimits(model_calls=getattr(request, "param", 10), tool_calls=10, seconds=120, tokens=200_000)
    with EventStore(tmp_path / "journal", run_id="roles", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        def make(factory=None, **kwargs):
            return RoleSession(store=store, frozen=Frozen(run), routes=ROUTES,
                adapter_factory=factory or (lambda *args: delivered_adapter()),
                limits=limits, root=ROOT, **kwargs)
        yield store, make


def test_completed_delivery_is_hashed_reusable_and_cannot_be_replaced(environment):
    store, make = environment
    session = make()
    first = asyncio.run(session.delegate_many([dispatch()]))["results"][0]
    assert first["status"] == "completed", first
    before = len(store.all_events)
    registry = ArtifactRegistry(store)
    assert registry.read("north")["image"] == "north.png"
    with pytest.raises(ValueError, match="hash"):
        registry.read("north", sha256="0" * 64)
    second = asyncio.run(make().delegate_many([dispatch()]))["results"][0]
    assert second["reused_saved_result"] and len(store.all_events) == before
    with pytest.raises(ValueError, match="cannot be overwritten"):
        registry.save(registry.task("north"), status="failed", reason="should preserve result")
    with pytest.raises(ValueError, match="different input"):
        asyncio.run(make().delegate_many([dispatch(instructions="Changed task")]))


def test_reader_runtime_receives_effective_context_limits_and_policy(environment):
    _, make = environment
    global_context = {"context_tokens": 220_000, "compact_at_tokens": 120_000,
                      "max_images": 4,
                      "max_image_bytes": 9_000_000}
    contexts = {name: effective_role_context(name, global_defaults=global_context)
                for name in ROUTES}
    contexts["elevation_reader"] = effective_role_context(
        "elevation_reader", global_defaults=global_context,
        role_overrides={"context_tokens": 180_000, "compact_at_tokens": 90_000,
                        "max_images": 2})
    seen = []

    def inspect(name, engine):
        if name == "before_request" and not seen:
            seen.append(engine._config())

    result = asyncio.run(make(role_contexts=contexts, reader_fault_hook=inspect)
                         .delegate_many([dispatch()]))
    assert result["results"][0]["status"] == "completed"
    assert seen[0]["limits"]["context_tokens"] == 180_000
    assert seen[0]["context_policy"]["compact_at_tokens"] == 90_000
    assert seen[0]["context_policy"]["max_images"] == 2
    assert seen[0]["context_policy"]["max_image_bytes"] == 9_000_000


def test_artifact_file_tampering_is_rejected_on_reopen(environment):
    store, make = environment
    result = asyncio.run(make().delegate_many([dispatch()]))["results"][0]
    path = store.directory / result["artifact"]["path"]
    path.write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        ArtifactRegistry(store)


def test_prepare_review_submit_ends_without_ack_in_same_role_and_root_budget(environment):
    store, make = environment
    session = make(lambda *args: delivered_adapter())
    row = asyncio.run(session.delegate_many([dispatch()]))["results"][0]
    assert row["status"] == "completed", row
    usage = session.state()["usage"]
    role = usage["by_role"]["elevation_reader"]
    assert usage["requests"] == role["requests"] == 2
    assert role["format_errors"] == role["repair_requests"] == role["repair_successes"] == 0
    assert role["first_pass_rate"] == 1 and role["after_repair_rate"] == 1
    assert sum(r["requests"] for r in usage["by_role"].values()) == usage["requests"]
    assert role["request_seconds"] >= 0


def test_answer_text_without_submit_stops_after_bounded_domain_continuations(environment):
    store, make = environment
    session = make(lambda *args: ScriptedAdapter([
        response(text=json.dumps(elevation())), response(text=json.dumps(elevation())), response(text=json.dumps(elevation())),
    ]))
    row = asyncio.run(session.delegate_many([dispatch()]))["results"][0]
    assert row["status"] == "failed" and row["artifact"] is None
    assert session.state()["usage"]["requests"] == 3
    assert row["runtime_status"] == "domain_completion_unmet"
    role = session.state()["usage"]["by_role"]["elevation_reader"]
    assert role["format_errors"] == role["repair_failures"] == 0
    assert role["repair_incomplete"] == 0 and role["delivery_failures"]
    assert role["task_elapsed_seconds_sum"] > 0 and role["task_wall_span_seconds"] > 0


def test_rejected_submit_tool_can_be_fixed_and_resubmitted_without_answer_repair(environment):
    store, make = environment
    session = make(lambda *args: ScriptedAdapter([
        response(("bad-submit", "submit_elevation_reading", {})),
        submit_elevation("good-submit"),
        accept_elevation,
    ]))
    row = asyncio.run(session.delegate_many([dispatch()]))["results"][0]
    assert row["status"] == "completed", row
    executions = [
        event.payload for event in store.all_events
        if event.payload.event_type == "tool_execution"
        and event.payload.tool_name == "submit_elevation_reading"
    ]
    assert [event.outcome for event in executions] == ["failed", "succeeded"]
    role = session.state()["usage"]["by_role"]["elevation_reader"]
    assert role["requests"] == 3
    assert role["format_errors"] == role["repair_requests"] == 0
    assert role["submission_attempts"] == 2 and role["submission_rejections"] == 1
    assert role["submission_repaired_tasks"] == 1
    assert role["first_pass_rate"] == 0 and role["after_repair_rate"] == 1
    assert session.registry.read("north")["orientation"] == "North"


@pytest.mark.parametrize("environment", [2], indirect=True)
def test_submit_on_last_model_request_is_still_a_completed_delivery(environment):
    store, make = environment
    session = make(lambda *args: ScriptedAdapter([submit_elevation(), accept_elevation]))
    row = asyncio.run(session.delegate_many([dispatch()]))["results"][0]
    assert row["status"] == "completed", row
    assert row["runtime_status"] == "completed"
    assert session.registry.read("north")["orientation"] == "North"
    assert sum(event.payload.event_type == "adapter_request" for event in store.all_events) == 2


def test_single_image_is_validated_for_entire_batch_before_any_model_call(environment):
    store, make = environment
    with pytest.raises(ValueError, match="original filename"):
        asyncio.run(make().delegate_many([dispatch(), dispatch("other", image="../secret.png")]))
    assert not store.all_events


def test_rework_has_new_identity_and_cannot_switch_original(environment):
    store, make = environment
    session = make()
    asyncio.run(session.delegate_many([dispatch()]))
    result = asyncio.run(session.delegate_many([dispatch("north-r2", previous_task_id="north",
        issues=["Check the unresolved edge"],
        rework_targets=["elevation.elevations:ground.value_m"])]))["results"][0]
    assert result["status"] == "completed"
    previous = session.registry.task("north-r2")["previous_artifact"]
    assert previous == session.registry.records["north"]["artifact"]
    handoff = session.registry.task("north-r2")["previous_task_handoff"]
    assert handoff["status"] == "completed" and handoff["tool_failures"] == []
    assert "earlier corrected failures are not current rework evidence" in handoff["instruction"]
    with pytest.raises(ValueError):
        asyncio.run(session.delegate_many([dispatch("north-r3", previous_task_id="north", target="wrong",
            issues=["wrong facade"],
            rework_targets=["elevation.elevations:ground.value_m"])]))


def test_elevation_rework_preserves_prior_originals_and_only_updates_target(environment):
    store, make = environment
    adapters = {}
    submissions = {
        "north": measured_elevation(),
        "north-r2": measured_elevation(
            eave=3.9, width=1.0, sill=1.0, head=2.36,
            evidence_type="visual_estimate",
        ),
    }

    def factory(task_id, *_):
        adapter = ScriptedAdapter([
            response((f"submit-{task_id}", "trial_elevation_reading", compact_elevation(submissions[task_id]))),
            accept_elevation,
        ])
        adapters[task_id] = adapter
        return adapter

    session = make(factory)
    first = asyncio.run(session.delegate_many([dispatch()]))["results"][0]
    assert first["status"] == "completed"
    second = asyncio.run(session.delegate_many([dispatch(
        "north-r2", previous_task_id="north", issues=["correct W1 head and evidence"],
        rework_targets=["elevation.openings:W1"],
    )]))["results"][0]
    assert second["status"] == "completed", second
    messages = json.loads(adapters["north-r2"].requests[0])["messages"]
    context = json.loads(next(row for row in messages if row["role"] == "user")["content"][0]["text"])
    projected = context["previous_artifact"]
    assert "regularization" not in projected and "ink_alignment" not in projected
    assert projected["context_audit"]["registry_reference"] == first["artifact"]
    assert projected["openings"] == session.registry.read("north")["openings"]
    artifact = session.registry.read("north-r2")
    readings = {(row["item_id"], row["field"]): row
                for row in artifact["regularization"]["readings"]}
    assert readings[("eave", "value_m")]["original_m"] == pytest.approx(3.94)
    assert readings[("eave", "value_m")]["adopted_m"] == pytest.approx(3.9)
    assert readings[("W1", "width_m")]["original_m"] == pytest.approx(1.04)
    assert readings[("W1", "width_m")]["evidence_type"] == "visual_estimate"
    assert readings[("W1", "head_m")]["original_m"] == pytest.approx(2.36)
    assert readings[("W1", "head_m")]["adopted_m"] == pytest.approx(2.4)
    assert readings[("W1", "head_m")]["evidence_type"] == "visual_estimate"

    protected_change = measured_elevation(eave=3.9, width=1.16, sill=1.0, head=2.36)
    with pytest.raises(ValueError, match="protected reading.*width_m"):
        merge_elevation_rework(
            session.registry.read("north"), protected_change,
            ["elevation.openings:W1.head_m"],
        )


def test_completed_elevation_rework_requires_explicit_targets(environment):
    store, make = environment
    session = make()
    asyncio.run(session.delegate_many([dispatch()]))
    with pytest.raises(ValueError, match="elevation_reader rework needs explicit rework_targets"):
        asyncio.run(session.delegate_many([
            dispatch("north-r2", previous_task_id="north", issues=["check one height"])
        ]))


def test_failed_rework_automatically_carries_located_tool_evidence_without_issues(environment):
    store, make = environment
    session = make()
    previous = session._task({
        "task_id": "plan-old", "role_id": "plan_reader", "image": "north.png",
        "target": "F1", "origin": "Shared origin",
    })
    child = session.registry.admit(previous)
    raw = envelope({
        "status": "failed",
        "reason": "opening W1 has no full host",
        "repair_hint": {"path": "plan.openings[0]", "note": "Move W1 onto its observed wall.",
                        "current": [0.0, 3.4], "allowed_floor_bounds_m": [0.0, 3.0],
                        "junction_repairs_remaining": 2},
        "changes_remaining": 4,
        "next_action": {"objects": ["plan.openings[0]"], "instruction": "Correct W1 and retry."},
        "receipt_file": "trial_receipts/trial_003.json",
    }, error=True)
    child.append(ToolExecutionPayload(
        call_id="trial-3", tool_name="trial_plan_bim", full_arguments={"plan": {}},
        raw_result=child.capture(raw, force_blob=True), shown_result=child.capture(raw, force_blob=True),
        repeatability="non_idempotent_write", operation_key="trial-3", outcome="failed",
    ))
    session.registry.save(previous, status="failed", reason="incomplete_response",
                          runtime={"status": "incomplete_response"})
    seen = []

    async def offline_reader(task):
        seen.append(task)
        return session.registry.save(task, status="failed", reason="offline behavior check")

    session.run_reader = offline_reader
    asyncio.run(session.delegate_many([{
        "task_id": "plan-rework", "role_id": "plan_reader", "image": "north.png",
        "target": "F1", "origin": "Shared origin", "previous_task_id": "plan-old",
    }]))
    handoff = seen[0]["previous_task_handoff"]
    assert handoff["previous_task_id"] == "plan-old"
    assert handoff["reason"] == "incomplete_response"
    failure = handoff["tool_failures"][0]
    assert failure["feedback"]["repair_hint"]["path"] == "plan.openings[0]"
    assert failure["feedback"]["repair_hint"]["current"] == [0.0, 3.4]
    assert failure["feedback"]["repair_hint"]["allowed_floor_bounds_m"] == [0.0, 3.0]
    assert failure["feedback"]["repair_hint"]["junction_repairs_remaining"] == 2
    assert failure["feedback"]["changes_remaining"] == 4
    assert failure["feedback"]["next_action"]["objects"] == ["plan.openings[0]"]
    assert failure["evidence"]["event_id"]
    assert failure["evidence"]["receipt_file"] == "trial_receipts/trial_003.json"
    assert handoff["complete_record"]["sha256"]


def test_unknown_latest_trial_does_not_import_orphan_receipt_or_older_failure(environment):
    from src.agent.runtime_roles.trial import PlanTrial
    from tests.test_role_trial import Tools, example

    store, make = environment
    session = make()
    previous = session._task({
        "task_id": "plan-orphan", "role_id": "plan_reader", "image": "north.png",
        "target": "F1", "origin": "Shared origin",
    })
    child = session.registry.admit(previous)
    workspace = child.task_directory / "bim/trial_workspace"
    (workspace / "images").mkdir(parents=True)
    source = session.run_directory / "images/north.png"
    (workspace / "images/north.png").write_bytes(source.read_bytes())
    (workspace / "inputs.json").write_text(json.dumps({
        "images": {"north.png": {
            "sha256": previous["input_sha256"], "size": [100, 100],
        }},
    }), encoding="utf-8", newline="\n")
    trial = PlanTrial(
        Tools(), image_name="north.png", workspace=workspace,
        receipt_directory=workspace / "trial_receipts",
    )
    invalid = example()
    invalid["openings"][0].update(p1=[4, 3], p2=[4, 4])
    first = asyncio.run(trial.call(invalid))
    assert first["structuredContent"]["status"] == "failed"
    compact_first = envelope({key: trial.receipts[-1][key] for key in (
        "status", "receipt_file", "plan_sha256", "compiled_numeric_plan_sha256",
    )}, error=True)
    inline = child.capture(compact_first)
    assert inline.kind == "inline"
    known = child.append(ToolExecutionPayload(
        call_id="trial-known", tool_name="trial_plan_bim", full_arguments={"plan": {}},
        raw_result=inline, shown_result=inline,
        repeatability="non_idempotent_write", operation_key="trial-known", outcome="failed",
    ))
    session.registry.save(previous, status="running")
    known_handoff = session._failure_handoff("plan-orphan")
    assert known_handoff["editable_draft"]["event_id"] == known.event_id
    assert known_handoff["editable_draft"]["plan_sha256"] == first["structuredContent"]["plan_sha256"]
    invocation = child.append(ToolInvocationPayload(
        call_id="trial-unknown", tool_name="trial_plan_bim", full_arguments={"plan": {}},
        repeatability="non_idempotent_write", operation_key="trial-unknown",
    ))
    second_plan = json.loads(json.dumps(invalid))
    second_plan["openings"][0]["z"] = [0, 2.2]
    second = asyncio.run(trial.call(second_plan))
    assert second["structuredContent"]["receipt_file"] == "trial_receipts/trial_002.json"
    interrupted_handoff = session._failure_handoff("plan-orphan")
    assert "editable_draft" not in interrupted_handoff
    assert interrupted_handoff["draft_recovery"]["event_id"] == invocation.event_id
    assert "no durable execution outcome" in interrupted_handoff["draft_recovery"]["reason"]
    unknown = child.append(ToolExecutionPayload(
        call_id="trial-unknown", tool_name="trial_plan_bim", full_arguments={"plan": {}},
        raw_result=MissingCapture(reason="interrupted after the receipt write"),
        shown_result=MissingCapture(reason="no result was presented"),
        repeatability="non_idempotent_write", operation_key="trial-unknown", outcome="unknown",
        invocation_event_id=invocation.event_id,
    ))
    assert known.event_id != unknown.event_id
    assert (workspace / "trial_receipts/trial_002.json").is_file()
    session.registry.save(previous, status="failed", reason="unknown_write_outcome",
                          runtime={"status": "unknown_write_outcome"})

    handoff = session._failure_handoff("plan-orphan")
    assert "editable_draft" not in handoff
    assert handoff["draft_recovery"]["status"] == "unavailable"
    assert handoff["draft_recovery"]["event_id"] == unknown.event_id
    assert "workspace-only receipts" in handoff["draft_recovery"]["reason"]
    admitted = session._task({
        "task_id": "plan-after-orphan", "role_id": "plan_reader", "image": "north.png",
        "target": "F1", "origin": "Shared origin", "previous_task_id": "plan-orphan",
    })
    assert "editable_draft" not in admitted["previous_task_handoff"]


@pytest.mark.parametrize("environment", [2], indirect=True)
def test_shared_request_limit_counts_all_concurrent_tasks(environment):
    store, make = environment
    session = make(max_concurrent_readers=4)
    assert store.budget_limit.calls == session.limits.model_calls == 2
    concurrent = 0
    maximum = 0
    class Slow(ScriptedAdapter):
        async def send(self, request, *, timeout):
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            try:
                await asyncio.sleep(0.03)
                return await super().send(request, timeout=timeout)
            finally:
                concurrent -= 1
    session.adapter_factory = lambda *args: Slow([submit_elevation()])
    rows = asyncio.run(session.delegate_many([dispatch(f"reader-{i}") for i in range(4)]))["results"]
    assert maximum == 2
    assert sum(row["status"] == "completed" for row in rows) == 0, rows
    # The two prepare calls exhaust the shared budget before either review; no false delivery.
    assert session.state()["usage"]["requests"] == 2


def test_readers_are_bounded_by_configured_concurrency(environment):
    _, make = environment
    concurrent = 0
    maximum = 0
    class Slow(ScriptedAdapter):
        async def send(self, request, *, timeout):
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            try:
                await asyncio.sleep(0.02)
                return await super().send(request, timeout=timeout)
            finally:
                concurrent -= 1
    session = make(lambda *args: Slow([
        submit_elevation(), accept_elevation,
    ]), max_concurrent_readers=1)
    rows = asyncio.run(session.delegate_many([dispatch(f"reader-{i}") for i in range(3)]))["results"]
    assert maximum == 1 and all(row["status"] == "completed" for row in rows)


def saved_plan_reading(session, task, *, selected_profile=False):
    """Save a real, image-bound 2D reading for assembly protocol tests."""
    from src.agent.runtime_roles.trial import PlanTrial
    from tests.test_role_trial import Tools, example

    workspace = session.registry.child(task["task_id"]).task_directory / "bim/trial_workspace"
    image = workspace / "images" / task["image"]
    image.parent.mkdir(parents=True)
    image.write_bytes((session.run_directory / "images" / task["image"]).read_bytes())
    with Image.open(image) as decoded:
        size = list(decoded.size)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (workspace / "inputs.json").write_text(json.dumps({"images": {task["image"]: {
        "sha256": digest, "size": size}}}), encoding="utf-8")
    original = example()
    profiles = workspace / "profiles"
    if selected_profile:
        profiles.mkdir()
        pixel = original["x_anchors"][0][0]
        (profiles / "profile_001.json").write_text(json.dumps({
            "name": task["image"], "image_sha256": digest, "axis": "x",
            "candidates": [{"id": "C01", "pixels": [pixel, pixel], "peak": pixel}],
        }), encoding="utf-8")
        original["x_anchors"][0][0] = {"profile": "profile_001", "candidate": "C01"}
    trial = PlanTrial(Tools(), image_name=task["image"], workspace=workspace,
                      receipt_directory=workspace / "trial_receipts", profile_directory=profiles)
    receipt = asyncio.run(trial.run(original))
    assert receipt["status"] == "passed", receipt
    numeric = trial.numeric_plan(receipt)
    row = session.registry.save(task, status="completed", artifact={"plan": numeric, "unresolved": []},
                                validation=trial.delivery_receipt(original))
    return row, original, numeric, workspace, receipt


def test_build_reference_expands_verified_plan_once_and_retains_original_audit(environment):
    store, make = environment
    session = make()
    task = session._task({**dispatch("plan"), "role_id": "plan_reader", "target": "F1"})
    row, original, numeric, workspace, receipt = saved_plan_reading(session, task)
    args = {"task_id": "plan", "sha256": row["artifact"]["sha256"]}
    first = asyncio.run(session.build_from_artifact(**args))
    second = asyncio.run(session.build_from_artifact(**args))
    assert first == second and len(session.frozen.calls) == 1
    expanded = json.loads(session.frozen.calls[0][1]["plan_json"])
    assert expanded == {key: value for key, value in numeric.items() if key != "reading_type"}
    assert json.loads((workspace / receipt["input_plan_file"]).read_bytes()) == original
    from src.harness_contracts import HashedBlobRef
    ref = first["structuredContent"]["role_application"]["expanded_parameters"]
    audit = json.loads(store.get_bytes(HashedBlobRef.model_validate(ref)))
    assert audit["reference"] == args and audit["tool"] == "build_plan_bim"
    assert json.loads(audit["arguments"]["plan_json"]) == expanded


def test_failed_trial_is_readable_but_cannot_build(environment):
    _, make = environment
    session = make()
    task = session._task({**dispatch("plan"), "role_id": "plan_reader", "target": "F1"})
    row = session.registry.save(task, status="completed", artifact={"plan": {}, "unresolved": ["invalid ring"]},
                               validation={"validation_passed": False, "reason": "invalid ring"})
    assert session.registry.read("plan")["unresolved"] == ["invalid ring"]
    with pytest.raises(ValueError, match="successful trial"):
        asyncio.run(session.build_from_artifact("plan", row["artifact"]["sha256"]))
    assert not session.frozen.calls


def test_unknown_write_intent_is_not_repeated(environment):
    store, make = environment
    session = make()
    identity = hashlib.sha256(b"test-write").hexdigest()
    store.write_json("role_operations/" + identity + ".json", {
        "operation_id": "test-write", "tool": "build_plan_bim", "arguments": {}, "reference": {}})
    with pytest.raises(ValueError, match="outcome unknown"):
        asyncio.run(session._once("test-write", "build_plan_bim", {}, reference={}))
    assert not session.frozen.calls


@pytest.mark.parametrize("field,value", [("validation", {"validation_passed": False}),
                                       ("role_id", "plan_reader"), ("target", "another floor")])
def test_completed_metadata_tampering_is_rejected(environment, field, value):
    store, make = environment
    session = make()
    asyncio.run(session.delegate_many([dispatch()]))
    path = session.registry.child("north").task_directory / "reader_record.json"
    record = json.loads(path.read_bytes())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="metadata hash"):
        ArtifactRegistry(store)


def test_admitted_instructions_are_bound_to_the_delivery(environment):
    store, make = environment
    session = make()
    asyncio.run(session.delegate_many([dispatch()]))
    path = session.registry.child("north").task_directory / "reader_task.json"
    task = json.loads(path.read_bytes())
    task["instructions"] = "silently changed after observation"
    path.write_text(json.dumps(task))
    with pytest.raises(ValueError, match="task definition changed"):
        ArtifactRegistry(store)


def test_missing_candidate_and_match_are_correctable_tool_errors(environment):
    _, make = environment
    session = make()
    asyncio.run(session.delegate_many([dispatch()]))
    candidate = asyncio.run(session.call_tool("match_elevation", {"task_id": "north", "candidate": "candidate_999"}))
    match = asyncio.run(session.call_tool("apply_elevation_heights", {"match_id": "0" * 64, "confirm": True}))
    # Unrecognized arguments remain explicit rejections, regardless of their type.
    string_confirm = asyncio.run(session.call_tool("apply_elevation_heights", {"match_id": "0" * 64, "confirm": "true"}))
    for result in (candidate, match, string_confirm):
        assert result["isError"] and result["structuredContent"]["status"] == "rejected"
    assert "confirm" in match["structuredContent"]["reason"]
    assert "confirm" in string_confirm["structuredContent"]["reason"]
    missing = asyncio.run(session.call_tool("apply_elevation_heights", {"match_id": "0" * 64}))
    assert missing["isError"] and "confirm" not in missing["structuredContent"]["reason"]
    assert not session.frozen.calls


def test_profile_plan_expands_verified_numeric_trial_without_retranscription(environment):
    _, make = environment
    session = make()
    task = session._task({**dispatch("plan"), "role_id": "plan_reader", "target": "F1"})
    row, original, numeric, workspace, receipt = saved_plan_reading(session, task, selected_profile=True)
    assert isinstance(original["x_anchors"][0][0], dict)
    assert isinstance(numeric["x_anchors"][0][0], (int, float))
    asyncio.run(session.build_from_artifact("plan", row["artifact"]["sha256"]))
    expanded = json.loads(session.frozen.calls[0][1]["plan_json"])
    assert expanded == {key: value for key, value in numeric.items() if key != "reading_type"}
    assert session.registry.read("plan")["plan"] == numeric
    assert json.loads((workspace / receipt["input_plan_file"]).read_bytes()) == original
    path = workspace / receipt["compiled_numeric_plan_file"]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="compiled reader plan hash"):
        asyncio.run(session.build_from_artifact("plan", row["artifact"]["sha256"]))
    assert len(session.frozen.calls) == 1


def test_bare_facade_target_takes_the_plan_floors(environment):
    # 10-07 run4: a bare "South" reader named its floor "GF", unmatched by plan F1.
    _, make = environment
    session = make()
    tasks = session._with_plan_floors([
        dispatch("plan", role_id="plan_reader", target="plan F1"),
        dispatch("south", target="South"), dispatch("east", target="East/F1")])
    assert [task["target"] for task in tasks] == ["plan F1", "South/F1", "East/F1"]
    assert session._task(tasks[1])["coordinate_contract"]["floors"] == ["F1"]


def test_first_dispatch_adds_known_missing_drawings_once_and_keeps_explicit_task(environment):
    _, make = environment
    session = make()
    for name in ("1f_view.png", "2f_view.png", "North_view.png", "South_view.png",
                 "East_view.png", "mystery.png"):
        path = session.run_directory / "images" / name
        Image.new("RGB", (20, 20), "white").save(path)
        session.manifest["images"][name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": [20, 20]}
    session.manifest.update(image_kind="drawings",
                            floor_plan_images=["1f_view.png", "2f_view.png"])
    (session.run_directory / "inputs.json").write_text(
        json.dumps(session.manifest), encoding="utf-8", newline="\n")
    seen = []

    async def offline_reader(task):
        seen.append(task)
        return session.registry.save(task, status="failed", reason="offline behavior check")

    session.run_reader = offline_reader
    explicit = dispatch(origin="Kept explicit origin", instructions="Kept explicit facts.")
    first = asyncio.run(session.delegate_many([explicit]))
    kept = next(task for task in seen if task["task_id"] == "north")
    assert kept["instructions"] == "Kept explicit facts."
    assert kept["origin"] == "Kept explicit origin"
    assert {task["origin"] for task in seen} == {"Kept explicit origin"}
    assert {row["image"] for row in first["auto_added"]} == {
        "1f_view.png", "2f_view.png", "South_view.png", "East_view.png"}
    assert "North_view.png" not in {row["image"] for row in first["auto_added"]}
    assert "mystery.png" not in {row["image"] for row in first["auto_added"]}
    assert {row["target"] for row in first["auto_added"]} == {
        "F1", "F2", "South/F1,F2", "East/F1,F2"}
    assert reader_batch_reply(first, {})["auto_added"] == first["auto_added"]

    seen.clear()
    second = asyncio.run(session.delegate_many([dispatch(
        "later", image="South_view.png", target="South/F1,F2", origin="same")]))
    assert second["auto_added"] == []
    assert [row["task_id"] for row in seen] == ["later"]


def test_first_elevation_only_dispatch_returns_auto_list_without_inventing_floors(environment):
    _, make = environment
    session = make()
    south = session.run_directory / "images" / "South_view.png"
    Image.new("RGB", (20, 20), "white").save(south)
    session.manifest["images"]["South_view.png"] = {
        "sha256": hashlib.sha256(south.read_bytes()).hexdigest(), "size": [20, 20]}
    session.manifest.update(image_kind="drawings", floor_plan_images=[])
    (session.run_directory / "inputs.json").write_text(
        json.dumps(session.manifest), encoding="utf-8", newline="\n")
    seen = []

    async def offline_reader(task):
        seen.append(task)
        return session.registry.save(task, status="failed", reason="offline behavior check")

    session.run_reader = offline_reader
    first = asyncio.run(session.delegate_many([dispatch(
        target="North", origin="Shared drawing origin")]))
    assert first["auto_added"] == [{
        "task_id": "auto_elevation_South_view", "role_id": "elevation_reader",
        "image": "South_view.png", "target": "South", "origin": "Shared drawing origin"}]
    assert {task["origin"] for task in seen} == {"Shared drawing origin"}
