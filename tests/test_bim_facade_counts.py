"""Independent observed facade totals must reveal absent windows, including zero-built walls."""
import asyncio
import json
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP

from scripts.tool_scripts import run_bim_agent as runner
from tests.test_bim_agent_tools import _run_with_one_image, _add_image
from tests.test_bim_claims import setup_run
from tests.test_source_proposal import _proposal


def count():
    return dict(observation_type="facade_count", image="plan.png", floor_id="F1",
                facade="West", window_count=2, reason="synthetic full-facade observation")


def save(toolkit, **changes):
    data = count()
    data.update(changes)
    return toolkit.record_claim(json.dumps(data))


def west(report):
    return next(row for row in report["scopes"] if row["facade"] == "West")


def test_prebuild_count_reports_missing_window_on_save_and_delivery(tmp_path):
    run = _run_with_one_image(tmp_path)
    toolkit = runner.Toolkit(run)
    saved = save(toolkit)
    assert not list(run.glob("candidate_*")) and not list((run / "claims").glob("claim_*.json"))
    result = toolkit.build(_proposal())
    row = west(result["facade_counts"])
    assert row["window"]["status"] == "count_mismatch" and row["window"]["missing_count"] == 1
    assert row["observation_ids"] == [saved["id"]]
    assert result["facade_counts"]["summary"]["window_status_counts"]["not_counted"] == 3
    delivered = toolkit.delivery(result["candidate"], selection_origin="agent_selected")
    assert west(delivered["facade_counts"])["window"]["missing_count"] == 1
    assert delivered["facade_counts"]["delivery_blocked"] is False
    assert "数量不符" in (run / "delivery.html").read_text()


def test_zero_count_and_corrected_count_persist_without_session_state(tmp_path):
    run, toolkit = setup_run(tmp_path)
    save(toolkit, facade="North", window_count=0, door_count=0)
    first = save(toolkit)
    second = save(toolkit, window_count=1)
    report = runner.Toolkit(run).facade_counts("seed")
    assert west(report)["observation_ids"] == [second["id"]]
    assert west(report)["window"]["status"] == "matches_observed_total"
    assert report["summary"]["window_status_counts"] == {"matches_observed_total": 2, "not_counted": 2}
    assert (run / "facade_counts" / (first["id"] + ".json")).exists()


def test_different_image_conflicts_and_geometry_revision_are_not_hidden(tmp_path):
    run, toolkit = setup_run(tmp_path)
    _add_image(run, "other.png")
    toolkit = runner.Toolkit(run)
    save(toolkit, window_count=1)
    save(toolkit, image="other.png", window_count=2)
    assert west(toolkit.facade_counts("seed"))["window"]["status"] == "conflicting_observations"
    save(toolkit, image="other.png", window_count=1)
    result = toolkit.revise("seed", json.dumps([dict(op="remove_opening", id="window",
        reason="synthetic change", source_refs=["synthetic"])]))
    row = west(result["facade_counts"])["window"]
    assert row["built_count"] == 0 and row["missing_count"] == 1
    assert row["status"] == "count_mismatch"


def test_unknown_floor_and_changed_image_are_explicit_not_matched(tmp_path):
    run, toolkit = setup_run(tmp_path)
    save(toolkit, floor_id="absent")
    assert toolkit.facade_counts("seed")["unused_observations"][0]["reason"] == "floor_not_resolved_in_candidate"
    save(toolkit)
    (run / "images/plan.png").write_bytes(b"changed bytes")
    report = toolkit.facade_counts("seed")
    assert all(r["reason"] == "source_image_changed" for r in report["unused_observations"])
    assert report["summary"]["window_status_counts"] == {"not_counted": 4}


def test_plan_image_scope_maps_after_assembly_renames_floor(tmp_path):
    run = _run_with_one_image(tmp_path)
    _add_image(run, "upstairs.png")
    toolkit = runner.Toolkit(run)
    data = count()
    del data["floor_id"]
    data.update(floor_plan_image="plan.png", window_count=0)
    toolkit.record_claim(json.dumps(data))
    plan = dict(floor_id="local", z_floor=0, ceiling_height=3,
        x_anchors=[[0, 0], [11, 11]], y_anchors=[[0, 7], [7, 0]], basis="synthetic frame",
        footprint_pixels=[[1, 1], [10, 1], [10, 6], [1, 6]],
        partitions=[], openings=[], assumptions=[], unresolved=[])
    built = toolkit.build_plan("plan.png", json.dumps(plan))
    assert west(built["facade_counts"])["floor_id"] == "local"
    upper = toolkit.build_plan("upstairs.png", json.dumps(plan))
    assembled = toolkit.assemble_plans(json.dumps([dict(draft_id="draft_001",
        expected_plan_sha256=built["plan_input"]["plan_sha256"], floor_id="F1", z_floor=0,
        evidence="synthetic floor mapping"), dict(draft_id="draft_002",
        expected_plan_sha256=upper["plan_input"]["plan_sha256"], floor_id="F2", z_floor=3,
        evidence="synthetic upper floor mapping")]))
    row = west(assembled["facade_counts"])
    assert row["floor_id"] == "F1" and row["window"]["status"] == "matches_observed_total"


@pytest.mark.parametrize("changes", [dict(window_count=-1), dict(window_count=True),
    dict(door_count=1.5), dict(floor_plan_image="plan.png"), dict(facade="wrong"), dict(reason="")])
def test_bad_counts_have_minimal_repair_example(tmp_path, changes):
    toolkit = runner.Toolkit(_run_with_one_image(tmp_path))
    with pytest.raises(ValueError, match="minimal example"):
        save(toolkit, **changes)
    assert not (toolkit.run / "facade_counts").exists()


def test_same_existing_mcp_tool_accepts_counts_without_candidate_or_new_tool(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        servers = []
        with patch.object(FastMCP, "run", lambda s: servers.append(s)):
            runner.serve(run)
        server = servers[0]
        assert "claim_transaction" in {tool.name for tool in await server.list_tools()}
        result = await server.call_tool("claim_transaction", {"entries_json": json.dumps([{"claim": count(), "action": "record"}])})
        assert not result.isError
        assert result.structuredContent["entries"][0]["status"] == "recorded"
        assert result.structuredContent["status"] == "completed"
        assert "已用／剩余分钟" in result.content[-1].text
    asyncio.run(scenario())
