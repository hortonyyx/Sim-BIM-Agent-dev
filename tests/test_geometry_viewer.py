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
                 'id="locale"', 'id="sections"'):
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
    public_regions = embedded["enclosure_regions"]
    assert [r["public_name"] for r in public_regions] == ["Z01_W1_Open1", "Z01_W1_Unknown1"]
    for region, original in zip(public_regions, data["enclosure_regions"]):
        assert region["verts"] == original["verts"] and region["parent_name"] == "Z01_W1"
        assert "source_refs" not in region and "assumptions" not in region
    assert embedded["visible_wall_parts"]["w1"][1]["enclosure_condition"] == "unknown"
    assert "LineDashedMaterial" in html and "ENCLOSURE_COLOR" in html and "ENCLOSURE_OPACITY" in html
    assert "allPickables().filter" in html and "raycaster.params.Line.threshold" in html
    assert "Open boundary" in html and "Unknown enclosure" in html
    # Both conditions create the same viewer-only helper mesh. Unknown projected
    # wall parts are skipped, so that helper does not overlap a second face.
    region_block = html[html.index("ENC_REGIONS.forEach"):html.index("WINS.forEach")]
    assert "new THREE.Mesh(" in region_block and "enclosureMeshes.push(mesh)" in region_block
    assert "if(enclosureCondition==='unknown') return" in html
    assert "r.condition==='unknown'?" not in region_block
    assert "open-key" not in html and "unknown-key" not in html
    assert "traceInfo(" not in html and "源边界 ID" not in html


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


def test_public_html_omits_workspace_evidence_from_the_embedded_model():
    data = copy.deepcopy(_GEO)
    private = "private-evidence-for-reconstruction-qa"
    source = data["source_model"]
    source.update(validation={"note": private}, generation={"provenance": private}, corrections=[private])
    for group in ("spaces", "floors", "boundaries", "openings"):
        source[group][0].update(source_refs=[private], assumptions=[private])
    data["surfaces"][0]["original_observation"] = private
    data["enclosure_regions"] = [{"boundary_id": "w1", "space_id": "Z1", "condition": "unknown",
                                  "verts": data["surfaces"][0]["verts"], "source_refs": [private]}]
    assert private not in rgv.build_viewer_html(data)
    assert source["spaces"][0]["source_refs"] == [private]


def test_model_title_cannot_change_the_viewer_brand_or_inject_markup():
    html = rgv.build_viewer_html(_GEO, title="<b>&x")
    assert "<title>Sim-BIM</title>" in html and "<b>&x" not in html


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
    data = copy.deepcopy(_GEO)
    for space in data["source_model"]["spaces"]:
        space.pop("role")
    html = rgv.build_viewer_html(data, title="t")
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
    assert "const okF=(u)=>partVisible(u,f,exploded)" in html
    # Primary floor remains single-valued for colouring/explode. Filtering uses
    # the declared local + spanning index set and never creates sliced geometry.
    assert "zoneFloors[z]=declared.length ? declared : [zoneFloor[z]]" in html


def test_viewer_uses_declared_source_floors():
    html = rgv.build_viewer_html(_GEO, title="source floors")
    start = html.index("window.GEO = ") + len("window.GEO = ")
    geo = json.loads(html[start:html.index(";</script>", start)])
    floor_fields = ("id", "name", "z_floor", "height")
    assert geo["source_model"]["floors"] == [
        {key: original[key] for key in floor_fields} for original in _GEO["source_model"]["floors"]]
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


@pytest.mark.parametrize("run_locale", ["en", "zh-CN"])
def test_viewer_starts_in_english_and_embeds_the_bilingual_room_catalog(run_locale):
    model = copy.deepcopy(_GEO)
    model["roles"] = {"Z1": "办公室", "Z2": "会议室"}
    model["source_model"]["spaces"][0]["name"] = "101"
    model["source_model"]["spaces"][1]["name"] = "Z02_F2_Meeting"
    page = rgv.build_viewer_html(model, locale=run_locale)
    markup = page.split("<script>", 1)[0]
    payload = json.loads(page.split("window.GEO = ", 1)[1].split(";</script>", 1)[0])
    assert '<html lang="en"' in markup and '>All floors<' in markup
    assert payload["locale"] == "en"
    assert payload["room_names"] == {"Z1": {"name": "101", "role": "unknown"}}
    assert payload["roles"] == {"Z1": "office", "Z2": "conference/meeting/multipurpose"}
    assert payload["messages"]["select by"] == ["Select by", "选择对象"]
    assert payload["messages"]["Elevation groups"] == ["Elevation groups", "高程分组"]
    for role in ["office", "office/enclosed", "corridor", "conference/meeting/multipurpose",
                 "lounge/breakroom", "shaft", "electrical/mechanical", "retail", "restroom", "stairwell", "lobby"]:
        labels = payload["room_types"][role]
        assert labels["label_en"].isascii() and not labels["label_zh"].isascii()


def test_selection_cards_show_canonical_identity_and_category_quantities_in_both_languages(tmp_path):
    model = copy.deepcopy(_GEO)
    model["source_model"]["spaces"][0].update(name="101", role="office")
    model["source_model"]["floors"][0]["name"] = "G"
    page = rgv.build_viewer_html(model)
    payload = page.split("window.GEO = ", 1)[1].split(";</script>", 1)[0]
    script = rgv.app_js()

    def section(start, end):
        return script[script.index(start):script.index(end)]

    output = _node_result(tmp_path, "const GEO="+payload+";"+"""
const el={style:{},dataset:{},replaceChildren(){}}, $=()=>el;
let inspected=null;
const SURF=GEO.surfaces,OPENS=GEO.openings,SOURCE=GEO.source_model;
const SOURCE_MAP={},SOURCE_BOUNDARIES={},SOURCE_SPACES={},ZONES=GEO.zones;
const zoneFloors={Z1:[0],Z2:[1]},zoneFloor={Z1:0,Z2:1},verticalCore=()=>false;
const NAMED_FLOORS=GEO.public_names.floors,BASES=NAMED_FLOORS.map(f=>f.z_floor),ROOM_TYPES=GEO.room_types;
""" + section("  let locale = ", "  const SURF =")
        + section("  function roleLabel(", "  // Geometry is drawn")
        + section("  const sourceFloor =", "  const _zFloorZ =")
        + section("  function ringArea3D(", "  function ringGeom(")
        + section("  function row(k,v)", "  function updateStats(")
        + section("  const surfaceByName=", "  function handleClick(") + """
const result={};
const hole=[[1,0,1],[2,0,1],[2,0,2],[1,0,2]];
const wall={kind:'surface',zone:'Z1',floor:0,type:'Wall',name:'w1',publicName:'Z01_W1_Part2',
  parentName:'Z01_W1',area:faceArea(SURF[0].verts,[hole])};
const win={kind:'window',zone:'Z1',floor:0,name:'win1',publicName:GEO.windows[0].public_name,
  parentName:GEO.windows[0].parent_name,area:faceArea(GEO.windows[0].verts)};
for(const lang of ['en','zh-CN']){
  locale=lang;
  const cases=[['zone',wall],['floor',wall],['surface',wall],['surface',win],['edge',{...wall,
    edgeRank:2,a:{distanceTo:()=>4},b:{}}]];
  result[lang]=cases.map(([mode,item])=>{showInspection(mode,item,0);return el.innerHTML;});
}
console.log(JSON.stringify(result));
""")
    canonical_room = json.loads(payload)["public_names"]["spaces"]["Z1"]
    for lang, room_use, volume, floor_volume, count, length in [
        ("en", "Office", "Modeled volume", "Storey volume", "Space count", "Length"),
        ("zh-CN", "办公 · Office", "模型体积", "本层体积", "空间数", "长度"),
    ]:
        zone, floor, surface, window, edge = output[lang]
        assert f'class="pick-name">{canonical_room}<' in zone and '>101<' not in zone
        assert room_use in zone and ">G<" in zone and zone.count('class="kv"') == 2
        assert volume in zone and "48.00 <small>m³" in zone and "m²" not in zone
        assert 'class="pick-name">G<' in floor and "F1" not in floor and floor.count('class="kv"') == 1
        assert count in floor and ">1<" in floor and "0.00 <small>m<" in floor
        assert floor_volume in floor and "48.00 <small>m³" in floor and "m²" not in floor
        assert 'class="pick-name">Z01_W1_Part2<' in surface and ">Z01_W1<" in surface and canonical_room in surface
        assert ">G<" in surface and "11.00 <small>m²" in surface and "m³" not in surface
        assert 'class="pick-name">Z01_W1_Win1<' in window and "2.00 <small>m²" in window
        assert 'class="pick-name">Z01_W1_Part2_Edge2<' in edge and 'class="kv"' not in edge
        assert length in edge and "4.00 <small>m<" in edge and edge.count('class="pick-metric"') == 1


def test_fragment_and_edge_names_survive_ring_reordering_and_match_the_picked_segment():
    model = copy.deepcopy(_GEO)
    ring1 = [[0, 0, 0], [2, 0, 0], [2, 0, 3], [0, 0, 3]]
    ring2 = [[3, 0, 0], [4, 0, 0], [4, 0, 3], [3, 0, 3]]

    def indexed(pieces):
        model["display_surface_parts"] = {"w1": [{"verts": ring} for ring in pieces]}
        page = rgv.build_viewer_html(model)
        geo = json.loads(page.split("window.GEO = ", 1)[1].split(";</script>", 1)[0])
        by_segment = {}
        for part in geo["visible_wall_parts"]["w1"]:
            ring = part["verts"]
            for i, rank in enumerate(part["edge_ranks"]):
                endpoints = tuple(sorted((tuple(ring[i]), tuple(ring[(i + 1) % len(ring)]))))
                by_segment[endpoints] = f'{part["public_name"]}_Edge{rank}'
        return by_segment

    first = indexed([ring1, ring2])
    assert first == indexed([list(reversed(ring2)), ring1[2:]+ring1[:2]])
    assert first[((0, 0, 0), (0, 0, 3))] == "Z01_W1_Part1_Edge1"
    assert first[((4, 0, 0), (4, 0, 3))] == "Z01_W1_Part2_Edge4"


def _display_payload(model):
    return json.loads(rgv.build_viewer_html(model).split("window.GEO = ", 1)[1].split(";</script>", 1)[0])


def test_standalone_display_restores_standard_names_without_inventing_source_quantities(tmp_path):
    model = copy.deepcopy(_GEO)
    model.pop("source_model")
    model["surfaces"].append({"zone": "Z1", "name": "original-floor", "type": "Floor",
                              "verts": [[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0]]})
    model["zones"] = ["Z1"]
    model["surfaces"] = [s for s in model["surfaces"] if s["zone"] == "Z1"]
    model["roles"] = {"Z1": "office"}
    saved = tmp_path / "building_geometry.json"
    saved.write_text(json.dumps(model), encoding="utf-8")
    result = _display_payload(rgv.load_viewer_geometry(saved))
    assert result["source_model"] is None and result["metrics"] == {"spaces": {}, "floors": {}}
    floor, = result["public_names"]["floors"]
    assert floor["source_name"] is None and floor["name"] == "F1"
    assert result["public_names"]["spaces"] == {"Z1": "Z01_F1_Office_C"}
    assert result["windows"][0]["public_name"] == "Z01_W1_Win1"
    assert result["windows"][0]["parent_name"] == "Z01_W1"
    assert result["visible_wall_parts"]["w1"][0]["holes"]

    model["public_names"] = {"floors": [{"id": "level-g", "name": "F9", "source_name": "Ground", "z_floor": 0}]}
    floor, = _display_payload(model)["public_names"]["floors"]
    assert floor["source_name"] == "Ground" and floor["name"] == "F9"


def test_partial_archived_source_uses_bound_names_and_preserves_host_numbers():
    model = copy.deepcopy(_GEO)
    source = model["source_model"]
    del source["boundaries"], source["openings"]
    source["floors"][0].pop("footprint")
    source["public_names"] = {"floors": {"F1": "F9"}, "spaces": {"Z1": "Z18_F9_Office_SE"},
                              "boundaries": {"w1": "Z18_W4"}, "openings": {"win1": "Z18_W4_Win3"}}
    geo = _display_payload(model)
    assert geo["public_names"]["floors"][0]["name"] == "F9"
    assert geo["public_names"]["spaces"]["Z1"] == "Z18_F9_Office_SE"
    assert geo["visible_wall_parts"]["w1"][0]["public_name"] == "Z18_W4"
    assert [geo["windows"][0][k] for k in ("public_name", "parent_name")] == ["Z18_W4_Win3", "Z18_W4"]


def test_partial_legacy_labels_reserve_floor_face_and_aperture_numbers():
    ring = [[0,0,12],[4,0,12],[4,3,12],[0,3,12]]
    faces = [{"zone":"A","name":"slab","type":"Floor","verts":ring}]
    for i, a in enumerate(ring):
        b = ring[(i+1)%4]
        faces.append({"zone":"A","name":f"w{i}","type":"Wall","verts":[a,b,[*b[:2],15],[*a[:2],15]]})
    windows = [{"name":f"win{i}","parent":"w0","verts":[[x,0,13],[x+0.5,0,13],[x+0.5,0,14],[x,0,14]]}
               for i,x in enumerate([0.5,2])]
    model = {"zones":["A"],"surfaces":faces,"windows":windows, "public_names":{
        "spaces":{"A":"Z18_F8_Office_S"},"boundaries":{"w0":"Z18_W2"},
        "objects":{"win0":"Z18_W2_Win2"}}}
    geo = _display_payload(model)
    assert geo["public_names"]["floors"][0]["name"] == "F8"
    assert geo["public_names"]["spaces"]["A"] == "Z18_F8_Office_S"
    wall_names = [s["public_name"] for s in geo["surfaces"] if s["type"] == "Wall"]
    assert wall_names[0] == "Z18_W2" and len(set(wall_names)) == 4
    assert geo["windows"][0]["public_name"] == "Z18_W2_Win2"
    assert geo["windows"][1]["public_name"] != geo["windows"][0]["public_name"]
    assert all(w["parent_name"]=="Z18_W2" and w["public_name"].startswith("Z18_W2_Win") for w in geo["windows"])
    assert geo["visible_wall_parts"]["w0"][0]["public_name"] == "Z18_W2"


@pytest.mark.parametrize("orientation", ["wall", "horizontal", "inclined"])
def test_legacy_visible_face_area_subtracts_coplanar_window(tmp_path, orientation):
    def vertex(x, y):
        return [x, 0, y] if orientation == "wall" else [x, y, y if orientation == "inclined" else 0]
    outer = [vertex(x,y) for x,y in [(0,0),(4,0),(4,3),(0,3)]]
    opening = [vertex(x,y) for x,y in [(1,1),(3,1),(3,2),(1,2)]]
    model = {"zones": ["room"], "surfaces": [{"name": "face", "type": "Wall" if orientation == "wall" else "Roof",
                                              "zone": "room", "verts": outer}],
             "windows": [{"name": "window", "parent": "face", "verts": opening}]}
    piece, = _display_payload(model)["visible_wall_parts"]["face"]
    assert len(piece["holes"]) == 1 and len(piece["edge_ranks"]) == 8
    js = rgv.app_js()
    calculate = js[js.index("  function ringArea3D("):js.index("  function ringGeom(")]
    value = _node_result(tmp_path, calculate + f"const piece={json.dumps(piece)};"
                         "console.log(JSON.stringify(faceArea(piece.verts,piece.holes)));")
    assert value == pytest.approx(10 * (2**0.5 if orientation == "inclined" else 1))


def test_legacy_explicit_courtyard_and_non_coplanar_window_are_preserved(tmp_path):
    face = {"name": "slab", "type": "Floor", "zone": "court", "verts": [[0,0,0],[5,0,0],[5,4,0],[0,4,0]],
            "holes": [[[1,1,0],[3,1,0],[3,3,0],[1,3,0]]]}
    model = {"zones": ["court"], "surfaces": [face], "windows": []}
    for windows in [[], [{"name": "separate-window", "parent": "slab", "verts": [[1,1,1],[2,1,1],[2,2,1],[1,2,1]]}]]:
        geo = _display_payload({**model, "windows": windows})
        actual = geo["surfaces"][0]
        assert "slab" not in geo["visible_wall_parts"] and actual["holes"] == face["holes"]
        assert len(actual["edge_ranks"]) == 8
        js = rgv.app_js()
        calculate = js[js.index("  function ringArea3D("):js.index("  function ringGeom(")]
        area = _node_result(tmp_path, calculate + f"const s={json.dumps(actual)};"
                            "console.log(JSON.stringify(faceArea(s.verts,s.holes)));")
        assert area == 16


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


def _node_result(tmp_path, code):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute viewer geometry")
    script = tmp_path / "viewer_properties.cjs"
    script.write_text(code, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("ring,holes", [
    # A concave quad must not use the diagonal from vertex zero.
    ([[0, 0], [2, 1], [0, 2], [0.5, 1]], []),
    # The former O21 cap contains a near-return at vertices 3/5.
    ([[-12.16957057592, 8.701179316718], [-7.547400862034, 6.54582618184],
      [-9.575968518389, 2.195548804064], [-7.038306714686, 1.01221767119],
      [-4.772537247094, -0.044327983161], [-7.038306714685692, 1.0122176711898565],
      [-7.207354019382, 0.649694556375], [-0.4, -2.5],
      [0.009172541800332945, -0.5657298023984259], [0.1, -0.7], [2.6, -0.7],
      [2.6, -2], [9.9, -2], [9.9, 2.7], [14.8, 2.7], [14.8, -6.208351418503],
      [14.163340567401, -7.8], [-1.577251940125, -7.8],
      [-16.11134547698, -1.184481562535], [-16.070150026769, 0.336359690373]], []),
    ([[0, 0], [6, 0], [6, 5], [0, 5]], [[[1, 1], [3, 1], [3, 3], [1, 3]]]),
])
@pytest.mark.parametrize("vertical", [False, True])
def test_viewer_triangles_cover_only_the_source_polygon(tmp_path, ring, holes, vertical):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    lift = lambda p: [p[0], 7, p[1]] if vertical else [*p, 75.6]
    loops = [[lift(p) for p in loop] for loop in [ring, *holes]]
    script = rgv.app_js()
    functions = script[script.index("  function projectionAxis("):script.index("  function edgeGeom(")]
    output = _node_result(tmp_path,
        f"const THREE=require({json.dumps(str(rgv._VENDOR / 'three.min.js'))});\n"
        + functions + f"\nconsole.log(JSON.stringify(triangulateRings({json.dumps(loops[0])},{json.dumps(loops[1:])})));\n")
    positions = output["positions"]
    points = [positions[i:i+3] for i in range(0, len(positions), 3)]
    points = [(p[0], p[2] if vertical else p[1]) for p in points]
    indices = output["indices"]
    triangles = [Polygon([points[j] for j in indices[i:i+3]]) for i in range(0, len(indices), 3)]
    expected = Polygon(ring, holes)
    actual = unary_union(triangles)
    assert expected.is_valid
    assert actual.difference(expected).area < 1e-8
    assert expected.difference(actual).area < 1e-8
    assert sum(p.area for p in triangles) == pytest.approx(expected.area, abs=1e-8)


def test_floor_filter_restores_caps_and_hides_core_geometry_consistently(tmp_path):
    script = rgv.app_js()
    functions = script[script.index("  const zoneForSpace ="):script.index("  // ---- zone centroids")]
    result = _node_result(tmp_path, """
const SOURCE_MAP={zones:{lower:'lower',upper:'upper',lift:'lift',stair:'stair'}};
const SOURCE_SPACES={};
const roles={lift:'shaft',stair:'stairwell',lower:'office',upper:'office'};
const roleOf=z=>roles[z];
const zoneFloor={lower:0,upper:1,lift:2,stair:3};
const zoneFloors={lower:[0],upper:[1],lift:[0,1,2],stair:[0,1,3]};
""" + functions + """
const cap={zone:'upper',floor:1,dup:true,occludedBy:['lower']};
const contact={zone:'upper',floor:1,dup:true,occludedBy:['lift']};
const core={zone:'lift',floor:2,dup:false};
console.log(JSON.stringify({capAll:partVisible(cap,-1,false),capUpper:partVisible(cap,1,false),
  capLower:partVisible(cap,0,false),capExploded:partVisible(cap,-1,true),
  contactAll:partVisible(contact,-1,false),contactSingle:partVisible(contact,1,false),
  coreAll:partVisible(core,-1,false),coreSingle:partVisible(core,1,false),
  coreExplodedSingle:partVisible(core,1,true),stairSingle:onFloor({zone:'stair',floor:3},1)}));
""")
    assert result == {"capAll": False, "capUpper": True, "capLower": False, "capExploded": True,
                      "contactAll": False, "contactSingle": True, "coreAll": True,
                      "coreSingle": False, "coreExplodedSingle": False, "stairSingle": False}


def test_storey_filter_only_clips_geometry_extending_beyond_its_bounds(tmp_path):
    script = rgv.app_js()
    functions = script[script.index("  function storeyPlanes("):script.index("  function applyClipping(")]
    result = _node_result(tmp_path, f"const THREE=require({json.dumps(str(rgv._VENDOR / 'three.min.js'))});\n" + """
let floorIndex=0, exploded=0;
const radius=80;
const $=id=>({value:id==='floorSel'?floorIndex:exploded});
const sourceFloor=i=>[{z_floor:8.4,height:3.2},{z_floor:-100.3,height:100.3},
                      {z_floor:-103143.303,height:103143.303}][i];
""" + functions + """
const section=new THREE.Plane(new THREE.Vector3(1,0,0),-5);
function planeNames(low,high){
  const band=storeyPlanes();
  const geometry=new THREE.BufferGeometry();
  geometry.setAttribute('position',new THREE.Float32BufferAttribute([0,0,low,10,0,high],3));
  return clippingForGeometry(geometry,[section],band).map(p=>p===section?'section':p===band[0]?'bottom':'top');
}
const planes=[[8.4,8.4],[11.6,11.6],[8.4,11.6],[0,9],[10,15],[0,20],[2,3],[19,21]].map(v=>planeNames(...v));
// A mixed primitive still requires a section through its other triangles.
// Its coincident cap must survive camera-space rounding when zoomed out.
const capTolerance=storeyPlanes()[1].constant-11.6;
const cameraError=Math.abs((Math.fround(1200+11.6)-1200)-11.6);
floorIndex=1;const basement=planeNames(-100.3,0);
floorIndex=2;const deepDatum=planeNames(-103143.303,0);
exploded=0.5;const explodedCount=storeyPlanes().length;
exploded=0;floorIndex=-1;const allCount=storeyPlanes().length;
console.log(JSON.stringify({planes,basement,deepDatum,capTolerance,cameraError,explodedCount,allCount}));
""")
    assert result.pop("cameraError") <= result.pop("capTolerance") <= 0.0005
    assert result == {
        "planes": [["section"], ["section"], ["section"], ["section", "bottom"], ["section", "top"],
                   ["section", "bottom", "top"], ["section", "bottom"], ["section", "top"]],
        "basement": ["section"], "deepDatum": ["section"], "explodedCount": 0, "allCount": 0,
    }


def test_3d_measurement_uses_source_distance_and_constant_pixel_diameters(tmp_path):
    script = rgv.app_js()
    markers = script[script.index("  let measuring="):script.index("  const CAND=")]
    readout = script[script.index("  function renderMeasurement("):script.index("  // Picking follows")]
    picking = script[script.index("  function snapPick("):script.index("  // ---- edge selection")]
    result = _node_result(tmp_path, f"const THREE=require({json.dumps(str(rgv._VENDOR / 'three.min.js'))});\n" + """
const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(42,2,.05,200);
const controls={addEventListener(){}},viewport={clientWidth:800,clientHeight:400},matchMedia=()=>({matches:false});
const card={classList:{toggle(){}}},$=id=>id==='meas'?card:{value:-1},msg=x=>x,esc=x=>x;
const renderer={domElement:{getBoundingClientRect:()=>({width:800,height:400})}};
const meshActive=()=>false,onFloor=()=>true,comparisonPlanes=()=>[],seeThrough=()=>true,zoneFloor={};
const CAND=[{v:new THREE.Vector3(),zone:'a'},{v:new THREE.Vector3(3,4,0),zone:'b'}];
let explosion=4;
const explodeOffset=zone=>new THREE.Vector3(0,0,zone==='b'?explosion:0);
""" + markers + readout + picking + """
camera.position.z=20;camera.updateMatrixWorld();
const displayed=CAND[1].v.clone().add(explodeOffset('b')).project(camera);
const hit=snapPick((displayed.x+1)*400,(1-displayed.y)*200);
measuring=true;measurePts=[CAND[0],hit];positionMeasurePair();renderMeasurement();
const cardAtExplosion=card.innerHTML,displayLength=measureLine.scale.y;
const nearRadii=endpoints.map(ball=>ball.scale.x);
camera.position.z=50;sizeMeasureMarkers();
const radiusRatio=endpoints[0].scale.x/nearRadii[0];
explosion=0;positionMeasurePair();renderMeasurement();
viewport.clientWidth=390;viewport.clientHeight=500;sizeMeasureMarkers();
const pixels=endpoints[0].scale.x*viewport.clientHeight*camera.projectionMatrix.elements[5]/50;
console.log(JSON.stringify({hit:hit.v.toArray(),cardAtExplosion,displayLength,resetLength:measureLine.scale.y,
  sameResult:card.innerHTML===cardAtExplosion,nearRatio:nearRadii[0]/nearRadii[1],radiusRatio,pixels}));
""")
    assert result["hit"] == [3, 4, 0]
    assert "5.000 <small>m" in result["cardAtExplosion"] and result["sameResult"]
    assert result["displayLength"] == pytest.approx(41 ** .5) and result["resetLength"] == 5
    assert result["nearRatio"] == pytest.approx(20 / 16)
    assert result["radiusRatio"] == pytest.approx(50 / 20)
    assert result["pixels"] == pytest.approx(15)


def test_viewer_triangulation_is_translation_invariant(tmp_path):
    script = rgv.app_js()
    functions = script[script.index("  function projectionAxis("):script.index("  function edgeGeom(")]
    results = _node_result(tmp_path,
        f"const THREE=require({json.dumps(str(rgv._VENDOR / 'three.min.js'))});\n"
        + functions + """
const ring=[[0,0,0],[4.1,0,0],[4.1,3.2,0],[2.2,3.2,0],[2.2,2.1,0],[0,2.1,0]];
const hole=[[0.2,0.2,0],[1.2,0.2,0],[1.2,1.2,0],[0.2,1.2,0]];
const result=[0,1e5,1e6].map(offset=>{
  const move=r=>r.map(p=>[p[0]+offset,p[1]+offset,p[2]+7]);
  const {positions,indices}=triangulateRings(move(ring),[move(hole)]);
  let area=0;
  for(let i=0;i<indices.length;i+=3){const [a,b,c]=indices.slice(i,i+3).map(j=>positions.slice(j*3,j*3+3));
    area+=Math.abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2;}
  return {area,first:positions.slice(0,3)};
});
console.log(JSON.stringify(result));
""")
    assert all(r["area"] == pytest.approx(results[0]["area"], abs=1e-8) for r in results)
    assert results[-1]["first"] == [1e6, 1e6, 7]
