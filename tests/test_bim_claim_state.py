import asyncio
import json

import pytest

from src.agent.execution.bim_claim_state import project
from tests.test_bim_claims import setup_run, adopt, claim, edit
from tests.test_bim_agent_tools import _json_result, _server_session


def window_claim(toolkit, candidate='seed'):
    return adopt(toolkit, claim(candidate=candidate, objects=[{'kind': 'window', 'id': 'window'}],
        values={'height': {'type': 'literal', 'value': [1, 2], 'unit': 'm'}},
        unresolved=['<b>Frame material unexamined</b>']))


def window_check(identity):
    return {'op': 'update_window', 'id': 'window',
        'changes': {'z': {'claim': identity, 'value': 'height'}}, 'reason': 'confirm drawn height'}


def states(toolkit, candidate):
    return {r['id']: r for r in project(toolkit.claims(), candidate)['claims']}


def test_projection_compares_each_expected_hash_for_the_same_candidate(tmp_path):
    run, toolkit = setup_run(tmp_path)
    first, second = window_claim(toolkit), window_claim(toolkit)
    toolkit.confirm_claims('seed', json.dumps([window_check(first['id'])]))
    confirmation = toolkit.confirm_claims('seed', json.dumps([window_check(second['id'])]))
    original_files = {name: (run / 'seed' / name).read_bytes()
                      for name in ('proposal.json', 'source_model.json')}
    baseline = project(toolkit.claims(), 'seed')
    assert len(baseline['claims']) == 2
    # A good first comparison must not bless a different expected hash later.
    claim_path = run / 'claims' / f"{second['id']}.json"
    original_claim = claim_path.read_bytes()
    changed = json.loads(original_claim)
    changed['parent_proposal_sha256'] = '0' * 64
    claim_path.write_text(json.dumps(changed))
    current = project(toolkit.claims(), 'seed')
    assert current['other_branch_claim_ids'] == [second['id']]
    assert [row['id'] for row in current['claims']] == [first['id']]
    claim_path.write_bytes(original_claim)
    confirmation_path = run / 'claims' / f"{confirmation['id']}.json"
    original_confirmation = confirmation_path.read_bytes()
    changed = json.loads(original_confirmation)
    changed['source_model_sha256'] = '0' * 64
    confirmation_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='applied source changed'):
        project(toolkit.claims(), 'seed')
    confirmation_path.write_bytes(original_confirmation)
    assert project(toolkit.claims(), 'seed') == baseline
    assert all((run / 'seed' / name).read_bytes() == raw for name, raw in original_files.items())


@pytest.mark.parametrize('tampered_file', ['proposal.json', 'source_model.json'])
def test_projection_hashes_refresh_between_calls_and_candidate_switches(tmp_path, tampered_file):
    run, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    toolkit.confirm_claims('seed', json.dumps([window_check(row['id'])]))
    baseline = project(toolkit.claims(), 'seed')
    descendant = toolkit.revise('seed', json.dumps([dict(op='set_notes',
        assumptions=['Notes do not change physical geometry'], unresolved=[])]))['candidate']
    projected = project(toolkit.claims(), descendant)
    assert projected['claims'][0]['state'] == 'confirmed_unchanged'
    path = run / 'seed' / tampered_file
    original = path.read_bytes()
    changed = json.loads(original)
    if tampered_file == 'proposal.json':
        changed['unresolved'].append('Changed after the previous projection')
    else:
        changed['spaces'][0]['height'] += .1
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='ancestry changed|applied source changed'):
        project(toolkit.claims(), descendant)
    if tampered_file == 'proposal.json':
        assert project(toolkit.claims(), 'seed')['other_branch_claim_ids'] == [row['id']]
    else:
        with pytest.raises(ValueError, match='applied source changed'):
            project(toolkit.claims(), 'seed')
    path.write_bytes(original)
    assert project(toolkit.claims(), descendant) == projected
    assert project(toolkit.claims(), 'seed') == baseline


def test_confirmation_has_no_mutation_and_follows_only_unchanged_descendants(tmp_path):
    run, toolkit = setup_run(tmp_path)
    initial = (run/'seed/proposal.json').read_bytes()
    window = window_claim(toolkit)
    toolkit.confirm_claims('seed', json.dumps([window_check(window['id'])]))
    assert (run/'seed/proposal.json').read_bytes() == initial
    assert not list(run.glob('candidate_*'))
    assert states(toolkit, 'seed')[window['id']]['state'] == 'confirmed_unchanged'
    door = adopt(toolkit, claim())
    revised = toolkit.revise('seed', json.dumps([edit(door['id'])]))['candidate']
    current = states(toolkit, revised)
    assert current[window['id']]['state'] == 'confirmed_unchanged'
    assert current[door['id']]['state'] == 'applied_current'
    delivery = toolkit.delivery(revised, selection_origin='agent_selected')
    assert not delivery['adopted_unapplied_claims']
    html = (run/'delivery.html').read_text()
    assert '已核对，无需修改' in html and '&lt;b&gt;Frame material unexamined&lt;/b&gt;' in html
    changed = toolkit.revise(revised, json.dumps([{'op': 'update_window', 'id': 'window',
        'changes': {'z': [1, 2.2]}, 'reason': 'new interpretation', 'source_refs': ['fixture']}]))['candidate']
    assert states(toolkit, changed)[window['id']]['state'] == 'changed_since_check'
    # A sibling rooted at seed must not inherit the door application.
    sibling = toolkit.revise('seed', json.dumps([{'op': 'replace_note', 'field': 'assumptions',
        'old': '<墙体位置由平面推断>', 'replacement': ['Unchanged geometry; note clarified'],
        'reason': 'fixture', 'source_refs': ['fixture']}]))['candidate']
    assert states(toolkit, sibling)[door['id']]['state'] == 'pending_application'
    toolkit.decide_claim(window['id'], 'retracted', 'conflicting source')
    assert states(toolkit, revised)[window['id']]['state'] == 'retracted'


def test_false_or_unbound_confirmation_is_rejected_and_partial_coverage_visible(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = adopt(toolkit, claim())
    with pytest.raises(ValueError, match='differs'):
        toolkit.confirm_claims('seed', json.dumps([edit(row['id'])]))
    window = adopt(toolkit, claim(objects=[{'kind': 'window', 'id': 'window'}],
        values={'height': {'type': 'literal', 'value': [1, 2], 'unit': 'm'},
                'span': {'type': 'literal', 'value': [1, 2], 'unit': 'm'}}))
    toolkit.confirm_claims('seed', json.dumps([window_check(window['id'])]))
    assert states(toolkit, 'seed')[window['id']]['state'] == 'partially_satisfied'
    unbound = window_check(window['id'])
    unbound['changes']['span'] = [1, 2]
    with pytest.raises(ValueError, match='every confirmed'):
        toolkit.confirm_claims('seed', json.dumps([unbound]))
    assert len(list((run/'claims').glob('confirmation_*'))) == 1


def test_role_edit_preserves_height_check_but_physical_host_move_invalidates_it(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    toolkit.confirm_claims('seed', json.dumps([window_check(row['id'])]))
    revised = toolkit.revise('seed', json.dumps([dict(op='set_space_role', space_id='hall',
        role='corridor', basis='inferred', source_refs=['plan.png synthetic circulation'],
        assumptions=['Inferred from connectivity'], reason='Classify use')]))['candidate']
    assert states(toolkit, revised)[row['id']]['state'] == 'confirmed_unchanged'
    moved = toolkit.revise(revised, json.dumps([dict(op='move_shared_wall',
        space_ids=['hall', 'room'], coordinate_m=3.2,
        reason='Move physical host wall', source_refs=['synthetic'])]))['candidate']
    assert states(toolkit, moved)[row['id']]['state'] == 'changed_since_check'
    before = json.loads((run / revised / 'source_model.json').read_text())
    after = json.loads((run / moved / 'source_model.json').read_text())
    assert next(o for o in before['openings'] if o['id'] == 'window')['vertices'] == next(
        o for o in after['openings'] if o['id'] == 'window')['vertices']


def test_bound_value_overridden_later_in_same_revision_is_not_current(tmp_path):
    _, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    result = toolkit.revise('seed', json.dumps([window_check(row['id']),
        {'op': 'update_window', 'id': 'window', 'changes': {'z': [1, 2.2]},
         'reason': 'Later unbound override in same batch', 'source_refs': ['fixture']}]))
    assert states(toolkit, result['candidate'])[row['id']]['state'] == 'changed_since_check'


def test_explicit_note_replacement_updates_source_and_keeps_history(tmp_path):
    run, toolkit = setup_run(tmp_path)
    replacement = {'op': 'replace_note', 'field': 'assumptions', 'old': '<墙体位置由平面推断>',
        'replacement': ['Wall position now checked; other properties remain unknown'],
        'reason': 'Local image review supersedes the previous assumption', 'source_refs': ['fixture']}
    result = toolkit.revise('seed', json.dumps([replacement]))
    source = json.loads((run/result['candidate']/'source_model.json').read_text())
    assert '<墙体位置由平面推断>' not in source['assumptions']
    assert replacement['replacement'][0] in source['assumptions']
    assert source['generation']['unresolved'] == ['<b>未确认门状态</b>']
    assert project(toolkit.claims(), result['candidate'])['superseded_notes'][0]['before'] == replacement['old']
    assert result['claim_application']['changes'] == []
    with pytest.raises(ValueError, match='exactly match'):
        toolkit.revise(result['candidate'], json.dumps([replacement]))


def test_imported_claim_ids_are_not_mistaken_for_new_run_decisions(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = adopt(toolkit, claim())
    revised = toolkit.revise('seed', json.dumps([edit(row['id'])]))['candidate']
    proposal = json.loads((run/revised/'proposal.json').read_text())
    other, next_toolkit = setup_run(tmp_path/'next', proposal)
    new = window_claim(next_toolkit)
    assert new['id'] == row['id']  # local numbering restarts
    current = project(next_toolkit.claims(), 'seed')
    assert current['claims'][0]['state'] == 'pending_application'
    assert len(current['inherited_observations']) == 1


def test_confirmation_tool_stdio_and_readonly_exclusion(tmp_path):
    async def scenario():
        run, toolkit = setup_run(tmp_path)
        row = window_claim(toolkit)
        async with _server_session(run, readonly=True) as session:
            assert 'claim_transaction' not in {t.name for t in (await session.list_tools()).tools}
        async with _server_session(run, readonly=False) as session:
            result = _json_result(await session.call_tool('claim_transaction', {
                'candidate': 'seed', 'entries_json': json.dumps([{'claim_id': row['id'],
                    'action': 'confirm', 'operations': [window_check(row['id'])]}])}))
            assert result['status'] == 'completed'
            assert result['entries'][0]['status'] == 'confirmed_unchanged'
            current = _json_result(await session.call_tool('claim_status', {'candidate': 'seed'}))
            assert current['claims'][0]['state'] == 'confirmed_unchanged'
    asyncio.run(scenario())
