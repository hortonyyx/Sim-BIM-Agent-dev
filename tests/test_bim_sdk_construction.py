"""Construction checks and one-call publication keep native authority intact."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.agent.sdk import BimSession
from tests.test_bim_agent_mesh import _prepare
from tests.test_parametric_proposal import plan


def test_width_summary_does_not_call_a_horizontal_hatch_diagonal_a_door_width():
    from scripts.tool_scripts.bim_agent_inference import _door_fact, door_width_summary
    horizontal = {"id": "hatch", "host_boundary_id": "roof", "space_ids": ["plant"],
                  "vertices": [[0, 0, 3], [1, 0, 3], [1, 2, 3], [0, 2, 3]]}
    vertical = {"id": "door", "host_boundary_id": "wall", "space_ids": ["plant", "hall"],
                "vertices": [[0, 0, 0], [0.17, 0, 0], [0.17, 0, 2], [0, 0, 2]]}
    boundaries = {"roof": {"geometry_type": "ceiling", "vertices": horizontal["vertices"]},
                  "wall": {"geometry_type": "wall", "vertices": vertical["vertices"]}}
    facts = [_door_fact(row, boundaries, {}) for row in (horizontal, vertical)]
    summary = door_width_summary(facts)
    assert summary["unmeasured_count"] == 1
    assert summary["width_range_m"] == [0.17, 0.17]
    assert summary["smallest_measured_doors"][0]["id"] == "door"


def test_check_plan_preserves_real_failures_and_does_not_allocate_or_render(tmp_path, monkeypatch):
    from src.agent.execution import source_proposal
    _, run, _ = _prepare(tmp_path, monkeypatch)
    original = plan()
    before = copy.deepcopy(original)
    def forbidden(*args, **kwargs):
        pytest.fail("checking a construction must not render/export a candidate")
    with BimSession(run, readonly=True) as session:
        with monkeypatch.context() as scope:
            scope.setattr(source_proposal, "export_source_proposal", forbidden)
            scope.setattr(source_proposal, "source_view_geometry", forbidden)
            checked = session.check_plan(original)
        assert checked.geometry_ready, str(checked)
        assert checked.source["openings"]
        assert json.loads(str(checked))["check_only"]
        assert "boundaries" not in json.loads(str(checked))
        assert json.loads(Path(checked.receipt_file).read_bytes())["source"] == json.loads(json.dumps(checked.source))
        broken = copy.deepcopy(original)
        broken["templates"]["typical"]["doors"][0]["p1"] = [2, 0.4]
        rejected = session.check_plan(broken)
        assert not rejected.geometry_ready
        assert rejected.report.get("error") or rejected.report.get("source_validation", {}).get("findings")
    assert original == before
    assert not list(run.glob("candidate_*"))


def test_check_plan_honors_mesh_identity_and_deadline(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    value = plan()
    value["mesh_frame"] = {"mesh_sha256": "0" * 64, "yaw_degrees": 0,
        "translation_m": [0, 0, 0], "reason": "test mismatch", "source_refs": ["fixture"]}
    with BimSession(run) as session:
        checked = session.check_plan(value)
        assert not checked.geometry_ready
        assert "admitted original mesh" in checked.report["error"]
        from scripts.tool_scripts import bim_agent_budget
        monkeypatch.setattr(bim_agent_budget, "time_status", lambda _: {"remaining_seconds": 0})
        with pytest.raises(ValueError, match="deadline"):
            session.check_plan(plan())


def test_finish_to_requires_real_complete_handoff_and_preserves_bytes(tmp_path, monkeypatch):
    from src.agent.sdk_delivery import publish_finished
    from src.agent.runtime_delivery import attest_finished_handoff
    _, run, _ = _prepare(tmp_path, monkeypatch, images=True)
    with BimSession(run) as session:
        value = plan()
        manifest = json.loads((run / "inputs.json").read_bytes())
        value["mesh_frame"] = {"mesh_sha256": manifest["mesh_input"]["sha256"], "yaw_degrees": 0,
            "translation_m": [0, 0, 0], "reason": "Synthetic protocol fixture", "source_refs": ["fixture"]}
        candidate = session.call("build_parametric_bim", plan_json=value).require_ok()["candidate"]
        target = tmp_path / "public"
        with pytest.raises(ValueError, match="incomplete"):
            session.finish_to(target, candidate)
        assert not target.exists()
        observation = session.call("view_mesh").require_ok()["observation"]
        projection = session.call("overlay_candidate", candidate=candidate, image="plan.png", floor_id="F1",
            x_anchors=[[0, 0], [9, 6]], y_anchors=[[0, 5], [9, 0]], basis="Synthetic publication fixture").require_ok()
        overlay = session.call("overlay_mesh_candidate", candidate=candidate,
                               observation=observation).require_ok()
        result = session.finish_to(target, candidate, reviews={"mesh_review": {
            "coverage": "whole_building", "summary": "Synthetic protocol test, not a quality result",
            "overlay_ids": [overlay["overlay_id"]]}})
        assert Path(result["viewer"]).is_file()
        assert (target / "source_model.json").read_bytes() == (run / candidate / "source_model.json").read_bytes()
        manifest = json.loads((target / "publication.json").read_bytes())
        for name, digest in manifest["public_files"].items():
            assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
        assert (target / candidate / "proposal.json").exists()
        assert (target / candidate / "report.json").exists()
        projection_file = projection["overlay_image"]
        assert (target / projection_file).read_bytes() == (run / projection_file).read_bytes()
        saved = (run / "delivery.json").read_bytes()
        with pytest.raises(FileExistsError):
            session.finish_to(target, candidate)
        assert (run / "delivery.json").read_bytes() == saved
        finish = session.call("finish_bim", candidate=candidate)
        assert attest_finished_handoff(run, finish.data, candidate)
        source_path = run / candidate / "source_model.json"
        source_path.write_bytes(source_path.read_bytes() + b" ")
        assert attest_finished_handoff(run, finish.data, candidate) is None
        with pytest.raises(ValueError, match="stale"):
            publish_finished(run, candidate, finish, tmp_path / "tampered")
        assert not (tmp_path / "tampered").exists()
