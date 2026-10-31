"""Literal plan linework becomes a source proposal without invented walls."""
from __future__ import annotations

import copy
import json

import pytest

from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.plan_partition import compile_plan_partition


def _plan() -> dict:
    return {
        "floor_id": "F1",
        "z_floor": 0.0,
        "ceiling_height": 3.2,
        "x_anchors": [[0, 0.0], [99, 9.9]],
        # Pixel Y increases downwards; world Y increases northwards.
        "y_anchors": [[0, 9.9], [99, 0.0]],
        "basis": "synthetic axis-line calibration",
        "footprint_pixels": [[10, 10], [90, 10], [90, 90], [10, 90], [10, 10]],
        "partitions": [
            {
                "id": "P_vertical",
                "points": [[50, 10], [50, 50], [50, 90]],
                "source_refs": ["plan:wall/vertical"],
            },
            {
                "id": "P_left_branch",
                "points": [[10, 50], [50, 50]],
                "source_refs": ["plan:wall/left-branch"],
            },
        ],
        "openings": [
            {
                "id": "W_north",
                "kind": "window",
                "p1": [62, 10],
                "p2": [76, 10],
                "z": [0.9, 2.4],
                "source_refs": ["plan:window/north"],
            },
            {
                "id": "D_internal",
                "kind": "door",
                "p1": [50, 62],
                "p2": [50, 72],
                "z": [0.0, 2.1],
                "source_refs": ["plan:door/internal"],
                "state": "closed",
                "assumptions": ["door swing is not represented"],
            },
            {
                "id": "D_south",
                "kind": "open",
                "p1": [64, 90],
                "p2": [76, 90],
                "z": [0.0, 2.4],
                "source_refs": ["plan:door/south"],
            },
        ],
        "space_seeds": [
            {
                "id": "suite",
                "point": [70, 35],
                "role": "office",
                "source_refs": ["plan:label/suite"],
            },
        ],
        "assumptions": ["representative wall lines are supplied explicitly"],
        "unresolved": ["wall thickness is not represented"],
    }


def _signed_area(ring: list[list[float]]) -> float:
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:] + ring[:1])
    ) / 2


def test_compiler_facts_do_not_create_authored_tasks_and_survive_export(tmp_path):
    plan = _plan()
    before = copy.deepcopy(plan)
    proposal, metadata = compile_plan_partition(plan, image_size=(100, 100), image_name='plan')
    assert proposal['assumptions'] == plan['assumptions']
    assert proposal['unresolved'] == plan['unresolved']
    assert metadata['calibration']['status'] == 'caller_supplied_not_independently_verified'
    assert metadata['method']['snap'] is False
    target = tmp_path / 'source'
    result = export_source_proposal(proposal, target, provenance={'plan_metadata': metadata})
    assert result['source_geometry_ready']
    source = json.loads((target / 'source_model.json').read_bytes())
    assert source['generation']['unresolved'] == plan['unresolved']
    assert source['generation']['provenance']['plan_metadata'] == metadata
    assert 'calibration' in source['generation']['notes']
    assert 'unverified' in source['generation']['notes']
    assert plan == before


def test_internal_preflight_omits_only_inert_audit_and_preserves_public_save_isolation():
    plan = _plan()
    plan['partitions'][0]['pixels'] = plan['partitions'][0].pop('points')
    evidence = [{'source_refs': ['synthetic original'], 'original_m': i / 1000}
                for i in range(2000)]
    plan['regularization_inputs'] = {'evidence': evidence}
    plan['regularization'] = {'items': copy.deepcopy(evidence)}
    before = copy.deepcopy(plan)
    proposal, complete = compile_plan_partition(plan, image_size=(100, 100), image_name='plan')
    preflight, metadata = compile_plan_partition(
        plan, image_size=(100, 100), image_name='plan', include_audit_metadata=False)
    assert preflight == proposal
    assert metadata == {key: value for key, value in complete.items()
                        if key not in {'regularization', 'regularization_inputs'}}
    assert complete['regularization_inputs'] == before['regularization_inputs']
    assert complete['regularization'] == before['regularization']
    complete['regularization_inputs']['evidence'][0]['original_m'] = -100
    complete['regularization']['items'][0]['source_refs'].append('changed output only')
    assert plan == before


@pytest.mark.parametrize('invalid', ['unknown_field', 'dangling_partition'])
def test_internal_preflight_keeps_unknown_fields_and_geometry_rejections(invalid):
    plan = _plan()
    if invalid == 'unknown_field':
        plan['not_a_plan_field'] = True
    else:
        plan['partitions'][0]['points'][-1][1] = 80
    failures = []
    for include_audit in (True, False):
        with pytest.raises(ValueError) as error:
            compile_plan_partition(plan, image_size=(100, 100), image_name='plan',
                                   include_audit_metadata=include_audit)
        failures.append(str(error.value))
    assert failures[0] == failures[1]


def test_t_partition_keeps_all_spaces_and_resolves_openings_without_resizing():
    proposal, metadata = compile_plan_partition(
        _plan(), image_size=(100, 100), image_name="plan.png",
    )

    geometry = proposal["geometry"]
    cells = geometry["floors"][0]["cells"]
    assert geometry["schema_version"] == "2"
    assert len(cells) == metadata["space_count"] == 3
    assert {cell["role"] for cell in cells if cell["id"] != "suite"} == {"unknown"}
    assert next(cell for cell in cells if cell["id"] == "suite")["role"] == "office"
    assert all(cell["source_refs"] and cell["polygon"] for cell in cells)
    assert all(_signed_area(cell["polygon"]) > 0 for cell in cells)

    # The left branch ends at a real wall. It creates three physical spaces,
    # not four directional/grid slices: the entire right side remains one cell.
    suite = next(cell for cell in cells if cell["id"] == "suite")
    assert suite["polygon"] == [[5.0, 0.9], [9.0, 0.9], [9.0, 8.9], [5.0, 8.9]]

    window = geometry["windows"][0]
    assert window["id"] == "W_north" and window["facade"] == "North"
    assert window["p1"] == [6.2, 8.9] and window["p2"] == [7.6, 8.9]
    assert window["width_m"] == 1.4 and window["z"] == [0.9, 2.4]

    by_id = {opening["id"]: opening for opening in geometry["openings"]}
    internal = by_id["D_internal"]
    assert internal["other_space_id"] is not None
    assert internal["p1"] == [5.0, 3.7] and internal["p2"] == [5.0, 2.7]
    assert internal["z"] == [0.0, 2.1] and internal["state"] == "closed"
    assert by_id["D_south"]["other_space_id"] is None

    hosts = {row["opening_id"]: row for row in metadata["opening_hosts"]}
    assert len(hosts["D_internal"]["space_ids"]) == 2
    assert hosts["D_internal"]["width_m"] == 1.0
    assert hosts["D_south"]["exterior"] is True
    assert metadata["method"] == {
        "polygonizer": "shapely.unary_union+polygonize_full",
        "snap": False,
        "extend": False,
        "clip": False,
        "inferred_partitions": False,
    }
    assert metadata["calibration"]["world_metres_per_pixel"]["y"] < 0
    assert proposal["unresolved"] == _plan()["unresolved"]


def test_folded_partition_preserves_nonrectangular_cells_and_collinear_cleanup():
    plan = _plan()
    plan["partitions"] = [{
        "id": "P_fold",
        "points": [[40, 10], [40, 30], [40, 50], [70, 50], [70, 90]],
        "source_refs": ["plan:wall/fold"],
    }]
    plan["openings"] = []
    plan["space_seeds"] = []

    proposal, _ = compile_plan_partition(plan, image_size=(100, 100), image_name="fold.png")
    cells = proposal["geometry"]["floors"][0]["cells"]
    assert len(cells) == 2
    assert all(len(cell["polygon"]) == 6 for cell in cells)
    assert all(cell["id"].startswith("F1_S") and cell["role"] == "unknown" for cell in cells)
    # The redundant point [40, 30] is omitted from the world polygons.
    assert all([4.0, 6.9] not in cell["polygon"] for cell in cells)


def test_dangle_and_outside_partition_are_rejected_with_id_and_pixel_location():
    dangling = _plan()
    dangling["partitions"] = [{
        "id": "P_dangle",
        "points": [[10, 45], [35, 45]],
        "source_refs": ["plan:wall/dangle"],
    }]
    dangling["openings"] = []
    dangling["space_seeds"] = []
    with pytest.raises(ValueError, match=r"dangles.*P_dangle.*10.0.*35.0"):
        compile_plan_partition(dangling, image_size=(100, 100), image_name="plan.png")

    outside = copy.deepcopy(dangling)
    outside["partitions"][0].update(id="P_outside", points=[[5, 45], [35, 45]])
    with pytest.raises(ValueError, match=r"P_outside.*outside footprint"):
        compile_plan_partition(outside, image_size=(100, 100), image_name="plan.png")


def test_exact_crossing_is_noded_but_overlap_and_opening_across_t_are_rejected():
    crossing_lines = _plan()
    crossing_lines["partitions"] = [
        {"id": "vertical", "points": [[50, 10], [50, 90]], "source_refs": ["plan:v"]},
        {"id": "horizontal", "points": [[10, 50], [90, 50]], "source_refs": ["plan:h"]},
    ]
    crossing_lines["openings"] = []
    crossing_lines["space_seeds"] = []
    proposal, _ = compile_plan_partition(
        crossing_lines, image_size=(100, 100), image_name="plan.png",
    )
    assert len(proposal["geometry"]["floors"][0]["cells"]) == 4

    overlapping = copy.deepcopy(crossing_lines)
    overlapping["partitions"].append({
        "id": "duplicate_piece", "points": [[50, 20], [50, 70]],
        "source_refs": ["plan:duplicate"],
    })
    with pytest.raises(ValueError, match=r"vertical.*duplicate_piece.*overlap"):
        compile_plan_partition(overlapping, image_size=(100, 100), image_name="plan.png")

    crossing = _plan()
    crossing["openings"] = [{
        "id": "D_cross_T",
        "kind": "door",
        "p1": [50, 45],
        "p2": [50, 55],
        "z": [0.0, 2.1],
        "source_refs": ["plan:door/cross-t"],
    }]
    with pytest.raises(ValueError, match=r"D_cross_T.*T-junction"):
        compile_plan_partition(crossing, image_size=(100, 100), image_name="plan.png")


def test_seed_on_boundary_duplicate_seed_face_and_unknown_fields_are_rejected():
    on_boundary = _plan()
    on_boundary["space_seeds"] = [{"id": "bad", "point": [50, 30]}]
    with pytest.raises(ValueError, match=r"bad.*strictly inside"):
        compile_plan_partition(on_boundary, image_size=(100, 100), image_name="plan.png")

    duplicate_face = _plan()
    duplicate_face["space_seeds"].append({"id": "suite_again", "point": [80, 70]})
    with pytest.raises(ValueError, match=r"suite.*suite_again.*same space"):
        compile_plan_partition(duplicate_face, image_size=(100, 100), image_name="plan.png")

    unknown = _plan()
    unknown["partitions"][0]["confidence"] = "high"
    with pytest.raises(ValueError, match=r"unknown confidence"):
        compile_plan_partition(unknown, image_size=(100, 100), image_name="plan.png")


@pytest.mark.parametrize("reflect_x", [False, True])
def test_concave_footprint_recessed_windows_and_connections_export_exactly(tmp_path, reflect_x):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    plan = _plan()
    plan["footprint_pixels"] = [
        [10, 10], [90, 10], [90, 50], [60, 50], [60, 90], [10, 90],
    ]
    if reflect_x:
        plan["x_anchors"] = [[0, 9.9], [99, 0.0]]
    plan["partitions"] = [{"id": "shared", "points": [[40, 10], [40, 90]],
                           "source_refs": ["synthetic wall"]}]
    plan["openings"] = [
        {"id": "recess_vertical", "kind": "window", "p1": [60, 60], "p2": [60, 75],
         "z": [0.9, 2.4], "source_refs": ["synthetic recessed window"]},
        {"id": "recess_horizontal", "kind": "window", "p1": [65, 50], "p2": [80, 50],
         "z": [0.9, 2.4], "source_refs": ["synthetic recessed window"]},
        {"id": "internal", "kind": "door", "p1": [40, 60], "p2": [40, 70],
         "z": [0, 2.1], "source_refs": ["synthetic door"]},
        {"id": "external", "kind": "door", "p1": [60, 78], "p2": [60, 88],
         "z": [0, 2.1], "source_refs": ["synthetic exterior door"]},
    ]
    plan["space_seeds"] = []
    original = copy.deepcopy(plan)
    proposal, metadata = compile_plan_partition(plan, image_size=(100, 100), image_name="plan.png")
    assert plan == original
    assert proposal["geometry"]["windows"][0]["facade"] == ("West" if reflect_x else "East")
    assert proposal["geometry"]["windows"][1]["facade"] == "South"
    report = export_source_proposal(proposal, tmp_path / "candidate")
    assert report["source_geometry_ready"], report
    source = json.loads((tmp_path / "candidate/source_model.json").read_text())
    assert len(source["spaces"]) == 2
    expected = Polygon(metadata["footprint"]["world_polygon_m"])
    assert unary_union([Polygon(s["polygon"]) for s in source["spaces"]]).equals(expected)
    assert Polygon(source["floors"][0]["footprint"]).equals(expected)
    assert expected.area < expected.envelope.area
    assert not source["unbuilt_openings"]
    for opening, host in zip(sorted(source["openings"], key=lambda o: o["id"]),
                             sorted(metadata["opening_hosts"], key=lambda o: o["opening_id"])):
        assert opening["id"] == host["opening_id"]
        assert {tuple(v[:2]) for v in opening["vertices"]} == {
            tuple(host["p1_world_m"]), tuple(host["p2_world_m"])}
        assert opening["space_ids"] == host["space_ids"]
        assert opening["exterior"] == host["exterior"]
    connections = {c["opening_id"]: c for c in source["connections"]}
    assert len(connections["internal"]["space_ids"]) == 2
    assert connections["external"]["exterior"]
    reloaded = json.loads((tmp_path / "candidate/proposal.json").read_text())
    replay = export_source_proposal(reloaded, tmp_path / "replay")
    assert replay["source_model_sha256"] == report["source_model_sha256"]


def test_concave_footprint_rejects_opening_across_recess():
    plan = _plan()
    plan.update(footprint_pixels=[[10, 10], [90, 10], [90, 50], [60, 50], [60, 90], [10, 90]],
                partitions=[], space_seeds=[])
    plan["openings"] = [{"id": "across_recess", "kind": "window", "p1": [50, 50],
                         "p2": [80, 50], "z": [1, 2], "source_refs": ["invalid span"]}]
    with pytest.raises(ValueError, match="across_recess.*full-boundary hosts"):
        compile_plan_partition(plan, image_size=(100, 100), image_name="plan.png")


def test_window_state_and_closed_open_passage_are_rejected_before_source_loss():
    window_state = _plan()
    window_state["openings"][0]["state"] = "closed"
    with pytest.raises(ValueError, match=r"W_north.*window.state.*would be lost"):
        compile_plan_partition(window_state, image_size=(100, 100), image_name="plan.png")

    closed_passage = _plan()
    closed_passage["openings"][2]["state"] = "closed"
    with pytest.raises(ValueError, match=r"D_south.*passage state must be open"):
        compile_plan_partition(closed_passage, image_size=(100, 100), image_name="plan.png")


def test_compiled_proposal_exports_as_ready_source_bim(tmp_path):
    plan = _plan()
    proposal, metadata = compile_plan_partition(
        plan, image_size=(100, 100), image_name="synthetic-plan.png",
    )
    out = tmp_path / "compiled"
    report = export_source_proposal(proposal, out, provenance={"partition_metadata": metadata})

    assert report["source_geometry_ready"] is True
    assert report["source_geometry_self_consistency"]["status"] == "pass"
    assert report["counts"]["spaces"] == 3
    source = json.loads((out / "source_model.json").read_text())
    assert {opening["id"] for opening in source["openings"]} == {
        "W_north", "D_internal", "D_south",
    }
    assert source["opening_hosts"]["D_internal"]
    assert source["generation"]["unresolved"] == plan["unresolved"]
    assert source["generation"]["provenance"]["partition_metadata"]["calibration"]["status"] == (
        "caller_supplied_not_independently_verified")


@pytest.mark.parametrize("basis", ["observed", "inferred", "unknown"])
def test_seed_use_evidence_is_validated_without_changing_geometry(basis):
    from src.agent.runtime_roles.plan_format import plan_format_errors

    plan = _plan()
    plan["reading_type"] = "plan"
    seed = plan["space_seeds"][0]
    seed["role"] = "unknown" if basis == "unknown" else "office"
    bare, metadata = compile_plan_partition(plan, image_size=(100, 100), image_name="plan.png")
    assert not any("role_evidence" in cell for cell in bare["geometry"]["floors"][0]["cells"])
    seed["role_evidence"] = {
        "role": seed["role"], "basis": basis,
        "source_refs": ["plan.png: original room label or explicit interpretation"],
        "assumptions": ["Furniture suggests office"] if basis == "inferred" else [],
    }
    seed.pop("role")  # The evidence already declares the role; no duplicate field is needed.
    before = copy.deepcopy(plan)
    assert plan_format_errors(plan) == []
    proposal, after_metadata = compile_plan_partition(plan, image_size=(100, 100), image_name="plan.png")
    assert plan == before and after_metadata == metadata
    cell = next(row for row in proposal["geometry"]["floors"][0]["cells"] if row["id"] == seed["id"])
    assert cell.pop("role_evidence") == seed["role_evidence"]
    assert proposal == bare  # All coordinates, partitions, openings and host IDs are identical.


@pytest.mark.parametrize("evidence", [
    None,
    {"role": "invented_room"},
    {"basis": "assumed"},
    {"source_refs": []},
    {"source_refs": [" "]},
    {"assumptions": []},
    {"role": "unknown"},
    {"basis": "unknown"},
    {"role": "corridor"},
])
def test_invalid_seed_use_evidence_is_never_inferred_or_silently_dropped(evidence):
    from src.agent.runtime_roles.plan_format import plan_format_errors

    plan = _plan()
    valid = {"role": "office", "basis": "inferred", "source_refs": ["plan: desks"],
             "assumptions": ["Furniture-based use"]}
    plan["space_seeds"][0]["role_evidence"] = None if evidence is None else {**valid, **evidence}
    before = copy.deepcopy(plan)
    assert plan_format_errors(plan)
    with pytest.raises(ValueError):
        compile_plan_partition(plan, image_size=(100, 100), image_name="plan.png")
    assert plan == before
