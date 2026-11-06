"""Offline checks for transport attribution, bounded metadata and safe evidence."""
import hashlib
import json

import httpx
import pytest

from src.dsh_runtime.transport import PooledTransport, classify_failure, public_error


@pytest.mark.parametrize('new_connection', [False, True])
def test_partial_upstream_stream_keeps_diagnosis_without_replaying_or_leaking(new_connection):
    records = []
    requests = []
    secret = 'authorization-private-fixture-token'
    private_body = 'private-response-body-fixture'

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"type":"response.created"}\n\n'
            raise httpx.RemoteProtocolError(
                'peer closed connection without sending complete message body '
                f'(incomplete chunked read) Authorization: Bearer {secret} {private_body}')

    class Upstream(httpx.BaseTransport):
        def handle_request(self, request):
            requests.append(request)
            trace = request.extensions['trace']
            if new_connection:
                trace('connection.connect_tcp.started', {})
            trace('http11.send_request_headers.started', {})
            trace('http11.send_request_body.started', {})
            return httpx.Response(200, headers={
                'x-request-id': 'req_test-123', 'request-id': 'public:correlation.1',
                'set-cookie': secret, 'authorization': secret, 'x-private': private_body,
            }, stream=Interrupted())

    with httpx.Client(transport=Upstream()) as client:
        transport = PooledTransport('https://api.openai.com/v1/responses',
            lambda event, **fields: records.append({'event': event, **fields}), client=client)
        with pytest.raises(httpx.RemoteProtocolError):
            with transport.stream('POST', transport.endpoint, content=b'{}',
                                  headers={'Authorization': f'Bearer {secret}'}, timeout=1) as response:
                response.read()

    assert len(requests) == 1
    assert transport.safe_to_repeat is False
    failure = transport.last_failure
    assert failure['category'] == 'upstream_transport'
    assert failure['origin'] == 'unresolved'
    assert failure['phase'] == 'response_body'
    assert failure['new_connection'] is new_connection
    assert failure['request_ids'] == {'x-request-id': 'req_test-123', 'request-id': 'public:correlation.1'}
    assert failure['causes'][0]['message_code'] == 'peer_closed_incomplete_body'
    assert records[-1]['retry'] is False
    assert records[0]['request_ids'] == failure['request_ids']
    captured = json.dumps(records)
    assert secret not in captured and private_body not in captured
    assert 'set-cookie' not in captured and 'x-private' not in captured


def test_unknown_exception_content_and_invalid_ids_are_not_retained():
    secret = 'secret-token-fixture'
    exc = httpx.RemoteProtocolError(f'unknown protocol bytes Authorization: Bearer {secret}')
    chain = public_error(exc)
    assert chain[0]['message_code'] == 'unclassified'
    assert chain[0]['message_sha256'] == hashlib.sha256(str(exc).encode()).hexdigest()
    assert secret not in json.dumps(chain)

    records = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=b'finished', headers={'x-request-id': f'Bearer {secret}',
                                             'request-id': 'x' * 129}))) as client:
        transport = PooledTransport('https://api.openai.com/v1/responses',
            lambda event, **fields: records.append(fields), client=client)
        transport.last_failure = {'category': 'previous_failure'}
        with transport.stream('POST', transport.endpoint, content=b'{}', headers={}, timeout=1) as response:
            assert response.read() == b'finished'
        assert transport.last_failure is None
    assert records[0]['request_ids'] == {}
    assert secret not in json.dumps(records)


@pytest.mark.parametrize('exc,category', [
    (httpx.RemoteProtocolError('peer closed'), 'upstream_transport'),
    (httpx.LocalProtocolError('invalid request'), 'local_transport'),
    (httpx.PoolTimeout('pool unavailable'), 'local_transport'),
    (ValueError('invalid application state'), 'unclassified'),
])
def test_internal_errors_are_not_attributed_to_external_disconnect(exc, category):
    assert classify_failure(exc)['category'] == category
