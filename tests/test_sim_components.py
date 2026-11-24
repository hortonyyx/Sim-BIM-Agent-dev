from copy import deepcopy

import pytest
from shapely.geometry import Polygon

from src.agent.geometry.component_attributes import thickness_record
from src.agent.geometry.sim_components import build_surface_properties, legacy_surface_properties
from src.agent.geometry.source_bim import build_source_bim
from src.agent.geometry.source_enclosure import apply_source_enclosure
from src.agent.geometry_model.schema import GeometryInput
from tests.test_source_bim import three_rooms
from tests.test_source_horizontal_enclosure import _declaration, _horizontal_pair, _row, _source


def stacked():
    geometry = GeometryInput.model_validate({
        "schema_version": "2", "footprint_x": [0, 8], "footprint_y": [0, 4],
        "floors": [
            {"name": "lower", "z_floor": 0, "ceiling_height": 3, "cells": [
                {"id": "left", "x": [0, 4], "y": [0, 4]},
                {"id": "right", "x": [4, 8], "y": [0, 4]}]},
            {"name": "upper", "z_floor": 3, "ceiling_height": 3,
             "cells": [{"id": "whole", "x": [0, 8], "y": [0, 4]}]}],
        "windows": [], "openings": []})
    source = build_source_bim(geometry)
    faces = {b["space_id"]: b["id"] for b in source["boundaries"] if
             (b["space_id"] == "whole" and b["geometry_type"] == "floor") or
             (b["space_id"] != "whole" and b["geometry_type"] == "ceiling")}
    return source, (faces["whole"], faces["left"], faces["right"])


def thickness(value, source="from material"):
    return {"value_mm": value, "source": source, "source_refs": ["plan:section-A"]}


def materials(en, zh):
    return {"en": en, "zh": zh, "source": "from user", "source_refs": ["block:3/caption"]}


def quantities(result, group, identity):
    return [result["components"][cid]["thickness"] for cid in result[group][identity]["component_ids"]]


def region_area(row):
    return Polygon([v[:2] for v in row["vertices"]], [[v[:2] for v in ring] for ring in row["holes"]]).area


def test_one_to_many_slab_is_one_construction_with_independent_side_finishes():
    source, (upper, left, right) = stacked()
    original = deepcopy(source)
    values = {"boundaries": {left: {"thickness": thickness(207.5), "material": materials("Paint", "涂料")},
                             upper: {"material": materials("Tile", "地砖")}}}
    result = build_surface_properties(source, {"horizontal": [120, 180]}, values)
    sides = result["boundaries"]
    ids = sides[upper]["component_ids"]
    assert len(ids) == 1 and ids == sides[left]["component_ids"] == sides[right]["component_ids"]
    assert quantities(result, "boundaries", upper) == [thickness(207.5)]
    assert "regions" not in sides[upper]
    assert sides[upper]["material"]["en"] == "Tile" and sides[left]["material"]["en"] == "Paint"
    assert sides[right]["material"]["source"] == "inference"
    assert source == original
    values["boundaries"][left]["thickness"] = thickness(211.5, "from user")
    changed = build_surface_properties(source, declarations=values)
    assert changed["boundaries"][upper]["component_ids"] == ids
    assert quantities(changed, "boundaries", right) == [thickness(211.5, "from user")]


def test_different_evidenced_ceiling_values_segment_floor_and_reject_conflicting_contact():
    source, (upper, left, right) = stacked()
    values = {"boundaries": {left: {"thickness": thickness(180)}, right: {"thickness": thickness(220)}}}
    result = build_surface_properties(source, declarations=values)
    whole = result["boundaries"][upper]
    assert len(whole["component_ids"]) == 2
    assert sorted(region_area(r) for r in whole["regions"]) == [16, 16]
    ids = {r["component_id"] for r in whole["regions"]}
    assert ids == {result["boundaries"][bid]["component_ids"][0] for bid in (left, right)}
    assert quantities(result, "boundaries", left)[0]["value_mm"] == 180
    assert quantities(result, "boundaries", right)[0]["value_mm"] == 220
    values["boundaries"][upper] = {"thickness": thickness(180)}
    with pytest.raises(ValueError, match="conflicting thicknesses"):
        build_surface_properties(source, declarations=values)
    estimates = {"boundaries": {left: {"thickness": thickness(150, "inference")},
                                right: {"thickness": thickness(200, "inference")}}}
    with pytest.raises(ValueError, match="conflicting thicknesses"):
        build_surface_properties(source, declarations=estimates)


def test_explicit_local_thickening_uses_contact_coordinates_and_leaves_source_elevations_unchanged():
    src, (top, left, right) = stacked()
    points = [[1, 0, 3], [2, 0, 3], [2, 4, 3], [1, 4, 3]]
    local = {"vertices": points, "thickness": thickness(252.75)}
    values = {"boundaries": {top: {"thickness_regions": [local]}}}
    result = build_surface_properties(src, {"horizontal": [166.5]}, values)
    for bid in (top, left):
        pieces = result["boundaries"][bid]["regions"]
        special = [p for p in pieces if result["components"][p["component_id"]]["thickness"]["value_mm"] == 252.75]
        assert sum(region_area(r) for r in special) == pytest.approx(4)
        assert all(p[2] == 3 for r in special for p in r["vertices"])
    assert [r["value_mm"] for r in quantities(result, "boundaries", right)] == [166.5]
    values["boundaries"][left] = {"thickness": thickness(210)}
    with pytest.raises(ValueError, match="conflicting thicknesses"):
        build_surface_properties(src, declarations=values)
    del values["boundaries"][left]
    local["thickness"] = thickness(200, "inference")
    with pytest.raises(ValueError, match="local thickness changes"):
        build_surface_properties(src, declarations=values)
    local["thickness"] = thickness(250)
    points[0][0] = -1
    with pytest.raises(ValueError, match="extends outside"):
        build_surface_properties(src, declarations=values)


def test_terrace_and_shared_floor_resolve_different_kinds_and_open_interfaces_have_no_solids():
    source = _source()
    lower, upper = _horizontal_pair(source)
    values = build_surface_properties(source, {"horizontal": [175], "roof": [260]})
    regions = values["boundaries"][lower["id"]]["regions"]
    assert sorted((values["components"][r["component_id"]]["kind"], region_area(r)) for r in regions) == [
        ("horizontal", 8), ("roof", 8)]
    assert quantities(values, "boundaries", upper["id"])[0]["value_mm"] == 175
    opened = apply_source_enclosure(source, _declaration(source, [_row(lower["id"]), _row(upper["id"])]))
    projected = build_surface_properties(opened)
    for bid in (lower["id"], upper["id"]):
        assert projected["boundaries"][bid] == {"component_ids": [], "material": None}
    with pytest.raises(ValueError, match="no physical area"):
        build_surface_properties(opened, declarations={"boundaries": {lower["id"]: {"thickness": thickness(500)}}})


def test_glass_total_decimal_candidates_door_sides_and_unfilled_opening():
    source = build_source_bim(three_rooms())
    window = next(o["id"] for o in source["openings"] if o["kind"] == "window")
    host = source["opening_hosts"]["door"][0]
    values = {"openings": {window: {"thickness": thickness(24)},
                           "door": {"materials_by_host": {host: materials("Oak", "橡木")}}}}
    result = build_surface_properties(source, {"glazing": [9.75]}, values)
    assert quantities(result, "openings", window) == [thickness(24)]
    assert result["openings"]["door"]["materials_by_host"] == {host: materials("Oak", "橡木")}
    assert quantities(result, "openings", "door")[0]["source"] == "inference"
    inferred = build_surface_properties(source, {"glazing": [9.75]})
    assert quantities(inferred, "openings", window) == [{"value_mm": 9.75, "source": "inference", "source_refs": []}]
    with pytest.raises(ValueError, match="use a glazing candidate"):
        build_surface_properties(source, {"glazing": [9.75]}, {"openings": {window: {"thickness": thickness(24, "inference")}}})
    source["openings"][next(i for i, o in enumerate(source["openings"]) if o["id"] == "door")]["kind"] = "open"
    assert build_surface_properties(source)["openings"]["door"] == {"component_ids": [], "material": None}


def test_archived_scope_is_preserved_and_explicit_values_precede_candidates():
    source, (whole, left, right) = stacked()
    first = thickness_record(source, left, .2075, basis="from user", source_refs=["brief"], host_scope="boundary")
    source["component_attributes"] = [first]
    history = legacy_surface_properties(source)
    assert history["boundaries"].keys() == {left}
    assert quantities(history, "boundaries", left) == [{"value_mm": 207.5, "source": "from user", "source_refs": ["brief"]}]
    assert history["boundaries"][left]["material"] is None
    assert quantities(build_surface_properties(source, {"horizontal": [150]}), "boundaries", right) == quantities(history, "boundaries", left)
    first["basis"] = "historic nominal size"
    assert quantities(legacy_surface_properties(source), "boundaries", left)[0]["source"] is None
    assert quantities(build_surface_properties(source, {"horizontal": [140.5]}), "boundaries", whole)[0]["value_mm"] == 140.5
    source.pop("component_attributes")
    assert legacy_surface_properties(source) == {"components": {}, "boundaries": {}, "openings": {}}


@pytest.mark.parametrize("scope,expected", [("complete wall", 250), ("lining only", 100)])
def test_standalone_wall_reading_respects_assembly_scope(scope, expected):
    from src.agent.execution.source_proposal import compile_source_proposal
    from tests.test_source_proposal import _proposal
    from tests.test_wall_reference import reference

    proposal = _proposal()
    bid = "space/hall/wall/3"
    ref = reference("west", bid, [-.125, .125])
    ref.update(thickness_m=.25, evidence_status="observed", reference_basis="dimensioned", thickness_scope=scope)
    proposal["wall_references"] = [ref]
    source, report = compile_source_proposal(proposal)
    assert report["source_geometry_ready"] and not source.get("component_attributes")
    assembly = quantities(build_surface_properties(source, {"wall": [100]}), "boundaries", bid)[0]
    assert assembly["value_mm"] == expected
    archived = legacy_surface_properties(source)
    if scope == "complete wall":
        assert assembly["source"] == "from material"
        assert quantities(archived, "boundaries", bid) == [assembly]
    else:
        assert bid not in archived["boundaries"]


@pytest.mark.parametrize("row", [
    {"thickness": {"value_mm": 5, "source": "inference", "source_refs": "bad"}},
    {"thickness": {"value_mm": .1, "source": "from material", "source_refs": []}},
    {"thickness": {"value_mm": float("nan"), "source": "from material", "source_refs": ["plan"]}},
    {"material": {"en": "Tile", "zh": "砖", "source": "observed", "source_refs": ["pic"]}},
])
def test_bad_property_declaration_is_rejected(row):
    source, (top, _, _) = stacked()
    with pytest.raises(ValueError):
        build_surface_properties(source, declarations={"boundaries": {top: row}})
