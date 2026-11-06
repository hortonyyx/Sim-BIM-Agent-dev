"""Offline authentication attribution, single-use refresh and privacy checks."""
import asyncio
import json
import ssl

import httpx
import pytest

from src.agent_runtime.openai_subscription import SubscriptionAuthError, _OAuthHttpError
from src.dsh_runtime import credentials, relay


class Credential:
    def __init__(self, result):
        self.result = result
        self.attempts = 0

    async def get_access_token(self):
        self.attempts += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _wrapped(inner, *, suppressed=False):
    outer = SubscriptionAuthError('private-wrapper-message')
    if suppressed:
        outer.__context__ = inner
        outer.__suppress_context__ = True
    else:
        outer.__cause__ = inner
    return outer


@pytest.fixture
def records(monkeypatch):
    records = []
    monkeypatch.setattr(relay, 'audit', lambda event, **fields: records.append({'event': event, **fields}))
    return records


def test_success_returns_token_without_logging(records):
    credential = Credential('private-token')
    assert asyncio.run(credentials.acquire_token(credential, 1)) == 'private-token'
    assert credential.attempts == 1 and not records


def test_suppressed_auth_network_context_is_classified_without_replay_or_leak(records):
    request = httpx.Request('POST', 'https://auth.example/token?secret=private-query',
                            headers={'Authorization': 'Bearer private-header'})
    error = httpx.ConnectError('private-network-message', request=request)
    credential = Credential(_wrapped(error, suppressed=True))
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and len(records) == 1 and not records[0]['retry']
    assert records[0]['phase'] == 'authentication' and records[0]['request_not_submitted']
    assert result.value.failure['category'] == 'external_auth'
    assert not result.value.failure['retryable']
    assert records[0]['diagnostic']['causes'] == [
        {'type': 'SubscriptionAuthError'}, {'type': 'ConnectError'}]
    assert 'private-' not in json.dumps(records)


@pytest.mark.parametrize('error_type', [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                                      httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout,
                                      httpx.RemoteProtocolError, httpx.CloseError])
def test_network_failures_never_replay_whole_credential_acquisition(records, error_type):
    credential = Credential(_wrapped(error_type('private-network-message')))
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and len(records) == 1
    failure = result.value.failure
    assert failure['category'] == 'external_auth' and failure['external']
    assert not failure['retryable'] and failure['attempts'] == 1
    assert not records[0]['retry'] and 'private-' not in json.dumps(records)


@pytest.mark.parametrize('status', [401, 403, 408, 429, 500, 503])
def test_auth_http_failures_are_explicit_external_without_whole_flow_retry(records, status):
    error = _OAuthHttpError('private-operation', status, 'private-provider-code')
    credential = Credential(_wrapped(error))
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and len(records) == 1
    failure = result.value.failure
    assert failure['category'] == 'external_auth' and failure['external']
    assert not failure['retryable'] and failure['diagnostic']['http_status'] == status
    assert not records[0]['retry'] and records[0]['request_not_submitted']
    assert 'private-' not in json.dumps(records)


@pytest.mark.parametrize('code', ['invalid_grant', 'refresh_token_expired', 'token_expired'])
def test_unusable_credentials_are_external_auth_without_retry(records, code):
    credential = Credential(_wrapped(_OAuthHttpError('refresh', 400, code)))
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    failure = result.value.failure
    assert credential.attempts == 1 and len(records) == 1
    assert failure['category'] == 'external_auth' and failure['external']
    assert not failure['retryable'] and failure['diagnostic']['oauth_code'] == code


@pytest.mark.parametrize('error', [
    SubscriptionAuthError('could not reach OpenAI'),
    SubscriptionAuthError('saved credentials are unavailable'),
    _wrapped(_OAuthHttpError('refresh', 400, 'private-unrecognized-code')),
    _wrapped(_OAuthHttpError('refresh', 404)),
    _wrapped(httpx.LocalProtocolError('private-local-error')),
    _wrapped(httpx.PoolTimeout('private-pool-error')),
    ValueError('private-local-error'),
])
def test_untyped_or_local_errors_never_claim_external_failure(records, error):
    credential = Credential(error)
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and len(records) == 1
    failure = result.value.failure
    assert failure['category'] == 'internal_error' and not failure['external']
    assert not failure['retryable'] and 'private-' not in json.dumps(records)


def test_local_error_during_network_error_handling_is_not_reclassified(records):
    error = OSError('private-save-failure')
    error.__context__ = httpx.ConnectError('private-earlier-network-failure')
    credential = Credential(error)
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert not result.value.failure['external'] and credential.attempts == 1
    assert records[0]['diagnostic']['causes'] == [{'type': 'OSError'}]


def test_httpx_status_error_uses_status_without_body_or_headers(records):
    response = httpx.Response(503, request=httpx.Request('POST', 'https://auth.example'),
                              headers={'Authorization': 'private-header'}, text='private-response')
    credential = Credential(httpx.HTTPStatusError('private-message', request=response.request,
                                                  response=response))
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and result.value.failure['external']
    assert records[0]['diagnostic']['http_status'] == 503 and not records[0]['retry']
    assert 'private-' not in json.dumps(records)


def test_connect_failure_after_token_rotation_never_reuses_consumed_refresh_token(records):
    class RotatedCredential:
        attempts = 0
        refresh_consumed = False

        async def get_access_token(self):
            self.attempts += 1
            assert not self.refresh_consumed, 'The refresh token has already been consumed'
            self.refresh_consumed = True
            # The owner validates a new ID token before persisting the rotated
            # credentials; this could be a failed OpenID or JWKS GET.
            raise _wrapped(httpx.ConnectError('private-post-refresh-jwks-error'))

    credential = RotatedCredential()
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and credential.refresh_consumed
    assert result.value.failure['external'] and not result.value.failure['retryable']
    assert len(records) == 1 and not records[0]['retry']


def test_certificate_failure_never_retries_authentication(records):
    error = httpx.ConnectError('private-certificate-error')
    error.__cause__ = ssl.SSLCertVerificationError('private-certificate-details')
    credential = Credential(_wrapped(error, suppressed=True))
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 1))
    assert credential.attempts == 1 and not result.value.failure['retryable']
    assert not records[0]['retry'] and 'private-' not in json.dumps(records)


def test_total_acquisition_timeout_does_not_claim_network_failure(records):
    class BlockedCredential:
        attempts = 0
        cancelled = False

        async def get_access_token(self):
            self.attempts += 1
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    credential = BlockedCredential()
    with pytest.raises(relay.ProviderInterruption) as result:
        asyncio.run(credentials.acquire_token(credential, 0.01))
    failure = result.value.failure
    assert credential.attempts == 1 and credential.cancelled
    assert failure['code'] == 'authentication_deadline_exceeded' and failure['deadline_exceeded']
    assert not failure['external'] and not failure['retryable']
    assert len(records) == 1 and records[0]['request_not_submitted']


def test_caller_cancellation_propagates_without_fabricated_auth_failure(records):
    class CancelledCredential:
        async def get_access_token(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(credentials.acquire_token(CancelledCredential(), 1))
    assert not records
