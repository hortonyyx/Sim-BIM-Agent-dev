"""Explicit declarations are frozen without following embedded paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from src.agent.bim_inputs import prepare_bim_inputs


def _images(folder: Path) -> Path:
    folder.mkdir()
    Image.new("RGB", (12, 8), "white").save(folder / "plan.png")
    Image.new("RGB", (10, 6), "gray").save(folder / "east.png")
    return folder


def _prepare(run, images, declaration=None):
    return prepare_bim_inputs(run, images_path=images, mesh_path=None,
        building_input_path=declaration, scope="explicit input test",
        image_kind="drawings", max_candidates=4)


def test_freezes_exact_json_and_exposes_structured_declaration(tmp_path):
    images = _images(tmp_path / "images")
    building_path = tmp_path / "formal declaration.json"
    declared = {"name": "建筑", "floors": 1,
        "Floor plans": [{"floor": 1, "path": "case/plan.png"}],
        "East view path": "elsewhere/east.png",
        "Unprovided view path": "private/never-read.png"}
    raw = (json.dumps(declared, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    building_path.write_bytes(raw)
    out = tmp_path / "run"
    manifest = _prepare(out, images, building_path)
    assert (out / "building_input.json").read_bytes() == raw
    assert manifest["building_input"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["building_input"]["declaration"] == declared
    assert manifest["source_input_mode"] == "original_images_with_building_declaration"
    assert manifest["input_contents"] == {
        "original_png_images": {"included": True, "count": 2},
        "building_declaration": {"included": True},
        "ground_truth_or_evaluation": {"included": False},
    }
    associations = {row["json_pointer"]: row for row in
                    manifest["building_input"]["image_path_associations"]}
    assert associations["/Floor plans/0/path"]["authorized_image"] == "plan.png"
    assert associations["/East view path"]["authorized_image"] == "east.png"
    assert associations["/Unprovided view path"]["status"] == "not_in_authorized_image_inventory"
    assert associations["/Unprovided view path"]["authorized_image"] is None
    assert manifest["building_input"]["source_path"] == str(building_path.resolve())
    assert "scripts/tool_scripts/bim_agent_inputs.py" in manifest["implementation_sha256"]


def test_omitted_declaration_does_not_read_neighbor_json(tmp_path):
    images = _images(tmp_path / "images")
    sentinel = "MUST_NOT_ENTER_PNG_ONLY_RUN"
    (images / "private.json").write_text(json.dumps({"secret": sentinel}), encoding="utf-8")
    out = tmp_path / "run"
    manifest = _prepare(out, images)
    assert manifest["source_input_mode"] == "original_images_only"
    assert manifest["input_contents"]["building_declaration"] == {"included": False}
    assert "building_input" not in manifest
    assert not (out / "building_input.json").exists()
    assert set(path.name for path in (out / "images").iterdir()) == {"plan.png", "east.png"}
    assert sentinel not in (out / "inputs.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("raw", [b"[]", b"null", b"not-json", b"\xff"])
def test_declaration_requires_utf8_json_object(tmp_path, raw):
    images = _images(tmp_path / "images")
    source = tmp_path / "bad.json"
    source.write_bytes(raw)
    with pytest.raises(ValueError, match="building input"):
        _prepare(tmp_path / "run", images, source)
    assert not (tmp_path / "run/inputs.json").exists()
    assert source.read_bytes() == raw
