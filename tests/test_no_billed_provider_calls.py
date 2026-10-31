"""Locks for the F-158 no-billed-calls egress gate (see tests/conftest.py).

These pin the gate's behaviour so a later edit that quietly removes its teeth
goes red. TEST-NET-1 (192.0.2.0/24, RFC 5737) is used as the "remote" target:
the gate fires *before* any real connection is attempted, so no packet ever
leaves — the address is only ever passed to the guarded ``connect``.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from no_billed_gate import ProviderCallBlocked, _is_local

# RFC 5737 TEST-NET-1: guaranteed non-routable, never a real provider.
_REMOTE = ("192.0.2.1", 80)


def test_is_local_recognizes_every_loopback_form():
    # Loopback / local IPC must pass through the gate.
    assert _is_local(("127.0.0.1", 443))
    assert _is_local(("127.5.6.7", 443))  # whole 127.0.0.0/8
    assert _is_local(("::1", 443))
    assert _is_local(("::ffff:127.0.0.1", 443))  # IPv4-mapped loopback
    assert _is_local(("localhost", 443))
    assert _is_local("/tmp/some.sock")  # AF_UNIX path
    assert _is_local(("0.0.0.0", 0))


def test_is_local_does_not_misjudge_a_real_external_address():
    # A routable public IP and a provider hostname must both read as remote.
    assert not _is_local(("93.184.216.34", 443))
    assert not _is_local(("api.deepseek.com", 443))
    assert not _is_local(("8.8.8.8", 53))


def test_raw_socket_connect_to_remote_is_blocked_and_names_this_test():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ProviderCallBlocked) as excinfo:
            s.connect(_REMOTE)
    finally:
        s.close()
    # The gate must name the offending test so a real failure is self-describing.
    assert "test_raw_socket_connect_to_remote_is_blocked_and_names_this_test" in str(
        excinfo.value
    )


def test_socket_create_connection_to_remote_is_blocked():
    # create_connection builds a socket and calls .connect() internally, so the
    # class-attribute wrap catches it too — this pins the actual provider path.
    with pytest.raises(ProviderCallBlocked):
        socket.create_connection(_REMOTE, timeout=0.1)


def test_connect_ex_to_remote_is_blocked():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ProviderCallBlocked):
            s.connect_ex(_REMOTE)
    finally:
        s.close()


def test_urllib_request_to_remote_is_blocked():
    # A high-level HTTP client (stdlib urllib) reaches the network through
    # socket.create_connection -> socket.connect, so the class-attribute wrap
    # catches it too. This makes the docstring claim "urllib is pinned" true
    # rather than merely argued (verdict N-3). urllib re-raises non-HTTP errors
    # as URLError, so ProviderCallBlocked surfaces wrapped in it.
    with pytest.raises((ProviderCallBlocked, urllib.error.URLError)) as excinfo:
        urllib.request.urlopen("http://192.0.2.1/", timeout=0.1)
    # Whatever the wrapper, the gate must be the root cause.
    root = excinfo.value
    if isinstance(root, urllib.error.URLError):
        root = root.reason
    assert isinstance(root, ProviderCallBlocked), (
        f"expected ProviderCallBlocked at the root, got {root!r}"
    )


def test_system_proxy_cannot_carry_a_remote_call():
    # With a loopback HTTP proxy configured (environment or the Windows
    # registry), every client request would be a local connection that the
    # gate must allow, and a billed call would leave through the proxy. The
    # gate removes the proxy settings so clients connect directly.
    import os

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert key not in os.environ
    assert os.environ.get("NO_PROXY") == "*"
    proxies = urllib.request.getproxies()
    assert not {scheme: url for scheme, url in proxies.items() if scheme != "no"}


def test_loopback_connection_is_allowed():
    # A test that talks to a local server it started must not be blocked.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))  # must NOT raise
        conn, _ = server.accept()
        conn.close()
    finally:
        client.close()
        server.close()


@pytest.mark.live
def test_live_marker_lifts_the_gate():
    """A @pytest.mark.live test may reach out; the gate must be lifted for it.

    Skipped in the default suite (select with -m live). Under the gate this
    would raise ProviderCallBlocked; lifted, connect_ex just returns an errno
    for the unreachable TEST-NET address instead.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.1)
    try:
        rc = s.connect_ex(_REMOTE)  # no ProviderCallBlocked => gate is lifted
        assert isinstance(rc, int)
    finally:
        s.close()
