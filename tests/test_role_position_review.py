"""Q2 independent readings, coordinator choices and durable local geometry edits."""

import asyncio
import copy
import hashlib
import json

import pytest

from src.agent.runtime_roles.assembly_review import PositionReview
from src.agent.runtime_roles.elevation import compare_opening_positions, match_elevation, validate_elevation_artifact
from src.agent.runtime_roles.elevation_regularization import regularize_elevation_artifact
from src.agent.runtime_roles.lineage import opening_plan
from tests.test_role_d1g import height_session
from tests.test_role_d1j import EditTools, drift_group


def shifted(session, shift, *, task_id="NorthShift", width_delta=0, evidence_type=None, grid_step_m=None):
    artifact = copy.deepcopy(session.registry.read("North"))
    artifact.pop("artifact_sha256")
    artifact["x_calibration"]["world_start_m"] += shift
    artifact["x_calibration"]["world_end_m"] += shift
    artifact["openings"][0]["width_m"] += width_delta
    if evidence_type is not None:
        artifact["openings"][0]["evidence_type"] = evidence_type
    # Rebuild metadata for this deliberately changed fixture through the normal
    # regularizer, retaining production replay checks against stale readings.
    kwargs = {} if grid_step_m is None else {"grid_step_m": grid_step_m}
    artifact, _ = regularize_elevation_artifact(artifact, **kwargs)
    artifact = validate_elevation_artifact(artifact)
    task = session._task({"task_id": task_id, "image": "plan.png", "role_id": "elevation_reader",
                          "target": "North/F1"})
    session.registry.save(task, status="completed", artifact=artifact, validation={"validation_passed": True})
    session.match(task_id, "candidate_01")
    rows = list(session.positions.current()["items"].values())
    return next(row for row in rows if row["elevation_task_id"] == task_id)


def decision(row, choice="keep_plan", **extra):
    return {"action": "position_decision", "decision_id": row["decision_id"], "choice": choice,
            "reason": "Original jamb pixels support this interval", **extra}


def test_independent_endpoints_width_thresholds_and_fit_does_not_hide_drift():
    before = [1, 2]
    assert compare_opening_positions(before, [1.1, 2.1])["status"] == "keep_plan"
    assert compare_opening_positions(before, [1.10001, 2.1])["status"] == "pending"
    # Equal centres do not hide a width disagreement.
    assert compare_opening_positions(before, [.94, 2.06])["bucket"] == "10_30cm"
    source, artifact = drift_group()
    result = match_elevation(source, artifact)
    assert result["horizontal_fits"] and len(result["matches"]) == 3
    assert all(row["bucket"] == "gt_30cm" for row in result["position_comparisons"])
    assert before == [1, 2]


def test_10_30cm_auto_keep_plan_is_durable_without_writes_or_delivery_block(tmp_path):
    with height_session(tmp_path) as session:
        row = shifted(session, .2)
        original = (session.run_directory / "candidate_01/source_model.json").read_bytes()
        saved_row = session.positions.current()["items"][row["decision_id"]]
        assert saved_row["status"] == "decided"
        assert saved_row["decision"]["choice"] == "keep_plan"
        assert saved_row["decision"]["automatic"] is True
        assert not session.positions.summary()["pending"]
        assert session.frozen.calls == []
        assert (session.run_directory / "candidate_01/source_model.json").read_bytes() == original
        session.positions.guard("candidate_01")
        session.positions = PositionReview(session)
        session.positions.guard("candidate_01")
        session.positions.write_delivery("candidate_01")
        saved = json.loads((session.run_directory / "position_review.json").read_bytes())
        assert saved["items"][row["decision_id"]]["decision"]["choice"] == "keep_plan"


def test_use_elevation_edits_only_xy_on_host_replays_and_rejects_overflow(tmp_path):
    with height_session(tmp_path) as session:
        session.frozen = EditTools(session.run_directory)
        row = shifted(session, .2)
        before = session._source("candidate_01")
        args = {"candidate": "candidate_01", "edits": [decision(row, "use_elevation")]}
        result = asyncio.run(session.call_tool("edit_bim", args))
        assert not result["isError"], result
        current = result["structuredContent"]["candidate"]
        after = session._source(current)
        original = next(r for r in before["openings"] if r["id"] == "North")
        revised = next(r for r in after["openings"] if r["id"] == "North")
        assert sorted({p[0] for p in revised["vertices"]}) == pytest.approx([1.2, 2.2])
        assert original["space_ids"] == revised["space_ids"]
        assert original["host_boundary_id"] == revised["host_boundary_id"]
        assert sorted({p[2] for p in original["vertices"]}) == sorted({p[2] for p in revised["vertices"]})
        assert before["spaces"] == after["spaces"]
        a, b = opening_plan(before), opening_plan(after)
        a.pop("North"), b.pop("North")
        assert a == b
        calls = len(session.frozen.calls)
        assert asyncio.run(session.call_tool("edit_bim", args))["structuredContent"] == result["structuredContent"]
        assert len(session.frozen.calls) == calls
        session.positions.guard(current)
        bad = {"candidate": current, "edits": [{"action": "position", "id": "North", "along_start_m": -1,
            "along_end_m": 2, "image": "plan.png", "reason": "outside wall"}]}
        assert asyncio.run(session.call_tool("edit_bim", bad))["isError"]
        assert len(session.frozen.calls) == calls


def test_over_30cm_requires_two_located_views_and_reread_stays_pending(tmp_path):
    with height_session(tmp_path) as session:
        row = shifted(session, .4)
        state = session.positions.current()
        # Synthetic fixture uses two distinct evidence regions in one admitted image.
        state["items"][row["decision_id"]]["plan_evidence"].update(image="plan.png", bbox=[7, 4, 11, 7])
        session.positions._save(state)
        args = {"candidate": "candidate_01", "edits": [decision(row)]}
        assert asyncio.run(session.call_tool("edit_bim", args))["isError"]
        toolkit = session.frozen.toolkit
        toolkit.view("plan.png", [0, 0, 3, 3])
        args["edits"][0]["view_ids"] = ["view_0001"]
        assert asyncio.run(session.call_tool("edit_bim", args))["isError"]
        # One pixel short on every side is accepted as image crop rounding.
        toolkit.view("plan.png", [8, 5, 10, 6])
        args["edits"][0]["view_ids"] = ["view_0001", "view_0002"]
        result = asyncio.run(session.call_tool("edit_bim", args))
        assert not result["isError"], result
        assert len(session.positions.current()["items"][row["decision_id"]]["decision"]["views"]) == 2
        session.positions.guard("candidate_01")
        row = shifted(session, .5, task_id="NorthReread")
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01", "edits": [decision(row, "reread_elevation")]}))
        assert not result["isError"], result
        assert result["structuredContent"]["decisions"][0]["rework"]["previous_task_id"] == "NorthReread"
        with pytest.raises(ValueError):
            session.positions.guard("candidate_01")


def test_unresolved_over_30cm_blocks_delivery_without_fabricating_a_default(tmp_path):
    with height_session(tmp_path) as session:
        row = shifted(session, .4)
        summary = session.positions.summary()
        assert summary["pending"][0]["decision_id"] == row["decision_id"]
        assert summary["delivery_blocking"] == summary["pending"]
        assert summary["delivery_defaults"] == []
        with pytest.raises(ValueError, match="position decision required before delivery"):
            session.positions.guard("candidate_01")
        unresolved = session.match("NorthShift", "candidate_01", height_bounds=True)["result"]
        assert unresolved["conflicts"][0]["type"] == "position_or_width_conflict"
        with pytest.raises(ValueError, match="position decision required before delivery"):
            session.positions.write_delivery("candidate_01")
        assert not (session.run_directory / "position_review.json").exists()


def test_decision_resume_after_inner_write_does_not_duplicate_candidate(tmp_path):
    with height_session(tmp_path) as session:
        session.frozen = EditTools(session.run_directory)
        row = shifted(session, .2)
        save = session.positions._save
        def killed(value):
            raise RuntimeError("process died after geometry receipt")
        session.positions._save = killed
        args = {"candidate": "candidate_01", "edits": [decision(row, "use_elevation")]}
        with pytest.raises(RuntimeError):
            asyncio.run(session.call_tool("edit_bim", args))
        count = len(session.frozen.calls)
        assert count == 1
        session.positions._save = save
        result = asyncio.run(session.call_tool("edit_bim", args))
        assert not result["isError"], result
        assert len(session.frozen.calls) == count


def bind_original_plan(session, *, omitted_openings=()):
    task = session._task({"task_id": "plan", "role_id": "plan_reader", "image": "plan.png", "target": "F1"})
    source = session._source("candidate_01")
    source["openings"] = [row for row in source["openings"] if row["id"] not in omitted_openings]
    # This consumer fixture freezes XY reading facts, without a 3D trial.
    product = {"mapping": {"floor_id": "F1", "space_mapping": [
        {"space_id": row["id"], "world_polygon_m": row["polygon"]} for row in source["spaces"]],
        "opening_hosts": [
            {"opening_id": row["id"], "kind": row["kind"], "space_ids": row["space_ids"],
             "p1_world_m": list(dict.fromkeys(tuple(v[:2]) for v in row["vertices"]))[0],
             "p2_world_m": list(dict.fromkeys(tuple(v[:2]) for v in row["vertices"]))[-1],
             "exterior": row["exterior"]} for row in source["openings"]]}}
    raw = json.dumps(product).encode()
    path = session.registry.child("plan").task_directory / "bim/trial_workspace/reading_product.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    session.registry.save(task, status="completed", artifact={"plan": {"floor_id": "F1"},
        "evidence": [{"item": "plan.openings:" + opening["id"], "bbox": [0, 0, 2, 2], "source": "plan.png"}
                     for opening in source["openings"]]},
        validation={"validation_passed": True, "candidate": "reading_001", "reading_product_file": "reading_product.json", "reading_product_sha256": hashlib.sha256(raw).hexdigest()})
    session.assembly.bind("plan")


def test_opening_added_after_plan_trial_requires_reread_instead_of_false_agreement(tmp_path):
    with height_session(tmp_path) as session:
        bind_original_plan(session, omitted_openings={"North"})
        row = shifted(session, 0)
        assert row["bucket"] == "unavailable" and row["status"] == "reread"
        assert row["plan_span_m"] is None and row["max_difference_m"] is None
        pending = session.positions.summary()["pending"]
        assert pending[0]["decision_id"] == row["decision_id"] and pending[0]["difference_cm"] is None
        for choice in ("keep_plan", "use_elevation"):
            result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01", "edits": [decision(row, choice)]}))
            assert result["isError"], result
        assert session.frozen.calls == []
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01", "edits": [decision(row, "reread_plan")]}))
        assert not result["isError"], result
        rework = result["structuredContent"]["decisions"][0]["rework"]
        assert rework["previous_task_id"] == "plan" and rework["rework_targets"] == ["plan.openings:North"]
        with pytest.raises(ValueError):
            session.positions.guard("candidate_01")


def test_explicit_scope_survives_newer_facade_and_reactivation_requires_finished_assembly(tmp_path):
    with height_session(tmp_path) as session:
        bind_original_plan(session)
        a = shifted(session, .2, task_id="a")
        shifted(session, .5, task_id="b")
        refs_a, refs_b = [{"task_id": "plan"}, {"task_id": "a"}], [{"task_id": "plan"}, {"task_id": "b"}]
        session.positions.bind("candidate_01", refs_a, assembly_id="assembly-a")
        state = session.positions.refresh("candidate_01")
        assert {r["elevation_task_id"] for r in state["items"].values()} == {"a"}
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01", "edits": [decision(a)]}))
        assert not result["isError"], result
        with pytest.raises(ValueError):
            session.positions.guard("candidate_01")
        revision_a = session.positions.revision()
        session.positions.bind("candidate_01", refs_a, assembly_id="assembly-a", position_revision=revision_a)
        session.positions.guard("candidate_01")
        session.positions.bind("candidate_01", refs_b, assembly_id="assembly-b")
        assert not session.positions.current()["items"]  # no excluded old pending rows
        assert {r["elevation_task_id"] for r in session.positions.refresh("candidate_01")["items"].values()} == {"b"}
        session.positions.bind("candidate_01", refs_a, assembly_id="assembly-a", position_revision=revision_a)
        session.positions.guard("candidate_01")
        assert session.positions._scope_record("candidate_01")["assembly_id"] == "assembly-a"


def test_keep_plan_resolves_only_position_conflict_and_never_waives_height_bounds(tmp_path):
    with height_session(tmp_path) as session:
        bind_original_plan(session)
        row = shifted(session, .4)
        artifact = session.registry.read("NorthShift")
        # Both independent evidence boxes are covered by an actual saved view.
        session.frozen.toolkit.view("plan.png", [0, 0, 12, 8])
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01",
            "edits": [decision(row, view_ids=["view_0001"])]}))
        assert not result["isError"], result
        resolved = session.match("NorthShift", "candidate_01", height_bounds=True)["result"]
        assert not resolved["conflicts"] and resolved["matches"][0]["position_decision_id"] == row["decision_id"]
        artifact.pop("artifact_sha256")
        artifact["openings"][0]["head_m"] = 9
        artifact, _ = regularize_elevation_artifact(artifact)
        artifact = validate_elevation_artifact(artifact)
        task = session._task({"task_id": "bad-height", "role_id": "elevation_reader", "image": "plan.png", "target": "North/F1"})
        session.registry.save(task, status="completed", artifact=artifact, validation={"validation_passed": True})
        session.match("bad-height", "candidate_01")
        row = next(r for r in session.positions.current()["items"].values() if r["elevation_task_id"] == "bad-height")
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01",
            "edits": [decision(row, view_ids=["view_0001"])]}))
        assert not result["isError"], result
        checked = session.match("bad-height", "candidate_01", height_bounds=True)["result"]
        assert not checked["matches"] and checked["conflicts"][0]["type"] == "height_outside_host"


def test_ink_inconsistency_is_recorded_without_blocking_agreeing_readings(tmp_path):
    with height_session(tmp_path) as session:
        bind_original_plan(session)
        artifact = copy.deepcopy(session.registry.read("North"))
        artifact.pop("artifact_sha256")
        opening = artifact["openings"][0]
        artifact["ink_alignment"] = {"schema_version": "elevation_ink_alignment_v2", "openings": [{"id": opening["id"], "aligned_bbox_px": opening["bbox"],
            "inconsistencies": [{"field": "width_m", "reader_value": opening["width_m"],
                "ink_value": opening["width_m"] + .2, "difference_m": .2, "tolerance_m": .05}]}]}
        artifact = validate_elevation_artifact(artifact)
        task = session._task({"task_id": "ink", "role_id": "elevation_reader", "image": "plan.png", "target": "North/F1"})
        session.registry.save(task, status="completed", artifact=artifact, validation={"validation_passed": True})
        comparison = session.match("ink", "candidate_01")["result"]["position_comparisons"][0]
        assert comparison["max_difference_m"] == 0 and comparison["status"] == "keep_plan"
        row = next(iter(session.positions.current()["items"].values()))
        assert row["status"] == "keep_plan" and row["ink_inconsistencies"][0]["field"] == "width_m"
        assert not session.positions.summary()["pending"]
        session.positions.guard("candidate_01")
        assert not session.frozen.calls


@pytest.mark.parametrize("width_delta", [.02, .4])
def test_using_elevation_cannot_replace_retained_numeric_width_even_inside_review_tolerance(tmp_path, width_delta):
    with height_session(tmp_path) as session:
        # Keep the 2cm case smaller than the position-review tolerance instead
        # of rounding away the mismatch this numeric-width protection tests.
        # This exercises the configurable fine-grid domain contract; the
        # product's default Lite grid remains 0.1m.
        row = shifted(session, .2, width_delta=width_delta, evidence_type="annotation_and_pixels", grid_step_m=.01)
        assert session.registry.read("NorthShift")["openings"][0]["width_m"] == pytest.approx(1 + width_delta)
        before = session._source("candidate_01")
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01",
            "edits": [decision(row, "use_elevation")]}))
        assert result["isError"]
        assert session._source("candidate_01") == before and not session.frozen.calls
        result = asyncio.run(session.call_tool("edit_bim", {"candidate": "candidate_01",
            "edits": [decision(row, "reread_elevation")]}))
        assert not result["isError"], result
