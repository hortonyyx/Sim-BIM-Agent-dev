"""Accepted independent readings become 3D only through explicit assembly decisions."""
from __future__ import annotations

import copy

import pytest

from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.plan_partition import compile_plan_partition
from src.agent.runtime_roles.elevation import validate_elevation_artifact
from src.agent.runtime_roles.plan_materialization import (
    PlanMaterializationError, materialize_plan_reading,
)


SIZE = (1201, 1001)


def plan():
    return {
        "reading_type": "plan", "floor_id": "F1", "z_floor": 3.6, "ceiling_height": 3.6,
        "x_anchors": [[100, 0], [1100, 10]], "y_anchors": [[100, 8], [900, 0]],
        "footprint_pixels": [[100, 100], [1100, 100], [1100, 900], [100, 900]],
        "partitions": [{"id": "P", "points": [[500, 100], [500, 900]]}],
        "space_seeds": [{"id": "L", "point": [200, 300]}, {"id": "R", "point": [1000, 500]}],
        "openings": [
            {"id": "WN", "kind": "window", "p1": [200, 100], "p2": [300, 100]},
            {"id": "DN", "kind": "door", "p1": [700, 100], "p2": [800, 100]},
            {"id": "WE", "kind": "window", "p1": [1100, 300], "p2": [1100, 400]},
            {"id": "DI", "kind": "door", "p1": [500, 400], "p2": [500, 500]},
            {"id": "OI", "kind": "open", "p1": [500, 600], "p2": [500, 700]},
        ],
        "basis": "Synthetic XY reading with assembly-resolved absolute floor levels.",
        "assumptions": [], "unresolved": [],
        "wall_metadata": {"P": {"thickness_m": .24, "basis": "annotation"}},
    }


def artifact(orientation, openings, *, name=None, floor="F1"):
    """Opening tuples are (id, kind, world_span, sill, head[, floor])."""
    positive = orientation in {"South", "East"}
    length = 10 if orientation in {"North", "South"} else 8
    start, end = (0, length) if positive else (length, 0)
    image = name or f"{orientation}.png"
    rows = []
    for identity, kind, span, sill, head, *row_floor in openings:
        pixels = sorted(100 + 1000 * (x - start) / (end - start) for x in span)
        rows.append({"id": identity, "kind": kind, "floor_id": row_floor[0] if row_floor else floor,
                     "x_px": pixels, "width_m": span[1] - span[0], "sill_m": sill, "head_m": head,
                     "evidence_type": "annotation_and_pixels", "bbox": [pixels[0], 100, pixels[1], 400]})
    rows.sort(key=lambda row: (row["floor_id"], sum(row["x_px"])))
    floors = sorted({floor, *[row["floor_id"] for row in rows]})
    return validate_elevation_artifact({
        "image": image, "orientation": orientation,
        "view_direction": {"North": "South", "South": "North", "East": "West", "West": "East"}[orientation],
        "x_calibration": {"pixel_start": 100, "pixel_end": 1100, "world_start_m": start, "world_end_m": end},
        "elevations": [{"id": "F1", "floor_id": floor, "kind": "floor", "value_m": 3.6,
                        "evidence_type": "annotation", "bbox": [0, 450, 80, 500]}],
        "openings": rows,
        "counts": [{"floor_id": level,
                    "window_count": sum(row["floor_id"] == level and row["kind"] == "window" for row in rows),
                    "door_count": sum(row["floor_id"] == level and row["kind"] == "door" for row in rows)}
                   for level in floors],
        "unresolved": [],
    })


def materialize(value, elevations):
    return materialize_plan_reading(value, elevations=elevations, image_size=SIZE, image_name="plan.png")


def height_decisions(decisions):
    return {row["opening_id"]: row for row in decisions if row["kind"] == "opening_height_materialization"}


def test_matching_and_explicit_internal_defaults_compile_to_real_source_without_mutating_readings(tmp_path):
    reading = plan()
    elevations = {
        "north-task": artifact("North", [("N-W", "window", [1, 2], 4.6, 6.2),
                                           ("N-D", "door", [6, 7], 3.6, 5.9)]),
        "east-task": artifact("East", [("E-W", "window", [5, 6], 4.8, 6.1)]),
    }
    original_plan, original_elevations = copy.deepcopy(reading), copy.deepcopy(elevations)
    assembled, decisions = materialize(reading, elevations)
    by_id = {row["id"]: row for row in assembled["openings"]}
    assert by_id["WN"]["z"] == [4.6, 6.2]
    assert by_id["DN"]["z"] == [3.6, 5.9]
    assert by_id["WE"]["z"] == [4.8, 6.1]
    assert by_id["DI"]["z"] == pytest.approx([3.6, 5.7])
    assert by_id["OI"]["z"] == [3.6, 7.2]
    assert "reader:north-task/opening:N-D" in by_id["DN"]["source_refs"]
    assert "not a plan-reading observation" in by_id["DI"]["assumptions"][0]
    assert by_id["DI"]["assumptions"][0] in assembled["assumptions"]
    assert "assumptions" not in by_id["WN"]
    assert "reading_type" not in assembled
    assert assembled["wall_metadata"] == reading["wall_metadata"]
    statuses = height_decisions(decisions)
    assert statuses["DN"]["facade"] == "North"
    assert statuses["WE"]["facade"] == "East"
    assert statuses["DI"]["status"] == "assembly_assumption"
    assert statuses["DI"]["references"] == []
    assert statuses["WN"]["status"] == "elevation"
    assert statuses["WN"]["references"][0]["artifact_sha256"] == elevations["north-task"]["artifact_sha256"]
    proposal, metadata = compile_plan_partition(assembled, image_size=SIZE, image_name="plan.png")
    exported = export_source_proposal(proposal, tmp_path / "source", provenance={"plan_metadata": metadata})
    assert exported["source_geometry_ready"]
    assert reading == original_plan and elevations == original_elevations


def test_no_elevation_uses_visible_assembly_defaults_only():
    reading = plan()
    assembled, decisions = materialize(reading, {})
    assert all(row["status"] == "assembly_assumption" for row in height_decisions(decisions).values())
    by_id = {row["id"]: row for row in assembled["openings"]}
    assert by_id["WN"]["z"] == [4.5, 6]
    assert by_id["DN"]["z"] == pytest.approx([3.6, 5.7])
    assert len(assembled["assumptions"]) == len(reading["openings"])
    assert all("z" not in row for row in reading["openings"])


@pytest.mark.parametrize("missing", ["z_floor", "ceiling_height"])
def test_materialization_never_invents_floor_levels(missing):
    reading = plan()
    reading.pop(missing)
    with pytest.raises(ValueError, match="assembly must resolve explicit"):
        materialize(reading, {})


def test_multiple_consistent_views_retain_each_source_without_averaging():
    first = artifact("North", [("W-a", "window", [1, 2], 4.6, 6.2)], name="North_a.png")
    second = artifact("North", [("W-b", "window", [1, 2], 4.6, 6.2)], name="North_b.png")
    assembled, decisions = materialize(plan(), {"b": second, "a": first})
    row = height_decisions(decisions)["WN"]
    assert row["z_m"] == [4.6, 6.2]
    assert [ref["task_id"] for ref in row["references"]] == ["a", "b"]
    assert assembled["openings"][0]["z"] == [4.6, 6.2]


def test_conflicting_accepted_view_heights_reject_atomically():
    reading = plan()
    elevations = {
        "first": artifact("North", [("W", "window", [1, 2], 4.6, 6.2)], name="North_a.png"),
        "second": artifact("North", [("W", "window", [1, 2], 4.6, 6.3)], name="North_b.png"),
    }
    before = copy.deepcopy((reading, elevations))
    with pytest.raises(PlanMaterializationError, match="accepted elevation views disagree") as error:
        materialize(reading, elevations)
    conflict = error.value.decisions[-1]
    assert conflict["opening_id"] == "WN" and conflict["status"] == "conflict"
    assert len(conflict["references"]) == 2
    assert (reading, elevations) == before


def test_explicit_plan_observation_is_retained_or_conflict_is_reported():
    reading = plan()
    reading["openings"][0]["z"] = [4.5, 6.1]
    assembled, decisions = materialize(reading, {})
    assert assembled["openings"][0]["z"] == [4.5, 6.1]
    assert height_decisions(decisions)["WN"]["status"] == "reader_observation"
    assert "assumptions" not in assembled["openings"][0]
    with pytest.raises(PlanMaterializationError, match="observed plan height conflicts"):
        materialize(reading, {"north": artifact("North", [("W", "window", [1, 2], 4.6, 6.3)])})


@pytest.mark.parametrize("kind,height", [("door", 2.0), ("window", 2.3)])
def test_default_height_cannot_be_clipped_to_a_short_floor(kind, height):
    reading = plan()
    reading["ceiling_height"] = height
    reading["openings"] = [next(row for row in reading["openings"] if row["kind"] == kind)]
    with pytest.raises(PlanMaterializationError, match="no clipping") as error:
        materialize(reading, {})
    assert error.value.decisions[-1]["height_source"] == "assembly_assumption"
    assert "z" not in reading["openings"][0]


def test_observed_height_outside_resolved_floor_is_rejected_without_clipping():
    reading = plan()
    with pytest.raises(PlanMaterializationError, match="no clipping") as error:
        materialize(reading, {"north": artifact("North", [("W", "window", [1, 2], 4.6, 7.3)])})
    assert error.value.decisions[-1]["proposed_z_m"] == [4.6, 7.3]
    assert error.value.decisions[-1]["height_source"] == "elevation"


@pytest.mark.parametrize("span", [[1.4, 2.4], [0.8, 2.2]])
def test_position_and_width_conflicts_do_not_become_observed_heights(span):
    assembled, decisions = materialize(plan(), {
        "north": artifact("North", [("W", "window", span, 4.6, 6.2)])})
    assert assembled["openings"][0]["z"] == [4.5, 6]
    assert height_decisions(decisions)["WN"]["status"] == "assembly_assumption"
    assert any(row["kind"] == "elevation_match_unresolved" and row.get("opening_id") == "WN"
               for row in decisions)


def test_floor_and_kind_are_independent_match_groups():
    assembled, decisions = materialize(plan(), {
        "north": artifact("North", [("wrong-floor", "window", [1, 2], 4.6, 6.2, "F2"),
                                       ("wrong-kind", "window", [6, 7], 4.7, 6.4)])})
    by_id = {row["id"]: row for row in assembled["openings"]}
    assert by_id["WN"]["z"] == [4.5, 6]
    assert by_id["DN"]["z"] == pytest.approx([3.6, 5.7])
    assert height_decisions(decisions)["WN"]["status"] == "assembly_assumption"
    assert height_decisions(decisions)["DN"]["status"] == "assembly_assumption"


def test_missing_middle_opening_keeps_neighbor_identities_in_order():
    reading = plan()
    reading["openings"] = [{"id": f"W{i}", "kind": "window", "p1": [x, 100], "p2": [x + 80, 100]}
                           for i, x in enumerate((200, 600, 900), 1)]
    elevations = {"north": artifact("North", [("third", "window", [8, 8.8], 4.8, 6.4),
                                               ("first", "window", [1, 1.8], 4.6, 6.2)])}
    assembled, decisions = materialize(reading, elevations)
    by_id = {row["id"]: row for row in assembled["openings"]}
    assert by_id["W1"]["z"] == [4.6, 6.2]
    assert by_id["W2"]["z"] == [4.5, 6]
    assert by_id["W3"]["z"] == [4.8, 6.4]
    assert height_decisions(decisions)["W2"]["status"] == "assembly_assumption"


def test_ambiguous_ordered_identity_cannot_supply_elevation_height():
    reading = plan()
    reading["partitions"] = []
    reading["space_seeds"] = [{"id": "whole", "point": [200, 300]}]
    reading["openings"] = [
        {"id": "left", "kind": "window", "p1": [440, 100], "p2": [520, 100]},
        {"id": "right", "kind": "window", "p1": [480, 100], "p2": [560, 100]},
    ]
    assembled, decisions = materialize(reading, {
        "north": artifact("North", [("uncertain", "window", [3.6, 4.4], 4.8, 6.4)])})
    assert all(row["z"] == [4.5, 6] for row in assembled["openings"])
    assert any(row.get("ambiguous") for row in decisions)
    assert all(row["status"] == "assembly_assumption" for row in height_decisions(decisions).values())


@pytest.mark.parametrize("orientation,p1,p2,span", [
    ("North", [700, 100], [800, 100], [6, 7]),
    ("South", [700, 900], [800, 900], [6, 7]),
    ("East", [1100, 300], [1100, 400], [5, 6]),
    ("West", [100, 300], [100, 400], [5, 6]),
])
def test_exterior_door_facade_uses_ccw_footprint_and_matches_mirrored_views(orientation, p1, p2, span):
    reading = plan()
    reading["openings"] = [{"id": "door", "kind": "door", "p1": p1, "p2": p2}]
    assembled, decisions = materialize(reading, {
        "view": artifact(orientation, [("observed", "door", span, 3.6, 5.9)])})
    assert assembled["openings"][0]["z"] == [3.6, 5.9]
    assert height_decisions(decisions)["door"]["facade"] == orientation
    assert height_decisions(decisions)["door"]["status"] == "elevation"


def test_tampered_accepted_artifact_is_rejected():
    observed = artifact("North", [("W", "window", [1, 2], 4.6, 6.2)])
    observed["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact_sha256"):
        materialize(plan(), {"north": observed})


def test_level_resolution_preserves_absolute_2d_observations_and_rejects_untyped_plan():
    from src.agent.runtime_roles.levels import apply_resolved_levels
    reading = plan()
    reading["openings"] = [{**reading["openings"][0], "z": [4.6, 6.2]}]
    resolution = {"z_floor": {"value_m": 4.0}, "ceiling_height": {"value_m": 3.6}}
    resolved = apply_resolved_levels(reading, resolution)
    assert resolved["openings"][0]["z"] == [4.6, 6.2]
    assert reading["openings"][0]["z"] == [4.6, 6.2]
    reading.pop("reading_type")
    with pytest.raises(ValueError, match="requires a 2D plan reading"):
        apply_resolved_levels(reading, resolution)
