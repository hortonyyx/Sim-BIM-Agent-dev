"""Behavioral offline checks of the native-host delivery/recovery driver."""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from src.dsh_runtime import lifecycle, relay
from src.dsh_runtime.completion import DELIVERY_MARKER, delivery_checkpoint
from src.dsh_runtime.lifecycle_evidence import work_snapshot


def _events(session, attempt, rows):
    path = lifecycle.attempt_directory(session, attempt) / 'events.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return path


def _end(error=False):
    return {'type': 'status', 'phase': 'turn_end', 'turn': 1,
            'reason': {'kind': 'error', 'error': {'code': 'PROVIDER_ERROR'}} if error else {'kind': 'completed'}}


def _tool_pair(name, cid):
    return [
        {'type': 'tool_call', 'tool': 'mcp__sim_bim__' + name, 'callId': cid,
         'input': {'candidate': 'candidate_01'}},
        {'type': 'tool_result', 'callId': cid, 'status': 'completed', 'result': '{}'},
    ]


@pytest.fixture
def context(tmp_path, monkeypatch):
    case = tmp_path / 'case'
    run = case / 'private/dsh/work/bim'
    run.mkdir(parents=True)
    (run / 'inputs.json').write_text('{"input":"original"}', encoding='utf-8')
    control = case / 'control'
    control.mkdir()
    (case / 'private/start.json').write_text(json.dumps({'epoch': time.time()}), encoding='utf-8')
    session = case / 'private/dsh/session'
    session.mkdir(parents=True)
    tools = [{'type': 'function', 'function': {'name': name, 'description': '',
              'parameters': {'type': 'object', 'properties': {}}}}
             for name in ('build_parametric_bim', 'finish_bim')]
    state = relay.Session([{'role': 'user', 'content': 'whole building'}], tools,
                          {'model_calls': 50, 'tool_calls': 100, 'wall_seconds': None})
    raw = {'model': relay.MODEL, 'stream': True, 'reasoning': {'effort': 'high'},
           'tools': [{'type': 'function', 'name': relay.native_name(name)} for name in sorted(state.names)],
           'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'whole building'}]}]}
    records = []
    monkeypatch.setattr(relay, 'CONTROL', control)
    monkeypatch.setattr(relay, 'audit', lambda event, **fields: records.append({'event': event, **fields}))
    return SimpleNamespace(case=case, run=run, control=control, session=session, raw=raw,
                           state=state, server=SimpleNamespace(state=state, minimum_floors=2),
                           records=records, waits=[])


def _pending(context, raw, body, *, snapshot=None):
    state = context.state
    state.requests += 1
    snapshot = work_snapshot(context.run) if snapshot is None else snapshot
    failure = {'category': 'upstream_transport', 'code': 'RemoteProtocolError',
               'message': 'Upstream response stream ended before its terminal event.',
               'external': True, 'retryable': True}
    state.pending = {'turn': state.requests, 'raw': copy.deepcopy(raw), 'body': copy.deepcopy(body),
                     'body_sha256': relay.digest(relay.encoded(body)), 'safe_to_repeat': False,
                     'failure': failure, 'work_snapshot': snapshot, 'reason': 'RemoteProtocolError'}
    state.failure = failure
    state.stop = True
    relay.save(context.control / 'pending.json', state.pending)


def _resume_raw(context):
    raw = copy.deepcopy(context.state.pending['raw'])
    raw['input'].append({'role': 'user', 'content': [{'type': 'input_text', 'text': relay.RESUME_MARKER}]})
    return raw


def _drive(context, host, *, first=1, resume=False):
    return lifecycle.drive(context.case, context.session, {}, context.server,
                           'http://127.0.0.1:1/dsh/v1', 'offline-nonce', first, resume, host,
                           wait=context.waits.append)


def _controlled_attestation(monkeypatch):
    """Only fake the separately tested domain attestation, not driver decisions."""
    inspected = []
    proof = {'candidate': 'candidate_01', 'verified_by': 'controlled_domain_attester'}

    def attest(run, logs, minimum_floors, recorded_proof=None):
        inspected.append({'logs': list(logs), 'recorded_proof': recorded_proof,
                          'minimum_floors': minimum_floors})
        rows = [json.loads(line) for path in logs for line in path.read_text().splitlines()]
        final_calls = [row for row in rows if row.get('type') == 'tool_call']
        if not final_calls or final_calls[-1].get('tool') != 'mcp__sim_bim__finish_bim':
            return None
        cid = final_calls[-1]['callId']
        receipt = [row for row in rows if row.get('type') == 'tool_result' and row.get('callId') == cid]
        return proof if len(receipt) == 1 and receipt[0].get('status') == 'completed' else None

    monkeypatch.setattr(lifecycle, 'attest_host_finish', attest)
    return inspected, proof


def test_resumed_host_auth_failure_is_not_misreported_as_no_progress(context):
    def host(case, session, config, url, nonce, attempt, mode):
        if attempt == 1:
            _pending(context, context.raw, context.state.initial)
        else:
            assert mode is True
            context.state.request(_resume_raw(context))
            context.state.failure = {'category': 'external_auth', 'code': 'authentication_network_error',
                'message': 'Authentication failed before model submission.', 'external': True, 'retryable': False}
        _events(session, attempt, [_end(error=True)])
        return 1
    result = _drive(context, host)
    assert result['failure']['category'] == 'external_auth'
    assert result['allowed_external_failure'] is True
    assert result['host_attempts_this_launch'] == 2
    assert result['model_requests'] == 1


def test_rejected_native_resume_cannot_reuse_stale_pending_forever(context):
    def host(case, session, config, url, nonce, attempt, mode):
        if attempt == 1:
            _pending(context, context.raw, context.state.initial)
        else:
            assert attempt == 2 and mode is True
            raw = _resume_raw(context)
            raw['input'][0]['content'][0]['text'] = 'mutated native history'
            with pytest.raises(ValueError):
                context.state.request(raw)
            context.state.failure = {'category': 'internal_error', 'code': 'ValueError',
                'message': 'Native continuation contract failed.', 'external': False, 'retryable': False}
        _events(session, attempt, [_end(error=True)])
        return 1
    result = _drive(context, host)
    assert result['failure']['category'] == 'internal_error'
    assert result['failure']['code'] == 'ValueError'
    assert result['allowed_external_failure'] is False
    assert result['host_attempts_this_launch'] == 2


def test_partial_provider_failure_restarts_same_session_without_reexecuting_closed_write(context, monkeypatch):
    inspected, proof = _controlled_attestation(monkeypatch)
    attempts, writes = [], []

    def host(case, session, config, url, nonce, attempt, mode):
        assert session == context.session
        attempts.append((attempt, mode))
        if attempt == 1:
            assert mode is False
            candidate = context.run / 'candidate_01/source_model.json'
            candidate.parent.mkdir()
            candidate.write_text('{"candidate":"saved-once"}', encoding='utf-8')
            writes.append(candidate.read_bytes())
            body = context.state.request(context.raw)
            _pending(context, context.raw, body)
            _events(session, attempt, _tool_pair('build_parametric_bim', 'build_1') + [_end(error=True)])
            return 1
        assert mode is True
        raw = _resume_raw(context)
        expected = copy.deepcopy(context.state.pending['body'])
        assert context.state.request(raw) == expected
        assert context.state.pending is None and not context.state.stop
        context.state.requests += 1
        _events(session, attempt, _tool_pair('finish_bim', 'finish_1') + [_end()])
        return 0

    terminal = _drive(context, host)
    assert attempts == [(1, False), (2, True)] and len(writes) == 1
    assert terminal['delivered'] and terminal['exit_code'] == 0
    assert terminal['proof'] == proof and terminal['automatic_recoveries'] == 1
    assert not terminal['pending'] and not terminal['allowed_external_failure']
    assert context.waits == [1]
    assert len(inspected) == 2 and len(inspected[-1]['logs']) == 2
    assert not (context.control / 'pending.json').exists()
    assert json.loads((context.control / 'terminal.json').read_text()) == terminal


def test_persistent_external_disconnect_stops_after_three_verified_retries(context, monkeypatch):
    inspected, _ = _controlled_attestation(monkeypatch)
    attempts = []

    def host(case, session, config, url, nonce, attempt, mode):
        attempts.append(mode)
        assert attempt <= 4, 'External retry policy must be bounded'
        raw = context.raw if attempt == 1 else _resume_raw(context)
        body = context.state.request(raw)
        _pending(context, raw, body)
        _events(session, attempt, [_end(error=True)])
        return 1

    terminal = _drive(context, host)
    assert attempts == [False, True, True, True]
    assert context.waits == [1, 2, 4]
    assert terminal['status'] == 'external_error' and terminal['allowed_external_failure']
    assert terminal['automatic_recoveries'] == 3 and terminal['host_attempts_this_launch'] == 4
    assert terminal['failure']['retries_exhausted'] and terminal['pending']
    assert not terminal['delivered'] and terminal['exit_code'] != 0
    assert len(inspected) == 4


def test_text_only_successful_host_exit_continues_native_checkpoint_until_attested_finish(context, monkeypatch):
    inspected, proof = _controlled_attestation(monkeypatch)
    attempts = []
    output = {'id': 'msg_text', 'type': 'message', 'role': 'assistant', 'status': 'completed',
              'content': [{'type': 'output_text', 'text': 'Here is the draft.', 'annotations': []}]}

    def host(case, session, config, url, nonce, attempt, mode):
        attempts.append(mode)
        if attempt == 1:
            body = context.state.request(context.raw)
            response = {'status': 'completed', 'output': [output]}
            context.state.requests += 1
            context.state.observed(body, response)
            context.state.request_snapshot = work_snapshot(context.run)
            context.state.completion_checkpoint = delivery_checkpoint(context.raw, body, response)
            relay.save(context.control / 'continuation.json', context.state.completion_checkpoint)
            _events(session, attempt, [{'type': 'text', 'text': 'Here is the draft.'}, _end()])
            return 0
        assert mode == 'delivery' and context.state.delivery_requested
        raw = copy.deepcopy(context.raw)
        raw['input'] += [output, {'role': 'user', 'content': [{'type': 'input_text', 'text': DELIVERY_MARKER}]}]
        body = context.state.request(raw)
        assert body['tool_choice'] == 'required'
        assert context.state.completion_checkpoint is None
        context.state.requests += 1
        _events(session, attempt, _tool_pair('finish_bim', 'finish_1') + [_end()])
        return 0

    terminal = _drive(context, host)
    assert attempts == [False, 'delivery']
    assert terminal['delivered'] and terminal['proof'] == proof
    assert terminal['delivery_continuations'] == 1 and terminal['automatic_recoveries'] == 0
    assert len(inspected) == 2 and not context.waits


@pytest.mark.parametrize('forged_proof', [False, True])
def test_exit_zero_and_saved_proof_file_cannot_replace_fresh_attestation(context, monkeypatch, forged_proof):
    inspected, _ = _controlled_attestation(monkeypatch)
    recorded = {'candidate': 'candidate_01', 'claimed_success': True}
    if forged_proof:
        relay.save(context.control / 'dsh-relay-finish.json', recorded)

    def host(case, session, config, url, nonce, attempt, mode):
        _events(session, attempt, [{'type': 'text', 'text': 'Finished'}, _end()])
        return 0

    terminal = _drive(context, host)
    assert len(inspected) == 1
    assert inspected[0]['recorded_proof'] == (recorded if forged_proof else None)
    assert not terminal['delivered'] and terminal['exit_code'] != 0
    assert not terminal['allowed_external_failure']
    assert terminal['failure']['code'] == 'host_exit_without_recoverable_state'


@pytest.mark.parametrize('unsafe', ['unknown_write', 'changed_checkpoint'])
def test_uncertain_write_or_changed_checkpoint_is_never_replayed(context, monkeypatch, unsafe):
    _controlled_attestation(monkeypatch)
    calls = []

    def host(case, session, config, url, nonce, attempt, mode):
        calls.append(attempt)
        assert attempt == 1, 'Unsafe writes must not be replayed'
        body = context.state.request(context.raw)
        _pending(context, context.raw, body)
        rows = _tool_pair('build_parametric_bim', 'build_1')
        if unsafe == 'unknown_write':
            rows.pop()
        else:
            (context.run / 'unexpected-write.json').write_text('{}', encoding='utf-8')
        _events(session, attempt, rows + [_end(error=True)])
        return 1

    terminal = _drive(context, host)
    assert calls == [1] and not context.waits
    assert terminal['status'] == 'internal_error' and not terminal['allowed_external_failure']
    assert terminal['failure']['code'] == 'recovery_state_unverified'
    assert terminal['automatic_recoveries'] == 0 and not terminal['delivered']


@pytest.mark.parametrize('limit', ['model_calls', 'tool_calls', 'wall_seconds'])
def test_budget_exhaustion_is_not_reported_as_external_failure(context, monkeypatch, limit):
    _controlled_attestation(monkeypatch)
    if limit == 'wall_seconds':
        context.state.limits[limit] = 0
    else:
        context.state.limits[limit] = 1

    def host(case, session, config, url, nonce, attempt, mode):
        body = context.state.request(context.raw)
        _pending(context, context.raw, body)
        if limit == 'tool_calls':
            context.state.tool_calls = 1
        _events(session, attempt, [_end(error=True)])
        return 1

    terminal = _drive(context, host)
    assert terminal['status'] == 'budget_exhausted'
    assert not terminal['allowed_external_failure'] and not terminal['delivered']
    assert terminal['automatic_recoveries'] == 0 and not context.waits


def test_recovery_host_that_makes_no_new_request_cannot_loop_indefinitely(context, monkeypatch):
    _controlled_attestation(monkeypatch)
    attempts = []

    def host(case, session, config, url, nonce, attempt, mode):
        attempts.append(attempt)
        assert attempt <= 3, 'Recovery repeated a stale pending request without making progress'
        if attempt == 1:
            body = context.state.request(context.raw)
            _pending(context, context.raw, body)
        _events(session, attempt, [_end(error=True)])
        return 1

    terminal = _drive(context, host)
    assert attempts == [1, 2]
    assert terminal['status'] == 'internal_error' and not terminal['allowed_external_failure']
    assert terminal['failure']['code'] == 'host_recovery_made_no_request'


@pytest.mark.parametrize('unsafe', ['changed_work', 'unclosed_tool', 'unclosed_turn'])
def test_explicit_resume_rejects_unverified_saved_state_before_starting_host(context, monkeypatch, unsafe):
    inspected, _ = _controlled_attestation(monkeypatch)
    body = context.state.request(context.raw)
    _pending(context, context.raw, body)
    rows = _tool_pair('build_parametric_bim', 'build_1') + [_end(error=True)]
    if unsafe == 'changed_work':
        (context.run / 'inputs.json').write_text('{"input":"changed-after-checkpoint"}', encoding='utf-8')
    elif unsafe == 'unclosed_tool':
        rows.pop(1)
    else:
        rows.pop()
    _events(context.session, 1, rows)
    hosts = []

    def host(*args):
        hosts.append(args)
        raise AssertionError('Explicit resume must validate saved state before starting the host')

    terminal = _drive(context, host, first=2, resume=True)
    assert not hosts and not inspected and not context.waits
    assert terminal['host_attempts_this_launch'] == 0 and terminal['host_exit_code'] is None
    assert terminal['status'] == 'internal_error' and terminal['exit_code'] != 0
    assert terminal['failure']['code'] == 'recovery_state_unverified'
    assert not terminal['failure']['inspection']['safe_to_resume']
    assert not terminal['delivered'] and not terminal['allowed_external_failure']
    assert terminal['pending'] and context.state.requests == 1
    admissions = [record for record in context.records if record['event'] == 'resume_admission_inspected']
    assert len(admissions) == 1 and not admissions[0]['safe_to_resume']


def test_explicit_resume_of_closed_unchanged_checkpoint_can_deliver(context, monkeypatch):
    inspected, proof = _controlled_attestation(monkeypatch)
    body = context.state.request(context.raw)
    _pending(context, context.raw, body)
    _events(context.session, 1, _tool_pair('build_parametric_bim', 'build_1') + [_end(error=True)])
    attempts = []

    def host(case, session, config, url, nonce, attempt, mode):
        attempts.append((attempt, mode))
        assert session == context.session
        assert context.state.request(_resume_raw(context)) == body
        context.state.requests += 1
        _events(session, attempt, _tool_pair('finish_bim', 'finish_1') + [_end()])
        return 0

    terminal = _drive(context, host, first=2, resume=True)
    assert attempts == [(2, True)] and terminal['host_attempts_this_launch'] == 1
    assert terminal['delivered'] and terminal['exit_code'] == 0 and terminal['proof'] == proof
    assert not terminal['pending'] and not terminal['allowed_external_failure']
    assert len(inspected) == 1 and len(inspected[0]['logs']) == 2
    assert terminal['automatic_recoveries'] == 0 and not context.waits
    admissions = [record for record in context.records if record['event'] == 'resume_admission_inspected']
    assert len(admissions) == 1 and admissions[0]['safe_to_resume']
