"""Reuse the actual viewed region and retire obsolete evidence without changing BIM."""
import asyncio
import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from scripts.tool_scripts.run_bim_agent import Toolkit
from tests.test_bim_claims import claim, edit, setup_run
from tests.test_bim_agent_tools import _json_result, _server_session


def viewed(toolkit, box=None):
    _, text = toolkit.view('plan.png', box, coordinate_grid=False, display_scale=2)
    return json.loads(text)


def test_view_reference_retains_actual_original_region_and_detects_record_change(tmp_path):
    run, toolkit = setup_run(tmp_path)
    meta = viewed(toolkit, [2, 1, 11, 7])
    assert meta['returned_size'] == [18, 12]
    row = toolkit.record_claim(json.dumps(claim(sources=[meta['source_reference']])))
    source = row['sources'][0]
    assert source['box'] == [2, 1, 11, 7] and source['size'] == [12, 8]
    assert source['view_id'] == meta['view_id']
    assert source['view_sha256'] == meta['view_record_sha256']
    assert source['sha256'] == meta['image_sha256']
    toolkit.decide_claim(row['id'], 'adopted', 'synthetic interpretation')
    saved = run / 'image_views' / (meta['view_id'] + '.json')
    changed = json.loads(saved.read_text())
    changed['box_original_pixels'] = [0, 0, 12, 8]
    saved.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='evidence changed'):
        toolkit.revise('seed', json.dumps([edit(row['id'])]))
    assert not (run / 'candidate_01').exists()


def test_source_replacement_preserves_numbers_retracts_old_and_requires_new_adoption(tmp_path):
    run, toolkit = setup_run(tmp_path)
    before = (run / 'seed/source_model.json').read_bytes()
    old = toolkit.record_claim(json.dumps(claim(sources=[{'image': 'plan.png', 'box': [2, 2, 8, 6]}])))
    toolkit.decide_claim(old['id'], 'adopted', 'old region')
    old_bytes = (run / 'claims' / (old['id'] + '.json')).read_bytes()
    whole = viewed(toolkit)
    new = toolkit.replace_claim_sources(old['id'], [whole['view_id']], 'Entire drawing contains the chain and object')
    for field in ('candidate', 'objects', 'values', 'value_targets', 'basis', 'unresolved', 'observation_mode'):
        assert new['claim'][field] == old['claim'][field]
    assert new['resolved_values'] == old['resolved_values']
    assert new['replaces_claim'] == old['id']
    assert new['sources'][0]['box'] == [0, 0, 12, 8]
    assert (run / 'claims' / (old['id'] + '.json')).read_bytes() == old_bytes
    assert (run / 'seed/source_model.json').read_bytes() == before
    decisions = toolkit.claims().decisions()
    assert decisions[old['id']]['disposition'] == 'retracted' and new['id'] not in decisions
    for identity in (old['id'], new['id']):
        with pytest.raises(ValueError, match='adopted'):
            toolkit.revise('seed', json.dumps([edit(identity)]))
    toolkit.decide_claim(new['id'], 'adopted', 'reviewed replacement')
    result = toolkit.revise('seed', json.dumps([edit(new['id'])]))
    assert result['claim_application']['status'] == 'applied'


def test_source_replacement_does_not_inherit_old_height_confirmation(tmp_path):
    from src.agent.execution.bim_height_coverage import height_coverage
    run, toolkit = setup_run(tmp_path)
    source = json.loads((run / 'seed/source_model.json').read_text())
    door = next(o for o in source['openings'] if o['id'] == 'door')
    z = [min(v[2] for v in door['vertices']), max(v[2] for v in door['vertices'])]
    old = toolkit.record_claim(json.dumps(claim(values={'height': {'type': 'literal', 'value': z, 'unit': 'm'}})))
    toolkit.decide_claim(old['id'], 'adopted', 'old observed source')
    toolkit.confirm_claims('seed', json.dumps([edit(old['id'])]))
    summary = lambda: height_coverage(toolkit.claims(), 'seed')['summary']['image_linked_count']
    assert summary() == 1
    old_confirmation = (run / 'claims/confirmation_0001.json').read_bytes()
    new = toolkit.replace_claim_sources(old['id'], [viewed(toolkit)['view_id']], 'Use full original')
    assert summary() == 0
    toolkit.decide_claim(new['id'], 'adopted', 'new region reviewed')
    assert summary() == 0
    toolkit.confirm_claims('seed', json.dumps([edit(new['id'])]))
    assert summary() == 1
    assert (run / 'claims/confirmation_0001.json').read_bytes() == old_confirmation


@pytest.mark.parametrize('ids', [[], ['view_9999'], ['../inputs'], [12]])
def test_invalid_view_replacement_leaves_existing_claim_and_decision_untouched(tmp_path, ids):
    run, toolkit = setup_run(tmp_path)
    row = toolkit.record_claim(json.dumps(claim()))
    toolkit.decide_claim(row['id'], 'adopted', 'original')
    before = {p.name: p.read_bytes() for p in (run / 'claims').glob('*.json')}
    with pytest.raises(ValueError):
        toolkit.replace_claim_sources(row['id'], ids, 'invalid reference')
    assert {p.name: p.read_bytes() for p in (run / 'claims').glob('*.json')} == before


def test_view_reference_rejects_changed_image_and_mixed_coordinate_override(tmp_path):
    run, toolkit = setup_run(tmp_path)
    meta = viewed(toolkit)
    for extra in ({'image': 'plan.png'}, {'box': [0, 0, 12, 8]}):
        with pytest.raises(ValueError):
            toolkit.record_claim(json.dumps(claim(sources=[{**meta['source_reference'], **extra}])))
    Image.new('RGB', (12, 8), 'red').save(run / 'images/plan.png')
    # An updated inventory must not silently rebind a saved view to changed pixels.
    toolkit.manifest['images']['plan.png']['sha256'] = hashlib.sha256((run / 'images/plan.png').read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='view original image changed'):
        toolkit.record_claim(json.dumps(claim(sources=[meta['source_reference']])))
    assert not (run / 'claims').exists()


def test_stdio_view_and_source_replacement_return_real_images_and_coordinator_only_tool(tmp_path):
    async def scenario():
        run, _ = setup_run(tmp_path)
        async with _server_session(run, readonly=True) as session:
            assert 'replace_claim_sources' not in {t.name for t in (await session.list_tools()).tools}
        async with _server_session(run, readonly=False) as session:
            original = await session.call_tool('view_image', {'name': 'plan.png', 'coordinate_grid': False})
            meta = next(json.loads(p.text) for p in original.content if p.type == 'text')
            raw = base64.b64decode(next(p.data for p in original.content if p.type == 'image'))
            assert hashlib.sha256(raw).hexdigest() == meta['returned_png_sha256']
            recorded = _json_result(await session.call_tool('claim_transaction', {'candidate': 'seed',
                'entries_json': json.dumps([{'claim': claim(), 'action': 'record'}])}))
            assert recorded['status'] == 'completed'
            old = {'id': recorded['entries'][0]['claim_id']}
            result = await session.call_tool('replace_claim_sources', {'claim_id': old['id'],
                'view_ids': [meta['view_id']], 'reason': 'Use the actual viewed original extent'})
            new = _json_result(result)
            assert new['sources'][0]['view_id'] == meta['view_id']
            assert new['superseded_decision']['disposition'] == 'retracted'
            pictures = [p for p in result.content if p.type == 'image']
            assert len(pictures) == 1
            image = Image.open(io.BytesIO(base64.b64decode(pictures[0].data)))
            assert image.size == (12, 8)
            assert new['verification'] == 'not_independently_verified'
            assert new['evidence_previews'][0]['view_id'] != meta['view_id']
    asyncio.run(scenario())
