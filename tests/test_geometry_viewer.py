"""Tests for the offline interactive source BIM viewer.

The rendered result needs a browser to confirm visually (headless container); these
tests assert the generated HTML is self-contained + offline + carries the geometry
and all controls, and that the app script parses (node --check, when node exists)."""

from __future__ import annotations

import json
import copy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("scripts/tool_scripts").resolve()))
import render_geometry_viewer as rgv  # noqa: E402

_GEO = {
    "zones": ["Z1", "Z2"],
    "surfaces": [
        {"name": "w1", "zone": "Z1", "type": "Wall", "obc": "Outdoors",
         "verts": [[0, 0, 0], [4, 0, 0], [4, 0, 3], [0, 0, 3]]},
        {"name": "w2", "zone": "Z2", "type": "Wall", "obc": "Surface",
         "verts": [[0, 0, 3], [4, 0, 3], [4, 0, 6], [0, 0, 6]]},
    ],
    "windows": [
        {"name": "win1", "parent": "w1", "verts": [[1, 0, 1], [3, 0, 1], [3, 0, 2], [1, 0, 2]]},
    ],
}

_GEO["source_model"] = {
    "schema_version": "source_bim_v2",
    "floors": [{"id": f"F{i}", "name": f"F{i}", "z_floor": z, "height": 3,
                "footprint": [[0, 0], [4, 0], [4, 4], [0, 4]]} for i, z in [(1, 0), (2, 3)]],
    "spaces": [{"id": f"Z{i}", "floor_id": f"F{i}", "role": "unknown", "z_floor": z, "height": 3,
                "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]]} for i, z in [(1, 0), (2, 3)]],
    "boundaries": [{"id": row["name"], "space_id": row["zone"], "kind": "physical",
                    "geometry_type": "wall", "vertices": row["verts"]} for row in _GEO["surfaces"]],
    "openings": [{"id": "win1", "kind": "window", "space_ids": ["Z1"],
                  "vertices": _GEO["windows"][0]["verts"]}],
    "opening_hosts": {"win1": ["w1"]}, "connections": [],
}



def test_viewer_is_offline_and_self_contained():
    html = rgv.build_viewer_html(_GEO, title="t")
    assert "unpkg.com" not in html and "cdn.jsdelivr" not in html  # offline
    assert "Three.js Authors" in html                              # three inlined
    assert "OrbitControls" in html                                 # orbit inlined
    assert "window.GEO = " in html                                 # geometry embedded


def test_viewer_embeds_geometry_verbatim():
    html = rgv.build_viewer_html(_GEO, title="t")
    start = html.index("window.GEO = ") + len("window.GEO = ")
    end = html.index(";</script>", start)
    geo = json.loads(html[start:end])
    assert len(geo["surfaces"]) == 2 and len(geo["windows"]) == 1 and geo["zones"] == ["Z1", "Z2"]
    # OBC metadata preserved (needed for colour-by-OBC)
    assert {s["obc"] for s in geo["surfaces"]} == {"Outdoors", "Surface"}


def test_viewer_has_all_controls():
    html = rgv.build_viewer_html(_GEO, title="t")
    for ctrl in ('id="opacity"', 'id="explode"', 'id="explodeMode"', 'id="measure"', 'id="savePng"',
                 'id="colorBy"', 'id="floorSel"', 'id="showWin"', 'id="showEdges"',
                 'id="showLogical"', 'id="showEnclosure"', 'id="hud"', 'id="meas"',
                 'section cuts', 'select by'):
        assert ctrl in html, f"missing control: {ctrl}"


def test_enclosure_projection_uses_one_viewer_only_aid_for_open_and_unknown():
    """Open and unknown use the same translucent aid without duplicate faces."""
    data = copy.deepcopy(_GEO)
    data["display_surface_parts"] = {
        "w1": [
            {"verts": [[0, 0, 0], [2, 0, 0], [2, 0, 3], [0, 0, 3]],
             "holes": [], "duplicate_at_rest": False, "enclosure_condition": "physical"},
            {"verts": [[3, 0, 0], [4, 0, 0], [4, 0, 3], [3, 0, 3]],
             "holes": [], "duplicate_at_rest": False, "enclosure_condition": "unknown"},
        ],
        "w2": [{"verts": data["surfaces"][1]["verts"], "holes": [],
                "duplicate_at_rest": False, "enclosure_condition": "physical"}],
    }
    data["enclosure_regions"] = [
        {"boundary_id": "w1", "space_id": "Z1", "condition": "open",
         "verts": [[2, 0, 0], [3, 0, 0], [3, 0, 3], [2, 0, 3]],
         "source_refs": ["test:opening"], "assumptions": []},
        {"boundary_id": "w1", "space_id": "Z1", "condition": "unknown",
         "verts": [[3, 0, 0], [4, 0, 0], [4, 0, 3], [3, 0, 3]],
         "source_refs": ["test:uncertain"], "assumptions": ["extent inferred"]},
    ]
    html = rgv.build_viewer_html(data, title="semi-open")
    start = html.index("window.GEO = ") + len("window.GEO = ")
    embedded = json.loads(html[start:html.index(";</script>", start)])
    assert embedded["enclosure_regions"] == data["enclosure_regions"]
    assert embedded["visible_wall_parts"]["w1"][1]["enclosure_condition"] == "unknown"
    assert "LineDashedMaterial" in html and "ENCLOSURE_COLOR" in html and "ENCLOSURE_OPACITY" in html
    assert "allPickables().filter" in html and "raycaster.params.Line.threshold" in html
    assert "源边界 ID" in html and "明确开敞区域" in html and "围护未知区域" in html
    # Both conditions create the same viewer-only helper mesh. Unknown projected
    # wall parts are skipped, so that helper does not overlap a second face.
    region_block = html[html.index("ENC_REGIONS.forEach"):html.index("WINS.forEach")]
    assert "new THREE.Mesh(" in region_block and "enclosureMeshes.push(mesh)" in region_block
    assert "if(enclosureCondition==='unknown') return" in html
    assert "r.condition==='unknown'?" not in region_block
    assert "open-key" not in html and "unknown-key" not in html
    assert "逻辑闭合只界定空间范围，不表示实体密闭或热区" in html
    assert "边界覆盖" in html and "来源" in html and "假设" in html


def _minimal_source_v2():
    from src.agent.geometry.source_model import _digest

    source = {
        "schema_version": "source_bim_v2",
        "spaces": [{"id": "room", "floor_id": "F1", "role": "corridor",
                    "source_refs": ["test:room"]}],
        "boundaries": [{"id": "room/wall/0", "space_id": "room", "kind": "physical",
                        "geometry_type": "wall",
                        "vertices": [[0, 0, 0], [4, 0, 0], [4, 0, 3], [0, 0, 3]],
                        "adjacent_space_ids": [], "counterpart_ids": [],
                        "source_refs": ["test:wall"]}],
        "boundary_relations": [], "openings": [], "opening_hosts": {},
        "connections": [], "validation": {"status": "pass"},
    }
    source["source_model_sha256"] = _digest(source)
    return source


def test_loader_opens_validated_source_bim_directly(tmp_path):
    path = tmp_path / "source_model.json"
    path.write_text(json.dumps(_minimal_source_v2()), encoding="utf-8")
    display = rgv.load_viewer_geometry(path)
    assert display["zones"] == ["room"]
    assert display["surfaces"][0]["name"] == "room/wall/0"
    assert display["source_model"]["schema_version"] == "source_bim_v2"

    damaged = _minimal_source_v2()
    damaged["spaces"][0]["role"] = "office"
    path.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        rgv.load_viewer_geometry(path)


def test_loader_projects_v3_open_boundary_without_a_wall_part(tmp_path):
    from src.agent.geometry.source_enclosure import apply_source_enclosure

    source = _minimal_source_v2()
    evidence = {"source_refs": ["test:explicit-open"], "assumptions": [],
                "evidence_kind": "example"}
    v3 = apply_source_enclosure(source, {
        "schema_version": "source_enclosure_input_v1",
        "base_source_model_sha256": source["source_model_sha256"],
        "spaces": [{"space_id": "room", "enclosure": "semi_open", **evidence}],
        "boundaries": [{"boundary_id": "room/wall/0", "condition": "open",
                        "scope": "whole", **evidence}],
    })
    path = tmp_path / "source_model.json"
    path.write_text(json.dumps(v3), encoding="utf-8")
    display = rgv.load_viewer_geometry(path)
    assert display["source_model"]["schema_version"] == "source_bim_v3"
    assert display["display_surface_parts"]["room/wall/0"] == []
    assert display["enclosure_regions"] == [{
        "boundary_id": "room/wall/0", "space_id": "room", "condition": "open",
        "verts": v3["boundaries"][0]["vertices"], "source_refs": ["test:explicit-open"],
        "assumptions": [], "evidence_kind": "example",
    }]


def test_loader_rejects_unknown_source_schema_instead_of_blank_legacy_view(tmp_path):
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"schema_version": "source_bim_v99"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported source BIM schema"):
        rgv.load_viewer_geometry(path)


def test_geometry_cannot_break_out_of_script():
    """High (review 2026-06-19): a string in the geometry containing </script>
    must not close the inline <script> — it must be escaped to \\u003c."""
    evil = {"zones": ["</script><img src=x onerror=alert(1)>"],
            "surfaces": [{"name": "n", "zone": "z", "type": "Wall", "obc": "Outdoors",
                          "verts": [[0, 0, 0], [1, 0, 0], [1, 0, 1]]}],
            "windows": []}
    evil["source_model"] = copy.deepcopy(_GEO["source_model"])
    html = rgv.build_viewer_html(evil, title="t")
    # exactly the 4 legitimate <script> blocks, none injected by the data
    assert html.count("<script>") == 4 and html.count("</script>") == 4
    assert "\\u003c/script\\u003e" in html  # the evil close-tag was escaped


def test_title_is_html_escaped():
    html = rgv.build_viewer_html(_GEO, title="<b>&x")
    assert "&lt;b&gt;&amp;x" in html                       # title escaped
    assert "Geometry inspection — <b>&x" not in html       # raw title not injected


def test_viewer_colours_zones_by_room_type():
    """zone mode colours each zone by its room type from a fixed palette + shows a
    swatch→type legend (user 2026-06-20)."""
    html = rgv.build_viewer_html(_GEO, title="t", roles={"Z1": "office", "Z2": "corridor"})
    start = html.index("window.GEO = ") + len("window.GEO = ")
    geo = json.loads(html[start:html.index(";</script>", start)])
    assert geo["roles"] == {"Z1": "office", "Z2": "corridor"}   # role map embedded
    assert "ROLE_COLORS" in html and "roleColor" in html         # fixed colour table + lookup
    assert "function updateLegend" in html and 'id="legend"' in html  # legend panel (swatch→type)
    assert 'MeshBasicMaterial' in html  # flat fill → whole zone one uniform colour (no lighting wash)


def test_viewer_without_roles_falls_back_to_white_zone_fill():
    html = rgv.build_viewer_html(_GEO, title="t")  # no roles
    start = html.index("window.GEO = ") + len("window.GEO = ")
    geo = json.loads(html[start:html.index(";</script>", start)])
    assert geo["roles"] == {}  # empty → JS HAS_ROLES false → white zone fill


def test_source_spanning_spaces_are_filter_members_without_slicing_geometry():
    data = copy.deepcopy(_GEO)
    data["zones"] = ["room-1", "room-2", "core"]
    data["surfaces"] = [
        {"name": "r1", "zone": "room-1", "type": "Wall",
         "verts": [[0, 0, 0], [1, 0, 0], [1, 0, 3], [0, 0, 3]]},
        {"name": "r2", "zone": "room-2", "type": "Wall",
         "verts": [[0, 0, 3], [1, 0, 3], [1, 0, 6], [0, 0, 6]]},
        {"name": "core-wall", "zone": "core", "type": "Wall",
         "verts": [[1, 0, 0], [2, 0, 0], [2, 0, 6], [1, 0, 6]]},
    ]
    data["windows"] = []
    data["source_model"] = {
        "floors": [
            {"id": "F1", "name": "F1", "z_floor": 0, "spanning_space_ids": ["core"]},
            {"id": "CORE", "name": "Core", "z_floor": 0},
            {"id": "F2", "name": "F2", "z_floor": 3, "spanning_space_ids": ["core"]},
        ],
        "spaces": [
            {"id": "room-1", "floor_id": "F1"},
            {"id": "room-2", "floor_id": "F2"},
            {"id": "core", "floor_id": "CORE"},
        ],
        "boundaries": [], "derived": {},
        "public_names": {"floors": {"F1": "F1", "CORE": "Core", "F2": "F2"}},
        "validation": {"status": "pass"}, "openings": [], "connections": [],
    }
    for floor in data["source_model"]["floors"]:
        floor["footprint"] = [[0, 0], [2, 0], [2, 1], [0, 1]]
    for space in data["source_model"]["spaces"]:
        space["polygon"] = ([[1, 0], [2, 0], [2, 1], [1, 1]] if space["id"] == "core"
                            else [[0, 0], [1, 0], [1, 1], [0, 1]])
    data["source_model"]["opening_hosts"] = {}
    data["source_model"].pop("public_names")
    html = rgv.build_viewer_html(data, title="spanning")
    start = html.index("window.GEO = ") + len("window.GEO = ")
    embedded = json.loads(html[start:html.index(";</script>", start)])
    floor_index = {row["id"]: i for i, row in enumerate(embedded["public_names"]["floors"])}
    assert embedded["floor_memberships"]["room-1"] == [floor_index["F1"]]
    assert embedded["floor_memberships"]["room-2"] == [floor_index["F2"]]
    assert embedded["floor_memberships"]["core"] == [
        floor_index["CORE"], floor_index["F1"], floor_index["F2"]]
    assert "const zoneFloor = {}, zoneFloors = {};" in html
    assert "const declared=(GEO.floor_memberships||{})[z]||[]" in html
    assert "const okF=(u)=>onFloor(u,f)" in html
    # Primary floor remains single-valued for colouring/explode. Filtering uses
    # the declared local + spanning index set and never creates sliced geometry.
    assert "zoneFloors[z]=declared.length ? declared : [zoneFloor[z]]" in html


def test_viewer_uses_declared_source_floors():
    html = rgv.build_viewer_html(_GEO, title="source floors")
    start = html.index("window.GEO = ") + len("window.GEO = ")
    geo = json.loads(html[start:html.index(";</script>", start)])
    assert geo["source_model"] == _GEO["source_model"]
    assert [row["id"] for row in geo["public_names"]["floors"]] == ["F1", "F2"]
    assert geo["floor_memberships"] == {"Z1": [0], "Z2": [1]}


def test_discover_roles_reads_only_display_geometry_metadata(tmp_path):
    path = tmp_path / "display_geometry.json"
    path.write_text(json.dumps({"zone_meta": [
        {"name": "Z1", "role": "office"}, {"name": "Z2", "role": "meeting"},
    ]}), encoding="utf-8")
    (tmp_path / "unrelated.json").write_text(json.dumps({"zone_meta": [
        {"name": "Z1", "role": "wrong"}]}), encoding="utf-8")
    assert rgv.discover_roles(path) == {"Z1": "office", "Z2": "meeting"}
    path.write_text(json.dumps({"zones": ["Z1", "Z2"]}), encoding="utf-8")
    assert rgv.discover_roles(path) == {}


def test_app_js_parses_with_node(tmp_path):
    """If node is available, the app script must parse (catches JS syntax errors I
    cannot catch by running the browser headless)."""
    node = shutil.which("node")
    if not node:
        return  # node not present in this environment — skip
    js = tmp_path / "app_check.js"
    js.write_text(rgv.app_js(), encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, f"node --check failed:\n{r.stderr}"
