"""SDK composes existing tools without bypassing geometry, evidence or delivery."""
import hashlib
import json
import math

import pytest
from shapely.geometry import LineString, Point

from src.agent.sdk import BimSession, edge_segment
from tests.test_bim_agent_mesh import _prepare
from tests.test_parametric_proposal import plan


def test_explicit_slanted_edge_keeps_host_and_distance():
    a, b = [-6.3792, 2.8338], [-4.4721, 1.9548]
    result = edge_segment(a, b, offset=0.25, width=0.9)
    edge = LineString([a, b])
    assert all(edge.distance(Point(result[key])) < 1e-12 for key in ("p1", "p2"))
    assert math.dist(result["p1"], result["p2"]) == pytest.approx(0.9)
    assert math.dist(a, result["p1"]) == pytest.approx(0.25)
    assert edge_segment([0, 0], [3, 4], offset=0, width=5) == {"p1": [0., 0.], "p2": [3., 4.]}


@pytest.mark.parametrize("a,b,offset,width", [
    ([0, 0], [0, 0], 0, 1), ([0, 0], [1, 0], -0.1, 0.5),
    ([0, 0], [1, 0], 0.5, 0.6), ([0, 0], [1, 0], 0, 0),
    ([False, 0], [1, 0], 0, 1), ([0, 0], [float('inf'), 0], 0, 1),
])
def test_explicit_edge_never_clips_or_guesses(a, b, offset, width):
    with pytest.raises(ValueError):
        edge_segment(a, b, offset=offset, width=width)


def test_session_images_failure_and_actual_handoff(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    with BimSession(run) as session:
        assert "review_detail" not in {item["name"] for item in session.catalog()}
        schema = session.describe("build_parametric_bim")
        schema["name"] = "tampered"
        assert session.describe("build_parametric_bim")["name"] == "build_parametric_bim"
        with pytest.raises(ValueError, match="unavailable"):
            session.call("review_detail", question="no hidden model calls")
        construction = plan()
        manifest = json.loads((run / "inputs.json").read_text(encoding="utf-8"))
        construction["mesh_frame"] = {"mesh_sha256": manifest["mesh_input"]["sha256"],
            "yaw_degrees": 0, "translation_m": [0, 0, 0],
            "reason": "Synthetic SDK plumbing, not a quality claim", "source_refs": ["fixture"]}
        result = session.call("build_parametric_bim", plan_json=construction)
        saved = result.require_ok()
        assert saved["source_geometry_ready"], result
        candidate = saved["candidate"]
        assert result.images
        assert all("data" not in picture for picture in result.images)
        assert "iVBORw0KGgo" not in str(result)
        for picture in result.images:
            from pathlib import Path
            path = Path(picture["path"])
            assert path.is_relative_to(run)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == picture["sha256"]
        receipt = json.loads(open(result.receipt_file, encoding="utf-8").read())
        assert json.loads(receipt["arguments"]["plan_json"]) == construction
        assert not receipt["model_called_by_sdk"]
        premature = session.call("finish_bim", candidate=candidate)
        # A Python wrapper must not turn a save into an accepted mesh handoff.
        assert premature.data.get("mesh_review", {}).get("complete_building") is not True
        observation = session.call("view_mesh").require_ok()["observation"]
        overlay = session.call("overlay_mesh_candidate", candidate=candidate,
                               observation=observation).require_ok()
        finished = session.call("finish_bim", candidate=candidate, reviews_json={
            "mesh_review": {"coverage": "whole_building", "summary": "Synthetic plumbing only.",
                            "overlay_ids": [overlay["overlay_id"]]}})
        assert finished.require_ok()["mesh_review"]["complete_building"]
        assert (run / "delivery.json").is_file()
        assert (run / "delivery.html").is_file()
    with pytest.raises(RuntimeError, match="closed"):
        session.call("inputs")


def test_readonly_sdk_cannot_write_and_bad_geometry_stays_rejected(tmp_path, monkeypatch):
    _, run, _ = _prepare(tmp_path, monkeypatch)
    with BimSession(run, readonly=True) as readonly:
        with pytest.raises(ValueError, match="unavailable"):
            readonly.call("build_parametric_bim", plan_json=plan())
    broken = plan()
    broken["templates"]["typical"]["doors"][0]["p1"] = [2, 0.4]
    with BimSession(run) as session:
        result = session.call("build_parametric_bim", plan_json=broken)
        assert result.is_error
        with pytest.raises(RuntimeError):
            result.require_ok()
        assert not list(run.glob("candidate_*"))
        assert list((run / "parametric_drafts").glob("draft_*.json"))


def test_named_image_argument_reaches_original_tool_in_python_and_cli(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    from src.agent.sdk import main

    _, run, _ = _prepare(tmp_path, monkeypatch, images=True)
    with BimSession(run) as session:
        reply = session.call("view_image", name="plan.png", coordinate_grid=False)
        reply.require_ok()
        assert reply.images
        receipt = json.loads(Path(reply.receipt_file).read_text(encoding="utf-8"))
        assert receipt["tool"] == "view_image"
        assert receipt["arguments"] == {"name": "plan.png", "coordinate_grid": False}
    arguments = tmp_path / "image_args.json"
    arguments.write_text(json.dumps({"name": "plan.png", "coordinate_grid": False}), encoding="utf-8")
    capsys.readouterr()
    assert main(["--run", str(run), "call", "view_image", "--arguments", str(arguments)]) == 0
    cli_reply = json.loads(capsys.readouterr().out)
    assert not cli_reply["is_error"]
    assert cli_reply["images"][0]["sha256"] == reply.images[0]["sha256"]


def test_budget_rejection_is_failure_in_python_and_cli(tmp_path, monkeypatch):
    from src.agent.sdk import main

    _, run, _ = _prepare(tmp_path, monkeypatch)
    manifest_path = run / "inputs.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["max_candidates"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with BimSession(run) as session:
        first = session.call("build_parametric_bim", plan_json=plan()).require_ok()
        source = run / first["candidate"] / "source_model.json"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        rejected = session.call("build_parametric_bim", plan_json=plan())
        assert "candidate budget exhausted" in rejected.data["error"]
        assert rejected.is_error
        with pytest.raises(RuntimeError):
            rejected.require_ok()
    arguments = tmp_path / "arguments.json"
    arguments.write_text(json.dumps({"plan_json": plan()}), encoding="utf-8")
    assert main(["--run", str(run), "call", "build_parametric_bim",
                 "--arguments", str(arguments)]) == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert len(list(run.glob("candidate_*"))) == 1
