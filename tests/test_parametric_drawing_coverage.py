"""Real source overlays bind ordinary parametric floors to admitted drawings."""
import copy
import json
import shutil

import pytest

from scripts.tool_scripts.bim_agent_budget import saved_floor_status
from scripts.tool_scripts.bim_agent_parametric import build_parametric
from scripts.tool_scripts.bim_agent_process_reviews import record_reviews, process_status
from scripts.tool_scripts.run_bim_agent import Toolkit
from tests.test_drawing_source_precision import drawing_run, rotated_plan


def _review(kind="plan", **kwargs):
    return {"kind": kind, "outcome": "consistent", "issues": [],
        "summary": "Synthetic binding test only; not an independent quality verdict.", **kwargs}


def _project(toolkit, candidate):
    _, metadata = toolkit.project_overlay(candidate, "plan.png", "F1",
        [[0,-4],[239,8]], [[0,8],[239,-4]], "Synthetic original image axes.",
        trigger_action="overlay_candidate")
    toolkit._save_calibration(candidate=candidate, image="plan.png", floor_id="F1",
        x_anchors=metadata["anchors"]["x"], y_anchors=metadata["anchors"]["y"],
        basis="Synthetic original image axes.", metadata=metadata)


@pytest.fixture(scope="module")
def reviewed_run(tmp_path_factory):
    toolkit = Toolkit(drawing_run(tmp_path_factory.mktemp("parametric_review")))
    result = build_parametric(toolkit, json.dumps(rotated_plan()))
    assert result.get("source_geometry_ready"), result
    candidate = result["candidate"]
    assert not saved_floor_status(toolkit, candidate)["complete_building"]
    _project(toolkit, candidate)
    assert not saved_floor_status(toolkit, candidate)["complete_building"]
    record_reviews(toolkit, candidate, json.dumps([
        _review(image="plan.png", floor_id="F1"), _review("building")]))
    assert saved_floor_status(toolkit, candidate)["complete_building"]
    assert process_status(toolkit, candidate)["ready"]
    return toolkit.run, candidate


@pytest.fixture
def reviewed_case(tmp_path, reviewed_run):
    run, candidate = reviewed_run
    copied = tmp_path / "bim"
    shutil.copytree(run, copied)
    return Toolkit(copied), candidate


@pytest.mark.parametrize("damage", ["original", "calibration", "overlay", "picture"])
def test_evidence_change_removes_review_derived_floor_coverage(reviewed_case, damage):
    toolkit, candidate = reviewed_case
    if damage == "original":
        path = toolkit.image_path("plan.png")
    elif damage == "calibration":
        _, calibration = toolkit.registered_calibrations()[0]
        toolkit._save_calibration(candidate=candidate, image="plan.png", floor_id="F1",
            x_anchors=[[0,-4],[239,8.5]], y_anchors=calibration["y_anchors"],
            basis="Synthetic changed ruler.", metadata=json.loads(
                (toolkit.run / candidate / "source_model.json").read_bytes()))
        path = None
    else:
        row = json.loads(next((toolkit.run / "process_reviews").glob("review_*.json")).read_bytes())
        path = toolkit.run / row["evidence_file"]
        if damage == "picture":
            path = toolkit.run / json.loads(path.read_bytes())["overlay_image"]
    if path:
        path.write_bytes(path.read_bytes() + b"changed")
    assert not saved_floor_status(toolkit, candidate)["complete_building"]
    assert not process_status(toolkit, candidate)["ready"]


@pytest.mark.parametrize("change", ["xy", "height"])
def test_reuse_is_bound_to_physical_plan_scope_not_parent_id(reviewed_case, change):
    toolkit, candidate = reviewed_case
    proposal = json.loads((toolkit.run / candidate / "proposal.json").read_bytes())
    if change == "height":
        proposal["geometry"]["floors"][0]["ceiling_height"] = 3.2
    else:
        # Removing the office partition changes the reviewed XY scope while
        # retaining exactly the same floor ID and a real parent link.
        floor = proposal["geometry"]["floors"][0]
        floor["cells"] = [copy.deepcopy(floor["cells"][0])]
        floor["cells"][0]["polygon"] = floor["footprint"]["vertices"]
        proposal["geometry"]["openings"] = []
    result = toolkit.build(proposal, parent=candidate)
    assert result.get("source_geometry_ready"), result
    covered = saved_floor_status(toolkit, result["candidate"])["complete_building"]
    assert covered is (change == "height")
    # Even a reusable plan observation does not authorize the changed building.
    assert not process_status(toolkit, result["candidate"])["ready"]


def test_needs_revision_is_not_new_floor_completion_evidence(reviewed_case):
    toolkit, candidate = reviewed_case
    row = _review(image="plan.png", floor_id="F1")
    row.update(outcome="needs_revision", issues=["Synthetic located discrepancy."])
    record_reviews(toolkit, candidate, json.dumps([row]))
    assert not saved_floor_status(toolkit, candidate)["complete_building"]
    assert not process_status(toolkit, candidate)["ready"]


def test_changed_hole_and_continuous_core_invalidate_containing_floor_review(tmp_path):
    from tests.test_source_space_holes import plan
    from scripts.tool_scripts.bim_agent_process_reviews import reviewed_floor_images
    from src.agent.runtime_roles.assembly_review import floor_facts
    toolkit = Toolkit(drawing_run(tmp_path))
    value = plan()
    first = build_parametric(toolkit, json.dumps(value))
    assert first["source_geometry_ready"], first
    _project(toolkit, first["candidate"])
    record_reviews(toolkit, first["candidate"], json.dumps([_review(image="plan.png", floor_id="F1")]))
    old = json.loads((toolkit.run / first["candidate"] / "source_model.json").read_bytes())
    assert reviewed_floor_images(toolkit, old) == {"plan.png": ["F1"]}
    template = value["templates"]["typical"]
    for polygon in (template["spaces"][0]["holes"][0], template["spaces"][1]["polygon"]):
        for point in polygon:
            if point[0] == 5:
                point[0] = 5.2
    second = build_parametric(toolkit, json.dumps(value))
    assert second["source_geometry_ready"], second
    new = json.loads((toolkit.run / second["candidate"] / "source_model.json").read_bytes())
    assert floor_facts(old, "F1") == floor_facts(new, "F1")  # Historical predicate misses this edit.
    assert reviewed_floor_images(toolkit, new) == {}
    assert not saved_floor_status(toolkit, second["candidate"])["complete_building"]


def test_physical_wall_changed_to_open_invalidates_review_then_can_be_rereviewed(reviewed_case):
    from scripts.tool_scripts.bim_agent_process_reviews import reviewed_floor_images
    toolkit, candidate = reviewed_case
    source = json.loads((toolkit.run / candidate / "source_model.json").read_bytes())
    hosts = {row["host_boundary_id"] for row in source["openings"]}
    wall = next(row for row in source["boundaries"] if row["geometry_type"] == "wall"
                and not row.get("adjacent_space_ids") and row["id"] not in hosts)
    value = rotated_plan()
    value["enclosure_declaration"] = {"schema_version": "source_enclosure_intent_v1", "boundaries": [{
        "boundary": {"boundary_id": wall["id"]}, "condition": "open", "scope": "whole",
        "evidence_kind": "inferred", "source_refs": ["Synthetic facade interpretation"],
        "assumptions": ["Synthetic explicit opening, not an observed building fact"]}]}
    result = build_parametric(toolkit, json.dumps(value))
    assert result["source_geometry_ready"], result
    changed = json.loads((toolkit.run / result["candidate"] / "source_model.json").read_bytes())
    assert reviewed_floor_images(toolkit, changed) == {}
    _project(toolkit, result["candidate"])
    record_reviews(toolkit, result["candidate"], json.dumps([
        _review(image="plan.png", floor_id="F1"), _review("building")]))
    assert saved_floor_status(toolkit, result["candidate"])["complete_building"]
    assert process_status(toolkit, result["candidate"])["ready"]


def test_sdk_ordinary_drawing_overlay_review_and_finish_to_are_executable(tmp_path):
    from src.agent.sdk import BimSession
    run = drawing_run(tmp_path)
    with BimSession(run) as session:
        candidate = session.call("build_parametric_bim", plan_json=rotated_plan()).require_ok()["candidate"]
        assert not saved_floor_status(Toolkit(run), candidate)["complete_building"]
        session.call("overlay_candidate", candidate=candidate, image="plan.png", floor_id="F1",
            x_anchors=[[0,-4],[239,8]], y_anchors=[[0,8],[239,-4]],
            basis="Synthetic original image axes; no independent quality claim.").require_ok()
        # In a real run the model must inspect this returned overlay before
        # supplying its actual judgment. This fixture tests evidence plumbing.
        publication = session.finish_to(tmp_path / "published", candidate, reviews=[
            _review(image="plan.png", floor_id="F1"), _review("building")])
        assert publication["candidate"] == candidate
    assert (tmp_path / "published" / "source_model.json").is_file()
    assert saved_floor_status(Toolkit(run), candidate)["complete_building"]
