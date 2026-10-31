"""Offline regressions for T1's observed interface failures."""
import asyncio
import json
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP
from scripts.tool_scripts.bim_agent_feedback import resolve_image_name
from scripts.tool_scripts.run_bim_agent import Toolkit, serve
from tests.test_bim_agent_tools import _run_with_one_image, _add_image, _server_session, _two_room_proposal, _json_result


def test_unique_image_aliases_and_ambiguous_names():
    images = {"East_view.png": {}, "West_view.png": {}}
    assert resolve_image_name(images, "EAST_VIEW") == "East_view.png"
    assert resolve_image_name(images, "east_VIEW.PNG") == "East_view.png"
    images["East_view.jpg"] = {}
    with pytest.raises(ValueError, match="ambiguous.*East_view.*Available filenames"):
        resolve_image_name(images, "East_view")
    for name in ("p1", "../East_view.png", "/East_view.png"):
        with pytest.raises(ValueError, match="Available filenames"):
            resolve_image_name(images, name)


@pytest.mark.parametrize("scale,used", [(0.7, 1), (9, 8), (-3, 1)])
def test_zoom_clamps_without_changing_original(tmp_path, scale, used):
    run = _run_with_one_image(tmp_path)
    original = (run / "images/plan.png").read_bytes()
    data = Toolkit(run).view("PLAN", display_scale=scale)
    info = json.loads(data[1])
    assert info["name"] == "plan.png"
    assert info["display_scale_requested"] == scale and info["display_scale_used"] == used
    assert "clamped" in info["display_scale_note"]
    assert (run / "images/plan.png").read_bytes() == original


def test_alias_error_and_image_results_survive_real_stdio(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        async with _server_session(run, readonly=False) as session:
            result = await session.call_tool("view_image", {"name": "PLAN", "display_scale": 0.5})
            assert not result.isError and any(c.type == "image" for c in result.content)
            assert "actual filename 'plan.png'" in "\n".join(c.text for c in result.content if c.type == "text")
            bad = await session.call_tool("view_pixel_profile", dict(name="p1", box=[0, 0, 4, 4], axis="x", rgb=[255, 255, 255]))
            assert bad.isError and "Available filenames: [\"plan.png\"]" in bad.structuredContent['error']
            assert {"view_image", "view_pixel_profile"} <= {tool.name for tool in (await session.list_tools()).tools}
    asyncio.run(scenario())


def test_plan_format_failure_supplies_correct_minimum_without_edit(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        servers = []
        with patch.object(FastMCP, "run", lambda s: servers.append(s)):
            serve(run)
        server = servers[0]
        # Exercise the response boundary with the actual compiler's format rejection.
        from src.agent.geometry.plan_revision import apply_plan_revision
        args = dict(draft_id="draft_001", expected_plan_sha256="0"*64,
                    operations_json=json.dumps([dict(op="update", collection="partitions", id="P1", points=[[1, 2], [4, 2]])]))
        with patch.object(Toolkit, "revise_plan", lambda *_: apply_plan_revision({}, json.loads(args["operations_json"]))):
            result = await server.call_tool("revise_plan_bim", args)
        assert result.isError
        assert "missing changes, reason, source_refs" in result.structuredContent['error']
        assert '"changes": {"points"' in result.structuredContent['repair_hint']
        assert not list(run.glob("candidate_*"))
    asyncio.run(scenario())


def test_drawing_process_tool_errors_link_to_their_actual_format(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        calls = {
            "prepare_elevation_reading": {"image": "plan.png", "facade": "North", "reading_json": "{}"},
            "finish_bim": {"candidate": "missing", "reviews_json": "[]"},
            "revise_plan_bim": {"draft_id": "missing", "expected_plan_sha256": "unused",
                                "operations_json": "[]", "reviews_json": "[]"},
            "apply_elevation_readings": {"candidate": "missing", "reading_ids": []},
        }
        async with _server_session(run, readonly=False) as session:
            for tool, arguments in calls.items():
                result = await session.call_tool(tool, arguments)
                assert result.isError
                hint = result.structuredContent["repair_hint"]
                topic = "plan_partition" if tool == "revise_plan_bim" else "drawing_process"
                assert f'topic="{topic}"' in hint
        assert not list(run.glob("candidate_*"))
    asyncio.run(scenario())


def test_wall_position_failure_points_to_exact_field_and_correct_reference(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        built = Toolkit(run).build(json.loads(_two_room_proposal()))
        assert built['source_geometry_ready']
        path = run / built['candidate'] / 'source_model.json'
        original = path.read_bytes()
        source = json.loads(original)
        wall = next(row for row in source['boundaries'] if row['geometry_type'] == 'wall'
                    and len({v[0] for v in row['vertices']}) == 1)
        position = dict(id='wall-check', boundary_id=wall['id'], axis='x', lengths=[1], unit='m',
                        origin_m=wall['vertices'][0][0], direction=1, node=0,
                        basis='explicit representative plane', source_refs=['plan.png: datum'])
        async with _server_session(run, readonly=False) as session:
            result = await session.call_tool('check_wall_dimensions', {
                'candidate': built['candidate'], 'positions_json': json.dumps([position])})
            assert result.isError
            error = result.structuredContent
            assert "positions[0] (id='wall-check'): missing fields: offset_m" in error['error']
            assert 'get_bim_reference(topic="wall_dimensions")' in error['repair_hint']
            assert 'plan_partition' not in error['repair_hint']
            saved = json.loads((run / error['details_file']).read_bytes())
            assert saved['error'] == error['error']
            assert path.read_bytes() == original
            # An explicit zero is accepted; the tool did not guess it on failure.
            checked = _json_result(await session.call_tool('check_wall_dimensions', {
                'candidate': built['candidate'], 'positions_json': json.dumps([{**position, 'offset_m': 0}])}))
            assert checked['wall_placement']['checked_positions'] == 1
            assert checked['wall_placement']['geometry_changed'] is False
        assert path.read_bytes() == original
        assert len(list(run.glob('candidate_*'))) == 1
    asyncio.run(scenario())


def test_facade_anchor_row_order_is_accepted_without_changing_observation_or_bim(tmp_path):
    async def scenario():
        run = _run_with_one_image(tmp_path)
        _add_image(run, 'elevation.png')
        originals = {path: path.read_bytes() for path in (run / 'images').iterdir()}
        raw = {
            'plan': {'axis_anchors': [[1, 0], [11, 10]],
                     'openings': [{'id': 'p', 'pixels': [2, 4], 'evidence': 'synthetic mark'}]},
            'elevation': {'axis_anchors': [[11, 0], [1, 10]],
                          'openings': [{'id': 'e', 'pixels': [2, 4]}]},
        }
        arguments = dict(plan_image='plan.png', elevation_image='elevation.png',
                         plan_axis='x', elevation_axis='x')
        async with _server_session(run, readonly=False) as session:
            first = _json_result(await session.call_tool('compare_facade_spans',
                {**arguments, 'observations_json': json.dumps(raw)}))
            for view in raw.values():
                view['axis_anchors'].reverse()
            second = _json_result(await session.call_tool('compare_facade_spans',
                {**arguments, 'observations_json': json.dumps(raw)}))
            for key in ('normalized_inputs', 'directions', 'direction_separation', 'original_images'):
                assert second[key] == first[key]
            saved = json.loads((run / second['record']).read_bytes())
            assert saved['observations'] == saved['submitted_observations'] == raw
            assert saved['evidence_status'] == 'caller_observations_not_independently_verified'
            raw['plan']['axis_anchors'] = [[1, 5], [11, 15]]
            failed = await session.call_tool('compare_facade_spans',
                {**arguments, 'observations_json': json.dumps(raw)})
            assert failed.isError
            assert 'plan.axis_anchors' in failed.structuredContent['error']
            assert 'topic="facade_correspondence"' in failed.structuredContent['repair_hint']
            assert 'plan_partition' not in failed.structuredContent['repair_hint']
        assert len(list((run / 'facade_comparisons').glob('comparison_*.json'))) == 2
        assert not list(run.glob('candidate_*'))
        assert all(path.read_bytes() == original for path, original in originals.items())
    asyncio.run(scenario())
