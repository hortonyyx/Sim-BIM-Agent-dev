"""Mixed mm/m declarations stop before geometry; metre inputs keep their values."""
from __future__ import annotations


import copy
import json
from pathlib import Path

import pytest

from scripts.tool_scripts.run_bim_agent import Toolkit
from src.agent.execution.source_proposal import export_source_proposal
from src.agent.geometry.input_scale import (
    ScaleMismatchError, check_floor_placements, check_geometry_scale, check_planar_scale,
)
from src.agent.geometry.parametric_proposal import expand_parametric_proposal
from src.agent.geometry.plan_assembly import assemble_plan_proposals
from src.agent.geometry.plan_partition import compile_plan_partition
from tests.test_bim_agent_plan_partition import example
from tests.test_bim_agent_tools import _run_with_one_image
from tests.test_parametric_proposal import plan as parametric_plan
from tests.test_source_proposal import _proposal

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("span,height", [
    (4999, 3), (5000, 5.001), (10000, 11),  # Both conditions are necessary.
    (1000, 2), (10000, 100), (0.01, 300),  # Long hall, huge hall, narrow tall space.
    (25000, 3600),  # All-mm ambiguity is explicitly outside this ratio check.
])
def test_conservative_limit_does_not_become_an_absolute_size_or_aspect_gate(span, height):
    check_planar_scale([("extent", span)], [("height", height)])


@pytest.mark.parametrize("span,height", [(5000, 5), (15000, 3), (25152, 3.6)])
def test_reject_reports_both_located_numbers_and_leaves_decision_to_caller(span, height):
    with pytest.raises(ScaleMismatchError) as exc:
        check_planar_scale([("plan.x_anchors", span)], [("plan.ceiling_height", height)])
    assert f"{span:g} m" in str(exc.value) and f"={height:g} m" in str(exc.value)
    assert "plan.x_anchors" in str(exc.value) and "plan.ceiling_height" in str(exc.value)
    assert "坐标看起来是毫米，世界坐标要用米" in str(exc.value)
    assert "no automatic conversion" in str(exc.value)


def test_use_largest_explicit_floor_height_and_no_guess_when_height_unknown():
    check_planar_scale([("extent", 20000)], [("low soffit", 0.01), ("hall", 40)])
    check_planar_scale([("extent", 20000)], [("missing", None), ("malformed", "3")])


def test_plan_rejects_before_polygonization_and_keeps_raw_declaration(monkeypatch):
    import src.agent.geometry.plan_partition as compiler
    plan = example()
    for axis in ("x", "y"):
        for anchor in plan[f"{axis}_anchors"]:
            anchor[1] *= 1000
    before = copy.deepcopy(plan)
    monkeypatch.setattr(compiler, "unary_union", lambda *_: pytest.fail("polygonizer ran"))
    with pytest.raises(ScaleMismatchError, match=r"plan.x_anchors.*6000.*ceiling_height"):
        compile_plan_partition(plan, image_size=(12, 8), image_name="plan.png")
    assert plan == before




def test_tool_rejects_plan_revision_and_restored_draft_assembly_without_new_source(tmp_path):
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    good = toolkit.build_plan("plan.png", json.dumps(example()))
    assert good["source_geometry_ready"]
    original_bytes = (run / "candidate_01/source_model.json").read_bytes()
    binding = good["plan_input"]
    operations = [{"op": "set", "field": "x_anchors", "value": [[1, 0], [11, 6000]],
                   "reason": "synthetic unit error", "source_refs": ["synthetic"]}]
    result = toolkit.revise_plan("draft_001", binding["plan_sha256"], json.dumps(operations))
    assert result["status"] == "error" and not result["source_geometry_ready"]
    assert "plan.x_anchors" in result["error"] and "毫米" in result["error"]
    failed = result["plan_input"]
    raw = json.loads((run / failed["plan_file"]).read_bytes())
    assert raw["x_anchors"][1][1] == 6000  # Never silently divide by 1,000.
    floors = [dict(draft_id=d, expected_plan_sha256=s, floor_id=f, z_floor=z, evidence="synthetic")
              for d, s, f, z in [("draft_001", binding["plan_sha256"], "F1", 0),
                                ("draft_002", failed["plan_sha256"], "F2", 3)]]
    # Q1: assembly regularizes the stack first and returns the unit error as a structured rejection.
    rejected = toolkit.assemble_plans(json.dumps(floors))
    assert rejected["status"] == "error" and not rejected["source_geometry_ready"]
    assert "plan.x_anchors" in json.dumps(rejected["regularization"]["rejections"], ensure_ascii=False)
    assert list(run.glob("candidate_*")) == [run / "candidate_01"]
    assert (run / "candidate_01/source_model.json").read_bytes() == original_bytes


def test_explicit_tagged_mm_is_still_supported_and_recorded(tmp_path):
    run = _run_with_one_image(tmp_path)
    plan = example()
    for axis in ("x", "y"):
        for anchor in plan[f"{axis}_anchors"]:
            anchor[1] = {"value": anchor[1] * 1000, "unit": "mm"}
    raw = json.dumps(plan)
    result = Toolkit(run).build_plan("plan.png", raw)
    assert result["source_geometry_ready"], result
    record = result["plan_input"]
    assert (run / record["submitted_plan_file"]).read_text(encoding="utf-8") == raw
    assert json.loads((run / record["plan_file"]).read_text(encoding="utf-8"))["x_anchors"] == example()["x_anchors"]


def test_survey_origin_and_negative_anchors_do_not_trigger_absolute_coordinate_check():
    plan = example()
    for axis, offset in (("x", 5_000_000), ("y", -7_000_000)):
        for anchor in plan[f"{axis}_anchors"]:
            anchor[1] += offset
    proposal, _ = compile_plan_partition(plan, image_size=(12, 8), image_name="plan.png")
    assert proposal["geometry"]["footprint_x"] == [5_000_000, 5_000_006]
    assert proposal["geometry"]["footprint_y"] == [-7_000_000, -6_999_996]


def test_direct_and_edited_source_proposals_are_stopped_before_kernel(tmp_path, monkeypatch):
    import src.agent.execution.source_proposal as exporter
    proposal = _proposal()
    proposal["geometry"]["footprint_x"] = [0, 6000]
    before = copy.deepcopy(proposal)
    monkeypatch.setattr(exporter, "build_source_bim", lambda *_, **__: pytest.fail("kernel ran"))
    result = export_source_proposal(proposal, tmp_path / "bad")
    assert result["status"] == "error" and not result["source_geometry_ready"]
    assert "proposal.geometry.footprint_x" in result["error"]
    assert not (tmp_path / "bad/source_model.json").exists()
    assert json.loads((tmp_path / "bad/proposal.json").read_text(encoding="utf-8")) == before == proposal


def test_numeric_strings_are_checked_after_existing_schema_parsing(tmp_path, monkeypatch):
    import src.agent.execution.source_proposal as exporter
    proposal = _proposal()
    proposal["geometry"]["footprint_x"] = ["0", "6000"]
    proposal["geometry"]["floors"][0]["ceiling_height"] = "3"
    monkeypatch.setattr(exporter, "build_source_bim", lambda *_, **__: pytest.fail("kernel ran"))
    result = export_source_proposal(proposal, tmp_path / "strings")
    assert result["status"] == "error" and "ScaleMismatchError" in result["error"]
    assert "footprint_x" in result["error"] and "6000" in result["error"]
    assert json.loads((tmp_path / "strings/proposal.json").read_text(encoding="utf-8")) == proposal


def test_normal_numeric_strings_keep_schema_acceptance(tmp_path):
    proposal = _proposal()
    proposal["geometry"]["footprint_x"] = ["0", "6"]
    proposal["geometry"]["floors"][0]["ceiling_height"] = "3"
    result = export_source_proposal(proposal, tmp_path / "normal-strings")
    assert result["source_geometry_ready"], result
    assert json.loads((tmp_path / "normal-strings/proposal.json").read_text(encoding="utf-8")) == proposal


def test_only_height_used_by_the_geometry_kernel_can_support_the_scale_check():
    geometry = _proposal()["geometry"]
    geometry["footprint_x"] = [0, 6000]
    # Cell is a permissive legacy schema, but source_primitives uses the FLOOR
    # height. Unused extra metadata must not neutralize the actual 3 m height.
    geometry["floors"][0]["cells"][0]["height"] = 10000
    with pytest.raises(ScaleMismatchError, match=r"ceiling_height=3"):
        check_geometry_scale(geometry)


def test_null_polygon_uses_the_same_rectangle_as_the_kernel():
    geometry = _proposal()["geometry"]
    geometry["floors"][0]["cells"][0].update(x=[0, 6000], polygon=None)
    with pytest.raises(ScaleMismatchError, match=r"cells\[0\].x"):
        check_geometry_scale(geometry)


def test_geometry_checks_actual_floor_rings_and_cells_not_global_offset_between_floors():
    proposal = _proposal()["geometry"]
    proposal["footprint_x"] = [0, 10_000_000]
    floor = proposal["floors"][0]
    floor["footprint"] = {"vertices": [[0, 0], [6, 0], [6, 6], [0, 6]]}
    check_geometry_scale(proposal)
    floor["cells"][0]["x"] = [0, 6000]
    with pytest.raises(ScaleMismatchError, match=r"floors\[0\].cells\[0\].x"):
        check_geometry_scale(proposal)


def test_parametric_footprint_and_floor_placement_are_checked_before_expansion():
    plan = parametric_plan()
    plan["templates"]["typical"]["footprint"][1][0] = 6000
    with pytest.raises(ScaleMismatchError, match=r"templates\['typical'\].footprint"):
        expand_parametric_proposal(plan)
    plan = parametric_plan()
    plan["instances"][1]["z"] = 3000
    with pytest.raises(ScaleMismatchError, match=r"instances\[1\].z=3000"):
        expand_parametric_proposal(plan)


def test_assembly_checks_restored_proposals_and_target_floor_intervals():
    proposal, _ = compile_plan_partition(example(), image_size=(12, 8), image_name="plan.png")
    items = [dict(proposal=proposal, floor_id="F1", z_floor=0),
             dict(proposal=copy.deepcopy(proposal), floor_id="F2", z_floor=3000)]
    before = copy.deepcopy(items)
    with pytest.raises(ScaleMismatchError, match=r"items\[1\].z_floor=3000"):
        assemble_plan_proposals(items)
    assert items == before
    items[1]["z_floor"] = 3
    items[1]["proposal"]["geometry"]["footprint_x"] = [0, 6000]
    with pytest.raises(ScaleMismatchError, match=r"items\[1\].proposal.geometry.footprint_x"):
        assemble_plan_proposals(items)


def test_floor_gaps_use_adjacent_bases_and_ignore_absolute_altitude():
    rows = [(f"f{i}.z", 5_000_000 + i * 3, f"f{i}.height", 3) for i in range(2000)]
    check_floor_placements(rows)  # Total building height > 5 km is not a gap.
    check_floor_placements([("lower", -120, "h0", 4), ("upper", 200, "h1", 50)])


def test_direct_geometry_floor_placement_cannot_bypass_assembly_check():
    geometry = _proposal()["geometry"]
    geometry["floors"].append(copy.deepcopy(geometry["floors"][0]))
    geometry["floors"][1]["z_floor"] = 3000
    with pytest.raises(ScaleMismatchError, match=r"floors\[1\].z_floor=3000"):
        check_geometry_scale(geometry)
