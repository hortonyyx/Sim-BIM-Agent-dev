"""Reader-unit parents keep sealed definitions without a redundant MCP process."""
from __future__ import annotations

import asyncio
import builtins
import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.tool_scripts import run_bim_agent as runner
from src.agent import runtime_tools
from src.agent.runtime_roles import entry
from src.agent.runtime_roles.plan_format import READER_PLAN_EXAMPLE
from src.agent_runtime import agent_registry
from src.agent_runtime.adapter import ScriptedAdapter
from src.harness_contracts.budget import BudgetAmounts
from test_agent_runtime import response


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sealed_checkout(monkeypatch):
    """Use an in-memory test seal; never register or write a product registry.

    The existing catalog hashes remain authoritative. Only fixture source-file
    hashes cover the current development checkout, and every production release
    verification still discovers and hashes its files.
    """
    registry = agent_registry.load_agent_registry(ROOT)
    for line in ("runtime", "domain"):
        record = copy.deepcopy(registry[f"{line}_versions"][registry[f"current_{line}_version"]])
        record["files"] = agent_registry.discover_files(ROOT, line, scope=registry["scope"])
        identity = f"{line}-test-reader-catalog"
        registry[f"{line}_versions"][identity] = record
        registry[f"current_{line}_version"] = identity
    load = agent_registry.load_agent_registry
    monkeypatch.setattr(agent_registry, "load_agent_registry", lambda root, registry_path=None:
        copy.deepcopy(registry) if Path(root).resolve() == ROOT else load(root, registry_path))
    checks = []
    verify = agent_registry._verify_files

    def observed_verify(root, record, **kwargs):
        checks.append(kwargs["line"])
        return verify(root, record, **kwargs)

    monkeypatch.setattr(agent_registry, "_verify_files", observed_verify)
    return registry, checks


def prepared_run(path, *, mesh=False):
    (path / "images").mkdir(parents=True)
    image = path / "images/plan.png"
    Image.new("RGB", (800, 600), "white").save(image)
    manifest = {"plan_regularization_rule": "plan_regularization_v1", "max_candidates": 24, "images": {"plan.png": {"size": [800, 600],
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}, "scope": "offline catalog test"}
    if mesh:
        manifest["mesh_input"] = {"sha256": "0" * 64, "frozen_path": "assets/input.glb"}
    (path / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def file_bytes(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("mesh", [False, True])
def test_shared_factory_matches_real_frozen_mcp_without_preparation_writes(tmp_path, sealed_checkout, readonly, mesh):
    async def scenario():
        run = prepared_run(tmp_path / "run", mesh=mesh)
        before = file_bytes(run)
        server = runner.build_tool_server(run, readonly=readonly)
        local = [tool.model_dump(mode="json", exclude_none=True) for tool in await server.list_tools()]
        assert file_bytes(run) == before and not (run / ".harness_tmp").exists()
        timing = None
        if not readonly and not mesh:
            started = time.perf_counter()
            parent = runtime_tools.FrozenBimCatalog(run, repository_root=ROOT)
            constructed = time.perf_counter()
            assert await parent.list_tools() == local
            listed = time.perf_counter()
            timing = {"scope": "one synthetic input, warm test parent, fresh MCP process, no provider",
                      "catalog_only": {"construct_seconds": constructed - started,
                                       "list_seconds": listed - constructed,
                                       "total_seconds": listed - started}}
        started = time.perf_counter()
        async with runtime_tools.frozen_bim_client(run, readonly=readonly, repository_root=ROOT) as client:
            constructed = time.perf_counter()
            remote = await client.list_tools()
            listed = time.perf_counter()
        if timing is not None:
            timing["frozen_mcp"] = {"construct_seconds": constructed - started,
                                    "list_seconds": listed - constructed,
                                    "total_seconds": listed - started}
            (tmp_path / "startup_timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
        assert local == remote
        runtime_tools.validate_frozen_catalog(local, readonly=readonly, mesh=mesh)

    asyncio.run(scenario())


def test_catalog_only_refuses_every_execution_and_does_not_share_mutable_catalogs(tmp_path, sealed_checkout):
    async def scenario():
        first = prepared_run(tmp_path / "first")
        second = prepared_run(tmp_path / "second", mesh=True)
        before = file_bytes(first)
        parent = runtime_tools.FrozenBimCatalog(first, repository_root=ROOT)
        tools = runtime_tools.FrozenBimTools(parent, runtime_tools.coordinator_role(BudgetAmounts(calls=3)),
                                          run_directory=first)
        original = await parent.list_tools()
        changed = await parent.list_tools()
        changed[0]["description"] = "not a shared definition"
        assert await parent.list_tools() == original
        for name in ("inputs", "view_image", "build_bim"):
            with pytest.raises(runtime_tools.ToolAccessDenied, match="cannot execute"):
                await parent.call_tool(name, {})
            with pytest.raises(runtime_tools.ToolAccessDenied, match="cannot execute"):
                await tools.call_tool(name, {})
        other = runtime_tools.FrozenBimCatalog(second, repository_root=ROOT)
        assert "view_mesh" not in {t["name"] for t in original}
        assert "view_mesh" in {t["name"] for t in await other.list_tools()}
        assert file_bytes(first) == before and not (first / ".harness_tmp").exists()

    asyncio.run(scenario())


def test_unsealed_parent_is_refused_before_catalog_construction(tmp_path, monkeypatch, sealed_checkout):
    registry, checks = sealed_checkout
    record = registry["domain_versions"][registry["current_domain_version"]]
    record["files"]["src/agent/runtime_tools.py"]["sha256"] = "0" * 64

    def unexpected(*args, **kwargs):
        pytest.fail("unsealed code must not construct a tool catalog")

    monkeypatch.setattr(runner, "build_tool_server", unexpected)
    with pytest.raises(runtime_tools.ToolCatalogMismatch, match="file mismatch"):
        runtime_tools.FrozenBimCatalog(prepared_run(tmp_path / "run"), repository_root=ROOT)
    assert checks == ["runtime", "domain"]


def test_stdio_keeps_windows_scipy_import_before_shared_builder_and_transport(monkeypatch):
    steps = []
    original_import = builtins.__import__

    def observed_import(name, *args, **kwargs):
        if name == "scipy.signal":
            steps.append("scipy")
        return original_import(name, *args, **kwargs)

    def build(run, *, readonly):
        assert steps and set(steps) == {"scipy"}
        assert readonly
        steps.append("build")
        return SimpleNamespace(run=lambda: steps.append("stdio"))

    monkeypatch.setattr(builtins, "__import__", observed_import)
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runner, "build_tool_server", build)
    runner.serve(Path("unused"), readonly=True)
    assert steps[-2:] == ["build", "stdio"]
    assert steps[:-2] and set(steps[:-2]) == {"scipy"}


def arguments(tmp_path, *, unit):
    images = tmp_path / "inputs"
    images.mkdir()
    for name in ("plan.png", "unselected.png"):
        Image.new("RGB", (800, 600), "white").save(images / name)
    script = tmp_path / "script.json"
    script.write_text("{}", encoding="utf-8")
    routes = {role: {"provider": "scripted", "model": "scripted-model", "output_tokens": 4000}
              for role in ("coordinator", "plan_reader", "elevation_reader")}
    task = {"task_id": "plan-unit", "role_id": "plan_reader", "image": "plan.png", "target": "F1"}
    return entry.parser().parse_args(["--out", str(tmp_path / "out"), "--run-root", str(tmp_path),
        "--images", str(images), "--provider", "scripted", "--model", "scripted-model", "--script", str(script),
        "--roles-json", json.dumps(routes), "--seconds", "120", "--model-calls", "3" if unit else "1",
        "--tool-calls", "4", "--tokens", "100000000",
        *(["--reader-task-json", json.dumps(task)] if unit else [])])


def test_reader_unit_completes_with_real_child_mcp_and_preserved_seals_budget_and_audit(
        tmp_path, monkeypatch, sealed_checkout):
    registry, checks = sealed_checkout
    args = arguments(tmp_path, unit=True)
    dispatched, opened = [], []
    real_client = runtime_tools.frozen_bim_client

    def no_parent_transport(*args, **kwargs):
        pytest.fail("reader unit must not start a coordinator MCP process")

    def child_client(run_directory, readonly=False, repository_root=None, **kwargs):
        opened.append((Path(run_directory), readonly))
        return real_client(run_directory, readonly=readonly, repository_root=repository_root, **kwargs)

    monkeypatch.setattr(entry, "frozen_bim_client", no_parent_transport)
    monkeypatch.setattr(runtime_tools, "frozen_bim_client", child_client)

    def factory(task_id, config, store):
        dispatched.append(task_id)
        return ScriptedAdapter([
            response(("view", "view_image", {"name": "plan.png", "box": [0, 0, 800, 600]})),
            response(("trial", "trial_plan_bim", {"plan": copy.deepcopy(READER_PLAN_EXAMPLE)})),
            response(("submit", "submit_plan_reading", {"trial_id": "latest", "local_review": {
                "summary": "Offline synthetic fixture checks the actual returned plan and overlay."},
                "notes": [{"item": "axis_orientation", "kind": "assumption",
                           "basis": "Absolute north is not established by the synthetic input."}]})),
        ])

    result = asyncio.run(entry.execute(args, adapter_factory=factory))
    assert result["status"] == "completed", result
    assert dispatched == ["plan-unit"] and len(opened) == 1 and opened[0][1] is True
    out, child = tmp_path / "out", opened[0][0]
    assert child.parent.parent == out / "tasks"
    assert not (out / "bim/.harness_tmp").exists()
    assert (child / ".harness_tmp").is_dir()
    assert not (child / "trial_workspace/.harness_tmp").exists()
    assert {p.name for p in (child / "images").iterdir()} == {"plan.png"}
    assert (child / "images/plan.png").read_bytes() == (tmp_path / "inputs/plan.png").read_bytes()
    assert result["execution_scope"] == "reader_unit" and not result["whole_building_delivered"]
    assert result["role_accounting"]["requests"] == 3
    assert result["role_accounting"]["by_role"]["coordinator"]["requests"] == 0
    assert result["role_accounting"]["by_role"]["plan_reader"]["tool_calls"] == 3
    assert checks == ["runtime", "domain"] * 10
    root_inputs = json.loads((out / "bim/inputs.json").read_bytes())
    timing = json.loads((child.parent / "reader_timing.json").read_bytes())
    assert root_inputs["started_epoch"] <= timing["started_epoch"]
    assert timing["deadline_epoch"] <= root_inputs["deadline_epoch"]
    assert not list(out.rglob("source_model.json"))
    submitted = json.loads((child / "reader_submission.json").read_bytes())
    assert submitted["validation"]["notes"][0]["item"] == "trial.axis_orientation"
    assert "axis_orientation" not in submitted["artifact"]["plan"]
    catalog = json.loads((out / "frozen/coordinator_tools.json").read_bytes())
    origin = catalog["definition_source"]
    assert origin["kind"] == "in_process_definition_factory"
    assert origin["stdio_server_started"] is False and origin["tool_execution_allowed"] is False
    manifest = json.loads((out / "frozen/tool_catalog_manifest.json").read_bytes())
    assert manifest["catalogs"]["coordinator"]["definition_source"] == origin
    domain = registry["domain_versions"][registry["current_domain_version"]]
    assert catalog["definitions_sha256"] == domain["tool_catalog_sha256"]["coordinator"]
    versions = json.loads((out / "versions.json").read_bytes())
    route_blob = versions["model_route"]["evidence"]["blob"]
    route = json.loads((out / route_blob["uri"]).read_bytes())
    assert route["parent_catalog_source"] == origin
    assert versions["domain_version"]["identifier"] == registry["current_domain_version"]
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    assert {event["task_id"] for event in events} == {"plan-unit"}
    assert all(event["parent_task"]["task_id"] == "coordinator" for event in events)


def test_ordinary_role_entry_keeps_real_parent_mcp_and_no_catalog_only_origin(tmp_path, monkeypatch, sealed_checkout):
    args = arguments(tmp_path, unit=False)
    opened, dispatched = [], []
    real_client = entry.frozen_bim_client

    def parent_client(run_directory, **kwargs):
        opened.append(Path(run_directory))
        return real_client(run_directory, **kwargs)

    def unexpected_catalog(*args, **kwargs):
        pytest.fail("ordinary role runs must keep the live parent MCP path")

    monkeypatch.setattr(entry, "frozen_bim_client", parent_client)
    monkeypatch.setattr(entry, "FrozenBimCatalog", unexpected_catalog)

    def factory(task_id, config, store):
        dispatched.append(task_id)
        return ScriptedAdapter([response(("inputs", "inputs", {}))])

    result = asyncio.run(entry.execute(args, adapter_factory=factory))
    out = tmp_path / "out"
    assert opened == [out / "bim"] and dispatched == ["coordinator"]
    assert (out / "bim/.harness_tmp").is_dir()
    assert result["role_accounting"]["by_role"]["coordinator"]["tool_calls"] == 1
    assert "reader" not in result and result.get("execution_scope") != "reader_unit"
    catalog = json.loads((out / "frozen/coordinator_tools.json").read_bytes())
    assert "definition_source" not in catalog
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    assert {event["task_id"] for event in events} == {"coordinator"}
