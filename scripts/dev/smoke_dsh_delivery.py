"""Offline delivery recovery through the installed DSH, relay, and real BIM MCP.

Only loopback fake SSE providers are used. Historical geometry is copied into a
new cache fixture; this does not run a model or establish provider reliability,
architectural quality, or a fresh model-generated building result.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def runtime_sources():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / 'src/dsh_runtime').iterdir())
            if path.is_file() and path.suffix in {'.py', '.cjs'}}


# Capture before importing the runtime, so evidence cannot silently mix cached
# Python modules with a changed on-disk launch manifest.
RUNTIME_SOURCES_AT_IMPORT = runtime_sources()

from src.dsh_runtime import launcher, relay
from src.dsh_runtime.completion import DELIVERY_MARKER


DEFAULT_FIXTURE = Path('D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-fixed-20261106/private/dsh/work/bim')
DEFAULT_EXE = Path('D:/Program_AI/deepseek harness/DeepSeek Harness.exe')
CACHE = Path('D:/Sim-BIM-Agent/cache')
MODEL = 'offline-dsh-delivery-fixture'
TOKEN = 'offline-loopback-fixture-no-real-credential'


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(folder):
    return {str(path.relative_to(folder)): sha(path) for path in folder.rglob('*') if path.is_file()}


def block_external_network():
    """Fail closed if test wiring accidentally asks Python for a non-loopback socket."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def check(address):
        if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1'}:
            raise RuntimeError('Offline smoke blocked a non-loopback socket')

    def connect(sock, address):
        check(address)
        return original(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex

    class ForbiddenCredentials:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('Offline smoke must never load real credentials')

        @classmethod
        def legacy_project_registration(cls):
            return cls()

    launcher.SubscriptionCredentials = ForbiddenCredentials
    relay.SubscriptionCredentials = ForbiddenCredentials
    from src.agent_runtime import openai_subscription
    openai_subscription.SubscriptionCredentials = ForbiddenCredentials


class OfflineCredential:
    async def get_access_token(self):
        return TOKEN


def prepare_case(folder, source, executable):
    from scripts.tool_scripts.bim_agent_mesh_review import carry_mesh_review, mesh_review_status
    from scripts.tool_scripts.run_bim_agent import Toolkit
    from src.agent.runtime_tools import frozen_bim_client
    from src.agent_runtime.agent_registry import agent_version_record

    case = folder / 'case'
    run = case / 'private/dsh/work/bim'
    shutil.copytree(source, run)
    candidate = read(run / 'delivery_selection.json')['candidate']
    declaration = read(run / candidate / 'mesh_review.json')['declaration']
    declaration['summary'] = ('OFFLINE FIXTURE SETUP: copied historical source and overlays; '
                              'rebinding their existing evidence for a transport smoke test, '
                              'not a new visual inspection or model-quality judgment.')
    manifest = read(run / 'inputs.json')
    manifest['started_epoch'] = time.time()
    manifest['deadline_epoch'] = None
    manifest['mesh_input']['source_path'] = str(run / 'assets/input.glb')
    manifest['mesh_input']['geometry']['mesh_path'] = str(run / 'assets/input.glb')
    write(run / 'inputs.json', manifest)
    # Rebinding a copied fixture is explicit setup; the production attester is unchanged.
    carry_mesh_review(Toolkit(run), candidate, json.dumps({'mesh_review': declaration}))
    assert mesh_review_status(Toolkit(run), candidate)['status'] == 'current'
    for name in ('delivery.json', 'delivery.html', 'delivery_selection.json'):
        path = (run / name).resolve()
        assert path.is_relative_to(run.resolve())
        path.unlink(missing_ok=True)
    baseline_lines = len((run / 'tools.jsonl').read_bytes().splitlines())
    task = 'Offline recovery fixture. Complete the admitted saved building handoff through the scripted local provider.'
    shared = case / 'shared'
    shared.mkdir()
    (shared / 'task.txt').write_text(task, encoding='utf-8')
    (shared / 'guide.txt').write_text('Offline scripted recovery fixture; no model is connected.', encoding='utf-8')
    limits = {'wall_seconds': None, 'model_calls': 8, 'tool_calls': 12, 'candidates': 256}
    config = {'model': MODEL, 'dsh_executable': str(executable), 'limits': limits,
              'minimum_floor_elevations': 2, 'offline_fixture': True}
    write(case / 'config.json', config)
    write(shared / 'limits.json', limits)
    write(shared / 'initial-messages.json', [
        {'role': 'system', 'content': 'This is a local scripted transport test.'},
        {'role': 'user', 'content': task},
    ])

    async def catalog():
        async with frozen_bim_client(run, readonly=False, repository_root=ROOT) as client:
            return await client.list_tools()

    tools = asyncio.run(catalog())
    write(shared / 'tools.json', [{'type': 'function', 'function': {
        'name': tool['name'], 'description': tool.get('description', ''),
        'parameters': tool['inputSchema'],
    }} for tool in tools])
    write(shared / 'freeze.json', {
        'created': time.time(), 'domain': agent_version_record(ROOT, verify=True),
        'files': {str(path.relative_to(case)): sha(path) for path in
                  [case / 'config.json', *shared.glob('*.txt'), shared / 'limits.json',
                   shared / 'tools.json', shared / 'initial-messages.json']},
    })
    write(folder / 'fixture.json', {
        'source_run': str(source), 'candidate': candidate, 'historical_tool_lines': baseline_lines,
        'source_model_sha256': sha(run / candidate / 'source_model.json'),
        'fixture_setup': 'Copied historical geometry; refreshed manifest and rebound existing mesh evidence.',
        'architectural_quality': 'not_tested',
    })
    return case, run, candidate, baseline_lines


def frames(turn, output, *, partial=False, output_limit=False):
    response = {'id': f'resp_smoke_{turn}', 'object': 'response', 'model': MODEL,
                'created_at': int(time.time()), 'status': 'in_progress', 'output': []}
    events = [('response.created', {'response': response})]
    for index, item in enumerate(output):
        empty = {**item, 'status': 'in_progress'}
        if item['type'] == 'function_call':
            empty['arguments'] = ''
        elif item['type'] == 'message':
            empty['content'] = []
        events.append(('response.output_item.added', {'output_index': index, 'item': empty}))
        if item['type'] == 'function_call':
            events.append(('response.function_call_arguments.delta', {
                'output_index': index, 'item_id': item['id'], 'delta': item['arguments'],
            }))
            if partial:
                break
            events.append(('response.function_call_arguments.done', {
                'output_index': index, 'item_id': item['id'], 'arguments': item['arguments'],
            }))
        elif item['type'] == 'message':
            events.extend([
                ('response.content_part.added', {'output_index': index, 'content_index': 0,
                    'item_id': item['id'], 'part': {'type': 'output_text', 'text': '', 'annotations': []}}),
                ('response.output_text.delta', {'output_index': index, 'content_index': 0,
                    'item_id': item['id'], 'delta': item['content'][0]['text']}),
            ])
        events.append(('response.output_item.done', {'output_index': index, 'item': item}))
    if not partial or output_limit:
        terminal = 'response.incomplete' if output_limit else 'response.completed'
        events.append((terminal, {'response': {**response,
            'status': 'incomplete' if output_limit else 'completed', 'output': output,
            **({'incomplete_details': {'reason': 'max_output_tokens'}} if output_limit else {}),
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2,
                      'input_tokens_details': {'cached_tokens': 0},
                      'output_tokens_details': {'reasoning_tokens': 0}}}}))
    return b''.join(relay.wire_frame(kind, {'type': kind, 'sequence_number': index, **fields})
                    for index, (kind, fields) in enumerate(events))


def call(name, arguments, identity):
    return {'type': 'function_call', 'id': 'fc_' + identity, 'call_id': 'call_' + identity,
            'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}


def run_scenario(root, scenario, source, executable):
    folder = root / scenario
    folder.mkdir()
    case, run, candidate, baseline = prepare_case(folder, source, executable)
    requests, errors = [], []

    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                assert self.path == '/v1/responses'
                assert self.headers.get('Authorization') == 'Bearer ' + TOKEN
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                turn = len(requests)
                write(folder / f'upstream-request-{turn:02}.json', body)
                expected_requests = 2 if scenario == 'trailing-disconnect' else 3
                assert turn <= expected_requests, 'Unexpected extra upstream request; final finish should close locally'
                if turn == 1:
                    output = [call('inputs', {}, 'inputs_once')]
                elif turn == 2 and scenario == 'early-text':
                    output = [{'type': 'message', 'id': 'msg_early', 'role': 'assistant', 'status': 'completed',
                               'content': [{'type': 'output_text', 'text': 'Stopping before delivery.', 'annotations': []}]}]
                elif turn == 2 and scenario == 'trailing-disconnect':
                    output = [call('finish_bim', {'candidate': candidate}, 'finish_once')]
                elif turn == 2:
                    output = [
                        {'type': 'reasoning', 'id': 'rs_partial', 'summary': [], 'encrypted_content': 'offline-opaque'},
                        call('finish_bim', {'candidate': candidate}, 'incomplete_must_not_execute'),
                    ]
                else:
                    if scenario == 'early-text':
                        assert body['tool_choice'] == 'required'
                        assert body['input'][:-2] == requests[1]['input']
                        assert body['input'][-1] == {'role': 'user', 'content': [
                            {'type': 'input_text', 'text': DELIVERY_MARKER}]}
                    elif scenario == 'output-limit':
                        assert body['tool_choice'] == 'required'
                        assert body['input'][:-1] == requests[1]['input']
                        assert body['input'][-1] == {'role': 'developer', 'content':
                            'The previous output exceeded the response limit and was not executed. '
                            'Continue the same BIM task using one smaller tool action at a time; '
                            'inspect saved state and avoid repeating completed writes.'}
                    else:
                        assert body == requests[1], 'Interrupted request was not restored byte-equivalently'
                    output = [call('finish_bim', {'candidate': candidate}, 'finish_once')]
                partial = turn == 2 and scenario in {'partial-sse', 'eof', 'output-limit'}
                message = frames(turn, output, partial=partial,
                                 output_limit=turn == 2 and scenario == 'output-limit')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                if scenario != 'eof' or not partial:
                    truncated = turn == 2 and scenario in {'partial-sse', 'trailing-disconnect'}
                    self.send_header('Content-Length', str(len(message) + (31 if truncated else 0)))
                self.end_headers()
                self.wfile.write(message)
                self.wfile.flush()
                self.close_connection = True
            except Exception as error:
                errors.append(type(error).__name__ + ': ' + str(error))
                self.send_error(409)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous_endpoint = relay.ENDPOINT
    relay.ENDPOINT = f'http://127.0.0.1:{server.server_port}/v1/responses'
    try:
        result = launcher.run(case, credential=OfflineCredential())
    finally:
        relay.ENDPOINT = previous_endpoint
        server.shutdown()
        server.server_close()
    terminal = read(case / 'control/terminal.json')
    launches = [read(path) for path in (case / 'control').glob('launch-*.json')]
    assert launches and all(entry['runtime_files'] == RUNTIME_SOURCES_AT_IMPORT for entry in launches)
    tools = [json.loads(line) for line in (run / 'tools.jsonl').read_bytes().splitlines()[baseline:]]
    actions = [entry['action'] for entry in tools]
    executions = [read(path) for path in (case / 'private/dsh/session').rglob('execution.json')]
    sessions = {entry['native_session_id'] for entry in executions}
    proof = terminal.get('proof') or {}
    summary = {
        'scenario': scenario, 'offline': True, 'actual_provider_calls': 0,
        'simulated_provider_failures': scenario != 'early-text', 'real_service_stability_tested': False,
        'architectural_quality': 'not_tested', 'exit_code': result,
        'local_upstream_requests': len(requests), 'native_host_attempts': len(executions),
        'automatic_recoveries': terminal.get('automatic_recoveries'),
        'delivery_continuations': terminal.get('delivery_continuations'),
        'native_session_ids': sorted(str(value) for value in sessions),
        'new_domain_actions': actions, 'delivered': terminal['delivered'], 'errors': errors,
        'artifact_hashes_current': len(proof.get('artifact_sha256', {})) == 7 and all(sha(run / name) == digest
            for name, digest in proof.get('artifact_sha256', {}).items()),
        'source_geometry_unchanged': sha(run / candidate / 'source_model.json') == read(folder / 'fixture.json')['source_model_sha256'],
    }
    write(folder / 'verification.json', summary)
    assert result == 0 and terminal['delivered'] and not errors, summary
    expected_requests, expected_attempts = (2, 1) if scenario == 'trailing-disconnect' else (3, 2)
    assert len(requests) == expected_requests and len(executions) == expected_attempts, summary
    assert len(sessions) == 1 and None not in sessions, summary
    assert actions.count('inputs') == 1 and actions.count('finish_bim') == 1, summary
    assert summary['artifact_hashes_current'] and summary['source_geometry_unchanged'], summary
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument('--dsh-executable', type=Path, default=DEFAULT_EXE)
    scenarios = ('early-text', 'partial-sse', 'eof', 'output-limit', 'trailing-disconnect')
    parser.add_argument('--scenario', choices=('all', *scenarios), default='all')
    args = parser.parse_args()
    source = args.fixture.resolve()
    assert source.is_dir() and args.dsh_executable.is_file()
    CACHE.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='offline-dsh-delivery-', dir=CACHE))
    assert root.resolve().is_relative_to(CACHE.resolve())
    original = snapshot(source)
    write(root / 'source-fixture-before.json', original)
    write(root / 'runtime-sources-before.json', RUNTIME_SOURCES_AT_IMPORT)
    print('Offline evidence directory: ' + str(root), flush=True)
    block_external_network()
    scenarios = scenarios if args.scenario == 'all' else (args.scenario,)
    results = []
    try:
        for scenario in scenarios:
            assert runtime_sources() == RUNTIME_SOURCES_AT_IMPORT, 'Runtime source changed after import'
            results.append(run_scenario(root, scenario, source, args.dsh_executable))
    finally:
        unchanged = snapshot(source) == original
        runtime_after = runtime_sources()
        runtime_unchanged = runtime_after == RUNTIME_SOURCES_AT_IMPORT
        write(root / 'runtime-sources-after.json', runtime_after)
        write(root / 'verification.json', {'offline': True, 'actual_provider_calls': 0,
            'real_service_stability_tested': False, 'original_fixture_unchanged': unchanged,
            'runtime_source_unchanged': runtime_unchanged,
            'runtime_source_sha256': RUNTIME_SOURCES_AT_IMPORT,
            'results': results})
        assert unchanged, 'Original historical fixture changed'
        assert runtime_unchanged, 'Runtime source changed during this smoke process'
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
