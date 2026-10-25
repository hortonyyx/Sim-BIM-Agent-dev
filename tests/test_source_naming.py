"""Public naming must preserve source IDs, hosts, geometry and CCW semantics."""
import copy
import hashlib
import json

import pytest

from src.agent.correction.schema import CorrectedGeometry
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.source_bim import build_source_bim, source_view_geometry
from src.agent.geometry.source_naming import build_public_names, viewer_names
from src.agent.roles import CATALOG, CATALOG_PATH, ROOM_TYPES, require_role


def proposal():
    return {"assumptions": [], "unresolved": [], "geometry": {
        "schema_version": "2", "footprint_x": [0, 8], "footprint_y": [0, 8],
        "floors": [{"name": "ground-original", "z_floor": 0, "ceiling_height": 3, "cells": [
            {"id": "west", "role": "meeting", "x": [0, 4], "y": [0, 8]},
            {"id": "east", "role": "office", "x": [4, 8], "y": [0, 8]}]}],
        "windows": [
            {"id": "low", "floor": "ground-original", "facade": "West", "room": "west", "span": [1, 2], "z": [1, 2]},
            {"id": "high", "floor": "ground-original", "facade": "West", "room": "west", "span": [6, 7], "z": [1, 2]}],
        "openings": [{"id": "shared", "kind": "door", "space_id": "west", "other_space_id": "east", "p1": [4, 3], "p2": [4, 4], "z": [0, 2], "source_refs": ["synthetic door"]}]}}


def source():
    return build_source_bim(CorrectedGeometry.model_validate(proposal()["geometry"]), capability_profile="orthogonal_polygon")


def test_catalog_is_exact_pinned_upstream_plus_explicit_unknown():
    raw = (CATALOG_PATH.parent / "openstudio/level_1_space_types.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CATALOG["source"]["sha256"]
    rows = json.loads(raw)
    assert len(rows) == 61
    assert set(ROOM_TYPES) == {r["space_type_name"] for r in rows} | {"unknown"}
    for row in rows:
        local = ROOM_TYPES[row["space_type_name"]]
        assert (local["upstream_id"], local["annotation"]) == (row["id"], row["annotation"])
        assert require_role(local["code"]) == local["code"]
        assert require_role(local["name_token"]) == local["code"]
        assert require_role(local["label_zh"]) == local["code"]
    assert ROOM_TYPES["unknown"]["upstream_id"] is None
    assert require_role("Meeting Room") == "conference/meeting/multipurpose"


def test_room_use_feedback_distinguishes_explained_unknown_and_missing_records():
    from src.agent.roles import room_use_review
    source = {"spaces": [
        {"id": "blank", "role": "unknown"},
        {"id": "old", "role": "office", "source_refs": ["legacy desk inference"]},
        {"id": "unclear", "role": "unknown", "role_evidence": {"basis": "unknown"}},
        {"id": "inferred", "role": "office", "role_evidence": {"basis": "inferred"}},
    ]}
    result = room_use_review(source)
    assert result["unrecorded_space_ids"] == ["blank", "old"]
    assert result["unknown_space_ids"] == ["blank", "unclear"]
    assert result["unknown_ids_truncated"] is False
    assert result["summary"] == dict(total_count=4, recorded_count=2, unrecorded_count=2,
                                     observed_count=0, inferred_count=1, unknown_count=1)
    assert result["delivery_blocked"] is False and result["drawing_fidelity"] == "not_evaluated"
    source["spaces"] = [{"id": str(i), "role": "unknown"} for i in range(100)]
    bounded = room_use_review(source)
    assert bounded["summary"]["unrecorded_count"] == 100
    assert len(bounded["unrecorded_space_ids"]) == 20 and bounded["unrecorded_ids_truncated"]
    assert bounded["unknown_space_ids"] == [str(i) for i in range(20)]
    assert bounded["unknown_ids_truncated"]


def test_room_wall_window_shared_door_names():
    s = source()
    n = s["public_names"]
    assert n["floors"] == {"ground-original": "F1"}
    assert n["source_floor_names"] == {"ground-original": "ground-original"}
    assert n["spaces"]["west"] == "Z01_F1_Conference-Meeting-Multipurpose_W"
    assert n["spaces"]["east"] == "Z02_F1_Office_E"
    walls = {n["boundaries"][b["id"]]: b for b in s["boundaries"] if b["space_id"] == "west"}
    for name, axis, value in [("Z01_W1", 1, 0), ("Z01_W2", 0, 4), ("Z01_W3", 1, 8), ("Z01_W4", 0, 0)]:
        assert all(v[axis] == value for v in walls[name]["vertices"])
    # Final CCW west wall runs north→south, opposite numeric Y ordering.
    assert n["openings"]["high"] == "Z01_W4_Win1"
    assert n["openings"]["low"] == "Z01_W4_Win2"
    assert set(n["opening_sides"]["shared"].values()) == {"Z01_W2_Door1", "Z02_W4_Door1"}
    assert len([o for o in s["openings"] if o["id"] == "shared"]) == 1
    assert {b["id"] for b in s["boundaries"]} == set(n["boundaries"])


def test_names_survive_record_order_ring_start_and_winding():
    s = source()
    original = copy.deepcopy(s)
    for key in ("floors", "spaces", "boundaries", "openings"):
        s[key].reverse()
    for space in s["spaces"]:
        space["polygon"] = list(reversed(space["polygon"][1:] + space["polygon"][:1]))
    for b in s["boundaries"]:
        b["vertices"].reverse()
    for hosts in s["opening_hosts"].values():
        hosts.reverse()
    assert build_public_names(s) == original["public_names"]


def horizontal_aperture_source():
    geometry = {"schema_version": "2", "footprint_x": [0, 3], "footprint_y": [0, 3],
                "floors": [], "windows": [], "openings": []}
    for i in range(2):
        geometry["floors"].append({"name": f"level-{i}", "z_floor": 3*i, "ceiling_height": 3,
            "cells": [{"id": f"stairs-{i}", "role": "stairwell", "x": [0, 3], "y": [0, 3]}]})
    s = build_source_bim(CorrectedGeometry.model_validate(geometry), capability_profile="orthogonal_polygon")
    ceiling = next(b["id"] for b in s["boundaries"] if b["space_id"] == "stairs-0" and b["geometry_type"] == "ceiling")
    floor = next(b["id"] for b in s["boundaries"] if b["space_id"] == "stairs-1" and b["geometry_type"] == "floor")
    for oid, x in [("north", 1.75), ("south", .25)]:
        s["openings"].append({"id": oid, "kind": "open", "host_boundary_id": ceiling,
            "space_ids": ["stairs-0", "stairs-1"], "exterior": False,
            "vertices": [[x, .5, 3], [x+.5, .5, 3], [x+.5, 1, 3], [x, 1, 3]],
            "connectivity": "open", "source_refs": ["synthetic horizontal aperture"]})
        s["opening_hosts"][oid] = [ceiling, floor]
        s["connections"].append({"opening_id": oid, "kind": "open", "space_ids": ["stairs-0", "stairs-1"], "exterior": False, "state": "open"})
    from src.agent.geometry.source_model import _digest
    s["source_model_sha256"] = _digest({k: v for k, v in s.items() if k != "source_model_sha256"})
    return s


def test_horizontal_apertures_have_stable_both_side_names_and_render_without_mutation():
    from scripts.tool_scripts.render_geometry_viewer import build_viewer_html

    s = horizontal_aperture_source()
    before = copy.deepcopy(s)
    names = build_public_names(s)
    assert s == before
    assert set(names["opening_sides"]["south"].values()) == {"Z01_Ceiling_Opening1", "Z02_Floor_Opening1"}
    assert set(names["opening_sides"]["north"].values()) == {"Z01_Ceiling_Opening2", "Z02_Floor_Opening2"}
    rendered = build_viewer_html(source_view_geometry(s), title="Horizontal aperture regression")
    assert "Z01_Ceiling_Opening1" in rendered and "Z02_Floor_Opening1" in rendered
    assert s == before
    s["openings"].reverse()
    for opening in s["openings"]:
        opening["vertices"].reverse()
        s["opening_hosts"][opening["id"]].reverse()
    assert build_public_names(s) == names


def test_unknown_opening_host_is_not_accepted_by_horizontal_name_fallback():
    s = horizontal_aperture_source()
    s["opening_hosts"]["south"] = ["missing-floor"]
    with pytest.raises(ValueError, match="unknown or unsupported.*missing-floor"):
        build_public_names(s)


def test_concave_ring_has_six_ccw_walls_from_southernmost_westernmost():
    p = proposal()["geometry"]
    ring = [[0, 0], [8, 0], [8, 4], [4, 4], [4, 8], [0, 8]]
    p["floors"][0]["cells"] = [{"id": "L", "role": "corridor", "x": [0, 8], "y": [0, 8], "polygon": ring}]
    from src.agent.correction.schema import FootprintRing
    geom = CorrectedGeometry.model_validate(p)
    geom.windows = []; geom.openings = []
    geom.floors[0].footprint = FootprintRing(vertices=ring)
    s = build_source_bim(geom, capability_profile="orthogonal_polygon")
    for b in s["boundaries"]:
        if b["geometry_type"] != "wall":
            continue
        index = int(s["public_names"]["boundaries"][b["id"]].split("_W")[1])-1
        assert {tuple(v[:2]) for v in b["vertices"]} == {tuple(ring[index]), tuple(ring[(index+1)%6])}


def test_reversed_floors_use_global_bbox_and_floor_identity():
    s = source()
    upper = {**s["floors"][0], "id": "upper", "name": "upper", "z_floor": 3,
             "footprint": [[0, 0], [4, 0], [4, 8], [0, 8]]}
    s["floors"].insert(0, upper)
    s["spaces"].append({**s["spaces"][0], "id": "upper-room", "floor_id": "upper",
                        "polygon": upper["footprint"], "z_floor": 3})
    n = build_public_names(s)
    assert n["floors"]["upper"] == "F2"
    assert n["spaces"]["upper-room"].endswith("_W")  # whole building, not its own center
    assert "_F2_" in n["spaces"]["upper-room"]


def test_floor_ordinals_preserve_declared_groups_separately_at_shared_heights():
    s = source()
    base = s["floors"][0]
    s["floors"] = [
        {**base, "id": "ANNEX", "name": "Annex", "z_floor": 0},
        {**base, "id": "F1", "name": "F1", "z_floor": 0},
        {**base, "id": "CORE", "name": "Core", "z_floor": 0},
        {**base, "id": "F2", "name": "F2", "z_floor": 3},
        {**base, "id": "F3", "name": "F3", "z_floor": 6},
        {**base, "id": "EMPTY", "name": "", "z_floor": 9},
    ]
    s["spaces"] = []
    s["boundaries"] = []
    s["openings"] = []
    s["opening_hosts"] = {}
    names = build_public_names(s)
    assert names["floors"] == {"ANNEX": "F1", "CORE": "F2", "F1": "F3",
                               "F2": "F4", "F3": "F5", "EMPTY": "F6"}
    assert names["source_floor_names"] == {f["id"]: f.get("name") or f["id"] for f in s["floors"]}


def test_export_rejects_invented_roles_preserves_input_and_uses_unknown(tmp_path):
    p = proposal()
    p["geometry"]["floors"][0]["cells"][0]["role"] = "office_inferred"
    before = copy.deepcopy(p)
    report = export_source_proposal(p, tmp_path / "invalid")
    assert report["status"] == "error" and "room_types" in report["error"]
    assert p == before
    del p["geometry"]["floors"][0]["cells"][0]["role"]
    report = export_source_proposal(p, tmp_path / "unknown")
    assert report["source_geometry_ready"]
    s = json.loads((tmp_path / "unknown/source_model.json").read_text())
    assert next(x for x in s["spaces"] if x["id"] == "west")["role"] == "unknown"
    assert "_Unknown_" in s["public_names"]["spaces"]["west"]
    assert json.loads((tmp_path / "unknown/proposal.json").read_text()) == p


def test_view_names_cover_objects_fragments_regions_and_edges_without_mutation():
    d = source_view_geometry(source())
    d["source_model"]["public_names"]["floors"]["ground-original"] = "stale-F99"
    b = d["surfaces"][0]
    d["enclosure_regions"] = [{"boundary_id": b["name"], "space_id": b["zone"], "condition": "unknown", "verts": b["verts"]}]
    before = copy.deepcopy(d)
    n = viewer_names(d, d["display_surface_parts"])
    assert d == before
    assert n["floors"] == [{"id": "ground-original", "name": "F1", "source_name": "ground-original", "z_floor": 0.0}]
    objects = d["surfaces"] + d["windows"] + d["openings"]
    assert set(n["objects"]) == {x["name"] for x in objects}
    assert len(set(n["objects"].values())) == len(objects)
    assert n["regions"][0].endswith("_Unknown1")
    for obj in objects:
        assert len(n["edges"][obj["name"]]) == len(obj["verts"])
        assert all(name.startswith(n["objects"][obj["name"]]+"_Edge") for name in n["edges"][obj["name"]])
    assert all(len(n["parts"][bid]) == len(rows) for bid, rows in d["display_surface_parts"].items())


def test_repeated_directions_follow_existing_order_per_floor_and_use_and_work_in_ep():
    from src.agent.execution.ep_branch import derive_ep_geometry

    geometry = {"schema_version": "2", "footprint_x": [0, 16], "footprint_y": [0, 8],
                "floors": [], "windows": []}
    for fi in (1, 2):
        cells = [{"id": f"{fi}-south-{i}", "role": "office/enclosed", "x": [4*i, 4*i+4], "y": [0, 4]}
                 for i in range(4)]
        cells += [{"id": f"{fi}-north-west", "role": "copy/print", "x": [0, 8], "y": [4, 8]},
                  {"id": f"{fi}-north-east", "role": "meeting", "x": [8, 16], "y": [4, 8]}]
        geometry["floors"].append({"name": f"PLAN.F{fi}", "z_floor": 3*(fi-1), "ceiling_height": 3,
                                   "cells": list(reversed(cells))})
    s = build_source_bim(CorrectedGeometry.model_validate(geometry), capability_profile="orthogonal_polygon")
    before = copy.deepcopy(s)
    names = s["public_names"]
    assert names["scheme_version"] == "bim_names_v3"
    for fi in (1, 2):
        assert [names["spaces"][f"{fi}-south-{i}"].split("_")[2:] for i in range(4)] == [
            ["Office-Enclosed", direction] for direction in ("SW1", "SW2", "SE1", "SE2")]
        assert names["spaces"][f"{fi}-north-west"].endswith("_Copy-Print_NW")
        assert names["spaces"][f"{fi}-north-east"].endswith("_Conference-Meeting-Multipurpose_NE")
    for key in ("floors", "spaces", "boundaries"):
        s[key].reverse()
    assert build_public_names(s) == names
    derive_ep_geometry(before, names["spaces"])
    assert s["spaces"] == list(reversed(before["spaces"]))


def test_old_source_plan_uses_current_public_labels_and_filename_without_mutation(tmp_path, monkeypatch):
    from PIL import ImageDraw
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from src.agent.geometry.source_model import _digest

    s = source()
    s["public_names"]["scheme_version"] = "bim_names_v2"
    s["public_names"]["spaces"]["west"] = "Z01_F1_Conference_Meeting_Multipurpose_W"
    s["source_model_sha256"] = _digest({k: v for k, v in s.items() if k != "source_model_sha256"})
    candidate = tmp_path / "candidate_01"
    candidate.mkdir()
    (tmp_path / "inputs.json").write_text('{"images": {}}', encoding="utf-8")
    path = candidate / "source_model.json"
    path.write_text(json.dumps(s), encoding="utf-8")
    before = path.read_bytes()
    drawn = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    _, metadata = Toolkit(tmp_path).plan_view("candidate_01", "ground-original")
    assert metadata["floor_id"] == "ground-original" and metadata["floor_name"] == "F1"
    assert metadata["plan_image"] == "candidate_01/plan_F1.png"
    assert metadata["space_ids"] == [row["id"] for row in s["spaces"]]
    assert "SOURCE BIM / F1 / not drawing evidence" in drawn
    assert "Z01_F1_Conference-Meeting-Multipurpose_W" in "".join(drawn)
    assert "ground-original" not in drawn and "west" not in drawn
    assert path.read_bytes() == before
    assert (candidate / "plan_F1.png").is_file()
    names = viewer_names(source_view_geometry(s), {})
    assert names["spaces"] == metadata["space_names"]


def test_public_prose_references_do_not_rewrite_paths_or_partial_ids():
    from src.agent.geometry.source_naming import public_reference_text

    references = {"seed-office-south": "Z08_F1_Office_SE", "PLAN.F1": "F1"}
    text = 'Room seed-office-south. PLAN.F1: check seed-office-south-2 and images/PLAN.F1.png.'
    assert public_reference_text(text, references) == (
        'Room Z08_F1_Office_SE. F1: check seed-office-south-2 and images/PLAN.F1.png.')


def test_delivery_and_viewer_prose_use_public_names_but_keep_saved_evidence(tmp_path):
    from scripts.tool_scripts.bim_agent_delivery_display import render_delivery_html
    from scripts.tool_scripts.run_bim_agent import Toolkit

    p = proposal()
    p["assumptions"] = ["Room west on ground-original; window high remains <unconfirmed>."]
    p["unresolved"] = ["Check shared between west and east. Image images/west.png remains original."]
    (tmp_path / "inputs.json").write_text('{"images": {}}', encoding="utf-8")
    toolkit = Toolkit(tmp_path)
    built = toolkit.build(p)
    candidate = tmp_path / built["candidate"]
    source_bytes = (candidate / "source_model.json").read_bytes()
    s = json.loads(source_bytes)
    names = s["public_names"]
    result = toolkit.delivery(built["candidate"], selection_origin="agent_selected")
    before = copy.deepcopy(result)
    page = render_delivery_html(result, s)
    viewer = (candidate / "viewer.html").read_text(encoding="utf-8")
    # Only inspect the prose banner; embedded source JSON intentionally retains IDs.
    banner = viewer[viewer.index("<aside "):]
    for rendered in (page, banner):
        assert f'Check {names["openings"]["shared"]} between {names["spaces"]["west"]} and {names["spaces"]["east"]}.' in rendered
        assert f'Room {names["spaces"]["west"]} on F1; window {names["openings"]["high"]}' in rendered
        assert "&lt;unconfirmed&gt;" in rendered and "images/west.png" in rendered
    assert result == before
    assert result["generation"]["unresolved"] == p["unresolved"]
    assert (candidate / "source_model.json").read_bytes() == source_bytes
