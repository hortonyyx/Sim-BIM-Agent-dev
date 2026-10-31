import copy
import json

import pytest

from src.agent.geometry.lite_bim_regularization import (
    compiled_grid_report, quantize_m, regularize_lite_plan,
)
from src.agent.geometry.plan_dimension_alignment import align_plan_to_dimensions
from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.geometry.plan_regularization import PlanRegularizationError, regularize_plan, regularize_plan_stack
from src.agent.runtime_roles.plan_format import plan_format_errors

SIZE = (1501, 1001)


def plan():
    return {
        "floor_id": "F1", "z_floor": .02, "ceiling_height": 3.94,
        "x_anchors": [[100, 0], [1300, 12]], "y_anchors": [[100, 8], [900, 0]],
        "footprint_pixels": [[100, 100], [1300, 100], [1300, 900], [100, 900]],
        "partitions": [{"id": "A", "points": [[494, 100], [494, 900]], "source_refs": ["drawing"]},
                       {"id": "B", "points": [[888, 100], [888, 900]], "source_refs": ["drawing"]}],
        "space_seeds": [{"id": "L", "point": [200, 300]}, {"id": "M", "point": [600, 300]},
                        {"id": "R", "point": [1000, 300]}],
        "openings": [{"id": "D", "kind": "door", "p1": [494, 420], "p2": [494, 514],
                      "z": [.02, 2.13], "source_refs": ["drawing"]},
                     {"id": "W", "kind": "window", "p1": [950, 100], "p2": [1094, 100],
                      "z": [.94, 2.64], "source_refs": ["drawing"]}],
        "basis": "synthetic annotated 3940 + 3940 + 4120", "assumptions": [], "unresolved": [],
    }


def chain(**kwargs):
    return {"id": "X", "axis": "x", "segments_mm": [3940, 3940, 4120],
            "tick_pixels": [100, 494, 888, 1300], "source_refs": ["drawing dimensions"], **kwargs}


def test_cumulative_dimension_chain_closes_without_independent_width_rounding():
    original = plan()
    original["dimension_chains"] = [chain()]
    assert plan_format_errors({**original, "reading_type": "plan"}) == []
    aligned, report = align_plan_to_dimensions(original, grid_step_m=.1)
    row = report["items"][0]
    assert row["adopted_tick_world_m"] == [0, 3.9, 7.9, 12]
    assert row["adopted_segments_mm"] == [3900, 4000, 4100]
    assert row["original_segments_mm"] == [3940, 3940, 4120]
    effective, grid = regularize_lite_plan(aligned, image_size=SIZE)
    compiled, _ = compile_plan_partition(effective, image_size=SIZE, image_name="plan")
    assert compiled_grid_report(compiled)["status"] == "pass"
    assert len(compiled["geometry"]["floors"][0]["cells"]) == 3


def test_explicit_fine_dimensions_hosts_xyz_and_idempotent_recompile():
    original = plan()
    effective, report = regularize_lite_plan(original, image_size=SIZE)
    assert original == plan()
    assert effective["partitions"][0]["points"][0][0] == 490
    assert effective["openings"][0]["p1"][0] == 490
    assert effective["z_floor"] == 0 and effective["ceiling_height"] == 4
    assert effective["space_seeds"] == original["space_seeds"]
    assert report["topology"]["before"] == report["topology"]["after"]
    again, second = regularize_lite_plan(effective, image_size=SIZE)
    assert again == effective and second == report
    final, receipt = regularize_plan(effective, image_size=SIZE, image_name="plan")
    assert final["partitions"] == effective["partitions"]
    assert receipt["lite_bim"]["compiled_grid"]["status"] == "pass"


def test_trusted_grid_parameter_overrides_plan_audit_and_keeps_original_readings():
    from src.agent.geometry.plan_alignment import _set_reading_alignment
    original = plan()
    _set_reading_alignment(original, "dimensions", {"status": "not_supplied"})
    original["regularization_inputs"]["reading_alignment"]["lite_bim"] = {"grid_step_m": .2}
    before = copy.deepcopy(original)
    effective, report = regularize_plan(original, image_size=SIZE, image_name="plan", grid_step_m=.1)
    assert original == before
    assert report["method"]["grid_step_m"] == .1
    assert effective["partitions"][0]["points"][0][0] == 490
    row = next(row for row in report["lite_bim"]["items"] if row["path"] == "partitions:A.points[0].x")
    assert row["original_m"] == pytest.approx(3.94) and row["adopted_m"] == 3.9
    assert report["lite_bim"]["topology"]["status"] == "preserved"


def test_trusted_stack_grid_applies_to_unmarked_plans_without_mutating_inputs():
    items = [{"plan": plan(), "floor_id": f"F{i + 1}", "image_size": SIZE,
              "image_name": "plan", "z_floor": i * 4., "source_ref": "drawing"} for i in range(2)]
    before = copy.deepcopy(items)
    result, report = regularize_plan_stack(items, grid_step_m=.1)
    assert items == before and report["status"] == "pass"
    for item in result:
        assert compiled_grid_report(item["proposal"])["status"] == "pass"
        assert report["floor_reports"][item["floor_id"]]["method"]["grid_step_m"] == .1


def test_digest_projections_preserve_full_evidence_hashes_and_input():
    import hashlib
    import json
    from src.agent.geometry.lite_bim_regularization import _geometry_digest
    from src.agent.geometry.plan_regularization import _plan_digest
    effective, _ = regularize_lite_plan(plan(), image_size=SIZE)
    before = copy.deepcopy(effective)
    expected = copy.deepcopy(effective)
    expected.pop('regularization', None)
    digest = lambda value, **kwargs: hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(',', ':'), **kwargs).encode()).hexdigest()
    assert _plan_digest(effective) == digest(expected, ensure_ascii=False)
    expected['regularization_inputs']['reading_alignment'].pop('lite_bim', None)
    assert _geometry_digest(effective) == digest(expected)
    assert effective == before


def test_local_chain_uses_first_tick_calibration_and_small_tail_is_code_managed():
    value = plan()
    value["dimension_chains"] = [chain(segments_mm=[3940], tick_pixels=[494, 888], total_mm=3960)]
    aligned, report = align_plan_to_dimensions(value, grid_step_m=.1)
    row = report["items"][0]
    assert row["action"] == "local_ticks_applied"
    assert row["adopted_tick_world_m"] == [3.9, 7.9]
    assert row["closure_error_mm"] == -20
    _, legacy = align_plan_to_dimensions(value)
    assert legacy["rejections"]
    value["dimension_chains"][0]["total_mm"] = 4500
    _, bad = align_plan_to_dimensions(value, grid_step_m=.1)
    assert bad["rejections"]


@pytest.mark.parametrize("value", [3.94, 3.95, 3.96, .05, 0, 100.15])
def test_quantization_is_mirror_symmetric(value):
    assert quantize_m(-value) == -quantize_m(value)


def test_conflicting_local_chain_cannot_overwrite_shared_node():
    value = plan()
    value["dimension_chains"] = [chain(), chain(id="local", segments_mm=[3940],
                                                tick_pixels=[494, 888], start_world_m=4.1)]
    _, report = align_plan_to_dimensions(value, grid_step_m=.1)
    assert any(row["reason"] == "shared dimension-chain node conflicts" for row in report["rejections"])


def test_mirrored_plan_geometry_and_opening_hosts_survive():
    value = plan()
    value["x_anchors"] = [[100, 0], [1300, -12]]
    effective, report = regularize_lite_plan(value, image_size=SIZE)
    proposal, _ = compile_plan_partition(effective, image_size=SIZE, image_name="plan")
    assert compiled_grid_report(proposal)["status"] == "pass"
    assert report["topology"]["status"] == "preserved"


def test_local_opening_rework_does_not_resnap_untouched_geometry():
    effective, _ = regularize_lite_plan(plan(), image_size=SIZE)
    revised = copy.deepcopy(effective)
    revised["openings"][0]["p2"][1] = 524
    final, _ = regularize_lite_plan(revised, image_size=SIZE)
    assert final["partitions"] == effective["partitions"]
    assert final["footprint_pixels"] == effective["footprint_pixels"]
    assert final["openings"][1] == effective["openings"][1]
    assert final["openings"][0]["p2"][1] == 520


def test_real_narrow_room_is_rejected_not_silently_merged():
    value = plan()
    value["partitions"][1]["points"] = [[534, 100], [534, 900]]
    value["space_seeds"][1]["point"] = [510, 200]
    with pytest.raises(PlanRegularizationError, match="hard constraint") as error:
        regularize_lite_plan(value, image_size=SIZE)
    assert error.value.report["status"] == "rejected"
    assert len(value["space_seeds"]) == 3


def test_rework_tracks_new_observation_without_accumulating_previous_grid_move():
    effective, _ = regularize_lite_plan(plan(), image_size=SIZE)
    revised = copy.deepcopy(effective)
    for p in revised["partitions"][0]["points"]:
        p[0] = 504
    for field in ("p1", "p2"):
        revised["openings"][0][field][0] = 504
    result, report = regularize_lite_plan(revised, image_size=SIZE)
    move = next(r for r in report["changes"] if r["type"] == "lite_grid_coordinate"
                and r["axis"] == "x" and abs(r["to_m"] - 4) < 1e-8)
    assert move["from_m"] == pytest.approx(4.04)
    assert move["movement_m"] == pytest.approx(-.04)
    assert move["before_relationships"] == move["after_relationships"]
    assert result["partitions"][1] == effective["partitions"][1]


def test_reader_first_draft_default_uses_grid_and_keeps_located_failure(tmp_path):
    from PIL import Image
    from src.agent.runtime_roles.trial import PlanTrial, _reading_align
    image = tmp_path / "plan.png"
    Image.new("RGB", SIZE, "white").save(image)
    trial = PlanTrial(None, image_name="plan.png")
    assert trial.grid_step_m == .1
    current = plan()
    current["reading_type"] = "plan"
    current["wall_metadata"] = [dict(target=t, thickness_m=.1, reference_side="centerline", evidence_status="inferred", basis="Synthetic assumption", source_refs=["plan.png:fixture"]) for t in ("footprint:*", "partition:*")]
    value, reading, alignment = _reading_align(current, image_path=image, image_name="plan.png", grid_step_m=.1)
    assert alignment["status"] == "lite_grid_applied"
    assert reading["lite_bim"]["compiled_grid"]["status"] == "pass"
    assert value["partitions"][0]["points"][0][0] == 490
    broken = copy.deepcopy(current)
    broken["openings"][0]["p1"][0] += 200
    with pytest.raises(PlanRegularizationError, match="adjusted draft") as failure:
        _reading_align(broken, image_path=image, image_name="plan.png", grid_step_m=.1)
    assert "opening D" in json.dumps(failure.value.report)


def test_stack_preserves_grid_and_shared_storey_levels():
    lower, _ = regularize_lite_plan(plan(), image_size=SIZE)
    upper = copy.deepcopy(lower)
    upper["floor_id"] = "F2"
    upper["z_floor"] = 4
    for opening in upper["openings"]:
        opening["z"] = [z + 4 for z in opening["z"]]
    items = [{"plan": p, "floor_id": p["floor_id"], "image_size": SIZE,
              "image_name": "plan", "z_floor": p["z_floor"], "source_ref": "drawing"}
             for p in (lower, upper)]
    result, report = regularize_plan_stack(items)
    assert report["status"] == "pass"
    assert not any(row["type"].startswith("lite_grid") for row in report["changes"])
    for item in result:
        assert compiled_grid_report(item["proposal"])["status"] == "pass"
        from src.agent.geometry.plan_regularization import _plan_digest
        assert report["floor_reports"][item["floor_id"]]["plan_sha256"] == _plan_digest(item["plan"])
    mixed = copy.deepcopy(items)
    mixed[1]["plan"]["regularization_inputs"]["reading_alignment"].pop("lite_bim")
    with pytest.raises(PlanRegularizationError, match="same grid_step_m"):
        regularize_plan_stack(mixed)


def test_stack_preserves_target_placement_and_assembly_translates_openings():
    from src.agent.geometry.plan_assembly import assemble_plan_proposals
    value = plan()
    value["floor_id"] = "F2"
    value["z_floor"] += 3
    for opening in value["openings"]:
        opening["z"] = [z + 3 for z in opening["z"]]
    effective, _ = regularize_lite_plan(value, image_size=SIZE)
    items = [{"plan": effective, "floor_id": "F2", "z_floor": 3.9, "height": 4,
              "image_size": SIZE, "image_name": "plan", "source_ref": "drawing"}]
    result, _ = regularize_plan_stack(items)
    assert result[0]["z_floor"] == 3.9 and result[0]["height"] == 4
    assert result[0]["plan"]["z_floor"] == 3
    assembled = assemble_plan_proposals([{key: result[0][key] for key in
                                          ("proposal", "floor_id", "z_floor", "height", "source_ref")}])
    assert assembled["geometry"]["floors"][0]["z_floor"] == 3.9
    door = next(o for o in assembled["geometry"]["openings"] if o["id"] == "F2:D")
    assert door["z"] == [3.9, 6]
    assert compiled_grid_report(assembled)["status"] == "pass"
    items[0]["z_floor"] = 3.94
    with pytest.raises(PlanRegularizationError, match="target floor placement/height is off grid"):
        regularize_plan_stack(items)
