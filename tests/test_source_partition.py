from __future__ import annotations

import copy
import json

import pytest

from scripts.dev.evaluation.source_partition import compare_partitions


def _room(identity, polygon, *, floor="F1", z=0.0, height=3.0):
    return {"id": identity, "floor_id": floor, "polygon": polygon, "z_floor": z, "height": height}


def _box(identity, xmin, xmax, ymin=0.0, ymax=4.0, **kwargs):
    return _room(identity, [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]], **kwargs)


def _codes(report):
    return {finding["code"] for finding in report["findings"]}


def test_l_shape_remains_one_source_space_despite_names_winding_and_edge_subdivision():
    reference = [_room("L", [[0, 0], [8, 0], [8, 3], [3, 3], [3, 8], [0, 8]])]
    candidate = [_room("renamed", [[3, 3], [8, 3], [8, 0], [4, 0], [0, 0], [0, 8], [3, 8], [3, 3]])]
    report = compare_partitions(reference, candidate)
    assert report["status"] == "pass"
    assert report["matches"][0]["candidate_id"] == "renamed"
    assert report["matches"][0]["iou"] == 1.0
    assert "connectivity" in report["not_evaluated"]
    assert "false_slabs" in report["not_evaluated"]
    json.dumps(report, allow_nan=False)


def test_l_shape_split_into_rectangular_source_spaces_is_severe_even_with_equal_union():
    reference = [_room("L", [[0, 0], [8, 0], [8, 3], [3, 3], [3, 8], [0, 8]])]
    candidate = [_box("bottom", 0, 8, 0, 3), _box("upper", 0, 3, 3, 8)]
    report = compare_partitions(reference, candidate)
    assert report["status"] == "severe"
    split = next(item for item in report["findings"] if item["code"] == "source_space_split")
    assert split["reference_ids"] == ["L"]
    assert set(split["candidate_ids"]) == {"bottom", "upper"}
    assert all(item["candidate_coverage_fraction"] == 1 for item in split["overlaps"])


def test_two_rooms_merged_is_severe_even_with_equal_footprint():
    reference = [_box("west", 0, 5), _box("east", 5, 10)]
    report = compare_partitions(reference, [_box("whole", 0, 10)])
    assert report["status"] == "severe"
    assert {"source_spaces_merged", "missing_source_space"} <= _codes(report)


def test_equal_room_count_and_total_area_do_not_hide_repartitioning():
    reference = [_box("west", 0, 5, 0, 10), _box("east", 5, 10, 0, 10)]
    candidate = [_box("south", 0, 10, 0, 5), _box("north", 0, 10, 5, 10)]
    report = compare_partitions(reference, candidate)
    assert report["reference_count"] == report["candidate_count"] == 2
    assert report["status"] == "severe"
    assert "partition_boundary_changed" in _codes(report)


def test_same_count_with_substantially_moved_partition_is_severe():
    report = compare_partitions([_box("a", 0, 5), _box("b", 5, 10)], [_box("a", 0, 5.5), _box("b", 5.5, 10)])
    assert report["status"] == "severe"
    assert "partition_boundary_changed" in _codes(report)


@pytest.mark.parametrize("delta", [0.001, 0.005, 0.010])
@pytest.mark.parametrize("mode", ["translation", "partition_shift", "vertex_jitter"])
def test_small_perturbations_preserve_source_partition(delta, mode):
    reference = [_box("a", 0, 5), _box("b", 5, 10)]
    candidate = copy.deepcopy(reference)
    if mode == "translation":
        for room in candidate:
            room["polygon"] = [[x + delta, y + delta] for x, y in room["polygon"]]
    elif mode == "partition_shift":
        candidate = [_box("x", 0, 5 + delta), _box("y", 5 + delta, 10)]
    else:
        for room in candidate:
            room["polygon"][0][0] += delta
            room["polygon"][1][1] += delta
    report = compare_partitions(reference, candidate)
    assert report["status"] in {"pass", "minor"}, report
    assert not {"source_space_split", "source_spaces_merged"} & _codes(report)


@pytest.mark.parametrize("width", [0.01, 0.001, 0.000001])
def test_true_narrow_room_loss_cannot_be_erased_by_tolerance(width):
    reference = [_box("large", 0, 5), _box("narrow", 5, 5 + width)]
    report = compare_partitions(reference, [_box("merged", 0, 5 + width)])
    assert report["status"] == "severe"
    assert "missing_source_space" in _codes(report)
    merge = next(item for item in report["findings"] if item["code"] == "source_spaces_merged")
    assert set(merge["reference_ids"]) == {"large", "narrow"}


def test_narrow_extra_partition_is_severe_despite_tolerance():
    report = compare_partitions([_box("whole", 0, 5)], [_box("large", 0, 4.999), _box("sliver", 4.999, 5)])
    assert report["status"] == "severe"
    assert {"extra_source_space", "source_space_split"} <= _codes(report)


def test_missing_and_extra_room_cannot_cancel_with_same_count():
    report = compare_partitions([_box("large", 0, 5), _box("narrow", 5, 5.001)], [_box("large", 0, 5.001), _box("invented", 20, 21)])
    assert report["status"] == "severe"
    assert {"missing_source_space", "extra_source_space"} <= _codes(report)


@pytest.mark.parametrize("field,value,code", [("floor_id", "F2", "floor_assignment_changed"), ("z_floor", 3.0, "vertical_extent_changed"), ("height", 3.5, "vertical_extent_changed")])
def test_floor_and_vertical_changes_are_severe(field, value, code):
    reference = [_box("space", 0, 5)]
    candidate = copy.deepcopy(reference)
    candidate[0][field] = value
    report = compare_partitions(reference, candidate)
    assert report["status"] == "severe"
    assert code in _codes(report)


def test_stacked_rooms_match_their_floors_despite_order_and_name_changes():
    reference = [_box("a", 0, 5), _box("b", 0, 5, floor="F2", z=3)]
    candidate = [_box("renamed-upper", 0, 5, floor="F2", z=3), _box("renamed-lower", 0, 5)]
    report = compare_partitions(reference, candidate)
    assert report["status"] == "pass"
    assert {(match["reference_id"], match["candidate_id"]) for match in report["matches"]} == {("a", "renamed-lower"), ("b", "renamed-upper")}


def test_swapped_room_names_do_not_override_geometric_correspondence():
    report = compare_partitions(
        [_box("a", 0, 5), _box("b", 5, 10)],
        [_box("b", 0, 5), _box("a", 5, 10)],
    )
    assert report["status"] == "pass"
    assert {(m["reference_id"], m["candidate_id"]) for m in report["matches"]} == {
        ("a", "b"), ("b", "a")
    }


def test_small_vertical_changes_are_minor_but_accumulated_top_change_is_checked():
    reference = [_box("a", 0, 5)]
    small = compare_partitions(reference, [_box("b", 0, 5, z=0.01, height=3.01)])
    accumulated = compare_partitions(reference, [_box("b", 0, 5, z=0.015, height=3.015)])
    assert small["status"] == "minor"
    assert accumulated["status"] == "severe"
    assert "vertical_extent_changed" in _codes(accumulated)


def test_no_reference_is_explicitly_not_evaluated():
    report = compare_partitions([], [_box("unverified", 0, 5)])
    assert report["status"] == "not_evaluated"
    assert report["evaluated"] == []
    assert "reference_unavailable" in _codes(report)


def test_empty_candidate_does_not_pass():
    report = compare_partitions([_box("missing", 0, 5)], [])
    assert report["status"] == "severe"
    assert "missing_source_space" in _codes(report)


@pytest.mark.parametrize("polygon", [[], [[0, 0], [1, 0], [2, 0]], [[0, 0], [2, 2], [2, 0], [0, 2]], [[0, 0], [1, 0], [float("nan"), 2]]])
def test_invalid_candidate_geometry_is_severe_and_json_serializable(polygon):
    report = compare_partitions([_box("good", 0, 5)], [_room("bad", polygon)])
    assert report["status"] == "severe"
    assert "invalid_candidate_space" in _codes(report)
    json.dumps(report, allow_nan=False)


def test_bad_reference_prevents_comparative_verdict():
    report = compare_partitions([_room("bad", [])], [_box("good", 0, 5)])
    assert report["status"] == "not_evaluated"
    assert "reference_unusable" in _codes(report)


@pytest.mark.parametrize("same_id", [False, True])
def test_duplicate_candidate_cannot_pass(same_id):
    candidate = [_box("a", 0, 5), _box("a" if same_id else "duplicate", 0, 5)]
    report = compare_partitions([_box("a", 0, 5)], candidate)
    assert report["status"] == "severe"
    assert ("invalid_candidate_space" if same_id else "candidate_spaces_overlap") in _codes(report)


def test_contained_narrow_duplicate_is_severe():
    report = compare_partitions([_box("a", 0, 5)], [_box("a", 0, 5), _box("tiny-duplicate", 0, 0.001)])
    overlap = next(item for item in report["findings"] if item["code"] == "candidate_spaces_overlap")
    assert overlap["severity"] == "severe"


def test_substantial_reference_overlap_is_not_accepted_as_ground_truth():
    reference = [_box("a", 0, 5), _box("b", 4, 6)]
    report = compare_partitions(reference, copy.deepcopy(reference))
    assert report["status"] == "not_evaluated"


@pytest.mark.parametrize("tolerance", [-0.001, float("nan"), float("inf")])
def test_invalid_tolerance_is_rejected(tolerance):
    with pytest.raises(ValueError, match="tolerance_m"):
        compare_partitions([], [], tolerance_m=tolerance)


def test_zero_tolerance_allows_only_exact_planar_geometry():
    reference = [_box("a", 0, 5)]
    assert compare_partitions(reference, reference, tolerance_m=0)["status"] == "pass"
    assert compare_partitions(reference, [_box("b", 0, 5.001)], tolerance_m=0)["status"] == "severe"
