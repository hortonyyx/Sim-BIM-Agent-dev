from __future__ import annotations

import copy
import json

import pytest
from shapely.geometry import Polygon

from src.agent.geometry_model.schema import GeometryInput, WallOpening
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.openings import visible_wall_parts
from scripts.dev.evaluation.source_connections import compare_connections


def _geom(**opening_updates):
    aperture = {"id": "hall_door", "kind": "door", "space_id": "A", "other_space_id": "B",
                "p1": [4, 1], "p2": [4, 2], "z": [0, 2.1], "source_refs": ["fixture:door"]}
    aperture.update(opening_updates)
    return GeometryInput.model_validate({
        "footprint_x": [0, 8], "footprint_y": [0, 4],
        "floors": [{"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
            {"id": "A", "x": [0, 4], "y": [0, 4]}, {"id": "B", "x": [4, 8], "y": [0, 4]},
        ]}], "openings": [aperture],
    })


def test_door_roundtrip_two_sides_and_one_connection():
    geom = _geom()
    decoded = GeometryInput.model_validate_json(geom.model_dump_json())
    assert decoded.openings == geom.openings
    source = build_source_bim(decoded)
    assert source["validation"]["status"] == "pass"
    assert len(source["opening_hosts"]["hall_door"]) == 2
    assert len(source["openings"]) == 1
    assert source["connections"] == [{"opening_id": "hall_door", "kind": "door", "space_ids": ["A", "B"],
                                      "exterior": False, "state": "unknown"}]


@pytest.mark.parametrize("updates", [
    {"other_space_id": None}, {"other_space_id": "missing"}, {"space_id": "missing"},
    {"p1": [3, 1], "p2": [3, 2]}, {"p1": [4, 3], "p2": [4, 5]}, {"z": [0, 3.1]},
])
def test_wrong_room_missing_host_crossed_edge_or_height_are_rejected(updates):
    assert build_source_bim(_geom(**updates))["validation"]["status"] == "severe"


def test_exterior_door_has_one_side_and_does_not_invent_a_neighbour():
    geom = _geom(other_space_id=None, p1=[1, 0], p2=[2, 0])
    source = build_source_bim(geom)
    assert len(source["opening_hosts"]["hall_door"]) == 1
    assert source["connections"][0]["space_ids"] == ["A"]
    assert source["connections"][0]["exterior"]


def test_open_aperture_is_open_and_closed_door_retains_state():
    assert _geom(kind="open").openings[0].state == "open"
    with pytest.raises(ValueError, match="unfilled"):
        _geom(kind="open", state="closed")
    geom = _geom(state="closed")
    assert build_source_bim(geom)["connections"][0]["state"] == "closed"


@pytest.mark.parametrize("updates", [
    {"p1": [float("nan"), 0]}, {"p2": [4, 1]}, {"z": [2, 1]}, {"source_refs": []},
    {"other_space_id": "A"}, {"p2": [5, 2]},
])
def test_invalid_source_apertures_are_not_accepted(updates):
    with pytest.raises(ValueError):
        _geom(**updates)


def test_duplicate_and_overlapping_apertures_are_rejected():
    geom = _geom()
    geom.openings.append(copy.deepcopy(geom.openings[0]))
    assert build_source_bim(geom)["validation"]["status"] == "severe"
    geom.openings[1].id = "another_door"
    assert build_source_bim(geom)["validation"]["status"] == "severe"




def test_display_subtracts_a_floor_touching_door_and_a_raised_hole():
    for z in ([0, 2.1], [.5, 2.1]):
        geom = _geom(z=z)
        parts = visible_wall_parts(source_view_geometry(build_source_bim(geom)))
        assert len(parts) == 2
        for pieces in parts.values():
            area = sum(Polygon([(y, height) for _, y, height in part["verts"]],
                               [[(y, height) for _, y, height in ring] for ring in part["holes"]]).area
                       for part in pieces)
            assert area == pytest.approx(12 - (z[1] - z[0]))
            assert bool(pieces[0]["holes"]) == (z[0] > 0)








@pytest.mark.parametrize("mutation, code", [
    ("drop", "connection_missing"), ("close", "connection_state_changed"),
    ("wrong_space", "connection_wrong_spaces"), ("wrong_host", "connection_wrong_host"),
])
def test_connection_comparison_catches_lost_or_changed_connections(mutation, code):
    geom = _geom(state="open")
    reference = build_source_bim(geom)
    changed = copy.deepcopy(reference)
    if mutation == "drop": changed["openings"] = []
    elif mutation == "close": changed["openings"][0]["connectivity"] = "closed"
    elif mutation == "wrong_space": changed["openings"][0]["space_ids"] = ["A", "C"]
    else: changed["openings"][0]["host_boundary_id"] = "another_wall"
    report = compare_connections(reference, changed)
    assert report["status"] == "severe"
    assert code in {f["code"] for f in report["findings"]}
    assert compare_connections(reference, reference)["status"] == "pass"
    assert compare_connections(None, reference)["status"] == "not_evaluated"


def test_viewer_embeds_cut_walls_and_connection_identity():
    from scripts.tool_scripts.render_geometry_viewer import build_viewer_html

    geom = _geom(kind="open")
    source = build_source_bim(geom)
    data = source_view_geometry(source)
    data["source_model"] = source
    page = build_viewer_html(data)
    start = page.index("window.GEO = ") + len("window.GEO = ")
    embedded = json.loads(page[start:page.index(";</script>", start)])
    assert len(embedded["openings"]) == 2
    assert set(source["opening_hosts"]["hall_door"]) <= set(embedded["visible_wall_parts"])
    assert embedded["source_model"]["connections"][0]["state"] == "open"


@pytest.mark.parametrize("candidate", [{}, {"openings": None}, {"openings": {}}, None])
def test_connection_comparison_rejects_missing_or_invalid_candidate_records(candidate):
    assert compare_connections({"openings": []}, candidate)["status"] == "severe"
