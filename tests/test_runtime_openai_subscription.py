from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import src.agent_runtime.openai_subscription as subscription
from src.agent_runtime.openai_subscription import (
    REQUIRED_SCOPE,
    SubscriptionAuthError,
    SubscriptionCredentials,
    SubscriptionModel,
)


class _ReversingProtector:
    def protect(self, value: bytes) -> bytes:
        return value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value[::-1]


def _seed(
    credentials: SubscriptionCredentials,
    *,
    access_token: str = "access-before",
    refresh_token: str = "refresh-before",
    expires_at: float = 10_000.0,
) -> None:
    with credentials._store.locked():
        host_id = credentials._store.host_id()
        credentials._store.save(
            {
                "version": 1,
                "active_client_id": "oaiapp_test",
                "registrations": {
                    "oaiapp_test": {
                        "email": "person@example.test",
                        "issuer": subscription.ISSUER,
                        "subject": "account-subject",
                        "client_id": "oaiapp_test",
                        "ext_agent_host_id": host_id,
                        "id_token": "id-before",
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "access_expires_at": expires_at,
                        "earliest_refresh_at": None,
                        "scopes": list(subscription.SCOPES),
                        "saved_at": "2026-10-11T00:00:00+00:00",
                    }
                },
            }
        )


def _signing_material() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    return private_key, jwk


def _id_token(
    private_key: object,
    *,
    nonce: str,
    audience: str = "oaiapp_issued",
    issuer: str = subscription.ISSUER,
    subject: str = "account-subject",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "email": "person@example.test",
            "iat": now,
            "exp": now + 600,
            "nonce": nonce,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def _discovery(jwks_url: str = "https://auth.openai.com/test-jwks") -> dict[str, str]:
    return {
        "issuer": subscription.ISSUER,
        "jwks_uri": jwks_url,
        "revocation_endpoint": "https://auth.openai.com/test-revoke",
    }


def test_loopback_receiver_binds_127_0_0_1_dynamic_port_and_exact_path() -> None:
    receiver = subscription._LoopbackReceiver()
    parsed = urlparse(receiver.redirect_uri)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert isinstance(parsed.port, int) and parsed.port > 0
    assert parsed.path == subscription.CALLBACK_PATH

    result: dict[str, str] = {}

    def wait() -> None:
        result.update(receiver.wait(2))

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL under test
            f"{receiver.redirect_uri}?code=offline-code&state=offline-state",
            timeout=2,
        ) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == {"code": "offline-code", "state": "offline-state"}
    finally:
        receiver.close()


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_windows_dpapi_round_trip_does_not_leave_plaintext() -> None:
    protector = subscription._WindowsDpapiProtector()
    plaintext = b"offline-fake-subscription-credential"
    protected = protector.protect(plaintext)
    assert protected != plaintext
    assert plaintext not in protected
    assert protector.unprotect(protected) == plaintext


def test_valid_token_is_returned_without_network_and_store_is_protected(tmp_path: Path) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(credentials)

    assert asyncio.run(credentials.get_access_token()) == "access-before"
    assert calls == 0
    raw = (tmp_path / "credentials.bin").read_bytes()
    assert b"access-before" not in raw
    assert b"refresh-before" not in raw
    status = credentials.status()
    assert {key: status[key] for key in status if key != "accounts"} == {
        "signed_in": True,
        "client_id": "oaiapp_test",
        "email": "person@example.test",
        "scopes": list(subscription.SCOPES),
        "account_count": 1,
    }
    assert status["accounts"] == [
        {
            "client_id": "oaiapp_test",
            "email": "person@example.test",
            "active": True,
            "signed_in": True,
        }
    ]


def test_refresh_rotates_atomically_and_uses_exact_public_client_grant(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = parse_qs(request.content.decode())
        assert form == {
            "grant_type": ["refresh_token"],
            "client_id": ["oaiapp_test"],
            "refresh_token": ["refresh-before"],
            "resource": [subscription.RESOURCE],
        }
        return httpx.Response(
            200,
            json={
                "access_token": "access-after",
                "refresh_token": "refresh-after",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(subscription.SCOPES),
                "earliest_refresh_at": 1_100,
            },
        )

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(credentials, expires_at=1_030.0)

    assert asyncio.run(credentials.get_access_token()) == "access-after"
    assert [request.url for request in requests] == [httpx.URL(subscription.TOKEN_ENDPOINT)]
    with credentials._store.locked():
        record = credentials._store.load()["registrations"]["oaiapp_test"]
    assert record["access_token"] == "access-after"
    assert record["refresh_token"] == "refresh-after"
    assert record["access_expires_at"] == 4_600.0


def test_two_instances_serialize_rotating_refresh(tmp_path: Path) -> None:
    refresh_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.03)
        return httpx.Response(
            200,
            json={
                "access_token": "access-after",
                "refresh_token": "refresh-after",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(subscription.SCOPES),
            },
        )

    transport = httpx.MockTransport(handler)
    first = SubscriptionCredentials(
        tmp_path,
        transport=transport,
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    second = SubscriptionCredentials(
        tmp_path,
        transport=transport,
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(first, expires_at=900.0)

    async def run_both() -> list[str]:
        return list(await asyncio.gather(first.get_access_token(), second.get_access_token()))

    assert asyncio.run(run_both()) == ["access-after", "access-after"]
    assert refresh_calls == 1


def test_terminal_refresh_error_clears_tokens_without_leaking_them(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "refresh_token_reused",
                    "message": "do not expose refresh-before",
                }
            },
        )

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(credentials, expires_at=900.0)

    with pytest.raises(SubscriptionAuthError) as caught:
        asyncio.run(credentials.get_access_token())
    assert "refresh-before" not in str(caught.value)
    with credentials._store.locked():
        record = credentials._store.load()["registrations"]["oaiapp_test"]
    assert "access_token" not in record
    assert "refresh_token" not in record
    assert "id_token" not in record


def test_model_catalog_filters_visibility_and_preserves_server_order(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(subscription.MODELS_ENDPOINT)
        assert request.headers["authorization"] == "Bearer access-before"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "model-b", "display_name": "Model B", "visibility": "list"},
                    {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"},
                    {"slug": "model-a", "display_name": "Model A", "visibility": "list"},
                    {"slug": "incomplete", "visibility": "list"},
                ]
            },
        )

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(credentials)
    assert asyncio.run(credentials.list_models()) == [
        SubscriptionModel("model-b", "Model B"),
        SubscriptionModel("model-a", "Model A"),
    ]


@pytest.mark.parametrize("new_account", [False, True])
def test_dynamic_login_uses_loopback_pkce_validates_jwt_and_saves_issued_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_account: bool
) -> None:
    private_key, public_jwk = _signing_material()
    observed: dict[str, object] = {}

    class FakeReceiver:
        redirect_uri = "http://127.0.0.1:43123/auth/callback"

        def wait(self, _timeout: float) -> dict[str, str]:
            query = parse_qs(urlparse(str(observed["authorization_url"])).query)
            return {
                "code": "one-use-code",
                "state": query["state"][0],
                "client_id": "oaiapp_issued",
            }

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(subscription, "_LoopbackReceiver", FakeReceiver)

    def open_browser(url: str) -> bool:
        observed["authorization_url"] = url
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(subscription.TOKEN_ENDPOINT):
            auth_query = parse_qs(urlparse(str(observed["authorization_url"])).query)
            form = parse_qs(request.content.decode())
            verifier = form["code_verifier"][0]
            expected_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            assert auth_query["code_challenge"] == [expected_challenge]
            assert auth_query["client_id"] == [subscription.DYNAMIC_CLIENT_ID]
            assert "id_token_hint" not in auth_query
            assert "login_hint" not in auth_query
            assert auth_query["agent_name_hint"] == [subscription.APP_NAME]
            assert auth_query["ext_agent_host_id"][0].startswith("urn:uuid:")
            assert auth_query["redirect_uri"] == [FakeReceiver.redirect_uri]
            assert form["client_id"] == ["oaiapp_issued"]
            assert form["redirect_uri"] == [FakeReceiver.redirect_uri]
            return httpx.Response(
                200,
                json={
                    "access_token": "login-access",
                    "refresh_token": "login-refresh",
                    "id_token": _id_token(
                        private_key,
                        nonce=auth_query["nonce"][0],
                        subject="new-account-subject" if new_account else "account-subject",
                    ),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": " ".join(subscription.SCOPES),
                    "earliest_refresh_at": None,
                },
            )
        if request.url == httpx.URL(subscription.DISCOVERY_ENDPOINT):
            return httpx.Response(200, json=_discovery())
        if request.url == httpx.URL("https://auth.openai.com/test-jwks"):
            return httpx.Response(200, json={"keys": [public_jwk]})
        raise AssertionError(f"unexpected offline request: {request.url}")

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _browser_opener=open_browser,
    )
    if new_account:
        _seed(credentials)
        old_state = credentials._store.load()
        old_host_id = credentials._store.host_id()
    result = asyncio.run(credentials.login(new_account=new_account, timeout=1))

    assert result.client_id == "oaiapp_issued"
    assert result.email == "person@example.test"
    assert REQUIRED_SCOPE in result.scopes
    assert observed["closed"] is True
    assert credentials.status()["signed_in"] is True
    state = credentials._store.load()
    assert state["active_client_id"] == "oaiapp_issued"
    if new_account:
        assert len(state["registrations"]) == 2
        assert state["registrations"]["oaiapp_test"] == old_state["registrations"]["oaiapp_test"]
        assert state["registrations"]["oaiapp_issued"]["subject"] == "new-account-subject"
        assert state["registrations"]["oaiapp_issued"]["ext_agent_host_id"] == old_host_id
    raw = (tmp_path / "credentials.bin").read_bytes()
    assert b"login-access" not in raw
    assert b"login-refresh" not in raw


@pytest.mark.parametrize(
    ("audience", "issuer", "nonce_override", "expected"),
    [
        ("wrong-client", subscription.ISSUER, None, "could not be verified"),
        (
            "oaiapp_issued",
            "https://attacker.invalid",
            None,
            "could not be verified",
        ),
        ("oaiapp_issued", subscription.ISSUER, "wrong-nonce", "nonce did not match"),
    ],
)
def test_login_rejects_wrong_jwt_identity_claims_without_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audience: str,
    issuer: str,
    nonce_override: str | None,
    expected: str,
) -> None:
    private_key, public_jwk = _signing_material()
    observed: dict[str, str] = {}

    class FakeReceiver:
        redirect_uri = "http://127.0.0.1:43124/auth/callback"

        def wait(self, _timeout: float) -> dict[str, str]:
            query = parse_qs(urlparse(observed["url"]).query)
            return {"code": "code", "state": query["state"][0], "client_id": "oaiapp_issued"}

        def close(self) -> None:
            return

    monkeypatch.setattr(subscription, "_LoopbackReceiver", FakeReceiver)

    def browser(url: str) -> bool:
        observed["url"] = url
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(subscription.TOKEN_ENDPOINT):
            requested_nonce = parse_qs(urlparse(observed["url"]).query)["nonce"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "must-not-save",
                    "refresh_token": "must-not-save-either",
                    "id_token": _id_token(
                        private_key,
                        nonce=nonce_override or requested_nonce,
                        audience=audience,
                        issuer=issuer,
                    ),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": " ".join(subscription.SCOPES),
                },
            )
        if request.url == httpx.URL(subscription.DISCOVERY_ENDPOINT):
            return httpx.Response(200, json=_discovery())
        return httpx.Response(200, json={"keys": [public_jwk]})

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _browser_opener=browser,
    )
    with pytest.raises(SubscriptionAuthError, match=expected):
        asyncio.run(credentials.login(timeout=1))
    assert credentials.status() == {
        "signed_in": False,
        "account_count": 0,
        "accounts": [],
    }


def test_callback_state_mismatch_stops_before_token_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeReceiver:
        redirect_uri = "http://127.0.0.1:43125/auth/callback"

        def wait(self, _timeout: float) -> dict[str, str]:
            return {"code": "code", "state": "attacker", "client_id": "oaiapp_issued"}

        def close(self) -> None:
            return

    monkeypatch.setattr(subscription, "_LoopbackReceiver", FakeReceiver)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _browser_opener=lambda _url: True,
    )
    with pytest.raises(SubscriptionAuthError, match="state did not match"):
        asyncio.run(credentials.login(timeout=1))
    assert calls == 0


def test_logout_revokes_refresh_token_then_clears_local_tokens(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(subscription.DISCOVERY_ENDPOINT):
            return httpx.Response(200, json=_discovery())
        assert request.url == httpx.URL("https://auth.openai.com/test-revoke")
        assert parse_qs(request.content.decode()) == {
            "token": ["refresh-before"],
            "token_type_hint": ["refresh_token"],
            "client_id": ["oaiapp_test"],
        }
        return httpx.Response(200)

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(credentials)

    result = asyncio.run(credentials.logout())
    assert result.signed_out is True
    assert result.revocation_confirmed is True
    status = credentials.status()
    assert status["signed_in"] is False
    assert status["client_id"] == "oaiapp_test"
    assert len(requests) == 2


def test_logout_network_failure_still_clears_tokens_and_reports_unconfirmed(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _clock=lambda: 1_000.0,
    )
    _seed(credentials)

    result = asyncio.run(credentials.logout())
    assert result.signed_out is True
    assert result.revocation_confirmed is False
    assert credentials.status()["signed_in"] is False


def test_missing_direct_scope_never_persists_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key, public_jwk = _signing_material()
    observed: dict[str, str] = {}

    class FakeReceiver:
        redirect_uri = "http://127.0.0.1:43126/auth/callback"

        def wait(self, _timeout: float) -> dict[str, str]:
            query = parse_qs(urlparse(observed["url"]).query)
            return {"code": "code", "state": query["state"][0], "client_id": "oaiapp_issued"}

        def close(self) -> None:
            return

    monkeypatch.setattr(subscription, "_LoopbackReceiver", FakeReceiver)

    def browser(url: str) -> bool:
        observed["url"] = url
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(subscription.TOKEN_ENDPOINT):
            nonce = parse_qs(urlparse(observed["url"]).query)["nonce"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "must-not-save",
                    "refresh_token": "must-not-save",
                    "id_token": _id_token(private_key, nonce=nonce),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "openid profile email offline_access resource.invoke",
                },
            )
        if request.url == httpx.URL(subscription.DISCOVERY_ENDPOINT):
            return httpx.Response(200, json=_discovery())
        return httpx.Response(200, json={"keys": [public_jwk]})

    credentials = SubscriptionCredentials(
        tmp_path,
        transport=httpx.MockTransport(handler),
        _protector=_ReversingProtector(),
        _browser_opener=browser,
    )
    with pytest.raises(SubscriptionAuthError, match="plan access was not granted"):
        asyncio.run(credentials.login(timeout=1))
    assert credentials.status() == {
        "signed_in": False,
        "account_count": 0,
        "accounts": [],
    }
