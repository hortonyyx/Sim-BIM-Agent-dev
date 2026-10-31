"""F-158 甲案 — a resident BEHAVIOURAL self-check that the no-billed-calls egress
gate is actually live in *this* pytest process.

Where the other F-158 locks sit
--------------------------------
* ``tests/test_no_billed_provider_calls.py`` pins the gate's *mechanism* while
  it is known to be installed (raw socket / create_connection / urllib all get
  ``ProviderCallBlocked``).
* ``tests/test_f158_early_gate_regression.py`` reproduces, in a child process,
  the pre-parse bypass carriers the cross-family review found, and (see the
  ``teeth`` lock there) proves *this* self-check flips red when the gate is off.

This file asks a blunter question at run time, in-process:

    right now, does an actual outbound connection get stopped by the gate?

It opens a real socket to RFC 5737 TEST-NET-1 (``192.0.2.1`` — guaranteed
non-routable, never a provider) with a short timeout and NO proxy (a raw
``socket.connect`` goes straight to the IP and never consults ``*_proxy`` env
vars) and asserts the attempt is turned into ``ProviderCallBlocked``.

Why a behaviour probe and NOT a flag read
-----------------------------------------
It deliberately does **not** read ``no_billed_gate._INSTALLED`` — that flag is
a *proxy* for "the gate says it is installed". We measure the thing that
actually matters: "the gate really intercepts egress".

It is equally deliberate that this module does **not** ``import
no_billed_gate`` at top level. Importing that module runs ``_install()`` and
arms the gate — which would mask exactly the runs we want to catch (a startup
config that left the gate OFF: ``-o addopts=``, a ``PYTEST_ADDOPTS`` override,
``-c <other config>``, dropping ``-p no_billed_gate``, a foreign rootdir with
no ``tests/conftest.py``, …). The blocking exception is therefore identified by
class name/module, without importing anything.

Honest scope (see T2 note in ``no_billed_gate`` docstring)
------------------------------------------------------------
This is a **detector, not an interceptor**. A test that runs *before* this one
in the same process and truly reached the network has already spent the money;
this only guarantees you will *see red* for a run whose gate was off. It is a
backstop that depends on none of the wiring (addopts pin / conftest re-import /
import-time self-install) — it observes behaviour directly.
"""

from __future__ import annotations

import socket

# RFC 5737 TEST-NET-1: guaranteed non-routable, never a real provider. A raw
# socket.connect goes straight to this IP; no HTTP proxy layer is involved.
_TARGET = ("192.0.2.1", 80)
# Short: in the gate-OFF failure path this connect times out, and this tiny wait
# is the only wall time it adds. In the gate-ON path the wrapper raises before
# any packet leaves, so it costs nothing.
_TIMEOUT_S = 0.05


def _is_provider_call_blocked(exc: BaseException) -> bool:
    """True iff *exc* is the gate's ``ProviderCallBlocked`` — checked WITHOUT
    importing ``no_billed_gate`` (importing it would arm the gate and defeat
    the purpose of this probe)."""
    cls = type(exc)
    return cls.__name__ == "ProviderCallBlocked" and cls.__module__ == "no_billed_gate"


def test_egress_gate_behaviorally_blocks_a_real_connection():
    """The no-billed-calls gate must turn a real outbound connect into
    ``ProviderCallBlocked``. If it does not, the way this pytest process was
    started disabled the gate and billed provider calls are possible."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT_S)
    try:
        try:
            sock.connect(_TARGET)
        except BaseException as exc:  # noqa: BLE001 — classify first, then decide
            if _is_provider_call_blocked(exc):
                return  # gate is live and intercepted egress — the guarantee holds
            raise AssertionError(
                "F-158 egress gate is NOT installed in this pytest process: a raw "
                f"socket connection to TEST-NET {_TARGET} escaped the gate and "
                f"failed only at the network layer ({type(exc).__name__}: {exc}). "
                "The way this suite was started disabled the no-billed-calls gate "
                "(e.g. `-o addopts=`, a PYTEST_ADDOPTS override, `-c <other "
                "config>`, dropping `-p no_billed_gate`, or running from a "
                "rootdir with no tests/conftest.py), so tests in this run could "
                "make real, billed provider calls. Start pytest so addopts keeps "
                "`-p no_billed_gate` first (see pyproject.toml / "
                "no_billed_gate.py)."
            ) from exc
        else:
            raise AssertionError(
                f"F-158 egress gate did NOT intercept a connection to TEST-NET "
                f"{_TARGET}: connect() returned instead of raising "
                "ProviderCallBlocked. The gate is not installed in this pytest "
                "process — the way it was started disabled the no-billed-calls "
                "gate, so billed provider calls are possible."
            )
    finally:
        sock.close()
