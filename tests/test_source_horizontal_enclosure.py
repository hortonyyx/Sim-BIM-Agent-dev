import pytest
from shapely.geometry import Polygon

from src.agent.geometry_model.schema import GeometryInput, FootprintRing
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_enclosure import apply_source_enclosure


def _source():
    """A low volume whose ceiling is partly shared with a smaller upper one."""
    geometry = GeometryInput.model_validate({
        "schema_version": "2",
        "footprint_x": [0, 4],
        "footprint_y": [0, 4],
        "floors": [
            {"name": "lower", "z_floor": 0, "ceiling_height": 3,
             "cells": [{"id": "lower", "x": [0, 4], "y": [0, 4]}]},
            {"name": "upper", "z_floor": 3, "ceiling_height": 2,
             "cells": [{"id": "upper", "x": [0, 2], "y": [0, 4]}]},
        ],
        "windows": [],
        "openings": [],
    })
    # The legacy correction model permits per-storey footprints as extra data,
    # while the source builder consumes their typed form.
    geometry.floors[0].footprint = FootprintRing(vertices=[(0, 0), (4, 0), (4, 4), (0, 4)])
    geometry.floors[1].footprint = FootprintRing(vertices=[(0, 0), (2, 0), (2, 4), (0, 4)])
    return build_source_bim(geometry, capability_profile="orthogonal_polygon")


def _declaration(source, boundaries):
    return {
        "schema_version": "source_enclosure_input_v1",
        "base_source_model_sha256": source["source_model_sha256"],
        "spaces": [],
        "boundaries": boundaries,
    }


def _row(boundary_id, condition="open", scope="whole", vertices=None):
    result = {
        "boundary_id": boundary_id,
        "condition": condition,
        "scope": scope,
        "source_refs": ["test:horizontal-enclosure"],
        "assumptions": [],
        "evidence_kind": "manual_annotation",
    }
    if vertices is not None:
        result["vertices"] = vertices
    return result


def _horizontal_pair(source):
    relation = next(row for row in source["boundary_relations"]
                    if set(row["space_ids"]) == {"lower", "upper"})
    by_id = {row["id"]: row for row in source["boundaries"]}
    lower = next(by_id[bid] for bid in relation["boundary_ids"]
                 if by_id[bid]["geometry_type"] == "ceiling")
    upper = next(by_id[bid] for bid in relation["boundary_ids"]
                 if by_id[bid]["geometry_type"] == "floor")
    return lower, upper


def _horizontal_area(parts):
    return sum(
        Polygon([point[:2] for point in part["verts"]]).area
        - sum(Polygon([point[:2] for point in hole]).area for hole in part["holes"])
        for part in parts
    )


def test_whole_horizontal_open_splits_shared_connection_from_exterior_without_a_slab():
    source = _source()
    lower, upper = _horizontal_pair(source)

    result = apply_source_enclosure(source, _declaration(source, [
        _row(lower["id"]),
        _row(upper["id"]),
    ]))

    connections = result["source_enclosure"]["open_connections"]
    internal = [row for row in connections if not row["exterior"]]
    exterior = [row for row in connections if row["exterior"]]
    assert len(internal) == 2
    assert all(set(row["space_ids"]) == {"lower", "upper"} for row in internal)
    assert sum(Polygon([point[:2] for point in row["vertices"]]).area for row in internal) == 16
    assert len(exterior) == 1
    assert exterior[0]["boundary_id"] == lower["id"]
    assert Polygon([point[:2] for point in exterior[0]["vertices"]]).area == 8
    assert len(result["spaces"]) == len(source["spaces"])
    assert len(result["boundaries"]) == len(source["boundaries"])
    assert result["boundary_relations"] == source["boundary_relations"]


def test_horizontal_shared_open_rejects_single_side_wrong_height_and_outside_region():
    source = _source()
    lower, upper = _horizontal_pair(source)

    with pytest.raises(ValueError, match="matching open declaration"):
        apply_source_enclosure(source, _declaration(source, [_row(lower["id"])]))

    wrong_height = [[0, 0, 3.01], [1, 0, 3.01], [1, 1, 3.01], [0, 1, 3.01]]
    with pytest.raises(ValueError, match="planar polygon on its source floor/ceiling"):
        apply_source_enclosure(source, _declaration(source, [
            _row(lower["id"], scope="partial", vertices=wrong_height),
        ]))

    outside = [[3, 0, 3], [4.1, 0, 3], [4.1, 1, 3], [3, 1, 3]]
    with pytest.raises(ValueError, match="outside its source boundary"):
        apply_source_enclosure(source, _declaration(source, [
            _row(lower["id"], scope="partial", vertices=outside),
        ]))

    diagonal = [[0, 0, 3], [1, 0, 3], [1.5, 1, 3], [0, 1, 3]]
    with pytest.raises(ValueError, match="orthogonal floor/ceiling-plane"):
        apply_source_enclosure(source, _declaration(source, [
            _row(upper["id"], scope="partial", vertices=diagonal),
        ]))


def test_horizontal_unknown_is_gray_but_does_not_create_connectivity():
    source = _source()
    lower, _upper = _horizontal_pair(source)
    region = [[2, 0, 3], [4, 0, 3], [4, 2, 3], [2, 2, 3]]

    result = apply_source_enclosure(source, _declaration(source, [
        _row(lower["id"], condition="unknown", scope="partial", vertices=region),
    ]))
    display = source_view_geometry(result)

    assert result["source_enclosure"]["open_connections"] == []
    assert result["validation"]["status"] == "warning"
    assert display["enclosure_regions"] == [{
        "boundary_id": lower["id"],
        "space_id": "lower",
        "condition": "unknown",
        "verts": region,
        "source_refs": ["test:horizontal-enclosure"],
        "assumptions": [],
        "evidence_kind": "manual_annotation",
    }]
    parts = display["display_surface_parts"][lower["id"]]
    assert _horizontal_area([part for part in parts if part["enclosure_condition"] == "unknown"]) == 4
    assert _horizontal_area([part for part in parts if part["enclosure_condition"] == "physical"]) == 12


def test_horizontal_open_ring_preserves_hole_and_view_subtracts_only_declared_area():
    source = _source()
    lower, upper = _horizontal_pair(source)
    ring = [
        [[0, 0, 3], [2, 0, 3], [2, 1, 3], [0, 1, 3]],
        [[0, 1, 3], [.5, 1, 3], [.5, 3, 3], [0, 3, 3]],
        [[1.5, 1, 3], [2, 1, 3], [2, 3, 3], [1.5, 3, 3]],
        [[0, 3, 3], [2, 3, 3], [2, 4, 3], [0, 4, 3]],
    ]
    exterior_patch = [[2, 0, 3], [4, 0, 3], [4, 4, 3], [2, 4, 3]]
    rows = [
        _row(boundary["id"], scope="partial", vertices=vertices)
        for boundary in (lower, upper)
        for vertices in ring
    ]
    rows.append(_row(lower["id"], scope="partial", vertices=exterior_patch))

    result = apply_source_enclosure(source, _declaration(source, rows))
    internal = [row for row in result["source_enclosure"]["open_connections"]
                if not row["exterior"]]
    assert len(internal) == 2
    assert all(len(row["holes"]) == 1 for row in internal)
    assert all({tuple(point) for point in row["holes"][0]} == {
        (.5, 1.0, 3.0), (1.5, 1.0, 3.0), (1.5, 3.0, 3.0), (.5, 3.0, 3.0),
    } for row in internal)

    display = source_view_geometry(result)
    assert _horizontal_area(display["display_surface_parts"][lower["id"]]) == 2
    assert _horizontal_area(display["display_surface_parts"][upper["id"]]) == 2
    assert len([row for row in display["enclosure_regions"] if row["condition"] == "open"]) == 9
    # Logical source faces remain complete; only disposable view meshes are cut.
    shown_lower = next(row for row in display["surfaces"] if row["name"] == lower["id"])
    assert Polygon([point[:2] for point in shown_lower["verts"]]).area == 16
