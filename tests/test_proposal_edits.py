"""Deterministic proposal edits preserve source identities and audit history."""
from __future__ import annotations

import copy

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.agent.geometry_model.parse import parse_geometry
from src.agent.geometry.proposal_edits import apply_proposal_edits
from src.agent.geometry.source_bim import build_source_bim


def _proposal():
    return {
        "geometry": {
            "schema_version": "2", "footprint_x": [0, 8], "footprint_y": [0, 6],
            "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
                {"id": "left", "role": "office", "x": [0, 4], "y": [0, 6],
                 "polygon": [[0, 0], [4, 0], [4, 2], [3, 2], [3, 4], [4, 4], [4, 6], [0, 6]]},
                {"id": "right", "role": "corridor", "x": [3, 8], "y": [0, 6],
                 "polygon": [[4, 0], [8, 0], [8, 6], [4, 6], [4, 4], [3, 4], [3, 2], [4, 2]]},
            ]}],
            "windows": [
                {"id": "north", "floor": "F1", "facade": "North", "span": [1, 2], "z": [1, 2], "room": "left"},
                {"id": "south", "floor": "F1", "facade": "South", "span": [5, 6], "z": [1, 2], "room": "right"},
                {"id": "east", "floor": "F1", "facade": "East", "span": [1, 2], "z": [1, 2], "room": "right"},
                {"id": "west", "floor": "F1", "facade": "West", "span": [4, 5], "z": [1, 2], "room": "left"},
            ],
            "openings": [{"id": "door", "kind": "door", "space_id": "left", "other_space_id": "right",
                          "p1": [4, .5], "p2": [4, 1.5], "z": [0, 2.1], "source_refs": ["fixture:door"]}],
        },
        "assumptions": ["west side inferred"],
        "unresolved": ["orientation needs review"],
    }


def _without_audit(geometry):
    result = copy.deepcopy(geometry)
    result.pop("corrections", None)
    return result


def test_room_function_edit_and_withdrawal_preserve_geometry_and_other_evidence():
    proposal = _proposal()
    cell = proposal["geometry"]["floors"][0]["cells"][0]
    cell.update(source_refs=["plan: wall trace"], assumptions=["wall plane simplified"])
    before = copy.deepcopy(proposal)
    op = {"op": "set_space_role", "space_id": "left", "role": "meeting",
          "basis": "inferred", "source_refs": ["plan: long table at pixels [1,2,3,4]"],
          "assumptions": ["No text label; use inferred from furniture"], "reason": "Review room use"}
    revised = apply_proposal_edits(proposal, [op])
    changed = revised["geometry"]["floors"][0]["cells"][0]
    assert changed["role"] == "conference/meeting/multipurpose"
    assert changed["role_evidence"]["basis"] == "inferred"
    assert changed["source_refs"] == cell["source_refs"]
    assert changed["assumptions"] == cell["assumptions"]
    source = build_source_bim(parse_geometry(revised["geometry"]), capability_profile="orthogonal_polygon")
    original = build_source_bim(parse_geometry(proposal["geometry"]), capability_profile="orthogonal_polygon")
    for key in ("boundaries", "openings", "connections", "floors", "opening_hosts", "boundary_relations"):
        assert source[key] == original[key]
    for space, old in zip(source["spaces"], original["spaces"]):
        assert {k: v for k, v in space.items() if k not in {"role", "role_evidence"}} == {
            k: v for k, v in old.items() if k not in {"role", "role_evidence"}}
    restored = apply_proposal_edits(revised, [{**op, "role": "unknown", "basis": "unknown",
        "source_refs": ["plan: furniture interpretation ambiguous"], "assumptions": []}])
    restored_cell = restored["geometry"]["floors"][0]["cells"][0]
    assert restored_cell["role"] == "unknown"
    assert restored_cell["role_evidence"]["source_refs"] == ["plan: furniture interpretation ambiguous"]
    assert restored["geometry"]["corrections"][-1]["before"]["role_evidence"] == changed["role_evidence"]
    assert proposal == before


@pytest.mark.parametrize("patch", [
    {"role": "office_inferred"}, {"basis": "observed", "role": "unknown"},
    {"basis": "unknown"}, {"assumptions": []}, {"source_refs": []},
    {"space_id": "missing"}, {"polygon": [[0, 0], [1, 0], [1, 1]]},
])
def test_invalid_room_function_edit_never_changes_input(patch):
    proposal = _proposal()
    before = copy.deepcopy(proposal)
    operation = {"op": "set_space_role", "space_id": "left", "role": "office", "basis": "inferred",
                 "source_refs": ["plan: desks"], "assumptions": ["Use inferred"], "reason": "Review use"}
    with pytest.raises(ValueError):
        apply_proposal_edits(proposal, [{**operation, **patch}])
    assert proposal == before


@pytest.mark.parametrize("basis,role", [("observed", "office"), ("unknown", "unknown")])
def test_room_function_edit_can_omit_unused_assumptions(basis, role):
    proposal = _proposal()
    operation = {"op": "set_space_role", "space_id": "left", "role": role, "basis": basis,
                 "source_refs": ["plan: explicit label or insufficient evidence"], "reason": "Review use"}
    revised = apply_proposal_edits(proposal, [operation])
    explicit = apply_proposal_edits(proposal, [{**operation, "assumptions": []}])
    assert revised == explicit
    assert revised["geometry"]["floors"][0]["cells"][0]["role_evidence"]["assumptions"] == []


def test_room_function_missing_inferred_assumption_and_bad_type_remain_errors():
    proposal = _proposal()
    before = copy.deepcopy(proposal)
    operation = {"op": "set_space_role", "space_id": "left", "role": "office", "basis": "inferred",
                 "source_refs": ["plan: furniture"], "reason": "Review use"}
    with pytest.raises(ValueError, match="inferred room role requires an explicit assumption"):
        apply_proposal_edits(proposal, [operation])
    for value in (None, "", [1]):
        with pytest.raises(ValueError, match="set_space_role: assumptions must be a list of strings"):
            apply_proposal_edits(proposal, [{**operation, "assumptions": value}])
    assert proposal == before


def _rectangular_wall_proposal():
    return {
        "geometry": {
            "schema_version": "2", "footprint_x": [0, 10], "footprint_y": [0, 4],
            "floors": [
                {"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
                    {"id": "left", "role": "office", "x": [0, 4], "y": [0, 4]},
                    {"id": "right", "role": "office", "x": [4, 10], "y": [0, 4]},
                ]},
                {"name": "F2", "z_floor": 3, "ceiling_height": 3, "cells": [
                    {"id": "upper_left", "role": "office", "x": [0, 4], "y": [0, 4]},
                    {"id": "upper_right", "role": "office", "x": [4, 10], "y": [0, 4]},
                ]},
            ],
            "windows": [{"id": "left_south", "floor": "F1", "facade": "South", "span": [1, 2],
                         "z": [1, 2], "room": "left"}],
            "openings": [
                {"id": "between", "kind": "door", "space_id": "left", "other_space_id": "right",
                 "p1": [4, 1], "p2": [4, 2], "z": [0, 2.1], "source_refs": ["fixture:shared wall"]},
                {"id": "south", "kind": "door", "space_id": "left", "other_space_id": None,
                 "p1": [2, 0], "p2": [3, 0], "z": [0, 2.1], "source_refs": ["fixture:south wall"]},
                {"id": "upper_between", "kind": "door", "space_id": "upper_left", "other_space_id": "upper_right",
                 "p1": [4, 1], "p2": [4, 2], "z": [3, 5.1], "source_refs": ["fixture:upper shared wall"]},
            ],
        },
        "assumptions": ["synthetic rectangle"], "unresolved": [],
    }


def _proposal_with_upper_floor():
    proposal = _proposal()
    proposal["geometry"]["floors"].append({
        "name": "F2", "z_floor": 3, "ceiling_height": 3, "cells": [
            {"id": "upper_left", "role": "office", "x": [0, 4], "y": [0, 6]},
            {"id": "upper_right", "role": "corridor", "x": [4, 8], "y": [0, 6]},
        ],
    })
    proposal["geometry"]["windows"].append({
        "id": "upper_north", "floor": "F2", "facade": "North", "span": [1, 2],
        "z": [4, 5], "room": "upper_left",
    })
    proposal["geometry"]["openings"].append({
        "id": "upper_door", "kind": "door", "space_id": "upper_left", "other_space_id": "upper_right",
        "p1": [4, 1], "p2": [4, 2], "z": [3, 5.1], "source_refs": ["fixture:upper door"],
    })
    return proposal


def _replace_region(polygon, *, space_id="left", neighbor_space_id="right"):
    return {
        "op": "replace_space_region", "space_id": space_id, "neighbor_space_id": neighbor_space_id,
        "polygon": polygon, "reason": "plan trace locates the corrected room boundary",
        "source_refs": ["plan: local boundary trace"],
    }


def test_reflection_preserves_nonrectangular_rooms_openings_and_source_buildability():
    proposal = _proposal()
    x_reflected = apply_proposal_edits(proposal, [{"op": "reflect", "axis": "x", "reason": "plan is mirrored"}])
    facades = {window["id"]: window["facade"] for window in x_reflected["geometry"]["windows"]}
    assert facades == {"north": "North", "south": "South", "east": "West", "west": "East"}
    assert x_reflected["geometry"]["openings"][0]["p1"] == [4, .5]
    assert x_reflected["geometry"]["floors"][0]["cells"][0]["id"] == "left"
    assert "direction language" in x_reflected["geometry"]["corrections"][-1]["notice"]
    assert any("direction language" in item for item in x_reflected["unresolved"])
    assert build_source_bim(parse_geometry(x_reflected["geometry"]),
                            capability_profile="orthogonal_polygon")["validation"]["status"] == "pass"

    y_reflected = apply_proposal_edits(proposal, [{"op": "reflect", "axis": "y", "reason": "north arrow corrected"}])
    assert {window["id"]: window["facade"] for window in y_reflected["geometry"]["windows"]} == {
        "north": "South", "south": "North", "east": "East", "west": "West"}
    twice = apply_proposal_edits(proposal, [
        {"op": "reflect", "axis": "x", "reason": "first transform"},
        {"op": "reflect", "axis": "x", "reason": "inverse transform"},
    ])
    assert _without_audit(twice["geometry"]) == _without_audit(proposal["geometry"])
    assert proposal == _proposal()
    assert apply_proposal_edits(proposal, [
        {"op": "reflect", "axis": "x", "reason": "plan is mirrored"},
        {"op": "set_notes", "assumptions": ["corrected orientation"], "unresolved": []},
    ])["unresolved"] == []


def test_updates_removal_and_notes_keep_full_audit_history():
    proposal = _proposal()
    revised = apply_proposal_edits(proposal, [
        {"op": "update_window", "id": "west", "changes": {"z": [1.2, 2.2], "assumptions": ["elevation replaces placeholder"]},
         "reason": "elevation confirms sill", "source_refs": ["elevation:west"]},
        {"op": "update_opening", "id": "door", "changes": {"state": "closed", "assumptions": ["leaf state observed"]},
         "reason": "door leaf visible", "source_refs": ["plan:door-leaf"]},
        {"op": "remove_opening", "id": "door", "reason": "later view disproves opening",
         "source_refs": ["elevation:no-door"]},
        {"op": "set_notes", "assumptions": ["new orientation"], "unresolved": []},
    ])
    assert proposal == _proposal()
    assert not revised["geometry"]["openings"]
    audit = revised["geometry"]["corrections"]
    assert [row["operation"] for row in audit] == ["update_window", "update_opening", "remove_opening", "set_notes"]
    assert audit[0]["before"]["z"] == [1, 2] and audit[0]["after"]["z"] == [1.2, 2.2]
    assert revised["geometry"]["windows"][3]["source_refs"] == ["elevation:west"]
    assert revised["geometry"]["windows"][3]["assumptions"] == ["elevation replaces placeholder"]
    assert audit[1]["before"]["source_refs"] == ["fixture:door"]
    assert audit[1]["after"]["source_refs"] == ["plan:door-leaf"]
    assert audit[1]["after"]["assumptions"] == ["leaf state observed"]
    assert audit[2]["before"]["id"] == "door" and audit[2]["after"] is None
    assert audit[2]["target_collection"] == "openings"
    assert audit[2]["source_refs"] == ["elevation:no-door"]
    assert revised["assumptions"] == ["new orientation"] and revised["unresolved"] == []


@pytest.mark.parametrize("field", ["assumptions", "unresolved"])
@pytest.mark.parametrize("replacement", [[], ["Review completed for both floors"]])
def test_duplicate_global_note_does_not_block_an_atomic_use_update(field, replacement):
    proposal = _proposal()
    old = "Plan overlays pending review"
    proposal[field] = ["Keep first", old, "Keep middle", old, "Keep last"]
    before = copy.deepcopy(proposal)
    revised = apply_proposal_edits(proposal, [
        {"op": "set_space_role", "space_id": "left", "role": "meeting", "basis": "inferred",
         "assumptions": ["Use inferred from furniture"], "reason": "Review use",
         "source_refs": ["plan: furniture"]},
        {"op": "replace_note", "field": field, "old": old, "replacement": replacement,
         "reason": "Reviewed both floor overlays", "source_refs": ["plan: both floors"]},
    ])
    assert revised[field] == ["Keep first", *replacement, "Keep middle", "Keep last"]
    assert revised["geometry"]["floors"][0]["cells"][0]["role"] == "conference/meeting/multipurpose"
    assert revised["geometry"]["windows"] == before["geometry"]["windows"]
    assert revised["geometry"]["openings"] == before["geometry"]["openings"]
    audit = revised["geometry"]["corrections"][-1]
    assert audit["before"] == old and audit["after"] == replacement
    assert audit["matched_count"] == 2 and audit["matched_indices"] == [1, 3]
    assert proposal == before


def test_note_replacement_still_rejects_a_rephrased_or_stale_target_without_mutation():
    proposal = _proposal()
    before = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match="old must exactly match"):
        apply_proposal_edits(proposal, [
            {"op": "set_space_role", "space_id": "left", "role": "meeting", "basis": "inferred",
             "assumptions": ["Use inferred from furniture"], "reason": "Review use",
             "source_refs": ["plan: furniture"]},
            {"op": "replace_note", "field": "unresolved", "old": "Orientation needs review",
             "replacement": [], "reason": "Reviewed orientation", "source_refs": ["plan: north arrow"]},
        ])
    assert proposal == before


def test_remove_opening_removes_a_built_window_by_its_unique_aperture_id():
    proposal = _proposal()
    revised = apply_proposal_edits(proposal, [{
        "op": "remove_opening", "id": "west", "reason": "facade review retracts this window",
        "source_refs": ["elevation: solid wall"],
    }])
    assert {row["id"] for row in revised["geometry"]["windows"]} == {"north", "south", "east"}
    assert revised["geometry"]["openings"] == proposal["geometry"]["openings"]
    audit = revised["geometry"]["corrections"][-1]
    assert audit["target_collection"] == "windows"
    assert audit["before"]["id"] == "west" and audit["after"] is None
    source = build_source_bim(parse_geometry(revised["geometry"]),
                              capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "pass"
    assert "west" not in {row["id"] for row in source["openings"]}


def test_remove_opening_retracts_unbuilt_window_from_parent_proposal():
    proposal = _proposal()
    bad = next(row for row in proposal["geometry"]["windows"] if row["id"] == "west")
    bad["span"] = [7, 8]
    before = build_source_bim(parse_geometry(proposal["geometry"]),
                              capability_profile="orthogonal_polygon")
    assert before["validation"]["status"] == "severe"
    assert [row["id"] for row in before["unbuilt_openings"]] == ["west"]

    revised = apply_proposal_edits(proposal, [{
        "op": "remove_opening", "id": "west", "reason": "invalid host disproves this window",
        "source_refs": ["source.opening_unbuilt diagnostic"],
    }])
    after = build_source_bim(parse_geometry(revised["geometry"]),
                             capability_profile="orthogonal_polygon")
    assert after["validation"]["status"] == "pass"
    assert after["unbuilt_openings"] == []
    assert revised["geometry"]["corrections"][-1]["target_collection"] == "windows"


def test_remove_opening_rejects_absent_or_cross_collection_duplicate_ids():
    operation = {"op": "remove_opening", "id": "absent", "reason": "synthetic absence",
                 "source_refs": ["synthetic review"]}
    with pytest.raises(ValueError, match="expected exactly one existing id 'absent', found 0"):
        apply_proposal_edits(_proposal(), [operation])

    duplicate = _proposal()
    duplicate["geometry"]["openings"][0]["id"] = "west"
    with pytest.raises(ValueError, match="duplicate opening id: west"):
        apply_proposal_edits(duplicate, [{**operation, "id": "west"}])


def test_move_shared_wall_preserves_unrelated_geometry_and_hosts_its_door():
    proposal = _rectangular_wall_proposal()
    revised = apply_proposal_edits(proposal, [{
        "op": "move_shared_wall", "space_ids": ["left", "right"], "coordinate_m": 4.5,
        "reason": "plan dimension locates the complete interior wall", "source_refs": ["plan: wall dimension"],
    }])
    floor = revised["geometry"]["floors"][0]
    cells = {cell["id"]: cell for cell in floor["cells"]}
    openings = {opening["id"]: opening for opening in revised["geometry"]["openings"]}
    assert cells["left"]["x"] == [0, 4.5]
    assert cells["right"]["x"] == [4.5, 10]
    assert openings["between"]["p1"] == [4.5, 1]
    assert openings["between"]["p2"] == [4.5, 2]
    assert openings["south"] == proposal["geometry"]["openings"][1]
    assert openings["upper_between"] == proposal["geometry"]["openings"][2]
    assert revised["geometry"]["windows"] == proposal["geometry"]["windows"]
    assert revised["geometry"]["floors"][1] == proposal["geometry"]["floors"][1]
    audit = revised["geometry"]["corrections"][-1]
    assert audit["axis"] == "x" and audit["from_coordinate_m"] == 4
    assert audit["moved_openings"][0]["id"] == "between"
    assert proposal == _rectangular_wall_proposal()
    assert build_source_bim(parse_geometry(revised["geometry"]),
                            capability_profile="orthogonal_polygon")["validation"]["status"] == "pass"


def test_reshape_spaces_replaces_nonrectangular_rings_without_touching_openings_or_other_floors():
    proposal = _rectangular_wall_proposal()
    proposal["geometry"]["floors"][0]["cells"][0]["label"] = "keep this source attribute"
    # This doorway remains on the unchanged lower section of the shared wall.
    proposal["geometry"]["openings"][0]["p1"] = [4, .2]
    proposal["geometry"]["openings"][0]["p2"] = [4, .8]
    original = copy.deepcopy(proposal)
    revised = apply_proposal_edits(proposal, [{
        "op": "reshape_spaces",
        "spaces": [
            {"id": "left", "polygon": [[0, 0], [4, 0], [4, 1], [5, 1], [5, 3], [4, 3], [4, 4], [0, 4]]},
            {"id": "right", "polygon": [[4, 0], [10, 0], [10, 4], [4, 4], [4, 3], [5, 3], [5, 1], [4, 1]]},
        ],
        "reason": "plan shows a stepped partition", "source_refs": ["plan: stepped partition"],
    }])
    cells = {cell["id"]: cell for cell in revised["geometry"]["floors"][0]["cells"]}
    assert cells["left"]["x"] == [0, 5] and cells["left"]["y"] == [0, 4]
    assert cells["right"]["x"] == [4, 10] and cells["right"]["y"] == [0, 4]
    assert cells["left"]["label"] == "keep this source attribute"
    assert revised["geometry"]["openings"] == original["geometry"]["openings"]
    assert revised["geometry"]["windows"] == original["geometry"]["windows"]
    assert revised["geometry"]["floors"][1] == original["geometry"]["floors"][1]
    audit = revised["geometry"]["corrections"][-1]
    assert audit["reason"] == "plan shows a stepped partition"
    assert audit["source_refs"] == ["plan: stepped partition"]
    assert audit["before"]["cells"]["left"] == original["geometry"]["floors"][0]["cells"][0]
    assert audit["after"]["cells"]["left"] == cells["left"]
    assert proposal == original
    assert build_source_bim(parse_geometry(revised["geometry"]),
                            capability_profile="orthogonal_polygon")["validation"]["status"] == "pass"


def test_reshape_spaces_requires_explicit_opening_changes_and_source_builder_keeps_its_gates():
    proposal = _rectangular_wall_proposal()
    reshaped = apply_proposal_edits(proposal, [{
        "op": "reshape_spaces",
        "spaces": [
            {"id": "left", "polygon": [[0, 0], [4, 0], [4, 1], [5, 1], [5, 3], [4, 3], [4, 4], [0, 4]]},
            {"id": "right", "polygon": [[4, 0], [10, 0], [10, 4], [4, 4], [4, 3], [5, 3], [5, 1], [4, 1]]},
        ],
        "reason": "synthetic stepped partition", "source_refs": ["plan: synthetic"],
    }])
    # The old door is deliberately untouched and is now unhosted.  The normal
    # source builder reports that fact instead of silently moving or resizing it.
    assert reshaped["geometry"]["openings"][0] == proposal["geometry"]["openings"][0]
    source = build_source_bim(parse_geometry(reshaped["geometry"]),
                              capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "severe"
    assert any(row["code"] == "source.opening_unbuilt" for row in source["validation"]["findings"])

    overlapping = apply_proposal_edits(proposal, [{
        "op": "reshape_spaces",
        "spaces": [
            {"id": "left", "polygon": [[0, 0], [6, 0], [6, 4], [0, 4]]},
            {"id": "right", "polygon": [[4, 0], [10, 0], [10, 4], [4, 4]]},
        ],
        "reason": "synthetic invalid overlap", "source_refs": ["plan: synthetic"],
    }])
    source = build_source_bim(parse_geometry(overlapping["geometry"]),
                              capability_profile="orthogonal_polygon")
    assert source["validation"]["status"] == "severe"
    assert any(row["code"] == "source.space_overlap" for row in source["validation"]["findings"])


def test_reshape_spaces_rejects_stale_wall_declarations_and_invalid_rows_without_mutating_input():
    proposal = _rectangular_wall_proposal()
    original = copy.deepcopy(proposal)
    operation = {"op": "reshape_spaces", "spaces": [{"id": "missing", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
                 "reason": "synthetic", "source_refs": ["plan: synthetic"]}
    with pytest.raises(ValueError, match="unknown existing space ids"):
        apply_proposal_edits(proposal, [operation])
    assert proposal == original
    with pytest.raises(ValueError, match="wall references/dimensions"):
        apply_proposal_edits({**proposal, "wall_references": [{"id": "W1"}]}, [{
            **operation, "spaces": [{"id": "left", "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]]}],
        }])
    with pytest.raises(ValueError, match="enclosure_declaration"):
        apply_proposal_edits({**proposal, "enclosure_declaration": {}}, [{
            **operation, "spaces": [{"id": "left", "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]]}],
        }])


def test_replace_space_region_turns_interlocking_rooms_into_rectangles_and_preserves_other_objects():
    proposal = _proposal_with_upper_floor()
    original = copy.deepcopy(proposal)
    old_cells = proposal["geometry"]["floors"][0]["cells"]
    old_union = unary_union([Polygon(row["polygon"]) for row in old_cells])
    revised = apply_proposal_edits(proposal, [
        _replace_region([[0, 0], [5, 0], [5, 6], [0, 6]]),
        {"op": "update_opening", "id": "door", "changes": {"p1": [5, .5], "p2": [5, 1.5]},
         "reason": "door follows the observed replacement partition", "source_refs": ["plan: door on trace"]},
    ])
    cells = {row["id"]: row for row in revised["geometry"]["floors"][0]["cells"]}
    assert Polygon(cells["left"]["polygon"]).equals(Polygon([[0, 0], [5, 0], [5, 6], [0, 6]]))
    assert Polygon(cells["right"]["polygon"]).equals(Polygon([[5, 0], [8, 0], [8, 6], [5, 6]]))
    assert cells["left"]["x"] == [0, 5] and cells["right"]["x"] == [5, 8]
    assert cells["left"]["y"] == cells["right"]["y"] == [0, 6]
    assert unary_union([Polygon(cells["left"]["polygon"]), Polygon(cells["right"]["polygon"])]).symmetric_difference(old_union).area < 1e-7
    assert revised["geometry"]["windows"] == original["geometry"]["windows"]
    assert revised["geometry"]["floors"][1] == original["geometry"]["floors"][1]
    assert next(row for row in revised["geometry"]["openings"] if row["id"] == "upper_door") == original["geometry"]["openings"][-1]
    assert next(row for row in revised["geometry"]["openings"] if row["id"] == "door")["p1"] == [5, .5]
    audit = revised["geometry"]["corrections"]
    assert [row["operation"] for row in audit] == ["replace_space_region", "update_opening"]
    assert audit[0]["before"]["cells"]["left"] == original["geometry"]["floors"][0]["cells"][0]
    assert audit[0]["after"]["cells"]["right"] == cells["right"]
    assert proposal == original
    assert build_source_bim(parse_geometry(revised["geometry"]),
                            capability_profile="orthogonal_polygon")["validation"]["status"] == "pass"


@pytest.mark.parametrize(("polygon", "message"), [
    ([[0, 0], [9, 0], [9, 6], [0, 6]], "extends outside"),
    ([[3, 1], [5, 1], [5, 3], [3, 3]], "without holes"),
    ([[3, 0], [5, 0], [5, 6], [3, 6]], "one valid polygon without holes"),
])
def test_replace_space_region_rejects_outside_hole_or_multiregion_remainders_without_mutating_input(polygon, message):
    proposal = _proposal()
    original = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match=message):
        apply_proposal_edits(proposal, [_replace_region(polygon)])
    assert proposal == original


def test_replace_space_region_rejects_spaces_on_different_floors_without_mutating_input():
    proposal = _proposal_with_upper_floor()
    original = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match="together on exactly one floor"):
        apply_proposal_edits(proposal, [_replace_region(
            [[0, 0], [4, 0], [4, 6], [0, 6]], space_id="left", neighbor_space_id="upper_right",
        )])
    assert proposal == original


def test_replace_space_region_requires_an_actual_shared_edge_without_mutating_input():
    proposal = _proposal()
    proposal["geometry"]["floors"][0]["cells"][1].update({
        "polygon": [[4, 6], [8, 6], [8, 8], [4, 8]], "x": [4, 8], "y": [6, 8],
    })
    original = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match="actual boundary edge"):
        apply_proposal_edits(proposal, [_replace_region([[0, 0], [4, 0], [4, 6], [0, 6]])])
    assert proposal == original


def test_invalid_operations_do_not_mutate_input_or_silently_ignore_unknowns():
    proposal = _proposal()
    original = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match="unsupported changes"):
        apply_proposal_edits(proposal, [{"op": "update_window", "id": "west", "changes": {"id": "new"},
                                         "reason": "bad", "source_refs": ["test"]}])
    assert proposal == original
    with pytest.raises(ValueError, match="nonempty enclosure_declaration"):
        apply_proposal_edits({**proposal, "enclosure_declaration": {"declared": True}}, [
            {"op": "reflect", "axis": "x", "reason": "would omit enclosure transform"}])
    with pytest.raises(ValueError, match="floor.footprint"):
        with_footprint = copy.deepcopy(proposal)
        with_footprint["geometry"]["floors"][0]["footprint"] = {"vertices": [[0, 0], [8, 0], [8, 6], [0, 6]]}
        apply_proposal_edits(with_footprint, [{"op": "reflect", "axis": "x", "reason": "would omit footprint"}])


@pytest.mark.parametrize("coordinate", [True, float("inf"), 0, 10])
def test_move_shared_wall_rejects_invalid_coordinate_without_mutating_input(coordinate):
    proposal = _rectangular_wall_proposal()
    original = copy.deepcopy(proposal)
    with pytest.raises(ValueError, match="coordinate_m"):
        apply_proposal_edits(proposal, [{
            "op": "move_shared_wall", "space_ids": ["left", "right"], "coordinate_m": coordinate,
            "reason": "synthetic", "source_refs": ["plan: synthetic"],
        }])
    assert proposal == original


def test_move_shared_wall_rejects_partial_polygon_and_enclosure_cases():
    proposal = _rectangular_wall_proposal()
    polygon = copy.deepcopy(proposal)
    polygon["geometry"]["floors"][0]["cells"][0]["polygon"] = [[0, 0], [4, 0], [4, 4], [0, 4]]
    partial = copy.deepcopy(proposal)
    partial["geometry"]["floors"][0]["cells"][1]["y"] = [1, 4]
    enclosed = {**proposal, "enclosure_declaration": {"declared": True}}
    operation = {"op": "move_shared_wall", "space_ids": ["left", "right"], "coordinate_m": 4.5,
                 "reason": "synthetic", "source_refs": ["plan: synthetic"]}
    with pytest.raises(ValueError, match="without polygon"):
        apply_proposal_edits(polygon, [operation])
    with pytest.raises(ValueError, match="complete axis-aligned"):
        apply_proposal_edits(partial, [operation])
    with pytest.raises(ValueError, match="explicit enclosure_declaration"):
        apply_proposal_edits(enclosed, [operation])


def test_move_shared_wall_rejects_same_floor_wrong_host_but_not_other_floor_xy_match():
    proposal = _rectangular_wall_proposal()
    operation = {"op": "move_shared_wall", "space_ids": ["left", "right"], "coordinate_m": 4.5,
                 "reason": "synthetic", "source_refs": ["plan: synthetic"]}
    moved = apply_proposal_edits(proposal, [operation])
    assert next(row for row in moved["geometry"]["openings"] if row["id"] == "upper_between") == proposal["geometry"]["openings"][2]

    wrong_host = copy.deepcopy(proposal)
    wrong_host["geometry"]["openings"][1].update({"p1": [4, 1], "p2": [4, 2]})
    with pytest.raises(ValueError, match="different spaces"):
        apply_proposal_edits(wrong_host, [operation])
