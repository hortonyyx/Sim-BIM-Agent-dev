"""A2-T: safe inheritance and one-call evidence lifecycles, without model calls."""
import asyncio
import copy
import json

import pytest

from scripts.tool_scripts.bim_agent_claims import ClaimBindingError
from src.agent.execution.bim_claim_state import project
from tests.test_bim_agent_tools import _json_result, _server_session
from tests.test_bim_claims import setup_run, claim, adopt, edit
from tests.test_bim_claim_state import window_check, window_claim
from tests.test_source_proposal import _proposal


def notes(toolkit, candidate="seed"):
    return toolkit.revise(candidate, json.dumps([dict(op="set_notes", assumptions=["Only a note changed"],
        unresolved=[])]))["candidate"]


def transaction(toolkit, candidate, entries):
    return toolkit.claim_transaction(candidate, json.dumps(entries))


def test_record_adopt_confirm_in_one_call_preserves_original_and_audit(tmp_path):
    run, toolkit = setup_run(tmp_path)
    before = (run / "seed/proposal.json").read_bytes()
    data = claim(objects=[dict(kind="window", id="window")],
                 values={"height": dict(type="literal", value=[1, 2], unit="m")})
    result = transaction(toolkit, "seed", [dict(claim=data, action="confirm", reason="Matches this opening",
        operations=[window_check("$claim")])])
    assert result["status"] == "completed"
    entry = result["entries"][0]
    assert entry["status"] == "confirmed_unchanged" and entry["decision_id"] and entry["confirmation_id"]
    assert "operation_reason_inheritance" not in entry  # Explicit operation reason is retained.
    assert (run / "seed/proposal.json").read_bytes() == before
    assert not list(run.glob("candidate_*"))
    journal = json.loads((run / result["audit_file"]).read_text())
    assert journal["entries"][0]["request"]["reason"] == "Matches this opening"
    assert project(toolkit.claims(), "seed")["claims"][0]["state"] == "confirmed_unchanged"


@pytest.mark.parametrize('action', ['confirm', 'apply'])
def test_claim_bound_operations_inherit_only_missing_reason(tmp_path, action):
    run, toolkit = setup_run(tmp_path)
    before = (run / 'seed/proposal.json').read_bytes()
    z = [1, 2] if action == 'confirm' else [1, 2.2]
    data = claim(objects=[dict(kind='window', id='window')],
                 values={'height': dict(type='literal', value=z, unit='m')})
    operation = window_check('$claim')
    del operation['reason']
    request = dict(claim=data, action=action, operations=[operation])
    if action == 'confirm':
        request['reason'] = 'Explicit entry decision reason'
    original = copy.deepcopy(request)
    result = transaction(toolkit, 'seed', [request])
    assert result['status'] == 'completed', result
    entry = result['entries'][0]
    expected = request.get('reason', data['reason'])
    assert entry['operation_reason_inheritance'] == [{
        'path': 'operations[0].reason',
        'from': 'entry.reason' if action == 'confirm' else f"claim:{entry['claim_id']}.reason",
    }]
    artifact_id = entry['confirmation_id' if action == 'confirm' else 'application_id']
    artifact = json.loads((run / 'claims' / f'{artifact_id}.json').read_bytes())
    assert artifact['resolved_operations'][0]['reason'] == expected
    journal = json.loads((run / result['audit_file']).read_bytes())
    assert journal['entries'][0]['request'] == original and request == original
    assert (run / 'seed/proposal.json').read_bytes() == before
    if action == 'confirm':
        assert not list(run.glob('candidate_*'))


@pytest.mark.parametrize('failure', ['different_value', 'blank_reason', 'null_reason'])
def test_inherited_reason_never_relaxes_confirmation_or_explicit_reason(tmp_path, failure):
    run, toolkit = setup_run(tmp_path)
    data = claim(objects=[dict(kind='window', id='window')], values={
        'height': dict(type='literal', value=[1, 2.2] if failure == 'different_value' else [1, 2], unit='m')})
    operation = window_check('$claim')
    del operation['reason']
    if failure != 'different_value':
        operation['reason'] = '' if failure == 'blank_reason' else None
    result = transaction(toolkit, 'seed', [dict(claim=data, action='confirm', operations=[operation])])
    assert result['status'] == 'failed'
    entry = result['entries'][0]
    assert ('differs from current geometry' if failure == 'different_value' else 'reason must') in entry['error']
    assert ('operation_reason_inheritance' in entry) == (failure == 'different_value')
    assert not list((run / 'claims').glob('confirmation_*.json'))
    assert not list(run.glob('candidate_*'))


def test_reason_inheritance_does_not_cover_unbound_edits_or_standalone_revision(tmp_path):
    run, toolkit = setup_run(tmp_path)
    data = claim(objects=[dict(kind='window', id='window')],
                 values={'height': dict(type='literal', value=[1, 2], unit='m')})
    bound = window_check('$claim')
    del bound['reason']
    unbound = dict(op='update_opening', id='door', changes={'z': [.3, 2.1]}, source_refs=['explicit'])
    result = transaction(toolkit, 'seed', [dict(claim=data, action='apply', operations=[bound, unbound])])
    assert result['status'] == 'failed' and 'reason must' in result['entries'][0]['error']
    assert result['entries'][0]['operation_reason_inheritance'][0]['path'] == 'operations[0].reason'
    with pytest.raises(ValueError, match='reason must'):
        toolkit.revise('seed', json.dumps([unbound]))
    assert not list(run.glob('candidate_*'))


def test_record_adopt_apply_in_one_call_and_reload(tmp_path):
    run, toolkit = setup_run(tmp_path)
    result = transaction(toolkit, "seed", [dict(claim=claim(), action="apply", operations=[edit("$claim")])])
    assert result["status"] == "completed"
    assert result["result_candidate"] == "candidate_01"
    assert result["entries"][0]["status"] == "applied"
    proposal = json.loads((run / "candidate_01/proposal.json").read_text())
    assert proposal["geometry"]["openings"][0]["z"] == [.3, 2.1]
    assert project(toolkit.claims(), "candidate_01")["claims"][0]["state"] == "applied_current"


def test_notes_only_inherits_with_immutable_claim_and_original_hash(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    original = (run / "claims" / (row["id"] + ".json")).read_bytes()
    candidate = notes(toolkit)
    result = toolkit.confirm_claims(candidate, json.dumps([window_check(row["id"])]))
    assert result["status"] == "confirmed_unchanged"
    assert result["evidence"]["claims"][row["id"]]["record"] == row
    assert result["evidence"]["inheritance"][0]["targets"][0]["eligible"]
    assert (run / "claims" / (row["id"] + ".json")).read_bytes() == original


@pytest.mark.parametrize("change,reason", [("geometry", "target_geometry_changed"),
    ("host", "host_changed"), ("view", "source_view_changed"),
    ("value", "values_or_computation_changed"), ("reverted", "target_geometry_changed")])
def test_changed_targets_views_values_are_never_inherited(tmp_path, change, reason):
    run, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    candidate = notes(toolkit)
    if change in {"geometry", "reverted"}:
        candidate = toolkit.revise(candidate, json.dumps([dict(op="update_window", id="window",
            changes={"z": [1, 2.2]}, reason="new height", source_refs=["fixture"])]))["candidate"]
        if change == "reverted":
            candidate = toolkit.revise(candidate, json.dumps([dict(op="update_window", id="window",
                changes={"z": [1, 2]}, reason="restored", source_refs=["fixture"])]))["candidate"]
    elif change == "host":
        candidate = toolkit.revise(candidate, json.dumps([dict(op="move_shared_wall", space_ids=["hall", "room"],
            coordinate_m=3.2, reason="move host", source_refs=["fixture"])]))["candidate"]
    else:
        # An immutable evidence file changed on disk; compare its actual view/value
        # to the stored binding, rather than accepting a now-matching candidate.
        path = run / "claims" / (row["id"] + ".json")
        record = json.loads(path.read_text())
        if change == "view":
            record["claim"]["sources"][0]["box"] = [1, 1, 11, 7]
        else:
            record["claim"]["values"]["height"]["value"] = [1, 2.2]
        path.write_text(json.dumps(record))
    with pytest.raises(ClaimBindingError) as error:
        toolkit.confirm_claims(candidate, json.dumps([window_check(row["id"])]))
    assert reason in error.value.report["targets"][0]["reasons"]
    assert not list((run / "claims").glob("confirmation_*.json"))


def test_multi_target_only_unchanged_target_inherits(tmp_path):
    proposal = _proposal()
    proposal["geometry"]["windows"].append({**proposal["geometry"]["windows"][0], "id": "second", "span": [3, 4]})
    run, toolkit = setup_run(tmp_path, proposal)
    row = adopt(toolkit, claim(objects=[dict(kind="window", id=name) for name in ("window", "second")],
                              values={"height": dict(type="literal", value=[1, 2], unit="m")}))
    candidate = toolkit.revise("seed", json.dumps([dict(op="update_window", id="second",
        changes={"z": [1, 2.2]}, reason="one window changed", source_refs=["fixture"])]))["candidate"]
    first, second = window_check(row["id"]), {**window_check(row["id"]), "id": "second"}
    result = transaction(toolkit, candidate, [dict(claim_id=row["id"], action="confirm", operations=[op])
                                             for op in (first, second)])
    assert result["status"] == "partial"
    assert [r["status"] for r in result["entries"]] == ["confirmed_unchanged", "failed"]
    state = project(toolkit.claims(), candidate)["claims"][0]
    assert state["state"] == "partially_satisfied"
    assert state["missing_bindings"] == [["height", "window", "second"]]


def test_changed_saved_view_prevents_inheriting_unchanged_target(tmp_path):
    from tests.test_bim_view_references import viewed
    run, toolkit = setup_run(tmp_path)
    view = viewed(toolkit, [0, 4, 4, 7])
    row = adopt(toolkit, claim(objects=[dict(kind="window", id="window")],
        sources=[view["source_reference"]],
        values={"height": dict(type="literal", value=[1, 2], unit="m")}))
    candidate = notes(toolkit)
    path = run / "image_views" / (view["view_id"] + ".json")
    changed = json.loads(path.read_text())
    changed["box_original_pixels"] = [0, 0, 12, 8]
    path.write_text(json.dumps(changed))
    result = transaction(toolkit, candidate, [dict(claim_id=row["id"], action="confirm",
        operations=[window_check(row["id"])])])
    assert result["status"] == "failed"
    assert "source_view_changed" in result["entries"][0]["inheritance"][0]["targets"][0]["reasons"]
    assert not list((run / "claims").glob("confirmation_*.json"))


def test_retraction_cannot_be_implicitly_undone_by_transaction(tmp_path):
    run, toolkit = setup_run(tmp_path)
    row = window_claim(toolkit)
    toolkit.decide_claim(row["id"], "retracted", "conflicting source")
    result = transaction(toolkit, notes(toolkit), [dict(claim_id=row["id"], action="confirm",
        operations=[window_check(row["id"])])])
    assert result["status"] == "failed" and "retracted" in result["entries"][0]["error"]
    assert toolkit.claims().decisions()[row["id"]]["disposition"] == "retracted"
    assert not list((run / "claims").glob("confirmation_*.json"))


def test_sibling_and_origin_tampering_refuse_inheritance(tmp_path):
    run, toolkit = setup_run(tmp_path)
    branch = notes(toolkit)
    row = window_claim(toolkit, branch)
    sibling = notes(toolkit)
    with pytest.raises(ClaimBindingError, match="ancestry"):
        toolkit.confirm_claims(sibling, json.dumps([window_check(row["id"])]))
    descendant = notes(toolkit, branch)
    path = run / branch / "proposal.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="ancestry changed"):
        toolkit.confirm_claims(descendant, json.dumps([window_check(row["id"])]))


def test_transaction_stdio_and_readonly_boundary(tmp_path):
    async def scenario():
        run, toolkit = setup_run(tmp_path)
        async with _server_session(run, readonly=True) as session:
            assert "claim_transaction" not in {t.name for t in (await session.list_tools()).tools}
        async with _server_session(run, readonly=False) as session:
            result = _json_result(await session.call_tool("claim_transaction", dict(candidate="seed",
                entries_json=json.dumps([dict(claim=claim(), action="apply", operations=[edit("$claim")])]))))
            assert result["status"] == "completed"
    asyncio.run(scenario())


def test_shared_behaviour_record_counts_transactions_and_preserves_partial_failure(tmp_path):
    from src.agent.runtime_behaviour import summarise
    run, toolkit = setup_run(tmp_path)
    result = transaction(toolkit, "seed", [dict(claim=claim(), action="apply", operations=[edit("$claim")]),
        dict(claim_id="claim_9999", action="confirm", operations=[edit("claim_9999")])])
    assert result["status"] == "partial"
    step = dict(index=1, t_call=0, tool="claim_transaction", arguments={},
        result_text=json.dumps(result), result_data=result, is_error=False, model_text_before="", thinking_tokens_before=0)
    record = dict(run="fixture", receipt={}, source_format="event_envelope_jsonl",
        _source_root=str(run), gaps=[], invocations=[dict(invocation=1, stream="events", steps=[step])])
    summary = summarise(record)
    assert summary["tools"]["claim_transaction"] == 1
    assert summary["claim_transactions"] == dict(calls=1, entries=2,
        status_counts={"applied":1, "failed":1}, recorded_claims=1, applied_entries=1)
    assert summary["claims_from_saved_files"] == 1
    assert summary["domain_failures"] == 1 and summary["call_errors"] == 0
