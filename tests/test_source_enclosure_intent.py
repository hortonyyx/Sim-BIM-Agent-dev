"""Authored enclosure intent resolves only on the actual compiled geometry."""
import copy
import json
import math

import pytest

from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_enclosure import apply_source_enclosure
from src.agent.geometry.source_enclosure_intent import apply_source_enclosure_intent
from src.agent.geometry.source_model import _digest
from src.agent.execution.source_proposal import export_source_proposal
from tests.test_source_bim import three_rooms


def evidence():
    return {"source_refs": ["synthetic supplied roof view"],
            "assumptions": ["Uncovered maintenance deck is an explicit hypothesis"],
            "evidence_kind": "inferred"}


def intent(boundaries=None, spaces=None):
    return {"schema_version": "source_enclosure_intent_v1", "spaces": spaces or [],
            "boundaries": boundaries or []}


def boundary(selector, **extra):
    return {"boundary": selector, "condition": "open", "scope": "whole", **evidence(), **extra}


def source():
    return build_source_bim(three_rooms(), capability_profile="orthogonal_polygon")


@pytest.mark.parametrize("yaw,origin,scale", [(0, [0, 0], 1), (31, [15, -8], 0.8), (-47, [-20, 7], 1.3)])
def test_parametric_roof_intent_compiles_holes_real_walls_and_door_without_guessing_digest(tmp_path, yaw, origin, scale):
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    def point(p):
        return [origin[0] + scale * (c*p[0] - s*p[1]), origin[1] + scale * (s*p[0] + c*p[1])]
    outer = list(map(point, [[0, 0], [12, 0], [12, 9], [0, 9]]))
    inner = list(map(point, [[3, 2], [7, 2], [7, 5], [3, 5]]))
    plan = {"schema_version": "source_polygon_v1",
            "templates": {"roof": {"footprint": outer, "spaces": [
                {"id": "deck", "role": "unknown", "polygon": outer, "holes": [inner], "source_refs": ["test"]},
                {"id": "plant", "role": "unknown", "polygon": inner, "source_refs": ["test"]}],
                "doors": [{"id": "plant_access", "space": "plant", "other_space": "deck",
                           "p1": point([4, 2]), "p2": point([5, 2]), "z": [0, 2.1], "source_refs": ["test"]}]}},
            "instances": [{"id": "R", "template": "roof", "z": 6, "height": 3}],
            "assumptions": ["Synthetic open deck"], "unresolved": [],
            "enclosure_declaration": intent(
                [boundary({"space_id": "R:deck", "geometry_type": "ceiling"})],
                [{"space_id": "R:deck", "enclosure": "semi_open", **evidence()}])}
    original = copy.deepcopy(plan)
    proposal = expand_parametric_proposal(plan)
    assert proposal["enclosure_declaration"] == plan["enclosure_declaration"]
    report = export_source_proposal(proposal, tmp_path / "open_roof")
    assert report["source_geometry_ready"], report
    result = json.loads((tmp_path / "open_roof/source_model.json").read_text(encoding="utf-8"))
    assert plan == original
    assert result["schema_version"] == "source_bim_v3"
    top = next(row for row in result["boundaries"] if row["space_id"] == "R:deck" and row["geometry_type"] == "ceiling")
    assert top["kind"] == "virtual" and len(top["holes"]) == 1
    assert top["enclosure_regions"][0]["evidence_kind"] == "inferred"
    assert top["enclosure_regions"][0]["assumptions"] == evidence()["assumptions"]
    assert source_view_geometry(result)["display_surface_parts"][top["id"]] == []
    walls = [row for row in result["boundaries"] if row["space_id"] == "R:deck" and row["geometry_type"] == "wall"]
    assert len(walls) == 8 and all(row["kind"] == "physical" for row in walls)
    assert len(result["openings"]) == 1
    assert set(result["openings"][0]["space_ids"]) == {"R:deck", "R:plant"}
    compiled = result["source_enclosure"]
    assert compiled["declaration"]["base_source_model_sha256"] == compiled["base_source_model_sha256"]
    assert compiled["intent_binding"]["intent_sha256"] == _digest(plan["enclosure_declaration"])
    assert compiled["intent_binding"]["boundary_resolutions"][0]["boundary_id"] == top["id"]


@pytest.mark.parametrize("selector", [{"space_id": "missing", "geometry_type": "ceiling"},
                                      {"space_id": "a", "geometry_type": "wall"},
                                      {"boundary_id": "missing"}])
def test_missing_and_ambiguous_selectors_reject_without_source_mutation(selector):
    base = source()
    before = copy.deepcopy(base)
    with pytest.raises(ValueError, match="resolved to"):
        apply_source_enclosure_intent(base, intent([boundary(selector)]))
    assert base == before


def test_intent_cannot_smuggle_a_frozen_source_digest_and_old_declaration_stays_strict():
    base = source()
    proposed = intent([boundary({"space_id": "a", "geometry_type": "ceiling"})])
    proposed["base_source_model_sha256"] = base["source_model_sha256"]
    with pytest.raises(ValueError, match="unknown.*base_source_model_sha256"):
        apply_source_enclosure_intent(base, proposed)
    with pytest.raises(ValueError, match="does not match"):
        apply_source_enclosure(base, {"schema_version": "source_enclosure_input_v1",
                                     "base_source_model_sha256": "0" * 64,
                                     "boundaries": [], "spaces": []})


def test_normal_tool_build_binds_after_adoption_and_explicit_patch_retains_intent(tmp_path, monkeypatch):
    from scripts.tool_scripts.bim_agent_parametric import build_parametric, patch_parametric
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from tests.test_bim_agent_mesh import _prepare
    from tests.test_mesh_architectural_policy import measured_plan
    _, run, _ = _prepare(tmp_path, monkeypatch)
    toolkit = Toolkit(run)
    plan = measured_plan()
    plan["enclosure_declaration"] = intent([boundary({"space_id": "F3:hall", "geometry_type": "ceiling"})])
    built = build_parametric(toolkit, json.dumps(plan))
    assert built["source_geometry_ready"], built
    first_path = run / built["candidate"] / "source_model.json"
    first_bytes = first_path.read_bytes()
    first = json.loads(first_bytes)
    top = next(row for row in first["boundaries"] if row["space_id"] == "F3:hall" and row["geometry_type"] == "ceiling")
    assert all(point[2] == 9 for point in top["vertices"])
    assert plan["repeats"][0]["height"] == 3.02
    assert top["kind"] == "virtual"
    result = patch_parametric(toolkit, json.dumps([
        {"op": "replace", "path": "/enclosure_declaration/boundaries/0/condition", "value": "unknown",
         "reason": "Explicitly retain uncertainty after checking the supplied view", "source_refs": ["test view"]}
    ]), candidate=built["candidate"], expected_sha=built["parametric"]["plan_sha256"])
    assert result["source_geometry_ready"], result
    revised = json.loads((run / result["candidate"] / "source_model.json").read_bytes())
    revised_top = next(row for row in revised["boundaries"] if row["id"] == top["id"])
    assert revised_top["kind"] == "unknown"
    assert revised_top["enclosure_regions"][0]["evidence_kind"] == "inferred"
    assert first_path.read_bytes() == first_bytes


def test_reciprocal_shared_openings_still_require_both_explicit_sides():
    base = source()
    relation = next(row for row in base["boundary_relations"] if set(row["space_ids"]) == {"a", "b"})
    first, second = relation["boundary_ids"]
    proposed = intent([boundary({"boundary_id": first})])
    with pytest.raises(ValueError, match="matching open declaration"):
        apply_source_enclosure_intent(base, proposed)
    proposed["boundaries"].append(boundary({"boundary_id": second}))
    result = apply_source_enclosure_intent(base, proposed)
    assert {row["boundary_id"] for row in result["source_enclosure"]["open_connections"]} == {first, second}


def test_opening_conflict_is_not_removed_by_authoring_intent():
    base = source()
    host = next(row["host_boundary_id"] for row in base["openings"] if row["kind"] == "window")
    with pytest.raises(ValueError, match="overlaps an existing"):
        apply_source_enclosure_intent(base, intent([boundary({"boundary_id": host})]))


@pytest.mark.parametrize("missing", ["source_refs", "assumptions"])
def test_inferred_requires_both_explicit_basis_and_assumptions(missing):
    row = boundary({"space_id": "a", "geometry_type": "ceiling"})
    row.pop(missing)
    with pytest.raises(ValueError, match="inferred enclosure requires both"):
        apply_source_enclosure_intent(source(), intent([row]))


def test_unknown_partial_region_preserves_uncertainty_and_rejects_wrong_plane():
    base = source()
    top = next(row for row in base["boundaries"] if row["space_id"] == "a" and row["geometry_type"] == "ceiling")
    row = boundary({"boundary_id": top["id"]}, condition="unknown", scope="partial",
                   vertices=[[3, 0, 3], [4, 0, 3], [4, 2, 3], [3, 2, 3]])
    result = apply_source_enclosure_intent(base, intent([row]))
    assert not result["source_enclosure"]["open_connections"]
    assert result["validation"]["status"] == "warning"
    changed = next(b for b in result["boundaries"] if b["id"] == top["id"])
    assert changed["kind"] == "physical" and changed["enclosure"] == "mixed"
    assert {part["enclosure_condition"] for part in source_view_geometry(result)["display_surface_parts"][top["id"]]} == {"physical", "unknown"}
    row["vertices"][0][2] += 0.01
    with pytest.raises(ValueError, match="planar polygon"):
        apply_source_enclosure_intent(base, intent([row]))


@pytest.mark.parametrize("geometry_type", ["wall", "ceiling"])
def test_materialized_v2_virtual_boundaries_have_no_opaque_fill_but_keep_logical_and_opening_identity(geometry_type):
    materialized = source()
    target = (next(b for b in materialized["boundaries"] if b["id"] == materialized["openings"][0]["host_boundary_id"])
              if geometry_type == "wall" else
              next(b for b in materialized["boundaries"] if b["geometry_type"] == "ceiling"))
    target["kind"] = "virtual"
    materialized["source_model_sha256"] = _digest({k: v for k, v in materialized.items() if k != "source_model_sha256"})
    before = copy.deepcopy(materialized)
    display = source_view_geometry(materialized)
    assert display["display_surface_parts"][target["id"]] == []
    assert any(row["name"] == target["id"] for row in display["surfaces"])
    assert display["source_model"]["openings"] == materialized["openings"]
    assert display["windows"] and display["openings"]
    assert materialized == before
