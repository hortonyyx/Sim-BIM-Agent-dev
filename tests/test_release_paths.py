"""Release identity and explicit external case/run paths; no model requests."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from src.agent_runtime.agent_registry import (
    AgentVersionMismatch, REGISTRY_RELATIVE_PATH, discover_files,
    release_records, source_commit,
)
from src.agent_runtime.run_paths import resolve_run_output


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _release(tmp_path):
    root = tmp_path / "product"
    for relative in ("src/agent_runtime/loop.py", "src/agent_runtime/model_profiles.json",
                     "src/harness_contracts/__init__.py", "src/agent/runtime_entry.py",
                     "src/agent/runtime_roles/entry.py", "src/agent/tool.py", "uv.lock", "README.txt"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    scope = {"runtime": ["src/agent_runtime", "src/harness_contracts", "uv.lock"], "domain": ["src/agent"]}
    registry = {"schema_version": "agent-version-registry.v2", "scope": scope, "aliases": {}}
    for line in ("runtime", "domain"):
        version = f"{line}-v1-20261011"
        registry[f"current_{line}_version"] = version
        registry[f"{line}_versions"] = {version: {"source_commit": "historical-registration",
            "coverage": "automatic_directories", "files": discover_files(root, line, scope=scope)}}
    _write_json(root / REGISTRY_RELATIVE_PATH, registry)
    manifest = {"schema_version": "sim-bim-release.v1", "source_commit": "a" * 40,
        "runtime_version": registry["current_runtime_version"],
        "domain_version": registry["current_domain_version"],
        "files": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in root.rglob("*") if path.is_file()}}
    _write_json(root / "release-manifest.json", manifest)
    return root, manifest


def test_release_identity_and_version_evidence_do_not_require_git(tmp_path):
    from src.agent_runtime.store import EventStore
    from src.agent_runtime.versions import make_versions
    from src.harness_contracts import BudgetAmounts

    root, manifest = _release(tmp_path)
    with patch("src.agent_runtime.agent_registry._git", side_effect=AssertionError("Git called")):
        assert source_commit(root) == manifest["source_commit"]
        releases = release_records(root)
        assert releases["runtime"]["version_id"] == manifest["runtime_version"]
        # Release provenance is the exported commit, not the historical commit
        # at which an unchanged version record happened to be registered.
        with EventStore(tmp_path / "runs" / "one", run_id="one", task_id="coordinator",
                        budget_limit=BudgetAmounts(tokens=100, calls=1)) as store:
            versions = make_versions(store, root=root, prompt="offline", tools=[],
                parameters={"max_tokens": 100}, route={"route_id": "offline", "model": "test"})
            assert versions.code_commit.identifier == manifest["source_commit"]
            assert versions.domain_version.identifier == manifest["domain_version"]


def test_identity_read_does_not_rescan_and_full_gate_hashes_each_payload_once(tmp_path):
    from src.agent_runtime import agent_registry

    root, manifest = _release(tmp_path)
    with patch.object(agent_registry, "_sha256", wraps=agent_registry._sha256) as digest:
        source_commit(root)
        assert digest.call_count == 0
        release_records(root)
        paths = [call.args[0].relative_to(root).as_posix() for call in digest.call_args_list]
        assert sorted(paths) == sorted(manifest["files"])


@pytest.mark.parametrize("change", ["registered", "extra", "missing", "member", "version", "registry"])
def test_release_gate_rejects_changed_or_incomplete_payload(tmp_path, change):
    root, manifest = _release(tmp_path)
    if change == "registered":
        (root / "src/agent/tool.py").write_text("changed", encoding="utf-8")
    elif change == "extra":
        (root / "README.txt").write_text("changed", encoding="utf-8")
    elif change == "missing":
        (root / "README.txt").unlink()
    elif change == "member":
        (root / "src/agent/new.py").write_text("new", encoding="utf-8")
    elif change == "version":
        manifest["domain_version"] = "domain-v999-20261011"
        _write_json(root / "release-manifest.json", manifest)
    else:
        path = root / REGISTRY_RELATIVE_PATH
        registry = json.loads(path.read_bytes())
        registry["release_note"] = "changed without updating release identity"
        _write_json(path, registry)
    with pytest.raises(AgentVersionMismatch):
        release_records(root)


@pytest.mark.parametrize("relative", ["../escape.py", "/absolute.py", "C:/outside.py", "a\\b.py", "a/./b.py"])
def test_manifest_paths_cannot_escape_or_use_host_specific_aliases(tmp_path, relative):
    root, manifest = _release(tmp_path)
    manifest["files"][relative] = "b" * 64
    _write_json(root / "release-manifest.json", manifest)
    with pytest.raises(AgentVersionMismatch, match="payload path"):
        source_commit(root)


def test_manifest_is_required_for_no_git_paths_and_never_silently_ignored(tmp_path):
    root, _ = _release(tmp_path)
    path = root / "release-manifest.json"
    path.write_text("broken json", encoding="utf-8")
    with patch("src.agent_runtime.agent_registry._git", side_effect=AssertionError("Git called")):
        with pytest.raises(AgentVersionMismatch, match="cannot be read"):
            source_commit(root)
        with pytest.raises(AgentVersionMismatch):
            resolve_run_output(Path("case"), repository_root=root, run_root=tmp_path / "runs")
    path.unlink()
    with pytest.raises(ValueError, match="checkout or a release manifest"):
        resolve_run_output(Path("case"), repository_root=root, run_root=tmp_path / "runs")


def test_release_gate_requires_covered_members_and_bundled_registry(tmp_path, monkeypatch):
    root, manifest = _release(tmp_path)
    alternate = tmp_path / "alternate.json"
    alternate.write_bytes((root / REGISTRY_RELATIVE_PATH).read_bytes())
    monkeypatch.setenv("BIM_AGENT_REGISTRY_PATH", str(alternate))
    with pytest.raises(AgentVersionMismatch, match="bundled"):
        release_records(root)
    monkeypatch.delenv("BIM_AGENT_REGISTRY_PATH")
    del manifest["files"]["src/agent/tool.py"]
    _write_json(root / "release-manifest.json", manifest)
    with pytest.raises(AgentVersionMismatch, match="registered file"):
        release_records(root)


def test_release_extra_resource_must_not_be_a_link(tmp_path):
    root, _ = _release(tmp_path)
    path = root / "README.txt"
    target = tmp_path / "external.txt"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(AgentVersionMismatch, match="linked path"):
        release_records(root)


def test_release_output_uses_explicit_external_root_without_git(tmp_path):
    root, _ = _release(tmp_path)
    runs = tmp_path / "runs"
    with patch("src.agent_runtime.run_paths.subprocess.check_output", side_effect=AssertionError("Git called")):
        assert resolve_run_output(Path("one"), repository_root=root, run_root=runs) == runs / "one"
        with pytest.raises(ValueError, match="external run_root"):
            resolve_run_output(root / "runs/one", repository_root=root)
        with pytest.raises(ValueError, match="external run_root"):
            resolve_run_output(Path("one"), repository_root=root, run_root=root / "runs")
        with pytest.raises(ValueError, match="external run_root"):
            resolve_run_output(root / "one", repository_root=root, run_root=tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            resolve_run_output(Path("../one"), repository_root=root, run_root=runs)
        with pytest.raises(ValueError, match="filesystem root"):
            resolve_run_output(Path("one"), repository_root=root, run_root=Path(root.anchor))
        foreign = runs / "another-repository"
        foreign.mkdir(parents=True)
        (foreign / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        with pytest.raises(ValueError, match="another worktree"):
            resolve_run_output(foreign / "one", repository_root=root, run_root=runs)


def _configuration(input_root, run_root):
    route = {"provider": "paratera", "model": "GLM-5.3-Flash",
             "reasoning_effort": "high", "output_tokens": 32000}
    common = {**route, "input_kind": "images", "image_kind": "drawings",
        "input": "one", "floor_plan_images": ["1f.png"], "scope": "offline path validation",
        "credentials_file": "private.json", "max_candidates": 2, "context_tokens": 200000,
        "limits": {"model_calls": 2, "tool_calls": 2, "seconds": 60, "tokens": None}}
    return {"schema_version": 1, "batch_id": "fixture", "approval_required_before_launch": True,
        "maximum_seconds_per_case": 60, "input_root": str(input_root), "run_root": str(run_root),
        "cases": [{**copy.deepcopy(common), "case_id": "single", "mode": "single_model", "output": "single"},
                  {**copy.deepcopy(common), "case_id": "roles", "mode": "role_division", "output": "roles",
                   "roles": {name: dict(route) for name in ("coordinator", "plan_reader", "elevation_reader")}}]}


def test_both_modes_route_external_inputs_and_shared_run_root(tmp_path, monkeypatch):
    from src.agent import runtime_configuration as configuration

    root, _ = _release(tmp_path)
    monkeypatch.setattr(configuration, "ROOT", root)
    inputs, runs = tmp_path / "inputs", tmp_path / "runs"
    (inputs / "one").mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(inputs / "one/1f.png")
    data = _configuration(inputs, runs)
    path = tmp_path / "config.json"
    _write_json(path, data)
    cases = configuration.load_configuration(path)["cases"]
    for case in cases:
        argv = configuration.argv_for(case)
        assert argv[argv.index("--images") + 1] == str(inputs / "one")
        assert argv[argv.index("--out") + 1] == str(runs / case["case_id"])
        assert argv[argv.index("--run-root") + 1] == str(runs)
        assert configuration.argv_for(case, resume=True) == [*argv, "--resume"]
    # An explicit case root overrides the common parent and may name the case itself.
    data["cases"][0].update(input_root=str(inputs / "one"), input=".")
    _write_json(path, data)
    assert configuration.load_configuration(path)["cases"][0]["input_root"] == str(inputs / "one")


def test_explicit_null_time_configuration_and_cli_are_single_model_only(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from src.agent import runtime_configuration as configuration, runtime_entry
    from src.agent.runtime_roles import entry as role_entry

    root, _ = _release(tmp_path)
    monkeypatch.setattr(configuration, "ROOT", root)
    inputs = tmp_path / "inputs"
    (inputs / "one").mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(inputs / "one/1f.png")
    data = _configuration(inputs, tmp_path / "runs")
    data["maximum_seconds_per_case"] = None
    data["cases"][0]["limits"]["seconds"] = None
    path = tmp_path / "config.json"
    _write_json(path, data)
    cases = configuration.load_configuration(path)["cases"]
    argv = configuration.argv_for(cases[0])
    assert "--no-time-limit" in argv and "--seconds" not in argv
    args = runtime_entry.parser().parse_args(argv[3:])
    assert args.seconds is None
    assert "--seconds" in configuration.argv_for(cases[1])
    with pytest.raises(ValueError, match="single_model only"):
        asyncio.run(role_entry.execute(SimpleNamespace(seconds=None)))
    data["cases"][1]["limits"]["seconds"] = None
    _write_json(path, data)
    with pytest.raises(ValueError, match="single_model only"):
        configuration.load_configuration(path)
    data["cases"] = data["cases"][:1]
    data["maximum_seconds_per_case"] = 600
    _write_json(path, data)
    with pytest.raises(ValueError, match="exceeds.*time ceiling"):
        configuration.load_configuration(path)
    data["maximum_seconds_per_case"] = None
    del data["cases"][0]["limits"]["seconds"]
    _write_json(path, data)
    with pytest.raises(ValueError, match="limits is missing: seconds"):
        configuration.load_configuration(path)


def test_single_model_image_history_configuration_reaches_cli(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from src.agent import runtime_configuration as configuration, runtime_entry

    root, _ = _release(tmp_path)
    monkeypatch.setattr(configuration, "ROOT", root)
    inputs = tmp_path / "inputs"
    (inputs / "one").mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(inputs / "one/1f.png")
    data = _configuration(inputs, tmp_path / "runs")
    data["cases"][0]["image_history_batches"] = 1
    path = tmp_path / "config.json"
    _write_json(path, data)
    cases = configuration.load_configuration(path)["cases"]
    argv = configuration.argv_for(cases[0])
    args = runtime_entry.parser().parse_args(argv[3:])
    assert args.image_history_batches == 1 and args.context
    assert "--image-history-batches" not in configuration.argv_for(cases[1])
    with pytest.raises(ValueError, match="requires --context"):
        asyncio.run(runtime_entry.execute(SimpleNamespace(image_history_batches=1, context=False)))
    data["cases"][1]["image_history_batches"] = 1
    _write_json(path, data)
    with pytest.raises(ValueError, match="single_model only"):
        configuration.load_configuration(path)
    with pytest.raises(ValueError, match="single_model only"):
        configuration.argv_for(data["cases"][1])


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1"])
def test_image_history_configuration_refuses_invalid_batch_count(value):
    from src.agent.runtime_configuration import context_arguments

    with pytest.raises(ValueError, match="positive integer"):
        context_arguments({"mode": "single_model", "image_history_batches": value})
    assert context_arguments({"mode": "single_model", "image_history_batches": None}) == []


def test_input_root_escape_and_linked_images_are_refused(tmp_path):
    from src.agent.runtime_configuration import resolve_case_input

    inputs = tmp_path / "inputs"
    case = inputs / "one"
    case.mkdir(parents=True)
    base = {"input_root": str(inputs), "input": "one", "input_kind": "images"}
    with pytest.raises(ValueError, match="absolute"):
        resolve_case_input({**base, "input_root": "relative"})
    with pytest.raises(ValueError, match="filesystem root"):
        resolve_case_input({**base, "input_root": inputs.anchor})
    with pytest.raises(ValueError, match="escapes"):
        resolve_case_input({**base, "input": "../outside"})
    image = tmp_path / "outside.png"
    image.write_bytes(b"outside")
    (case / "1f.png").symlink_to(image)
    with pytest.raises(ValueError, match="admitted case"):
        resolve_case_input(base)


def test_checkout_configuration_keeps_repository_relative_paths(tmp_path, monkeypatch):
    from src.agent import runtime_configuration as configuration

    root = tmp_path / "checkout"
    (root / "one").mkdir(parents=True)
    (root / "src/agent").mkdir(parents=True)
    (root / "src/agent/runtime_entry.py").write_text("# fixture\n", encoding="utf-8")
    (root / ".git").write_text("fixture", encoding="utf-8")
    Image.new("RGB", (2, 2), "white").save(root / "one/1f.png")
    monkeypatch.setattr(configuration, "ROOT", root)
    data = _configuration(root, root / "runs")
    data.pop("input_root")
    data.pop("run_root")
    data["cases"] = data["cases"][:1]
    path = tmp_path / "legacy.json"
    _write_json(path, data)
    with patch("src.agent_runtime.run_paths.subprocess.check_output", return_value=f"worktree {root}\n"):
        case = configuration.load_configuration(path)["cases"][0]
        argv = configuration.argv_for(case)
        assert argv[argv.index("--images") + 1] == str(root / "one")
        assert argv[argv.index("--out") + 1] == str(root / "single")
        assert "--run-root" not in argv


@pytest.mark.parametrize("contents,expected", [
    ("json_file", "must be a directory"),
    ("empty_directory", "at least one PNG"),
    ("corrupt_png", "valid readable PNG"),
    ("jpeg_named_png", "valid readable PNG"),
    ("directory_named_png", "regular file"),
])
def test_image_admission_rejects_inputs_the_runtime_cannot_open(tmp_path, contents, expected):
    from src.agent.runtime_configuration import resolve_case_input

    target = tmp_path / "input"
    if contents == "json_file":
        target.write_text("{}", encoding="utf-8")
    else:
        target.mkdir()
        image = target / "plan.png"
        if contents == "corrupt_png":
            image.write_bytes(b"not a PNG")
        elif contents == "jpeg_named_png":
            Image.new("RGB", (2, 2), "white").save(image, format="JPEG")
        elif contents == "directory_named_png":
            image.mkdir()
    with pytest.raises(ValueError, match=expected):
        resolve_case_input({"input_root": str(tmp_path), "input": target.name, "input_kind": "images"})


@pytest.mark.parametrize("name,is_directory,accepted", [
    ("model.glb", False, True), ("model.GLB", False, True),
    ("model.obj", False, False), ("model.glb", True, False),
])
def test_mesh_preflight_checks_file_and_extension_only(tmp_path, name, is_directory, accepted):
    from src.agent.runtime_configuration import resolve_case_input

    target = tmp_path / name
    if is_directory:
        target.mkdir()
    else:
        # Geometry parsing remains freeze_mesh's responsibility at execution.
        target.write_bytes(b"offline filename/availability fixture")
    case = {"input_root": str(tmp_path), "input": name, "input_kind": "mesh"}
    if accepted:
        assert resolve_case_input(case) == target
    else:
        with pytest.raises(ValueError, match=r"regular \.glb file"):
            resolve_case_input(case)
