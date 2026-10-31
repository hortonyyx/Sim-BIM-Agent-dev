from __future__ import annotations

import copy

import pytest

from src.agent.geometry_model.schema import GeometryInput
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.source_elevation_view import render_source_elevation


def _source():
    geometry = GeometryInput.model_validate({
        "footprint_x": [0, 4], "footprint_y": [0, 2],
        "floors": [
            {"name": "F1", "z_floor": 0, "ceiling_height": 3, "cells": [
                {"id": "F1_A", "x": [0, 2], "y": [0, 2]},
                {"id": "F1_B", "x": [2, 4], "y": [0, 2]},
            ]},
            {"name": "F2", "z_floor": 3, "ceiling_height": 3, "cells": [
                {"id": "F2_A", "x": [0, 2], "y": [0, 2]},
                {"id": "F2_B", "x": [2, 4], "y": [0, 2]},
            ]},
        ],
        "windows": [
            {"id": "north-f1", "floor": "F1", "facade": "North", "span": [0.3, 1.3], "z": [1, 2], "room": "F1_A"},
            {"id": "north-f2", "floor": "F2", "facade": "North", "span": [0.3, 1.3], "z": [4, 5], "room": "F2_A"},
            {"id": "south-f1", "floor": "F1", "facade": "South", "span": [0.3, 1.3], "z": [1, 2], "room": "F1_A"},
        ],
        "openings": [
            {"id": "north-door", "kind": "door", "space_id": "F1_B", "other_space_id": None,
             "p1": [2.3, 2], "p2": [3.2, 2], "z": [0, 2.1], "source_refs": ["test:north"]},
            {"id": "inside-door", "kind": "door", "space_id": "F1_A", "other_space_id": "F1_B",
             "p1": [2, 0.4], "p2": [2, 1.2], "z": [0, 2.1], "source_refs": ["test:inside"]},
        ],
    })
    return build_source_bim(geometry, capability_profile="orthogonal_polygon")


def test_elevation_filters_real_facade_hosts_and_preserves_world_heights():
    source = _source()
    before = copy.deepcopy(source)
    image, metadata = render_source_elevation(source, "North")

    assert image.mode == "RGB" and image.size == (1200, 800)
    assert source == before
    assert metadata["source_model_sha256"] == source["source_model_sha256"]
    assert metadata["facade"] == "North"
    assert metadata["horizontal_axis"] == "x"
    assert metadata["direction"] == "-x"
    assert {row["id"] for row in metadata["projected_openings"]} == {"north-f1", "north-f2", "north-door"}
    assert "south-f1" not in {row["id"] for row in metadata["projected_openings"]}
    assert "inside-door" not in {row["id"] for row in metadata["projected_openings"]}
    assert {row["floor_id"] for row in metadata["projected_floor_lines"]} == {"F1", "F2"}
    upper = next(row for row in metadata["projected_openings"] if row["id"] == "north-f2")
    lower = next(row for row in metadata["projected_openings"] if row["id"] == "north-f1")
    assert {point[2] for point in upper["world_vertices"]} == {4.0, 5.0}
    assert min(point[1] for point in upper["pixel_vertices"]) < min(point[1] for point in lower["pixel_vertices"])
    # North reverses x in the view: the source's increasing-x first edge runs left.
    assert lower["pixel_vertices"][0][0] > lower["pixel_vertices"][1][0]
    assert metadata["drawing_fidelity"] == "not_evaluated"
    heights = {row["id"]: row for row in metadata["opening_heights"]}
    assert heights["north-f2"]["absolute_z_m"] == [4, 5]
    assert heights["north-f2"]["above_floor_m"] == [1, 2]
    assert heights["north-f2"]["floor_base_z_m"] == 3
    assert heights["north-door"]["opening_height_m"] == 2.1
    assert heights["north-f1"]["horizontal_span_m"] == [0.3, 1.3]


@pytest.mark.parametrize(("facade", "axis", "direction"), [
    ("South", "x", "+x"), ("North", "x", "-x"),
    ("East", "y", "+y"), ("West", "y", "-y"),
])
def test_each_cardinal_view_declares_its_world_horizontal_direction(facade, axis, direction):
    _, metadata = render_source_elevation(_source(), facade)
    assert (metadata["horizontal_axis"], metadata["direction"]) == (axis, direction)


@pytest.mark.parametrize("facade", ["Diagonal", "north", ""])
def test_elevation_rejects_unknown_facades_and_tampered_source(facade):
    with pytest.raises(ValueError, match="facade must"):
        render_source_elevation(_source(), facade)
    source = _source()
    source["spaces"][0]["height"] = 4
    with pytest.raises(ValueError, match="source_model_sha256"):
        render_source_elevation(source, "North")
