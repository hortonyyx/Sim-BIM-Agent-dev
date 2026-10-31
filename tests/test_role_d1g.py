"""D1g observed failure paths, using local geometry/claims and no model service."""

import asyncio
import copy
import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.runtime_roles.coordinates import plan_orientation
from src.agent.runtime_roles.elevation import _canonical_hash, validate_elevation_artifact
from src.agent.runtime_roles.height_writes import load_match
from src.agent.runtime_roles.levels import apply_levels
from src.agent.runtime_roles.lineage import candidate_readers, latest_candidate
from src.agent.runtime_roles.session import RoleSession, envelope
from src.agent.runtime_roles.submission import ReaderSubmission
from src.agent.runtime_roles.trial import canonical_plan_sha256
from src.agent_runtime.loop import RunLimits
from src.agent_runtime.store import EventStore
from tests.test_bim_claims import setup_run
from tests.test_role_session import ROUTES, dispatch, environment
from tests.test_role_submission import PassedTrial, arguments
from tests.test_source_proposal import _proposal


class LocalTools:
    def __init__(self, run):
        self.run_directory, self.toolkit, self.calls = run, Toolkit(run), []

    async def call_tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        await asyncio.sleep(0)  # Expose races if the role write lock is absent.
        if name == "claim_transaction":
            return envelope(self.toolkit.claim_transaction(arguments["candidate"], arguments["entries_json"]))
        if name == "build_plan_bim":
            return envelope(self.toolkit.build_plan(arguments["image"], arguments["plan_json"]))
        raise AssertionError(name)


@contextmanager
def height_session(tmp_path):
    proposal = _proposal()
    proposal["geometry"]["windows"] = [{"id": side, "floor": "F1", "facade": side,
        "span": [1, 2], "z": [1, 2], "room": "room" if side == "East" else "hall"}
        for side in ("North", "South", "East", "West")]
    run, _ = setup_run(tmp_path, proposal)
    assert export_source_proposal(proposal, run / "candidate_01")["source_geometry_ready"]
    limits = RunLimits(model_calls=1, tool_calls=40, tokens=None, seconds=600)
    with EventStore(tmp_path / "journal", run_id="d1g", task_id="coordinator",
                    budget_limit=limits.ledger_limit()) as store:
        session = RoleSession(store=store, frozen=LocalTools(run), routes=ROUTES,
            adapter_factory=lambda *args: pytest.fail("no model service allowed"), limits=limits,
            root=Path(__file__).resolve().parents[1])
        for side, view in (("North", "South"), ("South", "North"), ("East", "West"), ("West", "East")):
            decreasing = side in ("North", "West")
            artifact = validate_elevation_artifact({"image": "plan.png", "orientation": side, "view_direction": view,
                "x_calibration": {"pixel_start": 0, "pixel_end": 12,
                    "world_start_m": 6 if decreasing else 0, "world_end_m": 0 if decreasing else 6},
                "elevations": [{"id": "floor", "kind": "floor", "floor_id": "F1", "value_m": 0,
                                "evidence_type": "pixels", "bbox": [0, 0, 2, 2]},
                               {"id": "top", "kind": "eave", "value_m": 4.5,
                                "evidence_type": "annotation", "bbox": [0, 0, 2, 2]}],
                "openings": [{"id": "observed-" + side, "floor_id": "F1", "kind": "window",
                    "x_px": [8, 10] if decreasing else [2, 4], "width_m": 1,
                    "sill_m": .9, "head_m": 2.3, "evidence_type": "pixels", "bbox": [0, 0, 2, 2]}],
                "counts": [{"floor_id": "F1", "window_count": 1, "door_count": 0}], "unresolved": []})
            task = session._task({"task_id": side, "image": "plan.png", "role_id": "elevation_reader",
                                  "target": side, "instructions": "Synthetic located facade"})
            session.registry.save(task, status="completed", artifact=artifact)
        yield session


@pytest.mark.parametrize("axes", [("x",), ("y",), ("x", "y")])
def test_mirrored_trial_needs_located_north_arrow_not_coordinator_permission(axes):
    trial = PassedTrial()
    for axis in axes:
        trial.plan[axis + "_anchors"] = [[p, -v] for p, v in trial.plan[axis + "_anchors"]]
    trial.receipt["plan_sha256"] = canonical_plan_sha256(trial.plan)
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    params = arguments(trial)
    with pytest.raises(ValueError, match="Mirrored calibration"):
        submission.submit(params)
    orientation = plan_orientation(trial.plan)
    params["north_arrow"] = {k: v for k, v in orientation.items() if k != "check"}
    params["north_arrow"].update(bbox=[0, 0, 10, 10], basis="Original arrow and compass labels point this way")
    assert submission.submit(params)["status"] == "accepted"
    assert submission.read()["validation"]["north_arrow"] == params["north_arrow"]


def test_normal_submission_has_no_added_form_and_wrong_arrow_is_rejected():
    trial = PassedTrial()
    submission = ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial)
    params = arguments(trial)
    assert submission.submit(params)["status"] == "accepted"
    assert "north_arrow" not in submission.read()["validation"]
    params["north_arrow"] = {"bbox": [0, 0, 10, 10], "basis": "arrow",
        "world_north_toward": "image_bottom", "world_east_toward": "image_right"}
    with pytest.raises(ValueError, match="contradicts"):
        submission.submit(params)


def test_no_topology_warning_error_explicitly_requests_empty_list():
    trial = PassedTrial()
    params = arguments(trial)
    params["topology_decisions"] = [{"issue_id": "invented", "decision": "retain_opening",
                                    "basis": "invented", "bbox": [0, 0, 10, 10]}]
    with pytest.raises(ValueError, match="本稿没有需要决定的警告，这一项传空列表"):
        ReaderSubmission(role_id="plan_reader", image_name="plan.png", trial=trial).submit(params)


def test_dispatch_attaches_fixed_coordinates_and_starts_plans_first(environment):
    _, make = environment
    session = make(max_concurrent_readers=1)
    starts = []
    async def reader(task):
        starts.append(task)
        return {"task_id": task["task_id"]}
    session.run_reader = reader
    plan = {**dispatch("plan"), "role_id": "plan_reader", "target": "F1", "origin": "southwest corner",
            "instructions": "WRONG: world y follows image downward"}
    result = asyncio.run(session.delegate_many([dispatch(), plan]))
    assert [row["task_id"] for row in starts] == ["plan", "north"]
    assert [row["task_id"] for row in result["results"]] == ["north", "plan"]
    for row in starts:
        assert "cannot redefine" in row["coordinate_contract"]["directions"]
    assert starts[0]["coordinate_contract"]["floors"] == ["F1"]
    assert starts[0]["coordinate_contract"]["origin"] == "southwest corner"
    assert make().max_concurrent_readers == 8


def test_level_overrides_resolve_delivered_items_and_leave_plan_immutable(tmp_path):
    with height_session(tmp_path) as session:
        original = {"floor_id": "F1", "z_floor": 0, "ceiling_height": 3, "basis": "plan assumption"}
        refs = dict(z_floor=0, z_floor_evidence={"task_id": "North", "elevation_id": "floor"},
                    ceiling_height=4.5, ceiling_height_evidence={"task_id": "North", "elevation_id": "top"})
        updated, evidence = apply_levels(session.registry, original, **refs)
        assert updated["ceiling_height"] == 4.5 and original["ceiling_height"] == 3
        assert evidence["ceiling_height"]["level"]["bbox"] == [0, 0, 2, 2]
        assert session.registry.records["North"]["artifact"]["sha256"] in updated["basis"]
        with pytest.raises(ValueError, match="expected 4.5"):
            apply_levels(session.registry, original, **{**refs, "ceiling_height": 3.6})
        with pytest.raises(ValueError, match="unknown elevation_id"):
            apply_levels(session.registry, original, **{**refs, "ceiling_height_evidence": {
                "task_id": "North", "elevation_id": "fictional"}})
        with pytest.raises(ValueError, match="needs finite metres"):
            apply_levels(session.registry, original, ceiling_height=4.5)


def _heights(session, candidate):
    return {row["id"]: sorted({p[2] for p in row["vertices"]})
            for row in session._source(candidate)["openings"] if row["kind"] == "window"}


def test_cited_levels_reach_real_compiled_source_and_preserve_reader_bytes(tmp_path):
    with height_session(tmp_path) as session:
        task = session._task({"task_id": "plan", "role_id": "plan_reader", "image": "plan.png",
                              "target": "F1", "instructions": "simple floor"})
        plan = {"reading_type": "plan", "floor_id": "F1", "z_floor": 0, "ceiling_height": 3,
            "x_anchors": [[1, 0], [11, 10]], "y_anchors": [[1, 6], [7, 0]],
            "basis": "plan height assumption", "footprint_pixels": [[1, 1], [11, 1], [11, 7], [1, 7]],
            "partitions": [], "openings": [], "space_seeds": [{"id": "room", "point": [5, 4]}],
            "assumptions": ["ceiling height assumed"], "unresolved": []}
        trial = session.registry.child("plan").task_directory / "bim/trial_workspace"
        shutil.copytree(session.run_directory / "images", trial / "images")
        shutil.copy2(session.run_directory / "inputs.json", trial / "inputs.json")
        from tests.test_role_trial import example, Tools
        from src.agent.runtime_roles.trial import PlanTrial
        plan["wall_metadata"] = example()["wall_metadata"]
        reading = PlanTrial(Tools(), image_name="plan.png", workspace=trial,
                            receipt_directory=trial / "trial_receipts")
        result = asyncio.run(reading.run(plan))
        assert result["reading_geometry_ready"], result
        row = session.registry.save(task, status="completed",
            artifact={"plan": reading.numeric_plan(result)},
            validation={**result, "validation_passed": True})
        artifact = session.store.directory / row["artifact"]["path"]
        before = artifact.read_bytes()
        built = asyncio.run(session.build_from_artifact(task_id="plan", sha256=row["artifact"]["sha256"],
            z_floor=0, z_floor_evidence={"task_id": "North", "elevation_id": "floor"},
            ceiling_height=4.5, ceiling_height_evidence={"task_id": "North", "elevation_id": "top"}))
        meta = built["structuredContent"]
        source = session._source(meta["candidate"])
        assert max(p[2] for boundary in source["boundaries"] for p in boundary["vertices"]) == 4.5
        assert meta["assembly_review"]["status"] == "unchanged"
        assert artifact.read_bytes() == before
        assert session.registry.records["North"]["artifact"]["sha256"] in json.dumps(source)
        assert meta["role_application"]["reference"]["levels"]["ceiling_height"]["previous_value_m"] == 3


@pytest.mark.parametrize("batched", [True, False])
def test_all_facades_survive_parallel_writes_and_repeated_supplements_do_not_save(tmp_path, batched):
    with height_session(tmp_path) as session, asyncio.Runner() as runner:
        ids = [session.match(side, "candidate_01")["match_id"] for side in ("North", "South", "East", "West")]
        async def apply():
            return ([await session.apply_heights(ids)] if batched else
                    await asyncio.gather(*(session.apply_heights(identity) for identity in ids)))
        results = runner.run(apply())
        assert all(row["structuredContent"]["status"] == "completed" for row in results)
        expected_count = 2 if batched else 5
        assert len(list(session.run_directory.glob("candidate_*"))) == expected_count
        latest = latest_candidate(session, "candidate_01")
        assert all(z == [.9, 2.3] for z in _heights(session, latest).values())
        before = (session.run_directory / latest / "source_model.json").read_bytes()
        # Re-match all four to model the coordinator's second parallel batch.
        ids[:] = [session.match(side, latest)["match_id"] for side in ("North", "South", "East", "West")]
        results = runner.run(apply())
        assert all(row["structuredContent"]["height_write"]["unchanged"] for row in results)
        assert all("已是该值" in row["structuredContent"]["height_write"]["message"] for row in results)
        assert len(list(session.run_directory.glob("candidate_*"))) == expected_count
        assert (session.run_directory / latest / "source_model.json").read_bytes() == before
        # Confirmations retain the per-opening image evidence on the current source.
        assert len(list((session.run_directory / "claims").glob("confirmation_*.json"))) >= 1


def test_changed_xy_rejects_old_match_without_writing_and_old_alias_is_rejected(tmp_path):
    with height_session(tmp_path) as session:
        match = session.match("West", "candidate_01")
        assert "match_id" not in match["result"]
        legacy = "elevation_match:" + _canonical_hash({k: v for k, v in match["result"].items()
                                                       if k != "source_file_sha256"})[:24]
        with pytest.raises(ValueError, match="hash returned by match_elevation"):
            load_match(session, legacy)
        result = session.frozen.toolkit.revise("candidate_01", json.dumps([{
            "op": "update_window", "id": "West", "changes": {"span": [1.1, 2.1]},
            "reason": "new observed XY", "source_refs": ["plan.png: shifted jambs"]}]))
        assert result["source_geometry_ready"]
        with pytest.raises(ValueError, match="重新对位"):
            asyncio.run(session.apply_heights(match["match_id"]))
        assert not session.frozen.calls
        assert len(list(session.run_directory.glob("candidate_*"))) == 2


def test_failed_height_batch_does_not_partly_apply_other_facades(tmp_path):
    with height_session(tmp_path) as session:
        ids = [session.match(side, "candidate_01")["match_id"] for side in ("North", "South")]
        with pytest.raises(ValueError, match="no saved match"):
            asyncio.run(session.apply_heights([*ids, "0" * 64]))
        assert len(list(session.run_directory.glob("candidate_*"))) == 1
        assert not session.frozen.calls


def test_old_plan_candidate_rejected_even_when_new_plan_has_same_xy(tmp_path):
    with height_session(tmp_path) as session:
        # Persist the same build receipt consumed by production lineage lookup.
        plan = {"floor_id": "F1"}
        task = session._task({"task_id": "plan", "role_id": "plan_reader", "image": "plan.png",
                              "target": "F1", "instructions": "plan"})
        row = session.registry.save(task, status="completed", artifact={"plan": plan},
                                     validation={"validation_passed": True})
        session.store.write_json("role_operations/build.json", {"tool": "build_plan_bim",
            "reference": {"task_id": "plan", "sha256": row["artifact"]["sha256"]},
            "result": envelope({"candidate": "candidate_01"})})
        assert candidate_readers(session, "candidate_01") == {"plan"}
        assert session.match("North", "candidate_01")["result"]["matches"]
        match = session.match("North", "candidate_01")
        asyncio.run(session.apply_heights(match["match_id"]))
        assert candidate_readers(session, "candidate_02") == {"plan"}
        changed = session._task({**{k: v for k, v in task.items() if k not in {"input_sha256", "coordinate_contract"}},
            "task_id": "plan-r2", "previous_task_id": "plan", "issues": ["height assumption"],
            "rework_targets": ["plan.ceiling_height"]})
        session.registry.save(changed, status="completed", artifact={"plan": plan, "unresolved": ["new"]},
                              validation={"validation_passed": True})
        with pytest.raises(ValueError, match="先用新产物建层"):
            session.match("North", "candidate_01")
        with pytest.raises(ValueError, match="先用新产物建层"):
            session.match("North", "candidate_02")


def test_height_continuation_survives_new_session_on_same_journal(tmp_path):
    with height_session(tmp_path) as session:
        ids = [session.match(side, "candidate_01")["match_id"] for side in ("North", "South")]
        asyncio.run(session.apply_heights(ids[0]))
        resumed = RoleSession(store=session.store, frozen=LocalTools(session.run_directory), routes=ROUTES,
            adapter_factory=lambda *args: pytest.fail("no model service"), limits=session.limits, root=session.root)
        result = asyncio.run(resumed.apply_heights(ids[1]))["structuredContent"]
        assert result["height_write"]["base_candidate"] == "candidate_02"
        assert _heights(resumed, "candidate_03")["North"] == [.9, 2.3]
        assert _heights(resumed, "candidate_03")["South"] == [.9, 2.3]


def test_unknown_height_write_outcome_never_repeats_mutation(tmp_path):
    with height_session(tmp_path) as session:
        match = session.match("North", "candidate_01")["match_id"]
        actual = session.frozen.call_tool
        async def interrupted(name, arguments):
            await actual(name, arguments)
            raise RuntimeError("simulated death before durable result")
        session.frozen.call_tool = interrupted
        with pytest.raises(RuntimeError, match="simulated death"):
            asyncio.run(session.apply_heights(match))
        assert len(list(session.run_directory.glob("candidate_*"))) == 2
        session.frozen.call_tool = actual
        with pytest.raises(ValueError, match="outcome unknown"):
            asyncio.run(session.apply_heights(match))
        assert len(session.frozen.calls) == 1
