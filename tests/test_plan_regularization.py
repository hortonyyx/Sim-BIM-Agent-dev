"""Q1 A-C: opt-in draft regularisation and post-save hard constraints."""
from __future__ import annotations

import copy

import pytest

from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.geometry.plan_regularization import (
    PlanRegularizationError,
    _attach_suspended_endpoints,
    _calibration,
    _complete_open_passage_boundaries,
    _plan_digest,
    _segments,
    _stack_offset_pairs,
    enforce_regularized_plan,
    prepare_plan_junctions,
    regularize_plan,
    regularize_plan_stack,
    validate_regularized_plan,
    validate_regularized_plan_stack,
)


IMAGE_SIZE = (101, 101)


def _plan(partitions, *, seeds=None, openings=None, floor_id="F1", z_floor=0.0,
          footprint=None):
    return {
        "floor_id": floor_id,
        "z_floor": z_floor,
        "ceiling_height": 3.0,
        "x_anchors": [[0, 0.0], [100, 10.0]],
        "y_anchors": [[0, 10.0], [100, 0.0]],
        "basis": "synthetic exact 0.1 m/pixel calibration",
        "footprint_pixels": footprint or [[0, 0], [100, 0], [100, 100], [0, 100]],
        "partitions": [
            {"id": identity, "points": copy.deepcopy(points), "source_refs": [f"drawing:{identity}"]}
            for identity, points in partitions
        ],
        "openings": copy.deepcopy(openings or []),
        "space_seeds": copy.deepcopy(seeds or []),
        "assumptions": [],
        "unresolved": [],
    }


def _door(identity, x, y1=60, y2=70):
    return {"id": identity, "kind": "door", "p1": [x, y1], "p2": [x, y2],
            "z": [0.0, 2.1], "state": "closed", "source_refs": [f"drawing:{identity}"]}


def _stack_items(lower, upper):
    return [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:upper"},
    ]


def _opening_alignment_evidence(opening_id, *, gap=True, hard_width=False):
    ink_item = {
        "object": f"opening:{opening_id}", "kind": "opening",
        "gap_evidence": {"status": "supported"} if gap else None,
    }
    dimensions = ([{"object": f"opening:{opening_id}", "kind": "opening_width",
                    "value_m": 1.8, "basis": "dimension"}]
                  if hard_width else [])
    section = lambda items: {
        "summary": {}, "items": items, "rejections": [],
        "search_or_tolerance": {},
    }
    return {
        "schema_version": "synthetic_reading_alignment_v1",
        "ink": section([ink_item]), "dimensions": section(dimensions),
    }


def test_direct_compiler_remains_literal_until_regularization_is_explicit():
    plan = _plan([("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]])])
    original = copy.deepcopy(plan)
    proposal, metadata = compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert plan == original
    assert len(proposal["geometry"]["floors"][0]["cells"]) == 3
    assert metadata["method"]["snap"] is False


@pytest.mark.parametrize("terminal_x", [39.9996, 40.0004])
def test_precompile_joins_tiny_gap_or_overshoot_and_projects_whole_opening(terminal_x):
    plan = _plan(
        [("vertical", [[40, 0], [40, 100]]),
         ("horizontal", [[0, 50], [terminal_x, 50]])],
        seeds=[{"id": "upper", "point": [20, 20]},
               {"id": "lower", "point": [20, 70]},
               {"id": "right", "point": [80, 50]}],
        openings=[{**_door("door", 0), "p1": [10, 50.0004], "p2": [20, 50.0004]}],
    )
    original = copy.deepcopy(plan)
    with pytest.raises(ValueError):
        compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    prepared, report = prepare_plan_junctions(plan, image_size=IMAGE_SIZE)
    proposal, _ = compile_plan_partition(prepared, image_size=IMAGE_SIZE, image_name="plan.png")
    assert prepared["partitions"][1]["points"][-1] == [40, 50]
    assert prepared["openings"][0]["p1"] == [10, 50]
    assert prepared["openings"][0]["p2"] == [20, 50]
    assert len(proposal["geometry"]["floors"][0]["cells"]) == 3
    assert prepared["space_seeds"] == plan["space_seeds"]
    assert plan == original and report["status"] == "applied"
    assert {row["type"] for row in report["changes"]} == {
        "join_near_endpoint", "project_opening_to_host"}
    assert max(report["tolerance_m"].values()) <= .30


def test_precompile_rolls_back_all_edits_if_a_real_missing_divider_remains():
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[0, 50], [39.9996, 50]])],
        seeds=[{"id": "first", "point": [10, 20]}, {"id": "second", "point": [20, 20]}],
    )
    prepared, report = prepare_plan_junctions(plan, image_size=IMAGE_SIZE)
    assert prepared == plan and report["status"] == "rolled_back"
    assert report["changes"] == [] and report["attempted_changes"]
    assert "first and second occupy the same space" in report["compile_error"]
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert caught.value.report["changes"] == []
    assert caught.value.report["junction_preparation"] == report


def test_precompile_never_bridges_over_tolerance_or_shortens_openings():
    for plan in (
        _plan([("too-far", [[3.01, 50], [100, 50]])]),
        _plan([("L", [[40, 0], [40, 50], [100, 50]])],
              openings=[_door("long-door", 40.001, 40, 70)]),
    ):
        prepared, report = prepare_plan_junctions(plan, image_size=IMAGE_SIZE)
        assert prepared == plan and report["compile_error"]
        assert report["changes"] == []


def test_precompile_partial_collinear_overlap_preserves_union_and_declared_rooms():
    plan = _plan(
        [("top", [[40, 0], [40, 60]]), ("bottom", [[40, 40], [40, 100]])],
        seeds=[{"id": "left", "point": [20, 50]}, {"id": "right", "point": [70, 50]}],
    )
    prepared, report = prepare_plan_junctions(plan, image_size=IMAGE_SIZE)
    proposal, _ = compile_plan_partition(prepared, image_size=IMAGE_SIZE, image_name="plan.png")
    assert len(proposal["geometry"]["floors"][0]["cells"]) == 2
    assert [row["id"] for row in prepared["partitions"]] == ["top", "bottom"]
    assert prepared["partitions"][1]["points"] == [[40, 60], [40, 100]]
    assert prepared["space_seeds"] == plan["space_seeds"]
    assert report["changes"][0]["partition_id"] == "bottom"


def test_precompile_respects_total_movement_and_equal_target_ambiguity():
    for partitions in (
        [("short-vertical", [[40, 0], [40, 47.5]]),
         ("short-horizontal", [[42.5, 50], [100, 50]])],
        [("vertical", [[40, 0], [40, 50]]),
         ("upper", [[0, 49], [100, 49]]),
         ("lower", [[0, 51], [100, 51]])],
    ):
        plan = _plan(partitions)
        prepared, report = prepare_plan_junctions(plan, image_size=IMAGE_SIZE)
        assert prepared == plan
        assert not report["changes"]
        assert not any(row["type"] == "join_near_endpoint" for row in report["attempted_changes"])


def test_duplicate_cleanup_transfers_dimension_evidence_to_retained_wall():
    plan = _plan([("first", [[40, 0], [40, 100]]),
                  ("dimensioned", [[40, 0], [40, 100]]),
                  ("nearby", [[42, 0], [42, 100]])])
    plan["regularization_inputs"] = {"line_references": [
        {"partition_id": "dimensioned", "basis": "dimension", "source_refs": ["dimension:40"]}]}
    prepared, report = prepare_plan_junctions(plan, image_size=IMAGE_SIZE)
    assert prepared["regularization_inputs"]["line_references"] == [
        {"partition_id": "first", "basis": "dimension", "source_refs": ["dimension:40"]}]
    assert "drawing:dimensioned" in prepared["partitions"][0]["source_refs"]
    assert report["changes"][0]["reference_survivor_ids"] == ["first"]
    regularized, _ = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert [row["id"] for row in regularized["partitions"]] == ["first"]
    assert regularized["partitions"][0]["points"] == [[40, 0], [40, 100]]


def test_saved_rule_metadata_only_short_circuits_the_exact_saved_plan():
    plan, first = regularize_plan(
        _plan([("A", [[40, 0], [40, 100]])]),
        image_size=IMAGE_SIZE, image_name="plan.png",
    )
    replay, replay_report = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert replay == plan and replay_report == first
    plan["partitions"].append({"id": "B", "points": [[42, 0], [42, 100]],
                               "source_refs": ["drawing:B"]})
    revised, report = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert [row["id"] for row in revised["partitions"]] == ["A"]
    assert report["summary"]["change_counts"] == {"merge_duplicate_wall_lines": 1}


def test_forged_matching_regularization_digest_cannot_skip_hard_rules():
    plan = _plan([("A", [[40, 0], [40, 100]]), ("B", [[43, 0], [43, 100]])])
    plan["regularization"] = {
        "schema": "plan_regularization_report_v1", "rule_version": "plan_regularization_v1",
        "status": "pass", "plan_sha256": _plan_digest(plan),
        "changes": [{"type": "caller_claimed_pass"}], "rejections": [],
    }
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert any(row["type"] == "space_width_under_0_60m"
               for row in caught.value.report["violations"])


def test_parallel_strip_without_seed_or_opening_merges_to_existing_priority_line():
    plan = _plan([
        ("measured", [[40, 0], [40, 100]]),
        ("dimensioned", [[42, 0], [42, 100]]),
        ("separate", [[70, 0], [70, 100]]),
    ])
    plan["regularization_inputs"] = {
        "line_references": [
            {"partition_id": "measured", "basis": "measured", "source_refs": ["drawing:pixel"]},
            {"partition_id": "dimensioned", "basis": "dimension", "source_refs": ["drawing:dimension"]},
        ],
        "coordinate_references": [{"axis": "x", "value_m": 4.2, "basis": "dimension",
                                    "chain_id": "X1", "tick_index": 1,
                                    "source_refs": ["drawing:dimension"]}],
    }
    regularized, report = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert {row["id"] for row in regularized["partitions"]} == {"dimensioned", "separate"}
    assert report["status"] == "pass"
    merge = next(row for row in report["changes"] if row["type"] == "merge_duplicate_wall_lines")
    assert merge["movement_m"] == pytest.approx(.2)
    assert merge["retained_partition_id"] == "dimensioned"
    assert report["preserved_separations"][0]["unchanged"] is True
    assert regularized["regularization"]["rule_version"] == "plan_regularization_v1"
    proposal, metadata = compile_plan_partition(
        regularized, image_size=IMAGE_SIZE, image_name="plan.png",
    )
    assert len(proposal["geometry"]["floors"][0]["cells"]) == 3
    assert metadata["regularization"]["changes"] == report["changes"]
    assert metadata["method"]["snap"] is False


def test_named_seed_in_narrow_strip_is_removed_with_applied_audit():
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]])],
        seeds=[{"id": "named-shaft", "point": [41, 50], "role": "shaft"}],
    )
    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert regularized["space_seeds"] == []
    removal = next(row for row in report["changes"]
                   if row["type"] == "remove_narrow_strip_space_seeds")
    assert removal["object_ids"]["space_seeds"] == ["named-shaft"]
    assert report["semantic_mapping"]["removed_space_seeds"][0]["id"] == "named-shaft"
    assert report["semantic_mapping"]["status"] == "applied"


def test_openings_on_both_duplicate_lines_move_and_overlap_collapses_but_disjoint_survives():
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]]),
         ("far", [[70, 0], [70, 100]])],
        openings=[
            _door("target-door", 40, 20, 30),
            _door("source-overlap", 42, 22, 32),
            _door("source-disjoint", 42, 60, 70),
            _door("far-door", 70, 40, 50),
        ],
    )
    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert {row["id"] for row in regularized["openings"]} == {
        "target-door", "source-disjoint", "far-door"}
    assert all(row["p1"][0] == row["p2"][0] == 40
               for row in regularized["openings"] if row["id"] != "far-door")
    merged = next(row for row in report["changes"]
                  if row["type"] == "merge_overlapping_openings")
    assert merged["retained_opening_id"] == "target-door"
    assert merged["removed_opening_ids"] == ["source-overlap"]
    mapping = {row["old_opening_id"]: row["surviving_opening_id"]
               for row in report["semantic_mapping"]["opening_id_map"]}
    assert mapping == {
        "source-disjoint": "source-disjoint",
        "source-overlap": "target-door",
        "target-door": "target-door",
    }
    assert "far-door" not in report["semantic_mapping"]["affected_opening_ids"]
    assert all(row["after_connection"] is not None
               for row in report["semantic_mapping"]["opening_id_map"])


def test_incompatible_overlapping_openings_reject_with_contradiction_and_no_applied_changes():
    window = {**_door("window", 42, 20, 30), "kind": "window", "z": [1.0, 2.0]}
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]])],
        openings=[_door("door", 40, 20, 30), window],
    )
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    report = caught.value.report
    conflict = next(row for row in report["rejections"]
                    if row["type"] == "narrow_strip_opening_conflict")
    assert conflict["contradiction_category"] == (
        "overlapping_openings_have_incompatible_kinds")
    assert report["changes"] == []
    assert report["semantic_mapping"]["status"] == "attempted_not_saved"


def test_opening_defaults_and_z_differences_merge_by_compiled_connectivity_meaning():
    target = _door("target", 40, 20, 30)
    target.pop("state")  # door default is effective "unknown"
    source = _door("source", 42, 22, 32)
    source["state"] = "unknown"
    source["z"] = [0.2, 2.4]
    target_open = {**_door("target-open", 40, 60, 70), "kind": "open"}
    target_open.pop("state")  # open passage default is effective "open"
    source_open = {**_door("source-open", 42, 62, 72), "kind": "open", "state": "open"}
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]])],
        openings=[target, source, target_open, source_open],
    )
    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert [row["id"] for row in regularized["openings"]] == ["target", "target-open"]
    merged = [row for row in report["changes"]
              if row["type"] == "merge_overlapping_openings"]
    door_merge = next(row for row in merged if row["retained_opening_id"] == "target")
    open_merge = next(row for row in merged if row["retained_opening_id"] == "target-open")
    assert door_merge["effective_state"] == "unknown"
    assert open_merge["effective_state"] == "open"
    assert door_merge["chosen_z_m"] == [0.0, 2.1]
    assert {tuple(row["z"]) for row in door_merge["before_z_m"]} == {
        (0.0, 2.1), (0.2, 2.4)}


def test_effective_passage_state_conflict_is_rejected_even_when_kind_matches():
    target = _door("target", 40, 20, 30)
    target.pop("state")
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]])],
        openings=[target, _door("closed", 42, 22, 32)],
    )
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert caught.value.report["rejections"][0]["contradiction_category"] == (
        "overlapping_openings_have_incompatible_passage_states")


def test_target_line_duplicates_and_transitive_overlap_group_collapse_together():
    plan = _plan(
        [("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]])],
        openings=[
            _door("target-a", 40, 20, 30),
            _door("target-b", 40, 25, 35),
            _door("source", 42, 32, 40),
        ],
    )
    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert [row["id"] for row in regularized["openings"]] == ["target-a"]
    merged = next(row for row in report["changes"]
                  if row["type"] == "merge_overlapping_openings")
    assert set(merged["removed_opening_ids"]) == {"target-b", "source"}
    assert merged["after_span_m"] == [6.0, 8.0]


def test_more_collinear_disjoint_wall_pieces_win_same_floor_merge_priority():
    plan = _plan([
        ("shared-top", [[40, 0], [40, 40]]),
        ("shared-bottom", [[40, 60], [40, 100]]),
        ("single", [[42, 0], [42, 100]]),
    ])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert all(point[0] == pytest.approx(40)
               for partition in regularized["partitions"]
               for point in partition["points"])
    merges = [row for row in report["changes"]
              if row["type"] == "merge_duplicate_wall_lines"]
    assert len(merges) == 2
    assert all(row["to_m"] == pytest.approx(4.0) for row in merges)
    assert all(row["basis"] == "more_collinear_wall_segments_then_line_priority"
               for row in merges)
    assert merges[0]["target_coordinate_support"] == {
        "floors": 1, "collinear_wall_segments": 2,
    }
    assert merges[0]["source_coordinate_support"] == {
        "floors": 1, "collinear_wall_segments": 1,
    }


def test_exactly_overlapping_internal_wall_lines_are_deduplicated():
    plan = _plan([
        ("A", [[40, 0], [40, 100]]),
        ("B", [[40, 0], [40, 100]]),
    ])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert [row["id"] for row in regularized["partitions"]] == ["A"]
    merge = next(row for row in report["changes"]
                 if row["type"] == "remove_redundant_partition_segments")
    assert merge["partition_id"] == "B" and merge["surviving_partition_ids"] == []
    assert merge["movement_m"] == 0
    assert report["hard_constraints"]["status"] == "pass"


def test_internal_wall_exactly_on_fixed_footprint_is_deduplicated():
    plan = _plan([("duplicate-west", [[0, 0], [0, 100]])])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["partitions"] == []
    merge = next(row for row in report["changes"]
                 if row["type"] == "remove_redundant_partition_segments")
    assert merge["partition_id"] == "duplicate-west" and merge["surviving_partition_ids"] == []
    assert merge["movement_m"] == 0
    assert report["hard_constraints"]["status"] == "pass"


def test_polyline_leg_merges_without_deleting_its_other_legs_and_inherits_refs():
    plan = _plan([
        ("poly", [[40, 0], [40, 50], [60, 50], [60, 100]]),
        ("straight", [[42, 0], [42, 50]]),
    ])
    plan["regularization_inputs"] = {
        "line_references": [{
            "partition_id": "poly", "basis": "dimension",
            "source_refs": ["drawing:dimension"],
        }],
        "coordinate_references": [],
    }

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    partitions = {row["id"]: row for row in regularized["partitions"]}
    assert partitions["poly"]["points"] == [[40, 0], [40, 50]]
    assert partitions["poly::segment-2"]["points"] == [[40, 50], [60, 50]]
    assert partitions["poly::segment-3"]["points"] == [[60, 50], [60, 100]]
    assert "straight" not in partitions
    split = next(row for row in report["changes"]
                 if row["type"] == "split_polyline_for_merge")
    assert split["geometry_changed"] is False and split["movement_m"] == 0
    assert [row["partition_id"] for row in split["segment_mapping"]] == [
        "poly", "poly::segment-2", "poly::segment-3",
    ]
    inherited = regularized["regularization_inputs"]["line_references"]
    assert {row["partition_id"] for row in inherited} == {
        "poly", "poly::segment-2", "poly::segment-3",
    }


def test_polyline_merge_can_select_the_straight_line_without_losing_other_legs():
    plan = _plan([
        ("poly", [[40, 0], [40, 50], [60, 50], [60, 100]]),
        ("dimensioned", [[42, 0], [42, 50]]),
    ])
    plan["regularization_inputs"] = {
        "line_references": [{
            "partition_id": "dimensioned", "basis": "dimension",
            "source_refs": ["drawing:dimension"],
        }],
        "coordinate_references": [],
    }

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    partitions = {row["id"]: row for row in regularized["partitions"]}
    assert partitions["dimensioned"]["points"] == [[42, 0], [42, 50]]
    assert partitions["poly::segment-2"]["points"] == [[42.0, 50], [60, 50]]
    assert partitions["poly::segment-3"]["points"] == [[60, 50], [60, 100]]
    assert "poly" not in partitions
    merge = next(row for row in report["changes"]
                 if row["type"] == "merge_duplicate_wall_lines")
    assert merge["retained_partition_id"] == "dimensioned"
    assert merge["removed_partition_id"] == "poly"


def test_polyline_leg_can_merge_into_fixed_footprint_without_moving_outline():
    plan = _plan([("poly", [[2, 0], [2, 50], [100, 50]])])
    footprint_before = copy.deepcopy(plan["footprint_pixels"])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["footprint_pixels"] == footprint_before
    assert [(row["id"], row["points"]) for row in regularized["partitions"]] == [
        ("poly", [[0.0, 50], [100, 50]]),
    ]
    assert [row["type"] for row in report["changes"]] == [
        "join_partition_to_footprint", "remove_redundant_partition_segments",
    ]
    assert report["hard_constraints"]["status"] == "pass"


def test_polyline_partial_overlap_retains_real_nonoverlap_and_connections():
    plan = _plan([
        ("poly", [[40, 0], [40, 50], [60, 50], [60, 100]]),
        ("long", [[42, 20], [42, 80]]),
        ("top-connection", [[42, 20], [0, 20]]),
        ("bottom-connection", [[42, 80], [100, 80]]),
    ])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    partitions = {row["id"]: row for row in regularized["partitions"]}
    assert partitions["poly"]["points"] == [[42.0, 20.0], [42.0, 0.0]]
    assert partitions["long"]["points"] == [[42, 20], [42, 80]]
    assert partitions["poly::segment-2"]["points"] == [[42.0, 50], [60, 50]]
    merge = next(row for row in report["changes"]
                 if row["type"] == "merge_duplicate_wall_lines")
    assert merge["span_m"] == [5.0, 8.0]
    assert merge["retained_remainder_partition_ids"] == ["poly"]
    assert report["hard_constraints"]["status"] == "pass"


def test_polyline_merge_moves_opening_and_removes_named_strip_seed_with_audit():
    plan = _plan([
        ("poly", [[40, 0], [40, 50], [60, 50], [60, 100]]),
        ("source", [[42, 0], [42, 50]]),
    ], seeds=[{"id": "strip", "point": [41, 25], "role": "office"}],
       openings=[_door("strip-door", 42, 20, 30)])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["space_seeds"] == []
    assert regularized["openings"][0]["p1"][0] == 40
    assert regularized["openings"][0]["p2"][0] == 40
    assert {row["type"] for row in report["changes"]} >= {
        "split_polyline_for_merge", "remove_narrow_strip_space_seeds",
        "move_openings_to_merged_wall", "merge_duplicate_wall_lines",
    }
    mapping = report["semantic_mapping"]["opening_id_map"][0]
    assert mapping["old_opening_id"] == mapping["surviving_opening_id"] == "strip-door"
    assert mapping["after_connection"]["connected_space_count"] == 2


def test_polyline_merge_real_opening_conflict_rolls_back_split_and_merge():
    window = {**_door("window", 42, 20, 30), "kind": "window", "z": [1.0, 2.0]}
    plan = _plan([
        ("poly", [[40, 0], [40, 50], [60, 50], [60, 100]]),
        ("source", [[42, 0], [42, 50]]),
    ], openings=[_door("door", 40, 20, 30), window])

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")

    report = caught.value.report
    assert report["changes"] == []
    assert any(row["type"] == "split_polyline_for_merge"
               for row in report["attempted_changes"])
    conflict = next(row for row in report["rejections"]
                    if row["type"] == "narrow_strip_opening_conflict")
    assert conflict["contradiction_category"] == (
        "overlapping_openings_have_incompatible_kinds")


def test_transitive_opening_survivor_mapping_resolves_to_final_opening():
    plan = _plan([
        ("line-a", [[40, 0], [40, 100]]),
        ("line-b", [[42, 0], [42, 100]]),
        ("line-c", [[44, 0], [44, 100]]),
    ], openings=[
        _door("open-a", 40, 20, 30),
        _door("open-b", 42, 20, 30),
        _door("open-c", 44, 20, 30),
    ])
    plan["regularization_inputs"] = {
        "line_references": [
            {"partition_id": "line-a", "basis": "inferred", "source_refs": ["a"]},
            {"partition_id": "line-b", "basis": "ink", "source_refs": ["b"]},
            {"partition_id": "line-c", "basis": "dimension", "source_refs": ["c"]},
        ],
        "coordinate_references": [],
    }

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert [row["id"] for row in regularized["openings"]] == ["open-c"]
    mapping = {row["old_opening_id"]: row
               for row in report["semantic_mapping"]["opening_id_map"]}
    assert set(mapping) == {"open-a", "open-b", "open-c"}
    assert all(row["surviving_opening_id"] == "open-c" for row in mapping.values())
    assert all(row["after_connection"] is not None for row in mapping.values())


def test_opening_crossing_partial_merge_boundary_is_an_explicit_contradiction():
    plan = _plan(
        [("source", [[40, 0], [40, 100]]), ("target", [[42, 20], [42, 80]])],
        openings=[_door("crossing", 40, 10, 30)],
    )
    plan["regularization_inputs"] = {
        "line_references": [{"partition_id": "target", "basis": "dimension",
                             "source_refs": ["drawing:dimension"]}],
        "coordinate_references": [],
    }
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    conflict = caught.value.report["rejections"][0]
    assert conflict["contradiction_category"] == "opening_crosses_partial_merge_boundary"
    assert conflict["opening_span_m"] == [7.0, 9.0]
    assert conflict["merge_span_m"] == [2.0, 8.0]


def test_overall_rejection_separates_prior_attempts_from_final_applied_changes():
    window = {**_door("window", 72, 20, 30), "kind": "window", "z": [1.0, 2.0]}
    plan = _plan([
        ("A", [[40, 0], [40, 100]]), ("B", [[42, 0], [42, 100]]),
        ("C", [[70, 0], [70, 100]]), ("D", [[72, 0], [72, 100]]),
    ], openings=[_door("door", 70, 20, 30), window])
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    report = caught.value.report
    assert report["changes"] == []
    assert any(row["type"] == "merge_duplicate_wall_lines"
               for row in report["attempted_changes"])
    assert report["summary"]["moved_or_merged"] == 0
    assert report["summary"]["attempted_changes"] > 0


def test_touching_offset_wall_ends_align_and_move_hosted_opening_without_resizing():
    plan = _plan(
        [("upper", [[40, 0], [40, 50]]), ("lower", [[42, 50], [42, 100]])],
        openings=[_door("D1", 42)],
    )
    regularized, report = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    lower = next(row for row in regularized["partitions"] if row["id"] == "lower")
    door = regularized["openings"][0]
    assert lower["points"] == [[40, 50], [40, 100]]
    assert door["p1"] == [40, 60] and door["p2"] == [40, 70]
    assert report["summary"]["maximum_movement_m"] == pytest.approx(.2)
    proposal, _ = compile_plan_partition(regularized, image_size=IMAGE_SIZE, image_name="plan.png")
    built = proposal["geometry"]["openings"][0]
    assert abs(built["p2"][1] - built["p1"][1]) == pytest.approx(1.0)
    assert built["other_space_id"] is not None


def test_dimensioned_internal_head_to_tail_moves_to_fixed_footprint_not_outline():
    footprint = [[0, 0], [100, 0], [100, 100], [20, 100], [20, 50], [0, 50]]
    plan = _plan([
        ("dimensioned-internal", [[20, 48], [70, 48]]),
        ("end-wall", [[70, 0], [70, 48]]),
    ], footprint=footprint)
    plan["regularization_inputs"] = {
        "line_references": [{
            "partition_id": "dimensioned-internal", "basis": "dimension",
            "source_refs": ["drawing:dimension-chain"],
        }],
        "coordinate_references": [],
    }
    before = copy.deepcopy(plan["footprint_pixels"])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["footprint_pixels"] == before
    internal = next(row for row in regularized["partitions"]
                    if row["id"] == "dimensioned-internal")
    end_wall = next(row for row in regularized["partitions"]
                    if row["id"] == "end-wall")
    assert internal["points"] == [[20, 50.0], [70, 50.0]]
    assert end_wall["points"][-1] == [70, 50.0]
    move = next(row for row in report["changes"]
                if row["type"] == "move_wall_line"
                and row["partition_id"] == "dimensioned-internal")
    assert move["basis"] == "fixed_exterior_footprint"
    assert move["from_m"] == pytest.approx(5.2)
    assert move["to_m"] == pytest.approx(5.0)


def test_attached_endpoint_does_not_chase_a_second_nearby_perpendicular_line():
    plan = _plan([
        ("target-a", [[40, 0], [40, 100]]),
        ("target-b", [[42, 0], [42, 100]]),
        ("suspended", [[41, 50], [60, 50]]),
    ])
    calibration = _calibration(plan, IMAGE_SIZE)
    changes = []
    assert _attach_suspended_endpoints(plan, calibration, changes) is True
    endpoint = list(plan["partitions"][2]["points"][0])
    assert endpoint in ([40, 50], [42, 50])
    assert _attach_suspended_endpoints(plan, calibration, changes) is False
    assert plan["partitions"][2]["points"][0] == endpoint


def test_fixed_footprint_absorbs_unprotected_duplicate_and_catches_near_endpoint():
    duplicate = _plan([("near-west", [[2, 0], [2, 100]])])
    regularized, report = regularize_plan(
        duplicate, image_size=IMAGE_SIZE, image_name="plan.png",
    )
    assert regularized["partitions"] == []
    change = report["changes"][0]
    assert change["type"] == "merge_duplicate_wall_into_fixed_footprint"
    assert change["basis"] == "fixed_exterior_footprint"
    assert change["from_m"] == pytest.approx(.2) and change["to_m"] == 0

    suspended = _plan([("cross-wall", [[2, 50], [100, 50]])])
    regularized, report = regularize_plan(
        suspended, image_size=IMAGE_SIZE, image_name="plan.png",
    )
    assert regularized["partitions"][0]["points"][0] == [0, 50]
    assert any(row["type"] == "join_near_endpoint" for row in report["changes"])


def test_named_strip_at_fixed_footprint_is_eliminated_without_moving_outline():
    plan = _plan([("near-west", [[2, 0], [2, 100]])],
                 seeds=[{"id": "named-strip", "point": [1, 50], "role": "storage"}],
                 openings=[_door("strip-door", 2)])
    before = copy.deepcopy(plan["footprint_pixels"])
    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert regularized["footprint_pixels"] == before
    assert regularized["partitions"] == [] and regularized["space_seeds"] == []
    assert regularized["openings"][0]["p1"][0] == 0
    assert {row["type"] for row in report["changes"]} == {
        "remove_narrow_strip_space_seeds",
        "move_openings_to_merged_wall",
        "merge_duplicate_wall_into_fixed_footprint",
    }
    mapping = report["semantic_mapping"]["opening_id_map"][0]
    assert mapping["old_opening_id"] == mapping["surviving_opening_id"] == "strip-door"
    assert mapping["before_connection"]["is_exterior"] is False
    assert mapping["after_connection"]["is_exterior"] is True


def test_opening_already_on_retained_footprint_gets_explicit_host_mapping():
    plan = _plan(
        [("near-west", [[2, 0], [2, 100]])],
        openings=[_door("outer-door", 0, 60, 70)],
    )

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["openings"][0]["id"] == "outer-door"
    retained = next(row for row in report["changes"]
                    if row["type"] == "retain_openings_on_merged_wall")
    assert retained["opening_ids"] == ["outer-door"]
    mapping = report["semantic_mapping"]["opening_id_map"][0]
    assert mapping["old_opening_id"] == mapping["surviving_opening_id"] == "outer-door"
    assert mapping["before_hosts"] and mapping["after_hosts"]


def test_partial_overlap_with_fixed_footprint_merges_only_overlap_and_keeps_remainder():
    footprint = [[20, 0], [100, 0], [100, 100], [0, 100], [0, 50], [20, 50]]
    plan = _plan([
        ("near-west", [[22, 0], [22, 100]]),
        ("cross", [[22, 50], [100, 50]]),
    ], footprint=footprint)
    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert regularized["footprint_pixels"] == footprint
    remainder = next(row for row in regularized["partitions"]
                     if row["id"] == "near-west")
    assert remainder["points"] == [[20, 100], [20, 50]]
    assert next(row for row in regularized["partitions"]
                if row["id"] == "cross")["points"][0] == [20, 50]
    merged = next(row for row in report["changes"]
                  if row["type"] == "merge_duplicate_wall_into_fixed_footprint")
    assert merged["span_m"] == [5.0, 10.0]
    assert merged["removed_partition_id"] is None
    assert merged["retained_remainder_partition_ids"] == ["near-west"]
    retained_move = next(row for row in report["changes"]
                         if row["type"] == "move_wall_line"
                         and row["partition_id"] == "near-west")
    assert retained_move["basis"] == "fixed_exterior_footprint"
    assert report["semantic_mapping"]["space_count_before"] == 3
    assert report["semantic_mapping"]["space_count_after"] == 3


def test_adjacent_partial_footprint_merges_collapse_the_corner_step():
    footprint = [[0, 0], [100, 0], [100, 100], [50, 100], [50, 50], [0, 50]]
    plan = _plan([
        ("near-vertical", [[52, 48], [52, 100]]),
        ("near-horizontal", [[0, 48], [70, 48]]),
        ("far-vertical", [[70, 0], [70, 48]]),
    ], footprint=footprint)
    stepped = _plan([("remainder", [[50, 48], [70, 48]])], footprint=footprint)
    before = validate_regularized_plan(stepped, image_size=IMAGE_SIZE)
    assert any(row["type"] == "small_wall_step_under_0_30m"
               for row in before["violations"])

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["footprint_pixels"] == footprint
    horizontal = next(row for row in regularized["partitions"]
                      if row["id"] == "near-horizontal")
    assert horizontal["points"] == [[50, 50.0], [70, 50.0]]
    assert not any(row["id"] == "near-vertical"
                   for row in regularized["partitions"])
    collapsed = next(row for row in report["changes"]
                     if row["type"] == "remove_collapsed_wall_step")
    assert collapsed["partition_id"] == "near-vertical"
    assert collapsed["movement_m"] == pytest.approx(.2)
    assert report["hard_constraints"]["status"] == "pass"


def test_adjacent_footprint_merges_remove_only_the_endpoint_collapsed_remainder():
    footprint = [[20, 0], [100, 0], [100, 100], [0, 100], [0, 50], [20, 50]]
    plan = _plan([
        ("mwW", [[20, 0], [20, 52]]),
        ("corrS", [[0, 52], [60, 52]]),
        ("far", [[60, 52], [60, 100]]),
    ], footprint=footprint)

    regularized, report = regularize_plan(
        plan, image_size=IMAGE_SIZE, image_name="plan.png")

    assert regularized["footprint_pixels"] == footprint
    assert [(row["id"], row["points"]) for row in regularized["partitions"]] == [
        ("corrS", [[20.0, 50.0], [60.0, 50.0]]),
        ("far", [[60, 50.0], [60, 100]]),
    ]
    collapsed = next(row for row in report["changes"]
                     if row["type"] == "remove_collapsed_wall_step")
    assert collapsed["partition_id"] == "mwW"
    assert set(map(tuple, collapsed["points_before"])) == {(20.0, 52.0), (20.0, 50.0)}
    assert collapsed["point_after"] == [20.0, 50.0]
    assert collapsed["movement_m"] == pytest.approx(.2)
    assert report["hard_constraints"]["status"] == "pass"
    compile_plan_partition(
        regularized, image_size=IMAGE_SIZE, image_name="plan.png")


def test_adjacent_footprint_merge_does_not_collapse_an_opening_host():
    footprint = [[20, 0], [100, 0], [100, 100], [0, 100], [0, 50], [20, 50]]
    plan = _plan([
        ("mwW", [[20, 0], [20, 52]]),
        ("corrS", [[0, 52], [60, 52]]),
        ("far", [[60, 52], [60, 100]]),
    ], footprint=footprint, openings=[{
        "id": "remainder-door", "kind": "door", "state": "closed",
        "p1": [20, 50.5], "p2": [20, 51.5], "z": [0.0, 2.1],
        "source_refs": ["drawing:remainder-door"],
    }])

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")

    report = caught.value.report
    assert report["changes"] == []
    conflict = next(row for row in report["rejections"]
                    if row["type"] == "narrow_strip_opening_host_ambiguous")
    assert conflict["opening_ids"] == ["remainder-door"]


def test_junction_preparation_removes_zero_length_and_duplicate_segments_with_audit():
    plan = _plan([
        ("duplicate-west", [[0, 0], [0, 100]]),
        ("preexisting-degenerate", [[0, 50], [0, 50]]),
    ])

    prepared, report = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert prepared["partitions"] == []
    assert {row["partition_id"] for row in report["changes"]} == {
        "duplicate-west", "preexisting-degenerate"}
    assert all(row["type"] == "remove_redundant_partition_segments" for row in report["changes"])
    assert report["hard_constraints"]["status"] == "pass"


def test_strict_less_than_0_30_does_not_merge_exact_threshold_but_width_gate_rejects():
    plan = _plan([("A", [[40, 0], [40, 100]]), ("B", [[43, 0], [43, 100]])])
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    report = caught.value.report
    assert not report["changes"]
    assert not any(row["type"] == "parallel_wall_lines_under_0_30m" for row in report["rejections"])
    assert any(row["type"] == "space_width_under_0_60m" for row in report["rejections"])


def test_minimum_width_gate_finds_concave_half_metre_neck():
    footprint = [
        [0, 0], [40, 0], [40, 47.5], [60, 47.5], [60, 0], [100, 0],
        [100, 100], [60, 100], [60, 52.5], [40, 52.5], [40, 100], [0, 100],
    ]
    plan = _plan([], footprint=footprint)
    report = validate_regularized_plan(plan, image_size=IMAGE_SIZE)
    item = next(row for row in report["violations"] if row["type"] == "space_width_under_0_60m")
    assert item["minimum_width_m"] == pytest.approx(.5)
    assert report["coverage"]["width_method"].startswith("orthogonal scan strips")
    with pytest.raises(PlanRegularizationError):
        enforce_regularized_plan(plan, image_size=IMAGE_SIZE)


def test_stack_changes_plan_draft_recompiles_and_aligns_xy_and_z():
    lower = _plan([("wall", [[40, 0], [40, 100]])], floor_id="source-lower")
    upper = _plan([("wall", [[42, 0], [42, 100]])], floor_id="source-upper",
                  openings=[_door("D-upper", 42)])
    items = [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.2, "source_ref": "drawing:upper"},
    ]
    regularized, report = regularize_plan_stack(items)
    assert items[1]["plan"]["partitions"][0]["points"][0][0] == 42
    assert regularized[1]["plan"]["partitions"][0]["points"][0][0] == 40
    assert regularized[1]["plan"]["openings"][0]["p1"][0] == 40
    assert regularized[1]["z_floor"] == pytest.approx(3.0)
    assert regularized[1]["proposal"]["geometry"]["openings"][0]["p1"][0] == pytest.approx(4.0)
    assert report["status"] == "pass" and report["hard_constraints"]["status"] == "pass"
    assert {row["type"] for row in report["changes"]} == {
        "align_storey_elevation", "move_wall_line",
    }
    assert report["elevation_references"]["L2"]["declared_trusted"] is False


def test_cross_storey_move_keeps_perpendicular_endpoint_attached_before_recompile():
    lower = _plan([("wall", [[40, 0], [40, 100]])])
    upper = _plan([
        ("moving-wall", [[42, 0], [42, 100]]),
        ("attached-wall", [[42, 50], [100, 50]]),
    ], openings=[{
        "id": "edge-open", "kind": "open", "state": "open",
        "p1": [42, 50], "p2": [52, 50], "z": [0, 2.1], "source_refs": ["synthetic"],
    }])
    items = [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:upper"},
    ]
    regularized, report = regularize_plan_stack(items)
    moved = next(row for row in regularized[1]["plan"]["partitions"]
                 if row["id"] == "moving-wall")
    attached = next(row for row in regularized[1]["plan"]["partitions"]
                    if row["id"] == "attached-wall")
    assert moved["points"][0][0] == 40
    assert attached["points"][0] == [40, 50]
    edge_open = regularized[1]["plan"]["openings"][0]
    assert edge_open["p1"] == [40, 50] and edge_open["p2"] == [50, 50]
    move = next(row for row in report["changes"]
                if row["type"] == "move_wall_line" and row["partition_id"] == "moving-wall")
    assert "edge-open" in move["moved_opening_ids"]
    assert report["status"] == "pass"


def test_cross_storey_target_accepts_multiple_disjoint_source_segments():
    lower = _plan([("target", [[40, 0], [40, 100]])])
    upper = _plan([
        ("source-top", [[42, 0], [42, 50]]),
        ("source-bottom", [[42, 50], [42, 100]]),
        ("divider", [[42, 50], [100, 50]]),
    ])
    items = [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:upper"},
    ]

    regularized, report = regularize_plan_stack(items)

    moved = {row["id"]: row for row in regularized[1]["plan"]["partitions"]}
    assert moved["source-top"]["points"] == [[40.0, 0], [40.0, 50]]
    assert moved["source-bottom"]["points"] == [[40.0, 50], [40.0, 100]]
    assert moved["divider"]["points"][0] == [40.0, 50]
    moves = [row for row in report["changes"] if row["type"] == "move_wall_line"]
    assert {row["partition_id"] for row in moves} == {"source-top", "source-bottom"}
    assert report["hard_constraints"]["status"] == "pass"


def test_cross_storey_target_lock_moves_connected_collinear_chain_without_cycle():
    lower = _plan([
        ("lower-a", [[0, 40], [33, 40]]),
        ("lower-b", [[33, 40], [66, 40]]),
        ("lower-c", [[66, 40], [100, 40]]),
    ])
    upper = _plan([
        ("upper-a", [[0, 42], [25, 42]]),
        ("upper-b", [[25, 42], [50, 42]]),
        ("upper-c", [[50, 42], [75, 42]]),
        ("upper-d", [[75, 42], [100, 42]]),
        ("vertical-a", [[25, 42], [25, 100]]),
        ("vertical-b", [[50, 42], [50, 100]]),
        ("vertical-c", [[75, 42], [75, 100]]),
    ], openings=[{
        "id": "corridor-door", "kind": "door", "state": "closed",
        "p1": [5, 42], "p2": [15, 42], "z": [0.0, 2.1],
        "source_refs": ["drawing:corridor-door"],
    }])
    items = [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:upper"},
    ]

    regularized, report = regularize_plan_stack(items)

    upper_plan = regularized[1]["plan"]
    for partition_id in ("upper-a", "upper-b", "upper-c", "upper-d"):
        points = next(row["points"] for row in upper_plan["partitions"]
                      if row["id"] == partition_id)
        assert points[0][1] == pytest.approx(40)
        assert points[1][1] == pytest.approx(40)
    assert upper_plan["openings"][0]["p1"][1] == pytest.approx(40)
    assert upper_plan["openings"][0]["p2"][1] == pytest.approx(40)
    assert not any(row["type"] == "endpoint_regularization_cycle"
                   for row in report["rejections"])
    chain_moves = [row for row in report["changes"]
                   if row["type"] == "move_wall_line"
                   and row["partition_id"].startswith("upper-")]
    assert {row["partition_id"] for row in chain_moves} == {
        "upper-a", "upper-b", "upper-c", "upper-d",
    }
    assert all(row["to_m"] == pytest.approx(6.0) for row in chain_moves)
    assert any(row["basis"] == "locked_cross_storey_target_coordinate"
               for row in chain_moves)


def test_cross_storey_move_translates_entity_opening_swept_by_connected_t_junction():
    lower = _plan([
        ("target", [[42, 0], [42, 50]]),
        ("lower-host", [[0, 50], [100, 50]]),
    ])
    upper = _plan(
        [("moving", [[40, 0], [40, 50]]),
         ("opening-host", [[0, 50], [100, 50]])],
        openings=[{
            "id": "passage", "kind": "open",
            "p1": [41, 50], "p2": [60, 50], "z": [0.0, 2.1],
            "source_refs": ["drawing:passage"],
        }],
    )

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    result = regularized[1]["plan"]
    assert next(row for row in result["partitions"] if row["id"] == "moving")[
        "points"] == [[42.0, 0], [42.0, 50]]
    passage = result["openings"][0]
    assert passage["p1"] == [42.0, 50]
    assert passage["p2"] == [61.0, 50]
    move = next(row for row in report["changes"]
                if row.get("partition_id") == "moving")
    assert move["adjusted_crossing_opening_ids"] == ["passage"]
    adjustment = move["opening_adjustments"][0]
    assert adjustment["opening_id"] == "passage"
    assert adjustment["mode"] == "translate_preserve_width"
    assert adjustment["jamb_deltas_m"] == {"p1": .1, "p2": .1}
    assert adjustment["before"]["width_m"] == adjustment["after"]["width_m"]
    assert adjustment["width_change_m"] == 0
    assert report["hard_constraints"]["status"] == "pass"


def test_evidence_backed_complete_open_passage_follows_terminal_boundaries():
    lower = _plan([
        ("target", [[42, 0], [42, 50]]),
        ("right", [[60, 0], [60, 50]]),
        ("lower-host", [[0, 50], [100, 50]]),
    ])
    upper = _plan(
        [("moving", [[40, 0], [40, 50]]),
         ("right", [[60, 0], [60, 50]]),
         ("opening-host", [[0, 50], [100, 50]])],
        openings=[{
            "id": "passage", "kind": "open", "state": "open",
            "p1": [41, 50], "p2": [59, 50], "z": [0.0, 2.1],
            "source_refs": ["drawing:structured-passage-evidence"],
        }],
    )
    upper["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [],
        "reading_alignment": _opening_alignment_evidence("passage"),
    }

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    result = regularized[1]["plan"]
    passage = result["openings"][0]
    assert passage["p1"] == [43.0, 50]
    assert passage["p2"] == [59, 50]
    move = next(row for row in report["changes"]
                if row.get("partition_id") == "moving")
    adjustment = move["opening_adjustments"][0]
    assert adjustment["mode"] == "open_passage_boundary_follow"
    assert adjustment["boundary_ids"] == {"p1": "moving", "p2": "right"}
    assert adjustment["jamb_deltas_m"] == {"p1": .2, "p2": 0.0}
    assert adjustment["boundary_offsets_before_m"] == {"p1": .1, "p2": -.1}
    assert adjustment["boundary_offsets_after_m"] == {"p1": .1, "p2": -.1}
    assert adjustment["before"]["width_m"] == 1.8
    assert adjustment["after"]["width_m"] == 1.6
    assert adjustment["width_change_m"] == -.2
    assert adjustment["inference"] == {
        "basis": "structured_complete_open_passage",
        "state": "open", "structured_gap_support": True,
        "complete_between_terminal_boundaries": True,
        "hard_width_reference": False,
    }
    assert report["hard_constraints"]["status"] == "pass"


def test_complete_open_passage_rejects_distinct_terminal_boundary_ambiguity():
    plan = _plan(
        [("moving", [[40, 0], [40, 50]]),
         ("right-near", [[60, 0], [60, 50]]),
         ("right-far", [[61, 0], [61, 50]]),
         ("opening-host", [[0, 50], [100, 50]])],
        openings=[{
            "id": "passage", "kind": "open", "state": "open",
            "p1": [41, 50], "p2": [59, 50], "z": [0.0, 2.1],
            "source_refs": ["drawing:structured-passage-evidence"],
        }],
    )
    plan["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [],
        "reading_alignment": _opening_alignment_evidence("passage"),
    }
    calibration = _calibration(plan, IMAGE_SIZE)
    segments = _segments(plan, calibration)
    moving = next(row for row in segments if row["partition_id"] == "moving")
    host = next(row for row in segments if row["partition_id"] == "opening-host")

    assert _complete_open_passage_boundaries(
        plan, plan["openings"][0], host, moving, calibration) is None


def test_complete_open_passage_deduplicates_collinear_terminal_segments():
    plan = _plan(
        [("moving", [[40, 0], [40, 50]]),
         ("right-a", [[60, 0], [60, 50]]),
         ("right-b", [[60, 0], [60, 50]]),
         ("opening-host", [[0, 50], [100, 50]])],
        openings=[{
            "id": "passage", "kind": "open", "state": "open",
            "p1": [41, 50], "p2": [59, 50], "z": [0.0, 2.1],
            "source_refs": ["drawing:structured-passage-evidence"],
        }],
    )
    plan["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [],
        "reading_alignment": _opening_alignment_evidence("passage"),
    }
    calibration = _calibration(plan, IMAGE_SIZE)
    segments = _segments(plan, calibration)
    moving = next(row for row in segments if row["partition_id"] == "moving")
    host = next(row for row in segments if row["partition_id"] == "opening-host")

    result = _complete_open_passage_boundaries(
        plan, plan["openings"][0], host, moving, calibration)

    assert result is not None
    assert result["p1"]["partition_id"] == "moving"
    assert result["p2"]["coordinate_m"] == pytest.approx(6.0)


@pytest.mark.parametrize(("kind", "opposite_side"), [
    ("window", False), ("door", True),
])
def test_passage_compatibility_excludes_windows_and_opposite_terminal_walls(
        kind, opposite_side):
    plan = _plan(
        [("moving", [[40, 0], [40, 50]]),
         ("right", [[60, 100 if opposite_side else 0], [60, 50]]),
         ("opening-host", [[0, 50], [100, 50]])],
        openings=[{
            "id": "candidate", "kind": kind, "state": "open",
            "p1": [41, 50], "p2": [59, 50], "z": [0.0, 2.1],
            "source_refs": ["drawing:structured-evidence"],
        }],
    )
    plan["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [],
        "reading_alignment": _opening_alignment_evidence("candidate"),
    }
    calibration = _calibration(plan, IMAGE_SIZE)
    segments = _segments(plan, calibration)
    moving = next(row for row in segments if row["partition_id"] == "moving")
    host = next(row for row in segments if row["partition_id"] == "opening-host")

    assert _complete_open_passage_boundaries(
        plan, plan["openings"][0], host, moving, calibration) is None


@pytest.mark.parametrize(
    ("gap", "complete", "hard_width"),
    [(False, True, False), (True, False, False), (True, True, True)],
    ids=["ordinary-state-open-door", "incomplete-span", "hard-width"],
)
def test_open_state_without_all_passage_evidence_preserves_entity_width(
        gap, complete, hard_width):
    lower_parts = [("target", [[42, 0], [42, 50]]),
                   ("lower-host", [[0, 50], [100, 50]])]
    upper_parts = [("moving", [[40, 0], [40, 50]]),
                   ("opening-host", [[0, 50], [100, 50]])]
    if complete:
        lower_parts.append(("right", [[60, 0], [60, 50]]))
        upper_parts.append(("right", [[60, 0], [60, 50]]))
    lower = _plan(lower_parts)
    upper = _plan(upper_parts, openings=[{
        "id": "open-door", "kind": "door", "state": "open",
        "p1": [41, 50], "p2": [59, 50], "z": [0.0, 2.1],
        "source_refs": ["drawing:ordinary-door"],
    }])
    upper["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [],
        "reading_alignment": _opening_alignment_evidence(
            "open-door", gap=gap, hard_width=hard_width),
    }

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    passage = regularized[1]["plan"]["openings"][0]
    assert passage["p1"] == [42.0, 50]
    assert passage["p2"] == [60.0, 50]
    move = next(row for row in report["changes"]
                if row.get("partition_id") == "moving")
    adjustment = move["opening_adjustments"][0]
    assert adjustment["mode"] == "translate_preserve_width"
    assert adjustment["before"]["width_m"] == adjustment["after"]["width_m"] == 1.8


def test_cross_storey_difference_within_numeric_tolerance_is_not_an_offset():
    lower = _plan([("lower", [[40, 0], [40, 100]])])
    upper = _plan([("upper", [[40.00000087, 0], [40.00000087, 100]])])

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    assert regularized[1]["plan"]["partitions"][0]["points"] == [
        [40.00000087, 0], [40.00000087, 100]]
    assert not any(row["type"] == "move_wall_line" for row in report["changes"])
    assert report["thresholds"]["numeric_equality_tolerance_m"] == 1e-7
    assert report["hard_constraints"]["status"] == "pass"


def test_cross_storey_coordinate_shared_by_more_floors_wins_before_lower_tie_break():
    items = [
        {"plan": _plan([("lower-only", [[40, 0], [40, 100]])]),
         "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": _plan([("shared-middle", [[41, 0], [41, 100]])]),
         "image_size": IMAGE_SIZE, "image_name": "middle.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:middle"},
        {"plan": _plan([("shared-upper", [[41, 0], [41, 100]])]),
         "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L3", "z_floor": 6.0, "source_ref": "drawing:upper"},
    ]

    regularized, report = regularize_plan_stack(items)

    lower_wall = regularized[0]["plan"]["partitions"][0]
    assert lower_wall["points"] == [[41.0, 0], [41.0, 100]]
    move = next(row for row in report["changes"] if row["type"] == "move_wall_line")
    assert move["floor_id"] == "L1"
    assert move["from_m"] == pytest.approx(4.0)
    assert move["to_m"] == pytest.approx(4.1)
    assert move["basis"] == "more_storeys_then_collinear_walls"
    assert move["target_coordinate_support"] == {
        "floors": 2, "collinear_wall_segments": 2,
    }


def test_cross_storey_move_may_change_real_separation_when_it_stays_at_least_0_30m():
    lower = _plan([
        ("near-target", [[40, 0], [40, 100]]),
        ("real-separate", [[80, 0], [80, 100]]),
    ])
    upper = _plan([("moving-wall", [[42, 0], [42, 100]])])
    items = [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:upper"},
    ]
    regularized, report = regularize_plan_stack(items)
    assert regularized[1]["plan"]["partitions"][0]["points"][0][0] == 40
    changed = next(row for row in report["preserved_separations"]
                   if row["before_distance_m"] == pytest.approx(3.8))
    assert changed["after_distance_m"] == pytest.approx(4.0)
    assert changed["unchanged"] is False


def test_cross_storey_move_that_creates_sub_0_30m_same_floor_pair_is_not_saved():
    lower = _plan([("target", [[44, 0], [44, 100]])])
    upper = _plan([
        ("moving", [[42, 0], [42, 100]]),
        ("stable", [[49, 0], [49, 100]]),
    ])
    items = [
        {"plan": lower, "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower"},
        {"plan": upper, "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.0, "source_ref": "drawing:upper"},
    ]
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)
    report = caught.value.report
    assert report["changes"] == []
    assert report["attempted_changes"]
    rejection = next(row for row in report["rejections"]
                     if row["type"] == "cross_storey_alignment_rejected")
    assert rejection["contradiction_category"] == "post_alignment_hard_constraint"
    assert any(row["type"] in {
        "parallel_wall_lines_under_0_30m", "space_width_under_0_60m"}
        for row in rejection["hard_violations"])


def test_cross_storey_skip_policy_preserves_unresolved_pair_with_audit():
    lower = _plan([("target", [[44, 0], [44, 100]])])
    upper = _plan([
        ("moving", [[42, 0], [42, 100]]),
        ("stable", [[49, 0], [49, 100]]),
    ])

    regularized, report = regularize_plan_stack(
        _stack_items(lower, upper),
        cross_storey_failure_policy="skip_failed_pair",
    )

    assert regularized[1]["plan"]["partitions"][0]["points"] == [
        [42, 0], [42, 100]]
    assert report["status"] == "pass"
    assert report["rejections"] == []
    assert report["summary"]["skipped_alignments"] == 1
    skipped = report["skipped_alignments"][0]
    assert skipped["type"] == "cross_storey_alignment_skipped"
    assert skipped["disposition"] == "original_pair_preserved_and_reported"
    assert skipped["hard_violation"]["type"] == "storey_wall_offset_under_0_30m"
    assert report["hard_constraints"]["status"] == "pass"
    assert report["hard_constraints"]["skipped_violations"]


def test_stack_rejects_upper_trusted_elevation_instead_of_changing_lower_height():
    items = [
        {"plan": _plan([("wall", [[40, 0], [40, 100]])]),
         "image_size": IMAGE_SIZE, "image_name": "lower.png",
         "floor_id": "L1", "z_floor": 0.0, "source_ref": "drawing:lower",
         "elevation_reference": False},
        {"plan": _plan([("wall", [[40, 0], [40, 100]])]),
         "image_size": IMAGE_SIZE, "image_name": "upper.png",
         "floor_id": "L2", "z_floor": 3.2, "source_ref": "drawing:upper",
         "elevation_reference": True},
    ]
    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)
    assert any(row["type"] == "conflicting_elevation_references"
               for row in caught.value.report["rejections"])


def test_stack_aligns_upper_convex_footprint_edge_to_lower_edge():
    lower = _plan([])
    upper = _plan([], footprint=[[2, 0], [100, 0], [100, 100], [2, 100]])
    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    assert regularized[0]["plan"]["footprint_pixels"] == lower["footprint_pixels"]
    assert regularized[1]["plan"]["footprint_pixels"] == [
        [0.0, 0], [100, 0], [100, 100], [0.0, 100],
    ]
    change = next(row for row in report["changes"]
                  if row["type"] == "move_footprint_edge")
    assert change["source_floor_id"] == "L2" and change["target_floor_id"] == "L1"
    assert change["source_edge_index"] == 3 and change["target_edge_index"] == 3
    assert change["from_m"] == pytest.approx(.2) and change["to_m"] == 0
    assert change["basis"] == "lower_storey_footprint_default"
    assert report["hard_constraints"]["status"] == "pass"


def test_stack_aligns_only_the_matching_concave_notch_edge():
    lower_footprint = [
        [0, 0], [100, 0], [100, 100], [60, 100],
        [60, 60], [40, 60], [40, 100], [0, 100],
    ]
    upper_footprint = [
        [0, 0], [100, 0], [100, 100], [62, 100],
        [62, 60], [40, 60], [40, 100], [0, 100],
    ]
    regularized, report = regularize_plan_stack(_stack_items(
        _plan([], footprint=lower_footprint),
        _plan([], footprint=upper_footprint),
    ))

    assert regularized[1]["plan"]["footprint_pixels"] == lower_footprint
    change = next(row for row in report["changes"]
                  if row["type"] == "move_footprint_edge")
    assert change["source_edge_index"] == 3
    assert change["span_m"] == [0.0, 4.0]
    assert change["footprint_before"] == upper_footprint
    assert change["footprint_after"] == lower_footprint


def test_stack_footprint_move_carries_openings_collinear_chain_and_perpendicular_endpoint():
    lower_footprint = [[20, 0], [100, 0], [100, 100], [0, 100], [0, 50], [20, 50]]
    upper_footprint = [[22, 0], [100, 0], [100, 100], [0, 100], [0, 50], [22, 50]]
    edge_window = {
        "id": "edge-window", "kind": "window", "p1": [22, 20], "p2": [22, 30],
        "z": [1.0, 2.0], "source_refs": ["drawing:edge-window"],
    }
    chain_door = {
        "id": "chain-door", "kind": "door", "state": "closed",
        "p1": [22, 60], "p2": [22, 70], "z": [0.0, 2.1],
        "source_refs": ["drawing:chain-door"],
    }
    upper = _plan([
        ("corridor-chain", [[22, 50], [22, 100]]),
        ("connected-divider", [[22, 75], [100, 75]]),
    ], footprint=upper_footprint, openings=[edge_window, chain_door])

    regularized, report = regularize_plan_stack(_stack_items(
        _plan([], footprint=lower_footprint), upper))

    result = regularized[1]["plan"]
    partitions = {row["id"]: row["points"] for row in result["partitions"]}
    assert partitions["corridor-chain"] == [[20.0, 50], [20.0, 100]]
    assert partitions["connected-divider"][0] == [20.0, 75]
    assert all(opening["p1"][0] == opening["p2"][0] == pytest.approx(20)
               for opening in result["openings"])
    exterior = next(row for row in report["changes"]
                    if row["type"] == "move_footprint_edge")
    assert set(exterior["object_ids"]["partitions"]) == {
        "corridor-chain", "connected-divider",
    }
    assert set(exterior["object_ids"]["openings"]) == {
        "edge-window", "chain-door",
    }
    chain_move = next(row for row in report["changes"]
                      if row["type"] == "move_wall_line"
                      and row["partition_id"] == "corridor-chain")
    assert chain_move["basis"] == "cross_storey_footprint_collinear_chain"


def test_connected_footprint_continuation_anchors_the_opposite_internal_wall():
    lower = _plan([
        ("lower-hall", [[0, 42], [60, 42]]),
        ("lower-divider", [[60, 42], [60, 100]]),
    ])
    upper_footprint = [
        [0, 0], [100, 0], [100, 40], [60, 40], [60, 100], [0, 100],
    ]
    upper = _plan([
        ("upper-hall", [[0, 40], [60, 40]]),
    ], footprint=upper_footprint)

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    lower_result = regularized[0]["plan"]
    upper_result = regularized[1]["plan"]
    assert upper_result["footprint_pixels"] == upper_footprint
    assert upper_result["partitions"][0]["points"] == [[0, 40], [60, 40]]
    assert lower_result["partitions"][0]["points"] == [[0, 40.0], [60, 40.0]]
    assert lower_result["partitions"][1]["points"][0] == [60, 40.0]
    assert not any(row["type"] == "move_footprint_edge"
                   for row in report["changes"])
    wall = next(row for row in report["changes"]
                if row.get("partition_id") == "lower-hall")
    assert wall["floor_id"] == "L1"
    assert wall["from_m"] == pytest.approx(5.8)
    assert wall["to_m"] == pytest.approx(6.0)
    assert report["hard_constraints"]["status"] == "pass"


def test_upper_only_dimension_evidence_reverses_footprint_alignment_direction():
    lower = _plan([])
    upper = _plan([], footprint=[[2, 0], [100, 0], [100, 100], [2, 100]])
    upper["regularization_inputs"] = {
        "line_references": [],
        "coordinate_references": [{
            "axis": "x", "value_m": .2, "basis": "dimension",
            "chain_id": "upper-x", "tick_index": 0,
            "source_refs": ["drawing:upper-dimension"],
        }],
    }

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    assert regularized[0]["plan"]["footprint_pixels"] == [
        [2.0, 0], [100, 0], [100, 100], [2.0, 100],
    ]
    assert regularized[1]["plan"]["footprint_pixels"] == upper["footprint_pixels"]
    change = next(row for row in report["changes"]
                  if row["type"] == "move_footprint_edge")
    assert change["source_floor_id"] == "L1" and change["target_floor_id"] == "L2"
    assert change["basis"] == "upper_only_dimension_reference"


def test_dimensions_on_both_footprint_edges_keep_default_upper_to_lower_direction():
    lower = _plan([])
    upper = _plan([], footprint=[[2, 0], [100, 0], [100, 100], [2, 100]])
    for plan, value, chain in ((lower, 0.0, "lower-x"), (upper, .2, "upper-x")):
        plan["regularization_inputs"] = {
            "line_references": [],
            "coordinate_references": [{
                "axis": "x", "value_m": value, "basis": "dimension",
                "chain_id": chain, "tick_index": 0,
                "source_refs": [f"drawing:{chain}"],
            }],
        }

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    assert regularized[0]["plan"]["footprint_pixels"] == lower["footprint_pixels"]
    assert regularized[1]["plan"]["footprint_pixels"][0][0] == pytest.approx(0)
    change = next(row for row in report["changes"]
                  if row["type"] == "move_footprint_edge")
    assert change["source_floor_id"] == "L2"
    assert change["basis"] == "lower_storey_footprint_default"


def test_upper_dimension_coordinate_propagates_through_three_storey_footprints():
    plans = [
        _plan([], footprint=[[offset, 0], [100, 0], [100, 100], [offset, 100]])
        for offset in (0, 1, 2)
    ]
    plans[2]["regularization_inputs"] = {
        "line_references": [],
        "coordinate_references": [{
            "axis": "x", "value_m": .2, "basis": "dimension",
            "chain_id": "top-x", "tick_index": 0,
            "source_refs": ["drawing:top-dimension"],
        }],
    }
    items = [
        {"plan": plan, "image_size": IMAGE_SIZE, "image_name": f"L{index}.png",
         "floor_id": f"L{index}", "z_floor": 3.0 * (index - 1),
         "source_ref": f"drawing:L{index}"}
        for index, plan in enumerate(plans, start=1)
    ]

    regularized, report = regularize_plan_stack(items)

    assert [row["plan"]["footprint_pixels"][0][0] for row in regularized] == [
        pytest.approx(2.0), pytest.approx(2.0), pytest.approx(2.0),
    ]
    footprint_changes = [
        row for row in report["changes"] if row["type"] == "move_footprint_edge"
    ]
    assert any(row["floor_id"] == "L1"
               and row["basis"] == "upper_dimension_reference_propagated"
               for row in footprint_changes)
    assert report["hard_constraints"]["status"] == "pass"


def test_cross_storey_footprint_setback_at_exact_threshold_is_preserved():
    lower = _plan([])
    upper = _plan([], footprint=[[3, 0], [100, 0], [100, 100], [3, 100]])

    regularized, report = regularize_plan_stack(_stack_items(lower, upper))

    assert regularized[1]["plan"]["footprint_pixels"] == upper["footprint_pixels"]
    assert not any(row["type"] == "move_footprint_edge" for row in report["changes"])


def _mixed_boundary_stack(offset=2):
    # The same continuous physical wall is exterior below the notch on L1,
    # but interior below it on L2.  Neither same-type segment pair overlaps.
    lower = _plan([("lower-extension", [[40, 0], [40, 50]])], footprint=[
        [0, 0], [100, 0], [100, 100], [40, 100], [40, 50], [0, 50]])
    x = 40 + offset
    upper = _plan([("upper-extension", [[x, 50], [x, 100]])], footprint=[
        [x, 0], [100, 0], [100, 100], [0, 100], [0, 50], [x, 50]],
        openings=[_door("chain-door", x), {
            "id": "exterior-window", "kind": "window",
            "p1": [x, 20], "p2": [x, 30], "z": [1.0, 2.0],
            "source_refs": ["drawing:exterior-window"],
        }])
    return _stack_items(lower, upper)


def _stack_source_precision(items):
    from src.agent.geometry_model.schema import GeometryInput, FootprintRing
    from src.agent.geometry.building_precision import precision_report
    from src.agent.geometry.plan_assembly import assemble_plan_proposals
    from src.agent.geometry.source_bim import build_source_bim

    compiled = []
    for item in items:
        proposal, _ = compile_plan_partition(
            item["plan"], image_size=tuple(item["image_size"]), image_name=item["image_name"])
        compiled.append({"proposal": proposal, "floor_id": item["floor_id"],
                         "z_floor": item["z_floor"], "source_ref": item["source_ref"]})
    proposal = assemble_plan_proposals(compiled)
    geometry = GeometryInput.model_validate(proposal["geometry"])
    for floor in geometry.floors:
        if isinstance(floor.footprint, dict):
            floor.footprint = FootprintRing.model_validate(floor.footprint)
    return precision_report(build_source_bim(geometry, capability_profile="orthogonal_polygon"))


def test_mixed_exterior_interior_chain_is_checked_aligned_and_accepted_by_source_gate():
    items = _mixed_boundary_stack()
    original = copy.deepcopy(items)
    initial_hard = validate_regularized_plan_stack(items)
    assert len([row for row in initial_hard["violations"]
                if row["type"] == "storey_wall_offset_under_0_30m"]) == 2
    assert _stack_source_precision(items)["counts"]["storey_wall_offset"]

    result, report = regularize_plan_stack(items)

    assert items == original
    assert report["hard_constraints"]["status"] == "pass"
    assert result[0]["plan"]["footprint_pixels"] == items[0]["plan"]["footprint_pixels"]
    upper = result[1]["plan"]
    assert upper["partitions"][0]["points"] == [[40.0, 50], [40.0, 100]]
    assert all(opening["p1"][0] == opening["p2"][0] == 40 for opening in upper["openings"])
    move = next(row for row in report["changes"] if row["type"] == "move_footprint_edge")
    assert move["matched_boundaries"]
    assert _stack_source_precision(result)["status"] == "pass"


def test_mixed_boundary_dimension_on_connected_partition_controls_outline_direction():
    items = _mixed_boundary_stack()
    items[1]["plan"]["regularization_inputs"] = {
        "line_references": [{"partition_id": "upper-extension", "basis": "dimension",
                             "source_refs": ["drawing:upper-extension-dimension"]}],
        "coordinate_references": [],
    }

    result, report = regularize_plan_stack(items)

    assert result[1]["plan"]["footprint_pixels"] == items[1]["plan"]["footprint_pixels"]
    assert result[0]["plan"]["partitions"][0]["points"] == [[42.0, 0], [42.0, 50]]
    assert report["hard_constraints"]["status"] == "pass"
    assert _stack_source_precision(result)["status"] == "pass"


def _three_storey_mixed_boundary_stack():
    pair = _mixed_boundary_stack(0)
    top = copy.deepcopy(pair[0]["plan"])
    for point in top["footprint_pixels"]:
        if point[0] == 40:
            point[0] = 42
    for partition in top["partitions"]:
        for point in partition["points"]:
            if point[0] == 40:
                point[0] = 42
    top["regularization_inputs"] = {
        "line_references": [{"partition_id": "lower-extension", "basis": "dimension",
                             "source_refs": ["drawing:top-dimension"]}],
        "coordinate_references": [],
    }
    return [*pair, {**pair[0], "plan": top, "floor_id": "L3", "z_floor": 6.0,
                    "image_name": "top.png", "source_ref": "drawing:top"}]


def test_mixed_boundary_upper_dimension_propagates_through_three_storeys():
    items = _three_storey_mixed_boundary_stack()

    result, report = regularize_plan_stack(items)

    assert [row["plan"]["partitions"][0]["points"][0][0] for row in result] == [42.0] * 3
    assert any(row["floor_id"] == "L1" and row["basis"] == "upper_dimension_reference_propagated"
               for row in report["changes"])
    assert report["hard_constraints"]["status"] == "pass"
    assert _stack_source_precision(result)["status"] == "pass"


def test_mixed_boundary_dimension_propagation_rejects_a_lower_floor_conflict():
    items = _three_storey_mixed_boundary_stack()
    items[0]["plan"]["partitions"].append({
        "id": "other-wall", "points": [[46, 0], [46, 100]], "source_refs": ["drawing:other-wall"]})
    before = copy.deepcopy(items)

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)

    assert items == before
    rejection = next(row for row in caught.value.report["rejections"]
                     if row["type"] == "cross_storey_footprint_alignment_rejected")
    assert rejection["floor_id"] == "L1"
    assert any(row["type"] == "space_width_under_0_60m" for row in rejection["hard_violations"])
    assert not caught.value.report["changes"]


@pytest.mark.parametrize("offset", [3, 8])
def test_mixed_boundary_real_setback_is_preserved(offset):
    items = _mixed_boundary_stack(offset)
    result, report = regularize_plan_stack(items)

    for original, changed in zip(items, result):
        assert changed["plan"]["footprint_pixels"] == original["plan"]["footprint_pixels"]
        assert changed["plan"]["partitions"] == original["plan"]["partitions"]
    assert not report["changes"]
    assert _stack_source_precision(result)["status"] == "pass"


def test_mixed_boundary_ordinary_wall_moves_to_fixed_exterior():
    lower = _plan([("interior", [[40, 0], [40, 100]])])
    upper = _plan([], footprint=[[42, 0], [100, 0], [100, 100], [42, 100]])
    result, report = regularize_plan_stack(_stack_items(lower, upper))

    assert result[0]["plan"]["partitions"][0]["points"] == [[42.0, 0], [42.0, 100]]
    assert result[1]["plan"]["footprint_pixels"] == upper["footprint_pixels"]
    assert report["hard_constraints"]["status"] == "pass"
    assert _stack_source_precision(result)["status"] == "pass"


def test_mixed_boundary_dimension_conflict_rejects_without_moving_outline():
    lower = _plan([("interior", [[40, 0], [40, 100]])])
    lower["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [{
            "axis": "x", "value_m": 4.0, "basis": "dimension", "chain_id": "lower-x",
            "tick_index": 0, "source_refs": ["drawing:lower-dimension"]}]}
    upper = _plan([], footprint=[[42, 0], [100, 0], [100, 100], [42, 100]])
    items = _stack_items(lower, upper)
    before = copy.deepcopy(items)

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)

    assert items == before
    violation = next(row for row in caught.value.report["rejections"]
                     if row["type"] == "storey_wall_offset_under_0_30m")
    assert {obj["partition_id"] for obj in violation["objects"]} == {"interior", "footprint[3]"}
    assert not caught.value.report["changes"]


def test_mixed_boundary_move_rolls_back_if_it_narrows_a_room():
    lower = _plan([("interior", [[40, 0], [40, 100]]), ("other", [[46, 0], [46, 100]])])
    upper = _plan([], footprint=[[42, 0], [100, 0], [100, 100], [42, 100]])
    items = _stack_items(lower, upper)
    before = copy.deepcopy(items)

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)

    assert items == before
    rejection = next(row for row in caught.value.report["rejections"]
                     if row["type"] == "cross_storey_alignment_rejected")
    assert any(row["type"] == "space_width_under_0_60m" for row in rejection["hard_violations"])
    assert not caught.value.report["changes"]


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_type_closer_matching_segments_defeat_false_pairing_symmetrically(reverse):
    # Pair selection also runs before the floor hard check.  The fragments
    # exercise a closer physical wall represented by multiple draft segments.
    first = _plan([("matched", [[40, 0], [40, 100]])])
    second = _plan([("closer-a", [[40, 0], [40, 50]]), ("closer-b", [[40, 50], [40, 100]])],
                   footprint=[[42, 0], [100, 0], [100, 100], [42, 100]])
    items = _stack_items(second, first) if reverse else _stack_items(first, second)

    assert not _stack_offset_pairs(*items)


@pytest.mark.parametrize("reverse", [False, True])
def test_closer_segments_on_different_coordinates_do_not_invent_one_matching_wall(reverse):
    first = _plan([("matched", [[40, 0], [40, 100]])])
    second = _plan([("closer-a", [[40, 0], [40, 50]]), ("closer-b", [[41, 50], [41, 100]])],
                   footprint=[[42, 0], [100, 0], [100, 100], [42, 100]])
    items = _stack_items(second, first) if reverse else _stack_items(first, second)

    assert any({a["partition_id"], b["partition_id"]} == {"matched", "footprint[3]"}
               for a, b in _stack_offset_pairs(*items))


def test_cross_storey_policy_does_not_change_same_floor_fixed_outline():
    footprint = [[0, 0], [100, 0], [100, 100], [0, 100]]
    regularized, report = regularize_plan(
        _plan([("near-west", [[2, 0], [2, 100]])], footprint=footprint),
        image_size=IMAGE_SIZE, image_name="single.png",
    )

    assert regularized["footprint_pixels"] == footprint
    assert any(row["type"] == "merge_duplicate_wall_into_fixed_footprint"
               for row in report["changes"])
    assert not any(row["type"] == "move_footprint_edge" for row in report["changes"])


def test_failed_cross_storey_footprint_move_rolls_back_transactionally():
    lower = _plan([], footprint=[[4, 0], [100, 0], [100, 100], [4, 100]])
    upper = _plan([("near-left", [[8, 0], [8, 100]])],
                  footprint=[[2, 0], [100, 0], [100, 100], [2, 100]])
    items = _stack_items(lower, upper)
    before = copy.deepcopy(items)

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)

    assert items == before
    report = caught.value.report
    rejection = next(row for row in report["rejections"]
                     if row["type"] == "cross_storey_footprint_alignment_rejected")
    assert rejection["contradiction_category"] == "post_alignment_hard_constraint"
    assert rejection["attempted_changes"][0]["type"] == "move_footprint_edge"
    assert any(row["type"] == "space_width_under_0_60m"
               for row in rejection["hard_violations"])


def test_wall_retry_without_a_corresponding_exterior_target_rejects_structurally():
    lower = _plan(
        [("lower-extension", [[44, 0], [44, 50]])],
        footprint=[[0, 0], [100, 0], [100, 100], [44, 100], [44, 50], [0, 50]],
    )
    upper = _plan(
        [
            ("upper-extension", [[42, 0], [42, 50]]),
            ("upper-near", [[49, 50], [49, 100]]),
            ("upper-cap", [[42, 50], [49, 50]]),
        ],
        footprint=[[0, 0], [100, 0], [100, 100], [42, 100], [42, 50], [0, 50]],
    )
    items = _stack_items(lower, upper)
    before = copy.deepcopy(items)

    assert validate_regularized_plan(lower, image_size=IMAGE_SIZE)["status"] == "pass"
    assert validate_regularized_plan(upper, image_size=IMAGE_SIZE)["status"] == "pass"

    with pytest.raises(PlanRegularizationError) as caught:
        regularize_plan_stack(items)

    assert items == before
    report = caught.value.report
    assert report["status"] == "rejected"
    footprint_rejection = next(
        row for row in report["rejections"]
        if row["type"] == "cross_storey_footprint_alignment_rejected"
    )
    retry_rejection = next(
        row for row in report["rejections"]
        if row["type"] == "cross_storey_alignment_rejected"
    )
    for rejection in (footprint_rejection, retry_rejection):
        assert rejection["contradiction_category"] == "post_alignment_hard_constraint"
        assert any(row["type"] == "space_width_under_0_60m"
                   for row in rejection["hard_violations"])
        exterior = next(row for row in rejection["attempted_changes"]
                        if row["type"] == "move_footprint_edge")
        assert exterior["source_edge_index"] == 3
        assert exterior["target_edge_index"] == 3


def test_reading_alignment_audit_is_persisted_but_never_drives_literal_compile():
    plan = _plan([("wall", [[40, 0], [40, 100]])])
    plan["regularization_inputs"] = {
        "line_references": [], "coordinate_references": [],
        "reading_alignment": {
            "schema_version": "reading_alignment_v1",
            "ink": {"summary": {"moved": 0}, "items": [], "rejections": [],
                    "search_or_tolerance": {"pixels": 3}},
            "dimensions": {"summary": {"accepted": 0}, "items": [], "rejections": [],
                           "search_or_tolerance": {"residual_m": .02}},
        },
    }
    proposal, metadata = compile_plan_partition(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert proposal["geometry"]["floors"][0]["cells"]
    assert metadata["regularization_inputs"] == plan["regularization_inputs"]
    assert metadata["method"]["snap"] is False
    regularized, report = regularize_plan(plan, image_size=IMAGE_SIZE, image_name="plan.png")
    assert report["reading_alignment"] == plan["regularization_inputs"]["reading_alignment"]
    assert regularized["regularization"]["reading_alignment"] == report["reading_alignment"]
