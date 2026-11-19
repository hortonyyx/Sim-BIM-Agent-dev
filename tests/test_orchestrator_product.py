import json
from pathlib import Path

from PIL import Image
import pytest

from src.agent.runtime_configuration import argv_for, load_configuration
from src.dsh_runtime.product import host_config, verify_resume_inputs
from src.dsh_runtime.relay import native_name


def project(tmp_path, **extra):
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (8, 8)).save(images / "Ground.png")
    case = dict(case_id="sample", mode="orchestrator", provider="chatgpt-subscription",
                model="gpt-6.1-sol", output_tokens=32000, max_candidates=12, output="sample",
                materials={"images": "images", "image_notes": {"Ground.png": "Ground floor"}},
                worker={"model": "gpt-6.1-terra", "max_concurrent": 2},
                limits={"seconds": 120, "tokens": None, "model_calls": 20, "tool_calls": 100})
    case.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(dict(schema_version=1, batch_id="sample", maximum_seconds_per_case=120,
                              approval_required_before_launch=True, input_root=str(tmp_path),
                              run_root=str(tmp_path / "runs"), cases=[case])))
    return path


def test_dsh_is_default_and_worker_allowance_is_inside_whole_case(tmp_path, monkeypatch):
    path = project(tmp_path)
    case = load_configuration(path)["cases"][0]
    assert case["runtime"] == "dsh" and "Ground floor" in case["scope"]
    assert "src.dsh_runtime.product" in argv_for(case, configuration_path=path)
    monkeypatch.setattr("src.dsh_runtime.product.installed_executable", lambda _: Path("dsh"))
    config = host_config(case, tmp_path)
    assert config["worker"]["model"] == "gpt-6.1-terra"
    for key in ("model_calls", "tool_calls"):
        assert config["worker"][key] + config["limits"][key] == case["limits"][key]
    assert native_name("delegate_workers") == "mcp__sim_bim_workers__delegate_workers"


def test_same_materials_and_worker_in_native_comparison(tmp_path):
    path = project(tmp_path, runtime="native")
    case = load_configuration(path)["cases"][0]
    argv = argv_for(case)
    assert "--orchestrator" in argv and "--worker-concurrency" in argv
    assert argv[argv.index("--worker-model") + 1] == "gpt-6.1-terra"
    assert argv[argv.index("--images") + 1] == str(tmp_path / "images")


def test_unbound_material_description_is_rejected(tmp_path):
    path = project(tmp_path, materials={"images": "images", "image_notes": {"missing.png": "standard floor"}})
    with pytest.raises(ValueError, match="outside the admitted"):
        load_configuration(path)


def test_dsh_resume_checks_source_bytes_even_when_original_names_match(tmp_path):
    from src.agent.bim_inputs import digest
    image = tmp_path / "Ground.png"
    Image.new("RGB", (8, 8)).save(image)
    materials = {"images": tmp_path, "mesh": None, "building_input": None}
    manifest = {"images": {image.name: {"sha256": digest(image)}}}
    verify_resume_inputs(manifest, materials)
    Image.new("RGB", (8, 8), color="red").save(image)
    with pytest.raises(ValueError, match="configured images differ"):
        verify_resume_inputs(manifest, materials)
