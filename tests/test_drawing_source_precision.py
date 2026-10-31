"""Ordinary drawings reach the shared source path without a global vertex grid.

Synthetic geometry exercises admission, not architectural fidelity. No model is
called, and the original PNG remains a declared floor-plan input throughout.
"""
import copy
import hashlib
import json
import math

from PIL import Image
import pytest

from scripts.tool_scripts.bim_agent_parametric import build_parametric
from scripts.tool_scripts.bim_agent_source_precision import (
    REFERENCE_LINES_POLICY, reference_line_policy, validate_reference_line_adoption)
from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.runtime_entry import prepare_inputs
from tests.test_parametric_proposal import plan


def drawing_run(tmp_path, *, reviews=True):
    images = tmp_path / "originals"
    images.mkdir()
    Image.new("RGB", (240, 240), "white").save(images / "plan.png")
    run, _, _ = prepare_inputs(tmp_path / "execution", images=images, mesh=None,
        building_input=None, scope="Synthetic admission test, no fidelity verdict.",
        image_kind="drawings", floor_plan_images=["plan.png"],
        max_candidates=20, process_reviews=reviews)
    return run


def rotated_plan():
    value = plan()
    value["schema_version"] = "source_polygon_v1"
    value["instances"] = value["instances"][:1]
    template = value["templates"]["typical"]
    template.pop("window_rows")
    origin, angle = [1.13, -2.07], 17
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    def world(p):
        return [origin[0] + c*p[0] - s*p[1], origin[1] + s*p[0] + c*p[1]]
    template["footprint"] = [world(p) for p in template["footprint"]]
    for space in template["spaces"]:
        x0, y0, x1, y1 = space.pop("rect")
        space["polygon"] = [world(p) for p in ((x0,y0),(x1,y0),(x1,y1),(x0,y1))]
    for door in template["doors"]:
        for field in ("p1", "p2"):
            door[field] = world(door[field])
    value["modelling"] = {"frames": [{"id": "building", "origin": origin,
        "yaw_degrees": angle, "templates": ["typical"]}]}
    return value


def _read(path):
    return json.loads(path.read_bytes())


def test_new_drawing_manifest_freezes_both_parameter_and_pixel_policies(tmp_path):
    manifest = Toolkit(drawing_run(tmp_path)).manifest
    assert manifest["source_precision_policy"] == REFERENCE_LINES_POLICY
    assert manifest["architectural_modelling_policy"]["grid_m"] == .1
    assert manifest["lite_bim_grid_step_m"] == .1
    assert manifest["floor_plan_images"] == ["plan.png"]
    assert manifest["drawing_review_policy"]
    assert not reference_line_policy({})
    with pytest.raises(ValueError, match="unknown source_precision_policy"):
        reference_line_policy({"source_precision_policy": "model_skip"})


@pytest.mark.parametrize("generic", [False, True])
def test_rotated_source_uses_adopted_parameters_and_retains_world_vertices(tmp_path, generic):
    from scripts.tool_scripts.bim_agent_regularization import source_grid_report
    toolkit = Toolkit(drawing_run(tmp_path))
    value = rotated_plan()
    if generic:
        context = value.pop("modelling")
        proposal = expand_parametric_proposal(value)
        proposal["modelling"] = context
        proposal["modelling"]["frames"][0]["templates"] = ["F1"]
        result = toolkit.build(proposal)
    else:
        result = build_parametric(toolkit, json.dumps(value))
    assert result.get("source_geometry_ready"), result
    source = _read(toolkit.run / result["candidate"] / "source_model.json")
    assert source_grid_report(source, .1)["violations"]
    assert source["validation"]["status"] == "pass"
    assert len(source["connections"]) == 1
    saved = _read(toolkit.run / result["candidate"] / "proposal.json")
    assert validate_reference_line_adoption(toolkit, saved,
        result["architectural_regularization"])["status"] == "adopted"


def test_legacy_drawing_manifest_keeps_world_vertex_gate(tmp_path):
    toolkit = Toolkit(drawing_run(tmp_path))
    toolkit.manifest.pop("source_precision_policy")
    toolkit.manifest.pop("architectural_modelling_policy")
    value = rotated_plan()
    value.pop("modelling")
    result = build_parametric(toolkit, json.dumps(value))
    assert not result.get("source_geometry_ready"), result
    assert "source_lite_grid_violation" in json.dumps(result)
    assert not list(toolkit.run.glob("candidate_*"))


@pytest.mark.parametrize("damage", ["missing", "artifact", "proposal", "policy", "audit_status", "effective", "curve"])
def test_reference_proof_cannot_be_replaced_by_a_self_report(tmp_path, damage):
    toolkit = Toolkit(drawing_run(tmp_path))
    result = build_parametric(toolkit, json.dumps(rotated_plan()))
    assert result.get("source_geometry_ready"), result
    proposal = _read(toolkit.run / result["candidate"] / "proposal.json")
    reference = copy.deepcopy(result["architectural_regularization"])
    if damage == "missing":
        reference.pop("effective")
    elif damage == "artifact":
        (toolkit.run / reference["audit"]["file"]).write_text("{}")
    elif damage == "proposal":
        proposal["assumptions"].append("Changed after adoption")
    elif damage == "policy":
        reference["policy"]["grid_m"] = .01
    else:
        field = "effective" if damage == "effective" else "audit"
        path = toolkit.run / reference[field]["file"]
        data = _read(path)
        if damage == "effective":
            data["assumptions"].append("Changed effective input")
        elif damage == "curve":
            data["not_assessed"] = [{"scope": "curve XY dimensions and narrow-space width"}]
        else:
            data["status"] = "not_requested"
        path.write_text(json.dumps(data), encoding="utf-8")
        reference[field]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises((ValueError, KeyError), match="architectural|proposal|reference-line"):
        validate_reference_line_adoption(toolkit, proposal, reference)


def test_new_policy_without_proof_does_not_waive_the_grid(tmp_path):
    from scripts.tool_scripts.bim_agent_regularization import reject_source_proposal
    toolkit = Toolkit(drawing_run(tmp_path))
    value = rotated_plan()
    value.pop("modelling")
    rejection = reject_source_proposal(toolkit, expand_parametric_proposal(value))
    assert rejection and "source_lite_grid_violation" in json.dumps(rejection)


def test_quickcheck_and_save_adopt_the_same_declared_local_frame(tmp_path):
    from src.agent.construction_check import check_parametric_plan
    toolkit = Toolkit(drawing_run(tmp_path))
    original = rotated_plan()
    checked = check_parametric_plan(toolkit, original)
    assert checked.geometry_ready, str(checked)
    assert not list(toolkit.run.glob("candidate_*"))
    result = build_parametric(toolkit, json.dumps(original))
    assert result["source_geometry_ready"], result
    saved = _read(toolkit.run / result["candidate"] / "source_model.json")
    for collection in ("floors", "spaces", "boundaries", "openings", "connections"):
        assert json.loads(json.dumps(checked.source[collection])) == saved[collection]


def test_adoption_does_not_repair_invalid_opening_hosts(tmp_path):
    toolkit = Toolkit(drawing_run(tmp_path))
    value = rotated_plan()
    value["templates"]["typical"]["doors"][0]["p1"] = [50, 50]
    result = build_parametric(toolkit, json.dumps(value))
    assert not result.get("source_geometry_ready"), result
    assert not list(toolkit.run.glob("candidate_*"))


def test_new_drawings_pixel_build_and_assembly_keep_the_construction_grid(tmp_path):
    from scripts.tool_scripts.bim_agent_regularization import source_grid_report
    from tests.test_bim_process_reviews import _plan
    toolkit = Toolkit(drawing_run(tmp_path))
    value = _plan([60, 100])
    result = toolkit.build_plan("plan.png", json.dumps(value))
    assert result["source_geometry_ready"], result
    assert "architectural_regularization" not in result
    record = result["plan_input"]
    assembled = toolkit.assemble_plans(json.dumps([
        {"draft_id": record["plan_file"].split("/")[1],
         "expected_plan_sha256": record["plan_sha256"], "floor_id": floor,
         "z_floor": z, "evidence": "Synthetic repeated plan."}
        for floor, z in (("F1", 0), ("F2", 3))]))
    assert assembled["source_geometry_ready"], assembled
    assert "architectural_regularization" not in assembled
    for built in (result, assembled):
        source = _read(toolkit.run / built["candidate"] / "source_model.json")
        assert not source_grid_report(source, .1)["violations"]


def test_adoption_does_not_hide_real_storey_overlap(tmp_path):
    toolkit = Toolkit(drawing_run(tmp_path))
    value = rotated_plan()
    value["instances"].append({"id": "F2", "template": "typical", "z": 2, "height": 3})
    result = build_parametric(toolkit, json.dumps(value))
    assert not result.get("source_geometry_ready"), result
