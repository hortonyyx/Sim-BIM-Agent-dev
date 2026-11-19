"""Current drawing reviews follow real source edits and gate only opted-in runs.

All drawings are synthetic. The MCP scenario executes the actual stdio tools,
plan compiler, source exporter and claim transaction; no model is contacted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil

import pytest
from PIL import Image, ImageDraw

from scripts.tool_scripts.run_bim_agent import Toolkit, digest, run_guide
from scripts.tool_scripts.bim_agent_drawing_readings import (
    compare_reading, load_reading, prepare_elevation_reading,
)
from scripts.tool_scripts.bim_agent_process_reviews import (
    POLICY, apply_readings, building_facts, carry_reviews, process_status, record_reviews, require_process_reviews,
)
from src.agent.runtime_entry import prepare_inputs
from tests.test_bim_agent_tools import _error_text, _json_result, _server_session


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _plan(window_pixels):
    return {
        "floor_id": "local", "z_floor": 0, "ceiling_height": 3,
        "x_anchors": [[20, 0], [200, 9]], "y_anchors": [[20, 10], [220, 0]],
        "basis": "Synthetic axis-aligned 9 by 10 metre floor.",
        "footprint_pixels": [[20, 20], [200, 20], [200, 220], [20, 220]],
        "partitions": [], "openings": [{
            "id": "W", "kind": "window", "p1": [window_pixels[0], 20],
            "p2": [window_pixels[1], 20], "z": [0.8, 2.0],
            "source_refs": ["Synthetic north-wall window."],
        }], "assumptions": [], "unresolved": [],
    }


def _reading():
    return {
        "x_calibration": {"pixel_start": 20, "pixel_end": 200,
            "distance_start_m": 0, "distance_end_m": 9, "facade_length_m": 9},
        "z_calibration": {"pixel_start": 20, "pixel_end": 140,
            "world_start_m": 6, "world_end_m": 0},
        "elevations": [
            {"id": "ground", "floor_id": "F1", "kind": "floor", "value_m": 0,
             "evidence_type": "annotation", "bbox": [20, 139, 200, 141]},
            {"id": "upper", "floor_id": "F2", "kind": "floor", "value_m": 3,
             "evidence_type": "annotation", "bbox": [20, 79, 200, 81]},
            {"id": "roof", "floor_id": "F2", "kind": "roof", "value_m": 6,
             "evidence_type": "annotation", "bbox": [20, 19, 200, 21]},
        ],
        "openings": [
            {"id": "W1", "floor_id": "F1", "kind": "window",
             "evidence_type": "pixels", "opening_bbox": [60, 96, 100, 120]},
            {"id": "W2", "floor_id": "F2", "kind": "window",
             "evidence_type": "pixels", "opening_bbox": [120, 36, 160, 60]},
        ],
        "counts": [{"floor_id": floor, "window_count": 1, "door_count": 0}
                   for floor in ("F1", "F2")],
        "unresolved": [],
    }


def _review(kind, **values):
    return {"kind": kind, "outcome": "consistent",
            "summary": "Compared the returned synthetic original and actual-result view.",
            "issues": [], **values}


def _plan_reviews():
    return [_review("plan", image=f"{floor}.png", floor_id=floor) for floor in ("F1", "F2")]


def _statuses(status):
    return {row["scope"]: row["status"] for row in status["reviews"]}


def _source(toolkit, candidate):
    return json.loads((toolkit.candidate_path(candidate) / "source_model.json").read_bytes())


def _prepare(toolkit):
    _, metadata = prepare_elevation_reading(toolkit, "North.png", "North", json.dumps(_reading()))
    return metadata


@pytest.fixture(scope="module")
def built_case(tmp_path_factory):
    """Compile and assemble once; tests get isolated copies of this tiny fixture."""
    root = tmp_path_factory.mktemp("process_reviews_build")
    images = root / "originals"
    images.mkdir()
    for floor, span in (("F1", [120, 160]), ("F2", [60, 100])):
        picture = Image.new("RGB", (220, 240), "white")
        ink = ImageDraw.Draw(picture)
        ink.rectangle((20, 20, 200, 220), outline="black")
        ink.line((span[0], 20, span[1], 20), fill="blue", width=2)
        picture.save(images / f"{floor}.png")
    facade = Image.new("RGB", (220, 180), "white")
    ink = ImageDraw.Draw(facade)
    for rectangle in ((60, 96, 100, 120), (120, 36, 160, 60)):
        ink.rectangle(rectangle, outline="black", width=1)
    facade.save(images / "North.png")
    run, guide, task = prepare_inputs(root / "execution", images=images, mesh=None,
        building_input=None, scope="Synthetic two-storey drawing-review behavior fixture.",
        image_kind="drawings", floor_plan_images=["F1.png", "F2.png"],
        max_candidates=20, process_reviews=True)
    toolkit = Toolkit(run)
    assert toolkit.manifest["drawing_review_policy"] == POLICY
    assert (run.parent / "guide.txt").read_text(encoding="utf-8") == guide
    assert (run.parent / "task.txt").read_text(encoding="utf-8") == task
    assert POLICY in guide and POLICY in run_guide(run)
    for name in ("prepare_elevation_reading", "reviews_json", "apply_elevation_readings"):
        assert name in guide and name in run_guide(run)
    assert all(digest(images / name) == digest(toolkit.image_path(name))
               for name in toolkit.manifest["images"])
    floors = []
    for index, span in enumerate(([120, 160], [60, 100]), 1):
        result = toolkit.build_plan(f"F{index}.png", json.dumps(_plan(span)))
        assert result["source_geometry_ready"], result
        draft = f"draft_{index:03d}"
        floors.append({"draft_id": draft, "expected_plan_sha256": toolkit.inspect_plan(draft)["plan_sha256"],
                       "floor_id": f"F{index}", "z_floor": 3 * (index - 1),
                       "evidence": "Synthetic explicit storey placement."})
    result = toolkit.assemble_plans(json.dumps(floors))
    assert result["source_geometry_ready"], result
    assert {row["floor_id"] for row in result["source_image_projections"]} >= {"F1", "F2"}
    return run, result["candidate"]


@pytest.fixture
def case(tmp_path, built_case):
    original, candidate = built_case
    run = tmp_path / "run"
    shutil.copytree(original, run)
    return Toolkit(run), candidate


def test_plan_reviews_follow_xy_scope_and_building_judgment_follows_current_source(case):
    toolkit, original = case
    before = (toolkit.candidate_path(original) / "source_model.json").read_bytes()
    saved = record_reviews(toolkit, original, json.dumps([*_plan_reviews(), _review("building")]))
    assert _statuses(saved["process_review"]) == {
        "plan:F1.png:F1": "consistent", "plan:F2.png:F2": "consistent", "building": "consistent"}
    assert saved["drawing_fidelity"] == "not_evaluated"
    assert saved["process_review"]["missing_images"] == ["North.png"]

    def revise(candidate, identity, changes):
        result = toolkit.revise(candidate, json.dumps([{
            "op": "update_window", "id": identity, "changes": changes,
            "reason": "Synthetic local revision.", "source_refs": ["Synthetic new observation."],
        }]))
        assert result["source_geometry_ready"], result
        return result["candidate"]

    height = revise(original, "F2:W", {"z": [4.0, 5.2]})
    assert _statuses(process_status(toolkit, height)) == {
        "plan:F1.png:F1": "consistent", "plan:F2.png:F2": "consistent", "building": "stale"}
    record_reviews(toolkit, height, json.dumps([_review("building")]))
    assert _statuses(process_status(toolkit, height))["building"] == "consistent"

    shifted = revise(height, "F2:W", {"span": [2.2, 4.2]})
    assert _statuses(process_status(toolkit, shifted)) == {
        "plan:F1.png:F1": "consistent", "plan:F2.png:F2": "stale", "building": "stale"}
    record_reviews(toolkit, shifted, json.dumps([_plan_reviews()[1]]))
    assert _statuses(process_status(toolkit, shifted))["plan:F2.png:F2"] == "consistent"
    assert (toolkit.candidate_path(original) / "source_model.json").read_bytes() == before


def test_orchestrated_handoff_binds_actual_inputs_and_selection_without_filename_assumptions(case):
    from src.agent.runtime_delivery import attest_finished_handoff

    toolkit, candidate = case
    manifest = dict(toolkit.manifest, orchestrator=True, floor_plan_images=[], floor_scope_source="not_declared")
    del manifest["drawing_review_policy"]
    _write(toolkit.run / "inputs.json", manifest)

    async def scenario():
        async with _server_session(toolkit.run, readonly=False) as session:
            async def finish(coverage):
                review = {"coverage": coverage, "summary": "Compared both storeys and the north elevation.",
                          "unresolved": ["Unmarked heights on interior doors."]}
                return _json_result(await session.call_tool("finish_bim", {
                    "candidate": candidate, "reviews_json": json.dumps({"drawing_review": review})}))

            assert "drawing_review" in _error_text(await session.call_tool("finish_bim", {"candidate": candidate}))
            partial = await finish("partial")
            assert attest_finished_handoff(toolkit.run, partial, candidate) is None
            complete = await finish("whole_building")
            proof = attest_finished_handoff(toolkit.run, complete, candidate)
            assert proof["scope_basis"] == "drawing_model_reported_review"
            assert proof["drawing_review"]["coverage"] == "whole_building"
            delivery = json.loads((toolkit.run / "delivery.json").read_bytes())
            assert delivery["drawing_review"]["floor_ids"] == ["F1", "F2"]
            assert delivery["floor_completeness"]["complete_building"] is None
            image = toolkit.run / "images/North.png"
            image.write_bytes(image.read_bytes() + b"changed")
            assert attest_finished_handoff(toolkit.run, complete, candidate) is None

    asyncio.run(scenario())


def test_orchestrator_respects_explicit_floor_plan_coverage(case):
    from scripts.tool_scripts.bim_agent_process_reviews import drawing_handoff_review
    toolkit, _ = case
    toolkit.manifest["orchestrator"] = True
    review = {"coverage": "whole_building", "summary": "First storey only.", "unresolved": []}
    with pytest.raises(ValueError, match="Missing declared floor-plan coverage: F2.png"):
        drawing_handoff_review(toolkit, "candidate_01", review)


@pytest.mark.parametrize("enabled", [True, False], ids=["policy-required", "historical-manifest"])
def test_finish_mcp_rejects_missing_reviews_without_formal_delivery_only_when_enabled(case, enabled):
    toolkit, candidate = case
    if not enabled:
        manifest = copy.deepcopy(toolkit.manifest)
        del manifest["drawing_review_policy"]
        _write(toolkit.run / "inputs.json", manifest)

    async def scenario():
        async with _server_session(toolkit.run, readonly=False) as session:
            result = await session.call_tool("finish_bim", {"candidate": candidate})
            if enabled:
                assert "finish_bim" in _error_text(result)
                assert all(not (toolkit.run / name).exists()
                           for name in ("delivery_selection.json", "delivery.json", "delivery.html"))
            else:
                assert _json_result(result)["candidate"] == candidate
                assert all((toolkit.run / name).is_file()
                           for name in ("delivery_selection.json", "delivery.json", "delivery.html"))
    asyncio.run(scenario())


def test_mcp_prepare_review_apply_and_finish_use_saved_height_transaction(case):
    toolkit, candidate = case
    old_bytes = (toolkit.candidate_path(candidate) / "source_model.json").read_bytes()

    async def scenario():
        async with _server_session(toolkit.run, readonly=False) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"prepare_elevation_reading", "apply_elevation_readings"} <= tools
            assert "record_bim_review" not in tools
            prepared = await session.call_tool("prepare_elevation_reading", {
                "image": "North.png", "facade": "North", "reading_json": json.dumps(_reading())})
            reading = _json_result(prepared)
            assert len([item for item in prepared.content if item.type == "image"]) == 2
            assert reading["independently_verified"] is False
            assert (toolkit.run / reading["reading_file"]).is_file()
            assert "read_candidate_items" in reading["details_read"]
            adopted = _json_result(await session.call_tool("read_candidate_items", {
                "candidate": "", "collection": "report", "report_file": reading["details_file"],
                "json_pointer": "/artifact/openings"}))
            assert adopted["sha256"] == reading["details_sha256"]
            assert {row["id"]: [row["sill_m"], row["head_m"]] for row in adopted["value"]} == {
                "W1": [1.0, 2.2], "W2": [4.0, 5.2]}
            assert not process_status(toolkit, candidate)["ready"]
            assert not list((toolkit.run / "process_reviews").glob("review_*.json"))
            comparison = compare_reading(toolkit, reading["reading_id"], candidate)
            assert not comparison["current_consistent"] and comparison["height_difference_count"] == 2

            applied = await session.call_tool("apply_elevation_readings", {
                "candidate": candidate, "reading_ids": [reading["reading_id"]],
                "reviews_json": json.dumps([*_plan_reviews(),
                    _review("elevation", reading_id=reading["reading_id"])])})
            updated = _json_result(applied)
            assert updated["candidate"] != candidate
            assert updated["saved_candidate"] == updated["candidate"]
            assert updated["all_readings_consistent"] and updated["matched_openings"] == 2
            assert updated["comparison_summary"] == [{"reading_id": reading["reading_id"],
                "facade": "North", "current_consistent": True, "matched_count": 2,
                "issue_count": 0, "height_difference_count": 0}]
            assert any(item.type == "image" for item in applied.content)
            source = _source(toolkit, updated["candidate"])
            assert {row["id"]: sorted({point[2] for point in row["vertices"]})
                    for row in source["openings"]} == {"F1:W": [1.0, 2.2], "F2:W": [4.0, 5.2]}
            assert compare_reading(toolkit, reading["reading_id"], updated["candidate"])["current_consistent"]
            details = toolkit.run / updated["details_file"]
            assert digest(details) == updated["details_sha256"]
            full_updated = json.loads(details.read_bytes())
            assert all(row["source_model_sha256"] == source["source_model_sha256"]
                       for row in full_updated["source_image_projections"])
            comparison_receipt = _json_result(await session.call_tool("read_candidate_items", {
                "candidate": "", "collection": "report", "report_file": updated["details_file"],
                "json_pointer": "/comparisons/0/details_file"}))
            positions = _json_result(await session.call_tool("read_candidate_items", {
                "candidate": "", "collection": "report", "report_file": comparison_receipt["value"],
                "json_pointer": "/match/position_comparisons"}))
            assert positions["sha256"] == full_updated["comparisons"][0]["details_sha256"]
            assert positions["reference"]["item_count"] == 2
            position_ids = set()
            # Position reports may be paged; inspect scalar children through the
            # same returned report rather than relying on an inline size limit.
            for index in range(2):
                fields = {}
                for key in ("artifact_opening_id", "max_difference_m"):
                    page = _json_result(await session.call_tool("read_candidate_items", {
                        "candidate": "", "collection": "report", "report_file": comparison_receipt["value"],
                        "json_pointer": f"/match/position_comparisons/{index}/{key}"}))
                    fields[key] = page["value"]
                position_ids.add(fields["artifact_opening_id"])
                assert fields["max_difference_m"] <= 1e-6
            assert position_ids == {"W1", "W2"}
            assert len(list((toolkit.run / "claims").glob("claim_*.json"))) == 1
            applications = list((toolkit.run / "claims").glob("application_*.json"))
            assert len(applications) == 1
            audit = json.loads(applications[0].read_bytes())
            assert audit["status"] == "applied" and len(audit["resolved_operations"]) == 2
            assert _statuses(process_status(toolkit, updated["candidate"])) == {
                "plan:F1.png:F1": "consistent", "plan:F2.png:F2": "consistent",
                "elevation:North.png:North": "consistent"}

            assert not process_status(toolkit, updated["candidate"])["ready"]
            delivered = _json_result(await session.call_tool("finish_bim", {"candidate": updated["candidate"],
                "reviews_json": json.dumps([_review("building", outcome="uncertain",
                    summary="Two explicit floors and facade openings agree; vertical circulation is not supplied.",
                    issues=["No stair information in these synthetic inputs."])])}))
            assert process_status(toolkit, updated["candidate"])["ready"]
            assert delivered["candidate"] == updated["candidate"]
            assert delivered["source_model_sha256"] == source["source_model_sha256"]
            selection = json.loads((toolkit.run / "delivery_selection.json").read_bytes())
            assert selection == {"candidate": updated["candidate"], "source_model_sha256": source["source_model_sha256"]}
    asyncio.run(scenario())
    assert (toolkit.candidate_path(candidate) / "source_model.json").read_bytes() == old_bytes


def test_unknown_unreviewed_and_superseded_readings_are_refused_before_source_edits(case):
    toolkit, candidate = case
    before = {path.name for path in toolkit.run.glob("candidate_*")}
    first = _prepare(toolkit)["reading_id"]
    with pytest.raises(ValueError):
        apply_readings(toolkit, candidate, [first])
    record_reviews(toolkit, candidate, json.dumps([_review("elevation", reading_id=first)]))
    second = _prepare(toolkit)["reading_id"]
    assert second != first
    for identity in (first, "reading_9999", "../reading_0001"):
        with pytest.raises(ValueError):
            apply_readings(toolkit, candidate, [identity])
        with pytest.raises(ValueError):
            record_reviews(toolkit, candidate, json.dumps([_review("elevation", reading_id=identity)]))
    assert _statuses(process_status(toolkit, candidate))["elevation:North.png:North"] == "stale"
    assert {path.name for path in toolkit.run.glob("candidate_*")} == before
    assert not list((toolkit.run / "claims").glob("application_*.json"))


def test_mcp_host_height_preflight_locates_repair_before_any_candidate_or_claim_write(case):
    toolkit, candidate = case
    before = (toolkit.candidate_path(candidate) / "source_model.json").read_bytes()
    candidates = set(toolkit.run.glob("candidate_*"))
    reading = _reading()
    reading["openings"][1].update(evidence_type="annotation", x_px=[120, 160],
                                  width_m=2, sill_m=4, head_m=6.4)

    async def scenario():
        async with _server_session(toolkit.run, readonly=False) as session:
            prepared = _json_result(await session.call_tool("prepare_elevation_reading", {
                "image": "North.png", "facade": "North", "reading_json": json.dumps(reading)}))
            assert load_reading(toolkit, prepared["reading_id"])["artifact"]["openings"][1]["head_m"] == 6.4
            result = _json_result(await session.call_tool("apply_elevation_readings", {
                "candidate": candidate, "reading_ids": [prepared["reading_id"]],
                "reviews_json": json.dumps([_review("elevation", reading_id=prepared["reading_id"],
                    outcome="uncertain", issues=["Synthetic height annotation conflicts with the drawn upper opening."])])}))
            assert result["status"] == "blocked"
            assert result["candidate"] == result["result_candidate"] == candidate
            assert result["saved_candidate"] is None
            assert result["save_effects"] == {"created_candidates": [], "geometry_applied": False, "audit_written": False}
            conflict, = result["height_conflicts"]
            assert conflict["opening_id"] == "F2:W" and conflict["floor_id"] == "F2"
            assert conflict["requested_z_m"] == [4, 6.4] and conflict["host_z_m"] == [3, 6]
            assert "revise_plan_bim" in conflict["repair"] and "ceiling_height" in conflict["repair"]
            assert not result["all_readings_consistent"]
    asyncio.run(scenario())
    assert (toolkit.candidate_path(candidate) / "source_model.json").read_bytes() == before
    assert set(toolkit.run.glob("candidate_*")) == candidates
    assert not list((toolkit.run / "claims").glob("*.json"))
    assert not (toolkit.run / "delivery_selection.json").exists()


def test_review_batch_requires_full_current_overlay_and_validates_before_any_save(case):
    toolkit, candidate = case
    # A valid first review must not be written when the second scope is invalid.
    with pytest.raises(ValueError):
        record_reviews(toolkit, candidate, json.dumps([
            _plan_reviews()[0], _review("elevation", reading_id="reading_9999")]))
    assert not list((toolkit.run / "process_reviews").glob("review_*.json"))


def test_carried_reviews_bind_explicit_prior_candidate_and_do_not_approve_new_result(case):
    toolkit, candidate = case
    carry_reviews(toolkit, json.dumps([dict(row, candidate=candidate) for row in _plan_reviews()]))
    assert all(value == "consistent" for value in _statuses(process_status(toolkit, candidate)).values())
    result = toolkit.revise(candidate, json.dumps([{
        "op": "update_window", "id": "F2:W", "changes": {"span": [2.2, 4.2]},
        "reason": "Synthetic changed plan evidence.", "source_refs": ["Synthetic drawing."]}]))
    assert result["source_geometry_ready"]
    status = _statuses(process_status(toolkit, result["candidate"]))
    assert status["plan:F1.png:F1"] == "consistent" and status["plan:F2.png:F2"] == "stale"


def test_empty_carried_reviews_allow_normal_action_without_approving_result(case):
    toolkit, candidate = case

    async def scenario():
        async with _server_session(toolkit.run, readonly=False) as session:
            result = _json_result(await session.call_tool("revise_bim", {
                "candidate": candidate, "reviews_json": "[]",
                "operations_json": json.dumps([{
                    "op": "update_window", "id": "F2:W", "changes": {"span": [2.2, 4.2]},
                    "reason": "Synthetic changed plan evidence.", "source_refs": ["Synthetic drawing."]}])}))
            assert result["source_geometry_ready"]
            assert not list((toolkit.run / "process_reviews").glob("review_*.json"))
            assert not process_status(toolkit, result["candidate"])["ready"]
            finish = await session.call_tool("finish_bim", {
                "candidate": result["candidate"], "reviews_json": "[]"})
            assert "finish_bim" in _error_text(finish)
            assert not (toolkit.run / "delivery_selection.json").exists()
    asyncio.run(scenario())


def test_building_projection_difference_is_visible_without_moving_or_classifying_walls(case):
    toolkit, candidate = case
    source = _source(toolkit, candidate)
    same = building_facts(source)["floor_projection_comparisons"][0]
    assert same["symmetric_difference_m2"] == 0
    for space in source["spaces"]:
        if space["floor_id"] == "F2":
            for point in space["polygon"]:
                point[0] += 0.3
    before = copy.deepcopy(source)
    changed = building_facts(source)["floor_projection_comparisons"][0]
    assert changed["boundary_hausdorff_m"] == 0.3
    assert changed["symmetric_difference_m2"] == 6
    assert changed["difference_bounds_xy"]
    assert source == before


def test_partial_overlay_cannot_authorize_full_plan_review(case):
    toolkit, candidate = case
    for path in (toolkit.run / "image_overlays").glob("overlay_*.json"):
        metadata = json.loads(path.read_bytes())
        if metadata["candidate"] == candidate and metadata["floor_id"] == "F1":
            metadata["box_original_pixels"] = [20, 20, 80, 80]
            _write(path, metadata)
    with pytest.raises(ValueError):
        record_reviews(toolkit, candidate, json.dumps([_plan_reviews()[0]]))
    assert not list((toolkit.run / "process_reviews").glob("review_*.json"))


@pytest.mark.parametrize("policy", ["local_drawing_reviews_v1", POLICY, "unknown_policy"])
def test_declared_review_policy_cannot_silently_bypass_finish(case, policy):
    toolkit, candidate = case
    toolkit.manifest["drawing_review_policy"] = policy
    with pytest.raises(ValueError, match="drawing_process"):
        require_process_reviews(toolkit, candidate)


def test_reference_uses_issued_view_and_cannot_hide_an_elevation_reading(case):
    toolkit, candidate = case
    _, view = toolkit.view("North.png", coordinate_grid=False)
    view = json.loads(view)
    entry = _review("reference", image="North.png", view_id=view["view_id"],
                    summary="Synthetic auxiliary sheet classification.")
    record_reviews(toolkit, candidate, json.dumps([entry]))
    assert _statuses(process_status(toolkit, candidate))["reference:North.png"] == "consistent"
    _prepare(toolkit)
    assert any(row["reason"] == "missing_local_review" for row in process_status(toolkit, candidate)["pending"])
    with pytest.raises(ValueError, match="reference-only"):
        record_reviews(toolkit, candidate, json.dumps([entry]))


@pytest.mark.parametrize("review_intermediate", [False, True])
def test_explicit_direction_correction_retires_only_replaced_reading_and_keeps_new_review_required(case, review_intermediate):
    toolkit, candidate = case
    for index in range(2):
        _, wrong = prepare_elevation_reading(toolkit, "North.png", "East", json.dumps(_reading()))
        if index == 0 or review_intermediate:
            record_reviews(toolkit, candidate, json.dumps([_review("elevation", reading_id=wrong["reading_id"])]))
    # A shared-sheet second facade is independent, and must not disappear.
    blank = _reading()
    blank["openings"] = []
    for count in blank["counts"]:
        count["window_count"] = 0
    _, south = prepare_elevation_reading(toolkit, "North.png", "South", json.dumps(blank))
    _, fixed = prepare_elevation_reading(toolkit, "North.png", "North", json.dumps(_reading()),
                                         replaces_reading_id=wrong["reading_id"])
    status = process_status(toolkit, candidate)
    assert _statuses(status)["elevation:North.png:East"] == "superseded"
    assert {row["reading_id"] for row in status["pending"] if row["reason"] == "missing_local_review"} == {
        fixed["reading_id"], south["reading_id"]}
    with pytest.raises(ValueError):
        apply_readings(toolkit, candidate, [wrong["reading_id"]])
    record_reviews(toolkit, candidate, json.dumps([*_plan_reviews(), _review("elevation", reading_id=fixed["reading_id"])]))
    updated = apply_readings(toolkit, candidate, [fixed["reading_id"]])["candidate"]
    record_reviews(toolkit, updated, json.dumps([_review("building")]))
    assert not process_status(toolkit, updated)["ready"]
    record_reviews(toolkit, updated, json.dumps([_review("elevation", reading_id=south["reading_id"])]))
    assert process_status(toolkit, updated)["ready"]
    assert load_reading(toolkit, wrong["reading_id"])["facade"] == "East"


@pytest.mark.parametrize("evidence", ["metadata", "picture"])
def test_changed_plan_overlay_evidence_makes_review_stale(case, evidence):
    toolkit, candidate = case
    record_reviews(toolkit, candidate, json.dumps([_plan_reviews()[0]]))
    review = json.loads(next((toolkit.run / "process_reviews").glob("review_*.json")).read_bytes())
    path = toolkit.run / review["evidence_file"]
    if evidence == "metadata":
        value = json.loads(path.read_bytes())
        value["basis"] = "Changed after review."
        _write(path, value)
    else:
        path = toolkit.run / json.loads(path.read_bytes())["overlay_image"]
        path.write_bytes(path.read_bytes() + b"changed")
    status = process_status(toolkit, candidate)
    assert _statuses(status)["plan:F1.png:F1"] == "stale"
    assert "changed" in status["reviews"][0]["reason"]
    with pytest.raises(ValueError):
        require_process_reviews(toolkit, candidate)


def test_changed_calibration_needs_new_projection_before_new_review(case):
    toolkit, candidate = case
    record_reviews(toolkit, candidate, json.dumps([_plan_reviews()[0]]))
    _, calibration = next((path, row) for path, row in toolkit.registered_calibrations()
                          if row["image"] == "F1.png" and row["floor_id"] == "F1")
    toolkit._save_calibration(candidate=candidate, image="F1.png", floor_id="F1",
        x_anchors=[[20, 0], [200, 9.2]], y_anchors=calibration["y_anchors"],
        basis="Synthetic revised calibration.", metadata=_source(toolkit, candidate))
    assert _statuses(process_status(toolkit, candidate))["plan:F1.png:F1"] == "stale"
    with pytest.raises(ValueError):
        record_reviews(toolkit, candidate, json.dumps([_plan_reviews()[0]]))


@pytest.mark.parametrize("evidence", ["artifact", "overlay", "original"])
def test_changed_reading_evidence_refuses_review_and_apply(case, evidence):
    toolkit, candidate = case
    reading = _prepare(toolkit)
    identity = reading["reading_id"]
    record_reviews(toolkit, candidate, json.dumps([_review("elevation", reading_id=identity)]))
    if evidence == "artifact":
        path = toolkit.run / reading["reading_file"]
        record = json.loads(path.read_bytes())
        record["artifact"]["openings"][0]["sill_m"] += 0.1
        _write(path, record)
    else:
        path = toolkit.run / (reading["overlay_file"] if evidence == "overlay" else "images/North.png")
        path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        load_reading(toolkit, identity)
    with pytest.raises(ValueError):
        record_reviews(toolkit, candidate, json.dumps([_review("elevation", reading_id=identity)]))
    with pytest.raises(ValueError):
        apply_readings(toolkit, candidate, [identity])
    assert _statuses(process_status(toolkit, candidate))["elevation:North.png:North"] == "stale"
    assert not list((toolkit.run / "claims").glob("application_*.json"))


def test_changed_review_record_cannot_authorize_height_application(case):
    toolkit, candidate = case
    identity = _prepare(toolkit)["reading_id"]
    record_reviews(toolkit, candidate, json.dumps([_review("elevation", reading_id=identity)]))
    path = next((toolkit.run / "process_reviews").glob("review_*.json"))
    record = json.loads(path.read_bytes())
    record["outcome"] = "uncertain"
    _write(path, record)
    with pytest.raises(ValueError):
        process_status(toolkit, candidate)
    with pytest.raises(ValueError):
        apply_readings(toolkit, candidate, [identity])
    assert not list((toolkit.run / "claims").glob("application_*.json"))
