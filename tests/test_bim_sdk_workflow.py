"""Composed saves retain native gates, evidence and partial-operation outcomes."""
import copy
import hashlib
import json
from pathlib import Path
import re

from PIL import Image
import pytest

from src.agent.runtime_entry import prepare_inputs
from src.agent.sdk import BimSession, main
from tests.test_drawing_source_precision import rotated_plan


def test_default_entry_examples_compile_with_real_kernel(tmp_path):
    from src.agent.execution.source_proposal import export_source_proposal
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal
    entry = Path(__file__).resolve().parents[1] / "src/agent/SDK.md"
    blocks = re.findall(r"```python\n(.*?)\n```", entry.read_text(encoding="utf-8"), re.S)
    example = next(block for block in blocks if block.startswith("plan ="))
    values = {}
    exec(example, values)
    result = export_source_proposal(expand_parametric_proposal(values["plan"]), tmp_path / "base")
    assert result["source_geometry_ready"], result
    roof = copy.deepcopy(values["plan"])
    roof["instances"] = [{"id": "R", "template": "typical", "z": 0, "height": 3}]
    roof["templates"]["typical"].update(spaces=[{
        "id": "deck", "role": "office", "rect": [0, 0, 12, 8],
        "source_refs": ["Synthetic documentation fixture"]}], doors=[], window_rows=[])
    values["plan"] = roof
    exec(next(block for block in blocks if block.startswith('plan["enclosure_declaration"]')), values)
    result = export_source_proposal(expand_parametric_proposal(roof), tmp_path / "enclosure")
    assert result["source_geometry_ready"], result


def _run(tmp_path, strict=False):
    images = tmp_path / "originals"
    images.mkdir()
    for name in ("plan.png", "section.png"):
        Image.new("RGB", (240, 240), "white").save(images / name)
    run, _, _ = prepare_inputs(tmp_path / "execution", images=images, mesh=None,
        building_input=None, scope="Synthetic protocol fixture; no drawing-fidelity claim.",
        image_kind="drawings", floor_plan_images=["plan.png"],
        max_candidates=5, process_reviews=strict)
    return run


def _drawing(image="plan.png"):
    return {"image": image, "floor_id": "F1", "x_anchors": [[0, -4], [239, 8]],
            "y_anchors": [[0, 8], [239, -4]], "basis": "Synthetic axes for evidence plumbing."}


def _review(kind, **fields):
    return {"kind": kind, "outcome": "consistent", "summary": "Synthetic protocol fixture only.",
            "issues": [], **fields}


@pytest.mark.parametrize("strict", [False, True])
def test_build_views_and_finish_preserve_actual_review_policy(tmp_path, strict):
    run = _run(tmp_path, strict)
    declaration, drawing = rotated_plan(), _drawing()
    original = copy.deepcopy(declaration)
    with BimSession(run) as bim:
        reference = bim.image("section.png", coordinate_grid=False)
        assert reference.images and reference.require_ok()["view_id"]
        image_receipt = json.loads(Path(reference.receipt_file).read_bytes())
        assert image_receipt["arguments"]["name"] == "section.png"
        saved = bim.build(declaration, drawings=[drawing]).require_ready()
        assert saved.images and saved.candidate == "candidate_01"
        assert len(saved.views) == 1 and not saved.views[0].is_error
        assert not list((run / "process_reviews").glob("*.json"))
        assert not (run / "delivery.json").exists()
        assert saved.process_review is not None if strict else saved.process_review is None
        if strict:
            assert set(saved.process_review["missing_images"]) == {"plan.png", "section.png"}
        source = (run / saved.candidate / "source_model.json").read_bytes()
        output = tmp_path / "output"
        with pytest.raises((RuntimeError, ValueError)):
            bim.finish_to(output, saved.candidate)
        assert not output.exists()
        reviews = [_review("plan", image="plan.png", floor_id="F1"), _review("building")]
        if strict:
            with pytest.raises((RuntimeError, ValueError)):
                bim.finish_to(output, saved.candidate, reviews=reviews)
            assert not output.exists()
            reviews.append(_review("reference", image="section.png", view_id=reference.data["view_id"]))
        # These explicit fixture judgments exercise binding only. Production
        # callers must actually inspect the returned images before supplying them.
        result = bim.finish_to(output, saved.candidate, reviews=reviews)
        assert Path(result["source_model"]).read_bytes() == source
        assert json.loads(Path(saved.receipt_file).read_bytes())["automatic_review"] is False
    assert declaration == original and drawing == _drawing()
    assert len(list(run.glob("candidate_*"))) == 1


def test_bad_request_is_rejected_before_save_and_bad_geometry_does_not_project(tmp_path):
    run = _run(tmp_path)
    with BimSession(run) as bim:
        with pytest.raises(ValueError, match="drawings"):
            bim.build(rotated_plan(), drawings=[{**_drawing(), "box": [0, 0, 20, 20]}])
        assert not list(run.glob("candidate_*"))
        broken = rotated_plan()
        broken["templates"]["typical"]["doors"][0]["p1"] = [50, 50]
        result = bim.build(broken, drawings=[_drawing()])
        assert not result.source_geometry_ready and not result.requested_views_complete
        assert not result.views and not list((run / "image_overlays").glob("*.json"))
        with pytest.raises(RuntimeError):
            result.require_ready()


def test_projection_failure_retains_candidate_and_retry_only_projects(tmp_path):
    run = _run(tmp_path)
    invalid = _drawing("section.png")
    invalid["x_anchors"] = [[0, 0], [0, 8]]
    with BimSession(run) as bim:
        result = bim.build(rotated_plan(), drawings=[_drawing(), invalid])
        assert result.source_geometry_ready and not result.requested_views_complete
        assert result.candidate and len(result.views) == 2
        assert result.views[0].is_error is False and result.views[1].is_error is True
        assert "Do not rebuild" in str(result)
        path = run / result.candidate / "source_model.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        bim.call("overlay_candidate", candidate=result.candidate, **_drawing("section.png")).require_ok()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        assert len(list(run.glob("candidate_*"))) == 1
        receipts = [json.loads(p.read_bytes()) for p in (run / "sdk/receipts").glob("*.json")]
        assert sum(row["tool"] == "build_parametric_bim" for row in receipts) == 1
        assert not (run / "delivery.json").exists()


def test_interrupted_projection_records_unknown_outcome_without_retry(tmp_path, monkeypatch):
    run = _run(tmp_path)
    with BimSession(run) as bim:
        actual = bim.call
        def interrupted(tool, **arguments):
            if tool == "overlay_candidate":
                raise OSError("synthetic interruption after save")
            return actual(tool, **arguments)
        monkeypatch.setattr(bim, "call", interrupted)
        with pytest.raises(OSError, match="synthetic interruption") as caught:
            bim.build(rotated_plan(), drawings=[_drawing()])
        record = json.loads(next((run / "sdk/workflows").glob("*.json")).read_bytes())
        assert record["candidate"] == "candidate_01" and record["source_geometry_ready"]
        assert not record["requested_views_complete"]
        assert record["exception"]["outcome"].startswith("unknown")
        assert "Do not blindly retry" in caught.value.__notes__[0]
        assert len(list(run.glob("candidate_*"))) == 1


def test_cli_inline_arguments_are_explicit_and_ambiguous_file_input_is_helpful(tmp_path, capsys):
    run = _run(tmp_path)
    args = json.dumps({"name": "plan.png", "coordinate_grid": False})
    assert main(["--run", str(run), "call", "view_image", "--arguments-json", args]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["images"] and result["data"]["view_id"]
    count = len(list((run / "sdk/receipts").glob("*.json")))
    with pytest.raises(SystemExit) as caught:
        main(["--run", str(run), "call", "view_image", "--arguments", args])
    assert caught.value.code == 2 and "file path" in capsys.readouterr().err
    assert len(list((run / "sdk/receipts").glob("*.json"))) == count
