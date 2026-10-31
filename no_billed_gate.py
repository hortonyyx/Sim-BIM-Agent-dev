"""Block non-local network calls before offline tests import application code.

The plugin loads through pytest addopts before test and fixture imports. Explicit
live acceptance uses the product command outside pytest and records its own use.
"""

from __future__ import annotations

import ipaddress
import os
import socket

import pytest

# Bind the genuine implementations once, at import, before anything wraps them.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex

# Hostnames (not yet resolved to an IP) that denote local IPC and never bill.
_LOCAL_NAMES = frozenset(
    {"localhost", "ip6-localhost", "ip6-loopback", "0.0.0.0", "::", ""}
)

# Per-process scope readout (see module docstring / verdict N-1). NOT the
# authoritative "no billed call" evidence — the FAILED-test set is.
_BLOCKED: list[tuple[str, str]] = []

# Lifted (True) only while a @pytest.mark.live test body runs.
_LIVE_ACTIVE = False

# Guard so a second import / a redundant pytest_configure re-install does not
# wrap the wrapper (we always rebuild from the saved real implementations).
_INSTALLED = False

# A system HTTP proxy (environment or, on Windows, the registry that urllib,
# httpx and requests all consult) sends every client request to a loopback
# port, which the locality test must allow; a billed request would then leave
# through the proxy unseen. Outside live tests the gate therefore removes the
# proxy variables and sets NO_PROXY=* so clients connect directly and stay
# visible to the wrapper. Windows os.environ is case-insensitive, so both
# spellings are handled.
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")
_SAVED_PROXY_ENV = {key: os.environ[key] for key in _PROXY_KEYS if key in os.environ}


def _disable_proxies() -> None:
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def _restore_proxies() -> None:
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)
    os.environ.update(_SAVED_PROXY_ENV)


class ProviderCallBlocked(RuntimeError):
    """Raised when a test tries to emit a real, billable network request."""


def _is_local(address: object) -> bool:
    """True iff connecting to *address* cannot bill a remote provider."""
    # AF_UNIX endpoints are a plain str/bytes path -> always local IPC.
    if not isinstance(address, tuple) or not address:
        return True
    host = str(address[0])
    if host in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A hostname that isn't a known-local name (e.g. api.deepseek.com).
        # connect() usually receives an already-resolved IP, but if a bare
        # hostname reaches here, treat it as remote — fail closed.
        return False
    if ip.is_loopback or ip.is_unspecified:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and (mapped.is_loopback or mapped.is_unspecified):
        return True
    return False


def _current_test() -> str:
    # pytest sets this for the whole life of each test item; absent during
    # collection / import, which we label explicitly rather than as a test.
    raw = os.environ.get("PYTEST_CURRENT_TEST", "<collection/import phase>")
    return raw.split(" (")[0]


def _record(test: str, dest: object) -> None:
    _BLOCKED.append((test, repr(dest)))
    log = os.environ.get("SIM_BIM_NO_BILLED_LOG")
    if log:
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{test}\t{dest!r}\n")
        except OSError:
            pass


def _guarded(orig):
    def wrapper(self, address, *args, **kwargs):
        if _LIVE_ACTIVE or _is_local(address):
            return orig(self, address, *args, **kwargs)
        test = _current_test()
        _record(test, address)
        raise ProviderCallBlocked(
            f"Offline network guard: test '{test}' tried to open a real "
            f"network connection to {address!r}. This would emit a real, billed "
            f"provider request. Give it a fake/stub so it runs offline, or — only "
            f"if it is meant to hit a live provider — mark it @pytest.mark.live "
            f"(excluded from the default suite; run with -m live)."
        )

    return wrapper


def _install() -> None:
    """Idempotent: always rebuilds the wrapper from the saved real methods."""
    global _INSTALLED
    socket.socket.connect = _guarded(_REAL_CONNECT)
    socket.socket.connect_ex = _guarded(_REAL_CONNECT_EX)
    _disable_proxies()
    _INSTALLED = True


def _uninstall() -> None:
    global _INSTALLED
    socket.socket.connect = _REAL_CONNECT
    socket.socket.connect_ex = _REAL_CONNECT_EX
    _restore_proxies()
    _INSTALLED = False


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: test may make a real, billed provider call; excluded from the "
        "default suite (select with -m live).",
    )
    # Defence in depth: the gate is already installed at import (below), but if
    # some other plugin restored the real methods, re-arm here.
    _install()


def pytest_unconfigure(config):
    _uninstall()


@pytest.fixture(autouse=True)
def _lift_gate_for_live(request):
    """Lift the egress gate for the duration of a @pytest.mark.live test."""
    global _LIVE_ACTIVE
    if request.node.get_closest_marker("live"):
        _LIVE_ACTIVE = True
        _restore_proxies()
        try:
            yield
        finally:
            _disable_proxies()
            _LIVE_ACTIVE = False
    else:
        yield


def pytest_collection_modifyitems(config, items):
    """Keep `live` tests out of the default suite. When the run explicitly asks
    for them (`-m live`) leave collection alone. This is the explicit-marker
    exit, NOT a no-API-key skip."""
    markexpr = getattr(config.option, "markexpr", "") or ""
    if "live" in markexpr:
        return
    skip_live = pytest.mark.skip(reason="live provider test; select with -m live")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip_live)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Per-process READOUT only (verdict N-1). Under xdist this reflects the
    master process, not workers; it is not the authoritative no-billed-call
    evidence — the FAILED-test set is."""
    if _BLOCKED:
        terminalreporter.section("Offline network guard: BLOCKED calls")
        for test, dest in _BLOCKED:
            terminalreporter.write_line(f"  BLOCKED {test} -> {dest}")
    else:
        terminalreporter.write_line(
            "Offline network guard READOUT (non-authoritative): 0 provider "
            "calls blocked in THIS process. Under -n parallelism this is the "
            "master process only, not workers; authoritative evidence that no "
            "billed call happened = the suite's FAILED-test set."
        )


# --- install at import: this is the fix. Everything above is machinery; the
# guarantee ("no test-owned import can bind the real connect") comes from this
# line running before any command-line -p plugin or initial conftest. ---
_install()
