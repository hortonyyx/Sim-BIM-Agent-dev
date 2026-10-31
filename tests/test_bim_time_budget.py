"""Scripted tools/model and simulated time; no model endpoint is reachable."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from scripts.tool_scripts import run_bim_agent as runner
from scripts.tool_scripts.bim_agent_budget import time_status, saved_floor_status, fallback_selection
from tests.test_bim_agent_tools import _run_with_one_image, _add_image


def _clocked_run(tmp_path):
    run = _run_with_one_image(tmp_path)
    _add_image(run, "upstairs.png")
    manifest = json.loads((run / "inputs.json").read_text())
    runner.dump(run / "inputs.json", dict(manifest, started_epoch=1000, deadline_epoch=7000,
        time_budget_seconds=6000, floor_plan_images=["plan.png", "upstairs.png"],
        floor_scope_source="explicit_input_filenames"))
    return run


def test_unlimited_manifest_reports_elapsed_and_allows_tools_after_ten_minutes(tmp_path, monkeypatch):
    async def scenario():
        run = _clocked_run(tmp_path)
        manifest = json.loads((run / "inputs.json").read_bytes())
        runner.dump(run / "inputs.json", dict(manifest, deadline_epoch=None, time_budget_seconds=None))
        monkeypatch.setattr("scripts.tool_scripts.bim_agent_budget.time.time", lambda: 1701)
        toolkit = runner.Toolkit(run)
        status = time_status(toolkit)
        assert status["active"] and status["elapsed_seconds"] == 701 and status["remaining_seconds"] is None
        assert "整案不设时间上限" in status["line"] and "未启用计时" not in status["line"]
        directory = run / ".harness_tmp"
        directory.mkdir(exist_ok=True)
        runner.dump(directory / "budget_status.json", {
            "schema_version": 1, "run_directory": str(run.resolve()), "started_epoch": 1000,
            "dimensions": {"calls": {"remaining": "3", "remaining_fraction": .5}}})
        status = time_status(toolkit)
        assert status["remaining_seconds"] is None and status["tightest_dimension"] == "calls"
        json.dumps(status, allow_nan=False)
        servers = []
        with patch.object(FastMCP, "run", lambda server: servers.append(server)):
            runner.serve(run)
        result = await servers[0].call_tool("inputs", {})
        content = result.content if hasattr(result, "content") else result[0] if isinstance(result, tuple) else result
        assert not getattr(result, "isError", False) and "整案不设时间上限" in content[-1].text
    asyncio.run(scenario())


def test_scripted_tool_returns_at_half_near_and_hard_deadline(tmp_path, monkeypatch):
    async def scenario():
        run = _clocked_run(tmp_path)
        clock = [1000]
        monkeypatch.setattr("scripts.tool_scripts.bim_agent_budget.time.time", lambda: clock[0])
        servers = []
        with patch.object(FastMCP, "run", lambda s: servers.append(s)):
            runner.serve(run)
        server = servers[0]
        results = []
        for offset in [0, 3000, 5101, 6000]:
            clock[0] = 1000 + offset
            result = await server.call_tool("inputs", {})
            content = result.content if hasattr(result, "content") else result[0] if isinstance(result, tuple) else result
            results.append("\n".join(c.text for c in content if c.type == "text"))
        assert "时间 100.0 分钟" in results[0]
        assert "最紧额度已过半" in results[1] and "upstairs.png" in results[1]
        assert "停止新范围探索" in results[2] and "有界复核已列严重问题" in results[2]
        assert "交付并列未决" in results[2]
        assert result.isError and "时间上限已到" in results[3]
        assert not list(run.glob("candidate_*"))
        clock[0] = 6500
        error = await server.call_tool("view_image", {"name": "label"})
        assert error.isError and "剩余" in error.content[-1].text
    asyncio.run(scenario())




def test_single_floor_fallback_reports_missing_upper_floor(tmp_path):
    from tests.test_bim_claims import setup_run
    run, toolkit = setup_run(tmp_path)
    # A saved source without image/floor provenance must not be called complete.
    toolkit.manifest.update(floor_plan_images=["plan.png", "upstairs.png"])
    assert fallback_selection(toolkit) == ("seed", "latest_saved_fallback_not_agent_selected")
    assert saved_floor_status(toolkit, "seed")["complete_building"] is False


def test_no_complete_candidate_still_delivers_latest_viewable_severe_draft(tmp_path):
    from tests.test_source_proposal import _proposal
    toolkit = runner.Toolkit(_run_with_one_image(tmp_path))
    toolkit.manifest.update(floor_plan_images=["plan.png"])
    proposal = _proposal()
    proposal["geometry"]["openings"][0]["other_space_id"] = "missing-room"
    result = toolkit.build(proposal)
    assert not result["source_geometry_ready"]
    candidate, origin = fallback_selection(toolkit)
    assert candidate == result["candidate"] and origin == "latest_saved_fallback_not_agent_selected"
    delivery = toolkit.delivery(candidate, selection_origin=origin)
    assert delivery["viewer_exists"]
    assert delivery["floor_completeness"]["complete_building"] is False
    assert delivery["floor_completeness"]["candidate_source_geometry_ready"] is False
