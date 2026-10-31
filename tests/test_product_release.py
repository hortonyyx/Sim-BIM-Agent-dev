"""Current product entry and deterministic distribution boundaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image

from scripts.release.product_projection import project_sources
from scripts.release.verify_product import _input_routes_probe

ROOT = Path(__file__).resolve().parents[1]


def test_payload_keeps_runtime_resources_and_excludes_development_evidence():
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                    cwd=ROOT).decode().split("\0")
    sources = {name: (ROOT / name).read_bytes() for name in files if name and (ROOT / name).is_file()}
    selected, origins, _, _ = project_sources(sources)
    assert {"src/agent/data/room_types.json", "src/agent_runtime/model_profiles.json",
            "scripts/tool_scripts/vendor/three.min.js", "README.md", "config.mesh.example.json",
            "Start.cmd", "sim_bim.py"}.issubset(selected)
    assert not any(name.startswith(("data/", "src/agent/judge/", "src/agent/legacy/", "src/rag/",
                                   "src/mcp/", "src/converters/", "tests/", "AI_agent/")) for name in selected)
    assert "src/agent/sdk.py" not in selected
    assert "src/validator/data_model.py" not in selected
    assert origins["src/agent/execution/__init__.py"]["transform"] == "package_facade"
    assert "scripts/tool_scripts/evaluate_bim_agent.py" not in selected


def test_cli_help_does_not_import_old_pipeline_or_need_git():
    # argparse exits successfully for --help; check imports in a separate fresh process.
    result = subprocess.run([sys.executable, "-B", str(ROOT / "sim_bim.py"), "--help"],
                            capture_output=True, text=True, check=True)
    assert "doctor" in result.stdout and "demo" in result.stdout
    probe = subprocess.run([sys.executable, "-B", "-c",
        "import sys; import src.agent.product_cli; assert 'src.agent.legacy.graph' not in sys.modules; assert 'langgraph' not in sys.modules"],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize('example', ['config.example.json', 'config.opencode-go.example.json'])
def test_example_configuration_has_explicit_external_storage(tmp_path, example):
    from src.agent.runtime_configuration import load_configuration, argv_for
    value = json.loads((ROOT / "product" / example).read_bytes())
    inputs = tmp_path / "input"
    (inputs / "my-building").mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(inputs / "my-building/plan.png")
    value["input_root"] = str(inputs)
    value["run_root"] = str(tmp_path / "runs")
    config = tmp_path / "run.json"
    config.write_text(json.dumps(value), encoding="utf-8")
    case = load_configuration(config)["cases"][0]
    command = argv_for(case)
    assert command[command.index("--images") + 1] == str(inputs / "my-building")
    assert command[command.index("--run-root") + 1] == str(tmp_path / "runs")
    if case["mode"] == "role_division":
        assert "src.agent.runtime_roles.entry" in command
        routes = json.loads(command[command.index("--roles-json") + 1])
        assert routes == case["roles"]
    else:
        assert "src.agent.runtime_entry" in command
        assert "--roles-json" not in command
    if case["limits"]["seconds"] is None:
        assert "--no-time-limit" in command and "--seconds" not in command
    else:
        assert command[command.index("--seconds") + 1] == str(case["limits"]["seconds"])
        assert "--no-time-limit" not in command
    assert "--no-token-limit" in command
    assert "--context-tokens" not in command and "--compact-at-tokens" not in command
    if "image_history_batches" in case:
        assert command[command.index("--image-history-batches") + 1] == str(case["image_history_batches"])
    else:
        assert "--image-history-batches" not in command
    assert command[command.index("--provider") + 1] == case["provider"]
    assert command[command.index("--model") + 1] == case["model"]


@pytest.mark.parametrize("input_case,expected", [
    ("json_file", "must be a directory"),
    ("empty_directory", "at least one PNG"),
    ("text_floor", "admitted input filenames"),
])
def test_product_configuration_check_rejects_unusable_image_inputs(tmp_path, input_case, expected):
    from src.agent.runtime_configuration import load_configuration

    value = json.loads((ROOT / "product/config.example.json").read_bytes())
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    value.update(input_root=str(inputs), run_root=str(tmp_path / "runs"))
    case = value["cases"][0]
    if input_case == "json_file":
        (inputs / "demo-result.json").write_text("{}", encoding="utf-8")
        case["input"] = "demo-result.json"
    else:
        target = inputs / case["input"]
        target.mkdir()
        if input_case == "text_floor":
            Image.new("RGB", (2, 2), "white").save(target / "plan.png")
            (target / "notes.txt").write_text("not an admitted image", encoding="utf-8")
            case["floor_plan_images"] = ["notes.txt"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        load_configuration(path)


def test_release_probe_exercises_both_shipped_input_routes_without_models(tmp_path):
    report = asyncio.run(_input_routes_probe(ROOT / "product", tmp_path))
    assert report["passed"] and report["live_model_requests"] == 0
    drawing, mesh = report["routes"]["drawings"], report["routes"]["mesh"]
    assert drawing["mode"] == "role_division"
    assert drawing["entrypoint"] == "src.agent.runtime_roles.entry"
    assert drawing["frozen_files"] == 1
    assert mesh["mode"] == "single_model"
    assert mesh["entrypoint"] == "src.agent.runtime_entry"
    assert mesh["source_input_mode"] == "native_mesh_with_images_with_building_declaration"
    assert mesh["frozen_files"] == 3
    assert mesh["render"]["surface_pixels"] > 0
    with Image.open(mesh["render"]["image"]) as picture:
        assert picture.size == (64, 64)
    assert "whole-building reconstruction" in report["not_exercised"]
