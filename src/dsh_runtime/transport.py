"""Verified pre-write reconnection and pooled official Responses transport."""
from contextlib import contextmanager
import time
import httpx

def public_error(exc):
    causes = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        causes.append(
            {k: v for k, v in {'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None), 'winerror': getattr(exc, 'winerror', None)}.items() if v is not None},
        )
        exc = exc.__cause__ or exc.__context__
    return causes

class PooledTransport:
    """No retry after request headers/body begin, server HTTP status, or SSE reception."""

    def __init__(self, endpoint, audit, *, client=None, wait=time.sleep):
        self.audit = audit
        self.safe_to_repeat = False
        self.endpoint = endpoint
        self.client = (
            client
            or httpx.Client(
                trust_env=False,
                follow_redirects=False,
                http2=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=300),
            )
        )
        self.wait = wait
        self.turn = None

    @contextmanager
    def stream(self, method, url, *, content, headers, timeout):
        if method != 'POST' or url != self.endpoint:
            raise ValueError('route not admitted')
        self.safe_to_repeat = False
        delays = (1, 2, 4, 8)
        response = None
        for attempt in range(1, len(delays) + 2):
            self.safe_to_repeat = False
            trace = []

            def observe(event, info):
                if event.endswith(('.started', '.failed')):
                    trace.append(event)
            req = self.client.build_request(
                method,
                url,
                content=content,
                headers=headers,
                timeout=timeout,
                extensions={'trace': observe},
            )
            try:
                response = self.client.send(req, stream=True, follow_redirects=False)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                started = any(('send_request_' in ev for ev in trace))
                establishing = any((ev.endswith(('connect_tcp.started', 'start_tls.started')) for ev in trace))
                verified = establishing and (not started)
                self.safe_to_repeat = verified
                causes = public_error(exc)
                cert_error = any((node['type'] == 'SSLCertVerificationError' for node in causes))
                repeat = verified and (not cert_error) and (attempt <= len(delays))
                wait = delays[attempt - 1] if repeat else None
                self.audit(
                    'transport_connect_failure',
                    turn=self.turn,
                    attempt=attempt,
                    trace=trace,
                    causes=causes,
                    proven_pre_write=verified,
                    retry=repeat,
                    backoff_seconds=wait,
                )
                if not repeat:
                    raise
                self.wait(wait)
            except httpx.HTTPError as exc:
                self.audit(
                    'transport_nonretryable_error',
                    turn=self.turn,
                    attempt=attempt,
                    trace=trace,
                    causes=public_error(exc),
                    retry=False,
                    maybe_submitted=any(('send_request_' in ev for ev in trace)),
                )
                raise
            else:
                self.audit(
                    'upstream_http_headers',
                    turn=self.turn,
                    attempt=attempt,
                    status=response.status_code,
                    new_connection=any((ev.endswith('connect_tcp.started') for ev in trace)),
                )
                break
        try:
            yield response
        except httpx.HTTPError as exc:
            self.audit(
                'transport_stream_interrupted',
                turn=self.turn,
                causes=public_error(exc),
                response_headers_seen=True,
                retry=False,
            )
            raise
        finally:
            response.close()
