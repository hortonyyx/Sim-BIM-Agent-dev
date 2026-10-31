"""A5-T public entrypoints retain measurements and source identity offline."""
import asyncio
import copy
import json
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from scripts.tool_scripts.run_bim_agent import Toolkit, serve
from src.agent.geometry.source_naming import build_public_names
from tests.test_bim_agent_tools import _run_with_one_image, _two_room_proposal
from tests.test_source_naming import source


RETIRED_TOOLS = {'preview_space_trace', 'pixel_profile', 'record_claim', 'view_claim_evidence', 'select_space_trace', 'decide_claim', 'confirm_claims', 'view_space_trace', 'view_candidate'}

def server(run, **kwargs):
    servers = []
    with patch.object(FastMCP, 'run', lambda s: servers.append(s)):
        serve(run, **kwargs)
    return servers[0]


def response_body(reply):
    assert not getattr(reply, 'isError', False)
    if getattr(reply, 'structuredContent', None) is not None:
        return reply.structuredContent
    return json.loads(getattr(reply, 'content', reply)[0].text)


def test_default_readonly_and_coordinator_hide_replay_handlers(tmp_path):
    run = _run_with_one_image(tmp_path)
    for readonly in (False, True):
        full = asyncio.run(server(run, readonly=readonly).list_tools())
        assert not RETIRED_TOOLS & {t.name for t in full}



def test_profile_without_picture_has_identical_measured_values_and_saved_ids(tmp_path):
    run = _run_with_one_image(tmp_path)
    api = server(run)
    name = next(iter(Toolkit(run).manifest['images']))
    args = dict(name=name, box=[0, 0, 4, 4], axis='x', rgb=[0, 0, 0])
    image_reply = asyncio.run(api.call_tool('view_pixel_profile', args))
    text_reply = asyncio.run(api.call_tool('view_pixel_profile', {**args, 'include_image': False}))
    assert not getattr(image_reply, 'isError', False) and not getattr(text_reply, 'isError', False)
    image_blocks = getattr(image_reply, 'content', image_reply)
    text_blocks = getattr(text_reply, 'content', text_reply)
    assert any(b.type == 'image' for b in image_blocks)
    assert all(b.type != 'image' for b in text_blocks)
    first, second = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run/'pixel_profiles').glob('profile_*.json'))]
    for key in ('matching_pixels', 'candidates', 'cross_axis_profile', 'threshold_excluded_support', 'crop_context'):
        assert first[key] == second[key]
    assert second['profile_id'] != first['profile_id']


def test_merged_profile_retains_weak_unthresholded_local_maxima(tmp_path):
    from PIL import Image
    import hashlib
    run = _run_with_one_image(tmp_path)
    path = run/'images/plan.png'
    pic = Image.new('RGB', (12, 8), 'white')
    for x,height in enumerate([0,1,1,2,1,6,1,3,3,1,0,0]):
        for y in range(height):
            pic.putpixel((x,y), (0,0,0))
    pic.save(path)
    manifest = json.loads((run/'inputs.json').read_text(encoding="utf-8"))
    manifest['images']['plan.png']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (run/'inputs.json').write_text(json.dumps(manifest))
    toolkit = Toolkit(run)
    _, metadata = toolkit.view_profile('plan.png', [0,0,12,8], 'x', [0,0,0], 0, .5)
    new = json.loads(metadata)
    assert [p['count'] for p in new['positive_support_runs'][0]['support_peaks']] == [2,6,3]
    assert len(new['candidates']) == 1 and new['candidates'][0]['peak'] == 5


def test_saved_claim_view_routes_to_the_existing_hash_checked_reader(tmp_path):
    run = _run_with_one_image(tmp_path)
    api = server(run)
    from mcp.types import CallToolResult, TextContent
    reply = CallToolResult(content=[TextContent(type='text', text='saved evidence')])
    with patch.object(Toolkit, 'view_claim_evidence', return_value=reply) as read:
        result = asyncio.run(api.call_tool('view_image', dict(claim_id='claim_0001', source_index=2, display_scale=3)))
    assert not result.isError
    read.assert_called_once_with('claim_0001', 2, 3)
    name = next(iter(Toolkit(run).manifest['images']))
    bad = asyncio.run(api.call_tool('view_image', dict(claim_id='claim_0001', name=name)))
    assert bad.isError


def test_merged_candidate_view_and_annotated_position_use_saved_source(tmp_path):
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    built = toolkit.build(json.loads(_two_room_proposal()))
    candidate = built['candidate']
    path = run/candidate/'source_model.json'
    before = path.read_bytes()
    api = server(run)
    reply = asyncio.run(api.call_tool('inspect_candidate', dict(candidate=candidate, include_plan=True)))
    assert not reply.isError and any(b.type == 'image' for b in reply.content)
    assert reply.structuredContent['source_plan_view']['candidate'] == candidate
    position = dict(id='p', boundary_id='space/left/wall/1', axis='x', lengths=[2900],
        unit='mm', origin_m=0, direction=1, node=1, offset_m=0,
        basis='synthetic labelled representative plane', source_refs=['plan.png: 2900'])
    reply = asyncio.run(api.call_tool('check_wall_dimensions', dict(candidate=candidate,
        positions_json=json.dumps([position]))))
    assert not getattr(reply, 'isError', False)
    report = json.loads(getattr(reply, 'content', reply)[0].text)['wall_placement']
    assert report['total'] == 1 and report['items'][0]['deviation_m'] == .1
    assert path.read_bytes() == before


def test_row_order_is_stable_across_rounding_boundaries_and_input_order():
    s = source()
    # Match the half-micro rounding boundary that reordered run99's north row.
    for room in s['spaces']:
        jitter = 0.00000000001 if room['id'] == 'east' else 0
        room['polygon'] = [[x, y + 2.5336925 + jitter] for x, y in room['polygon']]
    before = copy.deepcopy(s)
    result = build_public_names(s)
    assert result['spaces']['west'].startswith('Z01_F1_')
    assert result['spaces']['east'].startswith('Z02_F1_')
    assert s == before
    s['spaces'].reverse()
    assert build_public_names(s) == result


def test_summary_inspection_is_opt_in_and_never_selects_or_changes_source(tmp_path):
    from scripts.tool_scripts import bim_agent_saved_result as saved
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    candidate = toolkit.build(json.loads(_two_room_proposal()))['candidate']
    api = server(run)
    source = run/candidate/'source_model.json'
    original = source.read_bytes()
    with patch.object(saved, 'candidate_review', side_effect=AssertionError('not requested')):
        default = asyncio.run(api.call_tool('inspect_candidate', {'candidate':candidate}))
        assert 'current_checks' not in response_body(default)
    with patch.object(Toolkit, 'delivery', side_effect=AssertionError('inspection must not deliver')):
        reply = asyncio.run(api.call_tool('inspect_candidate', {'candidate':candidate, 'include_geometry':False}))
    checks = response_body(reply)['current_checks']
    assert checks['source']['status'] == 'current'
    assert checks['source']['validation_status'] == 'pass'
    assert checks['floors']['complete_building'] is None
    assert checks['opening_review']['scope_status_counts']['not_reviewed'] > 0
    assert checks['height_coverage']['summary']['exterior_count'] == 0
    assert checks['claims']['status'] == 'projected_current'
    assert checks['drawing_fidelity'] == 'not_evaluated'
    assert checks['calibration_independently_verified'] is False
    assert source.read_bytes() == original
    assert not (run/'delivery_selection.json').exists()
    assert not (run/'delivery.json').exists()


def test_summary_inspection_preserves_stale_calibration_and_original_evidence(tmp_path):
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    candidate = toolkit.build(json.loads(_two_room_proposal()))['candidate']
    source = json.loads((run/candidate/'source_model.json').read_bytes())
    image = next(iter(toolkit.manifest['images']))
    anchors = [[0,0],[10,10]]
    toolkit._save_calibration(candidate=candidate, image=image, floor_id='F1',
        x_anchors=anchors, y_anchors=anchors, basis='synthetic observed axes',
        metadata={'source_model_sha256':source['source_model_sha256']})
    folder = run/'opening_reviews'; folder.mkdir()
    path = folder/'review_001.json'
    path.write_text(json.dumps({'source_model_sha256':source['source_model_sha256'],
        'review_file':'opening_reviews/review_001.json',
        'review_scope':{'floor_id':'F1','kind':'window','coverage':'complete'},
        'image':{'name':image,'sha256':toolkit.manifest['images'][image]['sha256']},
        'location_check':{'status':'checked_against_supplied_plan_boxes',
                          'x_anchors':anchors,'y_anchors':anchors},
        'conclusion':'consistent_with_supplied_observations','findings':[]}), encoding='utf-8')
    original = path.read_bytes()
    api = server(run)
    args = {'candidate':candidate, 'include_geometry':False}
    first = response_body(asyncio.run(api.call_tool('inspect_candidate',args)))['current_checks']
    assert first['opening_review']['current_reviews_count'] == 1
    toolkit._save_calibration(candidate=candidate, image=image, floor_id='F1',
        x_anchors=[[0,0],[10,11]], y_anchors=anchors, basis='corrected synthetic axes',
        metadata={'source_model_sha256':source['source_model_sha256']})
    second = response_body(asyncio.run(api.call_tool('inspect_candidate',args)))['current_checks']
    assert second['opening_review']['current_reviews_count'] == 0
    assert second['opening_review']['stale_reviews_count'] == 1
    assert path.read_bytes() == original
    # The previous inspection remains an immutable, readable account of its time.
    read = first['details']['read']
    page = asyncio.run(api.call_tool(read['tool'],read['arguments']))
    assert response_body(page)['sha256'] == first['details']['report_sha256']


def test_summary_inspection_bounds_large_notes_and_keeps_exact_totals_and_reads(tmp_path):
    import hashlib
    from scripts.tool_scripts.bim_agent_replies import read_report
    run = _run_with_one_image(tmp_path)
    toolkit = Toolkit(run)
    proposal = json.loads(_two_room_proposal())
    proposal['unresolved'] = [f'question-{i}: ' + 'uncertain evidence ' * 100 for i in range(250)]
    candidate = toolkit.build(proposal)['candidate']
    api = server(run)
    reply = asyncio.run(api.call_tool('inspect_candidate', {'candidate':candidate, 'include_geometry':False}))
    body = response_body(reply)
    assert len(json.dumps(body,ensure_ascii=False).encode()) <= 24000
    raw = (run/body['details_file']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == body['details_sha256']
    full = json.loads(raw)
    assert len(full['current_checks']['notes']['unresolved']) == 250
    assert body['current_checks']['notes']['unresolved_count'] == 250
    # Reading details follows the existing bounded/hash-checked MCP contract.
    read = read_report(run, body['details_file'],0,10,json_pointer='/current_checks/notes/unresolved')
    assert read['sha256'] == body['details_sha256']
    original = raw
    toolkit.revise(candidate,json.dumps([{'op':'set_notes','assumptions':[],'unresolved':[]}]))
    assert (run/body['details_file']).read_bytes() == original


def test_summary_inspection_reprojects_claim_decisions_without_height_cache_reuse(tmp_path):
    from tests.test_bim_claim_state import window_claim, window_check
    from tests.test_bim_claims import setup_run
    from src.agent.execution import bim_claim_state
    run, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    api = server(run)
    args = {'candidate':'seed','include_geometry':False}
    with patch.object(bim_claim_state, 'project', wraps=bim_claim_state.project) as projected:
        first = response_body(asyncio.run(api.call_tool('inspect_candidate',args)))['current_checks']
        assert projected.call_count == 1
    assert first['claims']['state_counts']['pending_application'] == 1
    assert first['height_coverage']['openings_with_issues_count'] > 0
    toolkit.confirm_claims('seed', json.dumps([window_check(row['id'])]))
    confirmed = response_body(asyncio.run(api.call_tool('inspect_candidate',args)))['current_checks']
    assert confirmed['claims']['state_counts']['confirmed_unchanged'] == 1
    assert confirmed['claims']['adopted_unapplied_count'] == 0
    assert confirmed['claims']['claims_with_unresolved_count'] == 1
    assert confirmed['claims']['issues_count'] == 1
    height_cache = (run/'seed/height_coverage.json').read_bytes()
    toolkit.decide_claim(row['id'],'retracted','source interpretation withdrawn')
    retracted = response_body(asyncio.run(api.call_tool('inspect_candidate',args)))['current_checks']
    assert retracted['claims']['state_counts']['retracted'] == 1
    assert retracted['claims']['claims_with_unresolved_count'] == 0
    assert retracted['claims']['issues_count'] == 0
    assert retracted['claims']['retracted_claims_count'] == 1
    # The on-demand inspection did not overwrite/reuse the older height table.
    assert (run/'seed/height_coverage.json').read_bytes() == height_cache
    assert retracted['drawing_fidelity'] == 'not_evaluated'
    assert not (run/'delivery_selection.json').exists()


def test_summary_inspection_without_source_still_has_exact_readable_snapshot(tmp_path):
    run = _run_with_one_image(tmp_path)
    candidate = Toolkit(run).build(json.loads(_two_room_proposal()))['candidate']
    (run/candidate/'source_model.json').unlink()
    api = server(run)
    reply = response_body(asyncio.run(api.call_tool('inspect_candidate',
        {'candidate':candidate,'include_geometry':False})))
    checks = reply['current_checks']
    assert checks['status'] == 'source_not_available'
    assert checks['details']['report_sha256'] == reply['details_sha256']
    read = checks['details']['read']
    page = response_body(asyncio.run(api.call_tool(read['tool'],read['arguments'])))
    assert page['sha256'] == reply['details_sha256']


def test_centroid_rows_do_not_chain_across_more_than_tolerance():
    from src.agent.geometry.source_naming import _ordered_spaces
    from shapely.geometry import box
    rows = [{'id': key, 'floor_id': 'f'} for key in ('north', 'middle', 'south')]
    polygons = {key: box(x, y, x+1, y+1) for key,x,y in
                [('north',10,1.0000018), ('middle',5,1.0000009), ('south',0,1.0)]}
    ordered = _ordered_spaces(rows, polygons, {'f': 0})
    assert [s['id'] for s in ordered] == ['middle', 'north', 'south']
