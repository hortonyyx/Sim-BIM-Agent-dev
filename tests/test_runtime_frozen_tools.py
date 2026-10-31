"""Stage-1 checks for the frozen BIM MCP building adapter."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.agent.runtime_tools import (
    COORDINATOR_TOOL_NAMES,
    LOCAL_OBSERVER_TOOL_NAMES,
    MESH_COORDINATOR_TOOL_NAMES,
    MESH_LOCAL_OBSERVER_TOOL_NAMES,
    FrozenBimTools,
    ToolCatalogMismatch,
    coordinator_role,
    frozen_bim_client,
    local_observer_role,
    write_frozen_materials,
    write_frozen_tool_catalog,
)
from src.agent_runtime.mcp_tools import McpToolClient, McpTransportError
from src.agent_runtime.agent_registry import agent_version_record
import src.agent_runtime.mcp_tools as mcp_tools_module
from src.harness_contracts.budget import BudgetAmounts


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts/tool_scripts/run_bim_agent.py"


def _prepared_run(tmp_path: Path, *, mesh: bool = False) -> Path:
    run = tmp_path / "run"
    image_directory = run / "images"
    image_directory.mkdir(parents=True)
    image_path = image_directory / "plan.png"
    Image.new("RGB", (12, 8), "white").save(image_path)
    manifest = {"plan_regularization_rule": "plan_regularization_v1", "max_candidates": 24,
        "images": {"plan.png": {"size": [12, 8], "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()}},
        "scope": "stage-1 frozen tool test",
        "only_input": "synthetic admitted image",
    }
    if mesh:
        # Registration depends only on admitted mesh metadata. Calls that read
        # the asset remain outside this catalog-only test.
        manifest["mesh_input"] = {"sha256": "0" * 64, "frozen_path": "assets/input.glb"}
    run.joinpath("inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


def _registered_agent() -> dict:
    return agent_version_record(ROOT)


async def _catalog(run: Path, readonly: bool) -> list[dict]:
    async with frozen_bim_client(run, readonly=readonly, repository_root=ROOT) as client:
        return await client.list_tools()


def test_real_frozen_server_catalogs_roles_and_materials(tmp_path):
    async def scenario():
        registered = _registered_agent()
        run = _prepared_run(tmp_path)
        coordinator_catalog, observer_catalog = await asyncio.gather(
            _catalog(run, False), _catalog(run, True),
        )
        assert {tool["name"] for tool in coordinator_catalog} == set(COORDINATOR_TOOL_NAMES)
        assert {tool["name"] for tool in observer_catalog} == set(LOCAL_OBSERVER_TOOL_NAMES)

        coordinator = FrozenBimTools(
            _StaticClient(coordinator_catalog), coordinator_role(BudgetAmounts(calls=4)), run_directory=run,
        )
        observer = FrozenBimTools(
            _StaticClient(observer_catalog), local_observer_role(BudgetAmounts(calls=2)), run_directory=run,
        )
        await coordinator.list_tools()
        await observer.list_tools()
        assert coordinator.repeatability("build_bim") == "non_idempotent_write"
        assert observer.repeatability("view_image") == "read_only"
        assert coordinator.repeatability("inspect_candidate") == "read_only"
        assert coordinator.repeatability("read_candidate_items") == "read_only"
        assert coordinator.repeatability("check_source_space_relation") == "non_idempotent_write"
        assert coordinator.repeatability("audit_inference_candidate") == "non_idempotent_write"

        material_directory = tmp_path / "materials"
        material = write_frozen_materials(material_directory, repository_root=ROOT)
        coordinator_record = write_frozen_tool_catalog(material_directory, coordinator_catalog, readonly=False)
        observer_record = write_frozen_tool_catalog(material_directory, observer_catalog, readonly=True)
        assert material["baseline_commit"] == registered["source_commit"]
        assert len(material["references"]) > 5
        assert coordinator_record["tool_count"] == len(COORDINATOR_TOOL_NAMES)
        assert observer_record["tool_count"] == len(LOCAL_OBSERVER_TOOL_NAMES)
        saved = json.loads((material_directory / "coordinator_tools.json").read_text())
        assert saved["tools"] == coordinator_catalog
        assert len(saved["definition_sha256_by_name"]) == len(COORDINATOR_TOOL_NAMES)
        assert saved["policy_by_name"]["inspect_candidate"]["repeatability"] == "read_only"
        assert saved["policy_by_name"]["build_bim"]["repeatability"] == "non_idempotent_write"
        assert coordinator_record["definitions_sha256"] == saved["definitions_sha256"]
        assert material["prompts"]["drawing_system_prompt.txt"]["byte_count"] > 1000
        assert material["references"]["geometry"]["byte_count"] > 100
        assert material["source_sha256"]["scripts/tool_scripts/bim_agent_guidance.py"] == (
            registered["files"]["scripts/tool_scripts/bim_agent_guidance.py"]["sha256"]
        )
        assert registered["tool_catalog_sha256"]["coordinator"] == coordinator_record[
            "definitions_sha256"
        ]
        assert registered["tool_catalog_sha256"]["readonly"] == observer_record[
            "definitions_sha256"
        ]
        assert material["agent_version"] == registered["version_id"]
        assert material["source_sha256"] == {
            path: data["sha256"] for path, data in registered["files"].items()
        }
    asyncio.run(scenario())


def test_real_mesh_input_adds_exact_frozen_mesh_catalog_variants(tmp_path):
    async def scenario():
        registered = _registered_agent()
        run = _prepared_run(tmp_path, mesh=True)
        coordinator_catalog, observer_catalog = await asyncio.gather(
            _catalog(run, False), _catalog(run, True),
        )
        assert {tool["name"] for tool in coordinator_catalog} == set(MESH_COORDINATOR_TOOL_NAMES)
        assert {tool["name"] for tool in observer_catalog} == set(MESH_LOCAL_OBSERVER_TOOL_NAMES)
        assert len(coordinator_catalog) == len(MESH_COORDINATOR_TOOL_NAMES)
        assert len(observer_catalog) == len(MESH_LOCAL_OBSERVER_TOOL_NAMES)
        coordinator = FrozenBimTools(
            _StaticClient(coordinator_catalog), coordinator_role(BudgetAmounts(calls=2)),
            run_directory=run,
        )
        observer = FrozenBimTools(
            _StaticClient(observer_catalog), local_observer_role(BudgetAmounts(calls=2)),
            run_directory=run,
        )
        await coordinator.list_tools()
        await observer.list_tools()
        assert coordinator.repeatability("set_candidate_mesh_frame") == "non_idempotent_write"
        assert coordinator.repeatability("overlay_mesh_candidate") == "read_only"
        assert observer.repeatability("view_mesh") == "read_only"

        material_directory = tmp_path / "mesh_materials"
        coordinator_record = write_frozen_tool_catalog(
            material_directory, coordinator_catalog, readonly=False,
        )
        observer_record = write_frozen_tool_catalog(
            material_directory, observer_catalog, readonly=True,
        )
        assert registered["tool_catalog_sha256"]["coordinator_mesh"] == coordinator_record[
            "definitions_sha256"
        ]
        assert registered["tool_catalog_sha256"]["readonly_mesh"] == observer_record[
            "definitions_sha256"
        ]
    asyncio.run(scenario())


def test_real_readonly_call_preserves_sent_and_original_image_metadata(tmp_path):
    async def scenario():
        run = _prepared_run(tmp_path)
        role = local_observer_role(BudgetAmounts(calls=2))
        async with frozen_bim_client(run, readonly=True, repository_root=ROOT) as client:
            tools = FrozenBimTools(client, role, run_directory=run)
            reply = await tools.call_tool("view_image", {
                "name": "plan.png", "box": [1, 1, 10, 7], "coordinate_grid": False,
            })
            origins = tools.image_origins(reply)
            assert len(origins) == 1
            origin = next(iter(origins.values()))
            assert origin["transport"] == "derived_view_with_original_coordinates"
            assert Path(origin["original_path"]) == run / "images" / "plan.png"
            assert origin["box_original_pixels"] == [1, 1, 10, 7]
            assert origin["original_pixels_per_returned_pixel"] == [1.0, 1.0]
    asyncio.run(scenario())


def test_retired_tool_is_rejected_before_transport_execution(tmp_path):
    run = _prepared_run(tmp_path)

    async def scenario():
        client = _FailingWriteClient(await _catalog(run, False))
        tools = FrozenBimTools(
            client, coordinator_role(BudgetAmounts(calls=3)), run_directory=run
        )
        with pytest.raises(ToolCatalogMismatch, match="not in the registered Agent catalog"):
            await tools.call_tool("review_detail", {})
        assert client.calls == []
        with pytest.raises(ConnectionError):
            await tools.call_tool("build_bim", {"proposal_json": "{}"})
        assert client.calls == ["build_bim"]
    asyncio.run(scenario())




def test_artifacts_include_partial_viewer_and_source_bim_but_not_transport_scratch(tmp_path):
    run = _prepared_run(tmp_path)
    candidate = run / "candidate_01"
    candidate.mkdir()
    for name in ("source_model.json", "proposal.json", "report.json"):
        candidate.joinpath(name).write_text("{}", encoding="utf-8")
    candidate.joinpath("viewer.html").write_text("<html></html>", encoding="utf-8")
    run.joinpath("tools.jsonl").write_text("{}\n", encoding="utf-8")
    scratch = run / ".harness_tmp"
    scratch.mkdir()
    scratch.joinpath("transport.tmp").write_text("temporary", encoding="utf-8")
    tools = FrozenBimTools(
        _StaticClient([]), coordinator_role(BudgetAmounts(calls=1)), run_directory=run,
    )
    artifacts = {path.relative_to(run).as_posix() for path in tools.artifacts()}
    assert "candidate_01/source_model.json" in artifacts
    assert "candidate_01/proposal.json" in artifacts
    assert "candidate_01/report.json" in artifacts
    assert "candidate_01/viewer.html" in artifacts
    assert "tools.jsonl" not in artifacts
    assert ".harness_tmp/transport.tmp" not in artifacts


def test_mcp_tool_pagination_collects_pages_and_rejects_repeated_cursor(tmp_path):
    complete = McpToolClient(command="unused", run_directory=tmp_path)
    complete._session = _PagedSession([
        _page([_tool("first")], "cursor-1"),
        _page([_tool("second")], None),
    ])
    repeated = McpToolClient(command="unused", run_directory=tmp_path)
    repeated._session = _PagedSession([
        _page([_tool("first")], "cursor-1"),
        _page([_tool("second")], "cursor-1"),
    ])

    async def scenario():
        result = await complete.list_tools()
        assert [tool["name"] for tool in result] == ["first", "second"]
        assert complete._session.cursors == [None, "cursor-1"]
        with pytest.raises(McpTransportError, match="repeated pagination cursor"):
            await repeated.list_tools()
        assert repeated._session.cursors == [None, "cursor-1"]
    asyncio.run(scenario())


def test_frozen_catalog_rejects_same_names_with_changed_description(tmp_path):
    run = _prepared_run(tmp_path)

    async def scenario():
        catalog = await _catalog(run, False)
        catalog[0]["description"] += " changed"
        tools = FrozenBimTools(
            _StaticClient(catalog), coordinator_role(BudgetAmounts(calls=1)),
            run_directory=run,
        )
        with pytest.raises(ToolCatalogMismatch, match="definitions changed"):
            await tools.list_tools()
    asyncio.run(scenario())


def test_current_catalog_and_guidance_expectations_come_from_registry(monkeypatch):
    fake = {
        "version_id": "future-version",
        "source_commit": "future-commit",
        "files": {
            "scripts/tool_scripts/bim_agent_guidance.py": {"sha256": "1" * 64}
        },
        "tool_catalog_sha256": {
            "coordinator": "2" * 64,
            "readonly": "3" * 64,
            "coordinator_mesh": "4" * 64,
            "readonly_mesh": "5" * 64,
        },
    }
    monkeypatch.setitem(_registered_agent.__globals__, "agent_version_record", lambda _root: fake)
    expected = _registered_agent()
    assert expected["version_id"] == "future-version"
    assert expected["files"]["scripts/tool_scripts/bim_agent_guidance.py"]["sha256"] == "1" * 64
    assert expected["tool_catalog_sha256"]["coordinator"] == "2" * 64


def test_mcp_enter_closes_transport_when_session_initialization_fails(tmp_path, monkeypatch):
    closed = {"stdio": False, "session": False}

    @asynccontextmanager
    async def fake_stdio(_parameters):
        try:
            yield object(), object()
        finally:
            closed["stdio"] = True

    class FailingSession:
        def __init__(self, _reader, _writer):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed["session"] = True

        async def initialize(self):
            raise RuntimeError("synthetic initialize failure")

    monkeypatch.setattr(mcp_tools_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp_tools_module, "ClientSession", FailingSession)
    client = McpToolClient(command="unused", run_directory=tmp_path)

    async def scenario():
        with pytest.raises(RuntimeError, match="synthetic initialize failure"):
            await client.__aenter__()
        assert client._session is None
        assert client._stack is None
    asyncio.run(scenario())
    assert closed == {"stdio": True, "session": True}


class _StaticClient:
    def __init__(self, catalog: list[dict]):
        self.catalog = catalog

    async def list_tools(self) -> list[dict]:
        return self.catalog

    async def call_tool(self, name: str, arguments: dict) -> dict:
        raise AssertionError("this fixture is only for catalog inspection")


class _FailingWriteClient(_StaticClient):
    def __init__(self, catalog: list[dict]):
        super().__init__(catalog)
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append(name)
        raise ConnectionError("synthetic lost stdio response")


class _DumpableTool:
    def __init__(self, name: str):
        self.name = name

    def model_dump(self, **_kwargs):
        return {"name": self.name, "description": self.name, "inputSchema": {"type": "object"}}


def _tool(name: str):
    return _DumpableTool(name)


def _page(tools: list[_DumpableTool], cursor: str | None):
    return SimpleNamespace(tools=tools, nextCursor=cursor)


class _PagedSession:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.cursors = []

    async def list_tools(self, cursor=None):
        self.cursors.append(cursor)
        return next(self.pages)
