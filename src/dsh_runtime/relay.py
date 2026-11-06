"""One-run local Responses relay for the existing SIWC authorization. Never accepts an upstream URL."""
from __future__ import annotations
import asyncio
import copy
import gzip
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import httpx
from src.agent_runtime.responses import build_responses_body, ENDPOINT, TOOL_NAMESPACE
from src.agent_runtime.openai_subscription import SubscriptionCredentials
from .handoff import attest_response_input
from .transport import PooledTransport
MODEL = 'gpt-6.1-sol'
PREFIX = 'mcp__sim_bim__'
IO_TIMEOUT = 1500.0
CASE = CONTROL = WIRE = RUN = None

def configure(case, control, model):
    global CASE, CONTROL, WIRE, RUN, MODEL
    CASE, CONTROL = (Path(case).resolve(), Path(control).resolve())
    WIRE = CONTROL / 'wire/dsh'
    RUN = CASE / 'private/dsh/work/bim'
    MODEL = model

def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def clean(value):
    """Keep input media and public tools, while sealing private replay and suppressing credential fields."""
    if isinstance(value, list):
        return [clean(x) for x in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.lower().replace('-', '_') in {
                'authorization',
                'cookie',
                'set_cookie',
                'api_key',
                'access_token',
                'refresh_token',
                'password',
            }:
                result[key] = '[redacted]'
            elif (
                key in {'encrypted_content', 'reasoning_content', 'signature', 'reasoning_text'}
                and item is not None
            ):
                data = encoded(item)
                result[key + '_omitted'] = {'bytes': len(data), 'sha256': digest(data)}
            elif key == 'summary' and value.get('type') == 'reasoning':
                result['summary_omitted'] = True
            elif key == 'content' and value.get('type') == 'reasoning':
                data = encoded(item)
                result['content_omitted'] = {'bytes': len(data), 'sha256': digest(data)}
            else:
                result[key] = clean(item)
        return result
    return value

def save(path, value, *, compressed=False):
    buf = encoded(clean(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(gzip.compress(buf, compresslevel=1) if compressed else buf)
    temp.replace(path)

def cfg():
    shared = CASE / 'shared'
    return (
        json.loads((shared / 'initial-messages.json').read_bytes()),
        json.loads((shared / 'tools.json').read_bytes()),
    )

class Gate(Exception):
    pass

def collect_item(done, event):
    index, item = (event.get('output_index'), event.get('item'))
    if type(index) is not int or index < 0 or (not isinstance(item, dict)):
        raise Gate('invalid output_item.done')
    if index in done and done[index] != item:
        raise Gate('conflicting output_item.done')
    done[index] = item

def assembled(response, done):
    result = copy.deepcopy(response)
    output = result.get('output')
    if not isinstance(output, list):
        raise Gate('invalid terminal output')
    for index, item in sorted(done.items()):
        if index == len(output):
            output.append(item)
        elif index < len(output):
            final = output[index]
            if (
                not isinstance(final, dict)
                or any((k in final and final[k] != v for k, v in item.items()))
            ):
                raise Gate('conflicting terminal output')
            output[index] = {**item, **final}
        else:
            raise Gate('gap in output_item.done')
    return result

class Session:

    def __init__(self, messages, tools, limits, image_history=None):
        self.limits = limits
        self.image_history = image_history
        if image_history is not None:
            from .image_history import chat_tool
            if any(t['function']['name'] == chat_tool()['function']['name'] for t in tools):
                raise ValueError('Runtime image tool conflicts with a domain tool')
            tools = [*tools, chat_tool()]
        self.initial = build_responses_body(
            model=MODEL,
            messages=messages,
            tools=tools,
            parameters={'max_tokens': 128000, 'reasoning_effort': 'high'},
        )
        self.names = {x['function']['name'] for x in tools}
        self.history = []
        self.last_calls = []
        self.requests = 0
        self.tool_calls = 0
        self.stop = False
        self.pending = None

    def request(self, raw):
        if self.pending is not None:
            from .continuation import verified_resume
            from .image_history import native_tool
            body = verified_resume(
                self.pending, raw, RESUME_MARKER,
                runtime_tool=native_tool() if self.image_history is not None else None,
            )
            original_sha = digest(encoded(body))
            if self.image_history is not None:
                body = self.image_history.restore_full(body)
                body['tools'] = copy.deepcopy(self.initial['tools'])
            boundary = next(
                (i for i in range(len(body['input']) - 1, -1, -1) if body['input'][i].get('type') == 'function_call'),
                len(body['input']) - 1,
            )
            self.history = body['input'][:boundary + 1]
            previous = next(
                (i + 1 for i in range(boundary, -1, -1) if body['input'][i].get('type') == 'function_call_output'),
                0,
            )
            self.last_calls = [i for i in self.history[previous:] if i.get('type') == 'function_call']
            audit(
                'native_session_resumed',
                prior_turn=self.pending['turn'],
                body_sha256=original_sha,
                restored_full_body_sha256=digest(encoded(body)),
                runtime_image_overlay=self.image_history is not None,
                reexecuted_tools=0,
            )
            self.pending = None
            self.stop = False
            (CONTROL / 'pending.json').unlink(missing_ok=True)
            return body
        if raw.get('model') != MODEL or raw.get('stream') is not True:
            raise Gate('unexpected model or non-streaming request')
        if (raw.get('reasoning') or {}).get('effort') != 'high':
            raise Gate('reasoning effort differs from high')
        names = [f.get('name') for f in raw.get('tools', []) if f.get('type') == 'function']
        allowed = {native_name(n) for n in self.names}
        if (
            not isinstance(raw.get('tools'), list)
            or set(names) != allowed
            or len(raw['tools']) != len(allowed)
        ):
            raise Gate('client BIM tool catalog differs from frozen tools')
        body = copy.deepcopy(self.initial)
        if not self.requests:
            return body
        if not self.last_calls or self.stop:
            raise Gate('previous provider response did not request a tool')
        items = raw.get('input')
        if not isinstance(items, list):
            raise Gate('expected stateless Responses input list')
        ids = {a['call_id'] for a in self.last_calls}
        indices = [i for i, x in enumerate(items) if x.get('type') == 'function_call_output' and x.get('call_id') in ids]
        if {items[i]['call_id'] for i in indices} != ids or len(indices) != len(ids):
            raise Gate('unmatched latest function_call_output IDs')
        tail = copy.deepcopy(items[min(indices):])
        for item in tail:
            if item.get('type') == 'function_call_output' and item.get('call_id') in ids:
                continue
            if item.get('role') == 'user' and item.get('type') in {None, 'message'}:
                continue
            raise Gate('unexpected new item after the latest tool output')
        body['input'] = self.history + tail
        return body

    def observed(self, body, response):
        self.history = body['input'] + response.get('output', [])
        self.last_calls = [i for i in response.get('output', []) if i.get('type') == 'function_call']
        self.tool_calls += len(self.last_calls)

def native_name(name):
    from .image_history import NAME, PREFIX as IMAGE_PREFIX
    return (IMAGE_PREFIX if name == NAME else PREFIX) + name


def downstream(obj):
    """Native pi-ai uses flat names, upstream SIWC supports only namespaced functions."""
    if isinstance(obj, list):
        return [downstream(a) for a in obj]
    if isinstance(obj, dict):
        out = {k: downstream(v) for k, v in obj.items()}
        if out.get('type') == 'function_call':
            if out.get('namespace') not in {None, TOOL_NAMESPACE}:
                raise Gate('unexpected namespace')
            out.pop('namespace', None)
            if isinstance(out.get('name'), str) and not out['name'].startswith('mcp__'):
                out['name'] = native_name(out['name'])
        return out
    return obj

def evidence_event(obj):
    t = obj.get('type', '')
    if 'reasoning' in t:
        return {
            'type': t,
            'sequence_number': obj.get('sequence_number'),
            'payload_bytes': len(encoded(obj)),
        }
    if t.endswith('.delta'):
        return {
            'type': t,
            'sequence_number': obj.get('sequence_number'),
            'payload_bytes': len(encoded(obj)),
        }
    if t == 'response.completed':
        return {
            'type': t,
            'sequence_number': obj.get('sequence_number'),
            'response_id': obj.get('response', {}).get('id'),
        }
    return clean(obj)

def audit(event, **data):
    with (CONTROL / 'relay.jsonl').open('ab') as f:
        f.write(encoded({'time': time.time(), 'arm': 'dsh', 'event': event, **data}) + b'\n')

def provider_events(lines):
    name = None
    data = []
    for line in lines:
        if not line:
            if data:
                yield (name, '\n'.join(data))
            name, data = (None, [])
        elif line.startswith('event:'):
            name = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
    if data:
        yield (name, '\n'.join(data))

def wire_frame(name, obj):
    return (b'event: ' + name.encode() + b'\n' if name else b'') + b'data: ' + encoded(obj) + b'\n\n'

def local_completion():
    """DSH rejects empty output; a receipted local close is separate from provider output."""
    text = 'Verified whole-building handoff saved.'
    item = {
        'type': 'message',
        'role': 'assistant',
        'status': 'completed',
        'id': 'msg_local_attested_finish',
        'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
    }
    response = {
        'id': 'resp_local_attested_finish',
        'model': MODEL,
        'object': 'response',
        'status': 'in_progress',
        'created_at': int(time.time()),
        'output': [],
    }
    part = {'type': 'output_text', 'text': '', 'annotations': []}
    events = [
        ('response.created', {'response': response}),
        (
            'response.output_item.added',
            {'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}},
        ),
        (
            'response.content_part.added',
            {'output_index': 0, 'content_index': 0, 'item_id': item['id'], 'part': part},
        ),
        (
            'response.output_text.delta',
            {'output_index': 0, 'content_index': 0, 'item_id': item['id'], 'delta': text},
        ),
        ('response.output_item.done', {'output_index': 0, 'item': item}),
        (
            'response.completed',
            {
                'response': {
                    **response,
                    'status': 'completed',
                    'output': [item],
                    'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
                },
            },
        ),
    ]
    return b''.join(
        (wire_frame(kind, {'type': kind, 'sequence_number': i, **fields}) for i, (kind, fields) in enumerate(events)),
    )

def read_limits():
    limits = json.loads((CASE / 'shared/limits.json').read_bytes())
    for key in ('model_calls', 'tool_calls', 'wall_seconds'):
        value = limits[key]
        if key == 'wall_seconds' and value is None:
            continue
        if type(value) is not int or value <= 0:
            raise Gate('invalid frozen budget: ' + key)
    return limits

def remaining(deadline):
    if deadline is None:
        return IO_TIMEOUT
    available = deadline - time.time()
    if available <= 0:
        raise Gate('run wall deadline exceeded')
    return min(available, IO_TIMEOUT)

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *_):
        pass

    def error(self, status, why):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        try:
            self.wfile.write(encoded({'error': {'message': why}}))
        except OSError:
            pass

    def begin(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.flush()

    def do_POST(self):
        if self.path != '/dsh/v1/responses' or self.client_address[0] != '127.0.0.1':
            return self.error(404, 'unknown local route')
        if not hmac.compare_digest(self.headers.get('Authorization', ''), self.server.auth):
            return self.error(403, 'local relay authorization required')
        if not self.server.mutex.acquire(False):
            return self.error(409, 'another request is active')
        opened = False
        try:
            state = self.server.state
            if state.stop and state.pending is None:
                raise Gate('session already sealed')
            epoch = json.loads((CASE / 'private' / 'start.json').read_bytes())['epoch']
            if time.time() < epoch:
                raise Gate('run has not started')
            wall = state.limits['wall_seconds']
            deadline = None if wall is None else epoch + wall
            remaining(deadline)
            size = int(self.headers.get('Content-Length', '-1'))
            if size <= 0:
                raise Gate('invalid request length')
            raw = json.loads(self.rfile.read(size))
            body = state.request(raw)
            full_body = body
            if any((call.get('name') == 'finish_bim' for call in state.last_calls)):
                bim_run = CASE / 'private/dsh/work/bim'
                latest = body['input'][len(state.history):]
                check = attest_response_input(
                    bim_run,
                    state.last_calls + latest,
                    minimum_floors=self.server.minimum_floors,
                )
                if check:
                    review = check.get('mesh_review', {})
                    if (
                        check.get('scope_basis') != 'mesh_model_reported_review'
                        or review.get('status') != 'current'
                        or review.get('coverage') != 'whole_building'
                    ):
                        raise Gate('handoff proof disagrees with whole-building mesh contract')
                    save(CONTROL / 'dsh-relay-finish.json', check)
                    state.stop = True
                    audit(
                        'verified_local_finish_no_upstream',
                        candidate=check['candidate'],
                        call_id=check['call_id'],
                    )
                    self.begin()
                    opened = True
                    self.wfile.write(local_completion())
                    self.wfile.flush()
                    return
                audit(
                    'incomplete_finish_continued',
                    turn=state.requests,
                    call_ids=[c['call_id'] for c in state.last_calls if c['name'] == 'finish_bim'],
                )
            if (
                state.requests >= state.limits['model_calls']
                or state.tool_calls >= state.limits['tool_calls']
            ):
                raise Gate('run budget reached')
            available = remaining(deadline)
            token = asyncio.run(asyncio.wait_for(self.server.credential.get_access_token(), available))
            turn = state.requests + 1
            save(WIRE / f'{turn:02}-in.json.gz', raw, compressed=True)
            if state.image_history is not None:
                body, projection = state.image_history.project(full_body)
                save(WIRE / f'{turn:02}-full.json.gz', full_body, compressed=True)
                save(WIRE / f'{turn:02}-projection.json', projection)
                audit('image_history_projection', turn=turn, **projection)
            save(WIRE / f'{turn:02}-out.json.gz', body, compressed=True)
            left = remaining(deadline)
            state.requests = turn
            self.server.transport.turn = turn
            publish_budget(state)
            audit(
                'request',
                turn=turn,
                model=MODEL,
                body_sha256=digest(encoded(body)),
                bytes=len(encoded(body)),
                upstream=ENDPOINT,
            )
            started = time.time()
            with self.server.transport.stream(
                'POST',
                ENDPOINT,
                content=encoded(body),
                timeout=httpx.Timeout(left, connect=min(20, left)),
                headers={
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'text/event-stream',
                    'Content-Type': 'application/json',
                },
            ) as r:
                del token
                if not r.is_success:
                    raw_error = r.read()
                    try:
                        problem = json.loads(raw_error).get('error', {})
                    except ValueError:
                        problem = {}
                    public = {
                        'error': {
                            'type': problem.get('type'),
                            'code': problem.get('code'),
                            'param': problem.get('param'),
                            'status': r.status_code,
                        },
                    }
                    save(WIRE / f'{turn:02}-reply.json', public)
                    audit('provider_error', turn=turn, status=r.status_code, error=public['error'])
                    state.stop = True
                    if r.status_code == 429 or r.status_code >= 500:
                        mark_pending(state, raw, body, turn, f'HTTP {r.status_code}', False)
                    return self.error(
                        r.status_code,
                        'subscription upstream rejected request; see relay audit',
                    )
                self.begin()
                opened = True
                seen = None
                proposals = set()
                done = {}
                with gzip.open(WIRE / f'{turn:02}-events.jsonl.gz', 'wb', compresslevel=1) as log:
                    for name, data in provider_events(r.iter_lines()):
                        remaining(deadline)
                        if data == '[DONE]':
                            self.wfile.write(b'data: [DONE]\n\n')
                            self.wfile.flush()
                            continue
                        obj = json.loads(data)
                        if seen is None:
                            seen = time.time()
                            audit('first_event', turn=turn, seconds=seen - started)
                        log.write(
                            encoded({'time': time.time(), 'event': evidence_event(obj)}) + b'\n',
                        )
                        kind = obj.get('type')
                        if kind == 'response.output_item.done':
                            collect_item(done, obj)
                        if (
                            kind in {'response.output_item.added', 'response.output_item.done'}
                            and obj.get('item', {}).get('type') == 'function_call'
                        ):
                            proposals.add(obj['output_index'])
                            if state.tool_calls + len(proposals) > state.limits['tool_calls']:
                                raise Gate('tool call budget exceeded')
                        if kind in {'response.completed', 'response.failed', 'response.incomplete'}:
                            terminal = obj['response']
                            save(WIRE / f'{turn:02}-reply.json', terminal)
                            rep = assembled(terminal, done) if kind != 'response.failed' else terminal
                            save(WIRE / f'{turn:02}-assembled-reply.json', rep)
                            audit(
                                'response',
                                turn=turn,
                                seconds=time.time() - started,
                                upstream_model=rep.get('model'),
                                finish_reason=rep.get('status'),
                                usage=rep.get('usage'),
                                tool_calls=[it['name'] for it in rep.get('output', []) if it.get('type') == 'function_call'],
                            )
                            if kind == 'response.completed':
                                if state.tool_calls + sum(
                                    (i.get('type') == 'function_call' for i in rep.get('output', [])),
                                ) > state.limits['tool_calls']:
                                    raise Gate('tool call budget exceeded')
                                state.observed(full_body, rep)
                                publish_budget(state)
                            else:
                                state.stop = True
                        self.wfile.write(wire_frame(name, downstream(obj)))
                        self.wfile.flush()
                if not (WIRE / f'{turn:02}-reply.json').exists():
                    raise Gate('stream ended before terminal response')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.server.state.stop = True
            audit('relay_error', reason='downstream connection lost')
        except Exception as exc:
            self.server.state.stop = True
            reason = str(exc) if isinstance(exc, Gate) else type(exc).__name__
            audit('relay_error', reason=reason)
            recoverable = isinstance(exc, httpx.TransportError)
            if recoverable and 'body' in locals():
                mark_pending(state, raw, body, turn, reason, self.server.transport.safe_to_repeat)
            if not opened:
                self.error(502 if recoverable else 409, reason)
        finally:
            self.close_connection = True
            self.server.mutex.release()
RESUME_MARKER = 'Resume the interrupted provider request.'

def mark_pending(state, raw, body, turn, reason, safe_to_repeat):
    record = {'turn': turn, 'safe_to_repeat': safe_to_repeat, 'reason': reason,
              'body_sha256': digest(encoded(body))}
    state.pending = {**record, 'raw': raw, 'body': body}
    save(CONTROL / 'pending.json', record)

def publish_budget(state):
    manifest = json.loads((RUN / 'inputs.json').read_bytes())
    dimensions = {}
    for key, limit, used in (
        ('calls', state.limits['model_calls'], state.requests),
        ('tool_calls', state.limits['tool_calls'], state.tool_calls),
    ):
        remaining = max(0, limit - used)
        dimensions[key] = {'remaining': str(remaining), 'remaining_fraction': remaining / limit}
    save(
        RUN / '.harness_tmp/budget_status.json',
        {
            'schema_version': 1,
            'dimensions': dimensions,
            'run_directory': str(RUN),
            'started_epoch': manifest.get('started_epoch'),
        },
    )

def create_server(case, control, model, credential, minimum_floors=1, image_history=None):
    configure(case, control, model)
    CONTROL.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.state = Session(*cfg(), read_limits(), image_history=image_history)
    server.mutex = threading.Lock()
    server.credential = credential
    server.minimum_floors = minimum_floors
    server.transport = PooledTransport(ENDPOINT, audit)
    publish_budget(server.state)
    return server

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))
