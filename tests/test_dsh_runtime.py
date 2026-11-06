"""Offline regressions for real Responses assembly, recovery, and DSH wire accounting."""
import copy
import json

import httpx
import pytest

from src.dsh_runtime import relay
from src.dsh_runtime.continuation import verified_resume
from src.dsh_runtime.transport import PooledTransport


def test_sparse_completed_stream_preserves_exact_opaque_and_rejects_conflicts():
    hidden={"id":"rs_1","type":"reasoning","encrypted_content":"private-fixture","summary":[]}
    call={"id":"fc_1","type":"function_call","name":"inputs","call_id":"call_1","arguments":"{}","namespace":"runtime"}
    done={}
    relay.collect_item(done,{"output_index":1,"item":call})
    relay.collect_item(done,{"output_index":0,"item":hidden})
    result=relay.assembled({"output":[]},done)
    assert result["output"]==[hidden,call]
    assert relay.assembled({"output":[hidden,call]},done)==result
    with pytest.raises(relay.Gate,match="conflicting"):
        relay.assembled({"output":[hidden,{**call,"arguments":"changed"}]},done)
    with pytest.raises(relay.Gate,match="gap"):
        relay.assembled({"output":[]},{1:call})


def test_resume_recovers_opaque_with_same_canonical_bytes_and_rejects_tool_mutation():
    hidden={"id":"rs_1","type":"reasoning","encrypted_content":"private-fixture","summary":[]}
    call={"id":"fc_1","type":"function_call","name":"inputs","call_id":"call_1","arguments":"{}"}
    receipt={"type":"function_call_output","call_id":"call_1","output":"actual saved result"}
    raw={"model":"fixture","input":[hidden,call,receipt]}
    body={"model":"fixture","input":[hidden,call,receipt],"stream":True}
    pending={"raw":relay.clean(raw),"body":relay.clean(body),"redacted":True,
             "body_sha256":relay.digest(relay.encoded(body))}
    resumed={**raw,"input":raw["input"]+[{"role":"user","content":[{"type":"input_text","text":relay.RESUME_MARKER}]}]}
    assert verified_resume(pending,resumed,relay.RESUME_MARKER)==body
    assert "private-fixture" not in json.dumps(pending)
    broken=copy.deepcopy(resumed)
    broken["input"][-2]["output"]="different result"
    with pytest.raises(ValueError,match="differs"):
        verified_resume(pending,broken,relay.RESUME_MARKER)
    broken=copy.deepcopy(resumed)
    broken["input"][0]["encrypted_content"]="different"
    with pytest.raises(ValueError,match="differs"):
        verified_resume(pending,broken,relay.RESUME_MARKER)


@pytest.mark.parametrize("error,started,attempts",[
    (httpx.ConnectError,False,5),(httpx.ConnectTimeout,False,5),
    (httpx.ConnectError,True,1),(httpx.WriteError,True,1),(httpx.ReadTimeout,True,1)])
def test_only_verified_connection_establishment_is_retried(error,started,attempts):
    count=0
    delays=[]
    records=[]
    class Broken(httpx.BaseTransport):
        def handle_request(self,request):
            nonlocal count
            count+=1
            trace=request.extensions["trace"]
            trace("connection.connect_tcp.started",{})
            if started:trace("http11.send_request_headers.started",{})
            raise error("fixture",request=request)
    with httpx.Client(transport=Broken()) as client:
        pool=PooledTransport("https://api.openai.com/v1/responses",
            lambda event,**kw:records.append((event,kw)),client=client,wait=delays.append)
        with pytest.raises(error):
            with pool.stream("POST",pool.endpoint,content=b"{}",headers={},timeout=httpx.Timeout(1)):
                pytest.fail("transport must fail")
    assert count==attempts
    assert delays==([1,2,4,8] if attempts==5 else [])
    assert pool.safe_to_repeat==(not started)


def test_project_legacy_registration_keeps_native_refresh_store(monkeypatch,tmp_path):
    from src.agent_runtime import openai_subscription as module
    path=tmp_path/"EnergyPlus-Agent/openai-subscription"
    path.mkdir(parents=True)
    (path/"credentials.bin").write_bytes(b"protected fixture")
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setattr(module.os,"name","nt")
    credentials=module.SubscriptionCredentials.legacy_project_registration()
    assert credentials.directory==path
    assert credentials._store._protector._entropy==b"EnergyPlus-Agent/openai-subscription/v1"
    assert credentials._store.lock_path==path/"credentials.lock"

@pytest.mark.parametrize("with_tool",[False,True])
def test_live_session_resume_handles_first_request_and_completed_tool_batch(tmp_path,monkeypatch,with_tool):
    monkeypatch.setattr(relay,"CONTROL",tmp_path)
    monkeypatch.setattr(relay,"audit",lambda *a,**k:None)
    state=relay.Session([{"role":"user","content":"fixture"}],[],{"model_calls":10,"tool_calls":20,"wall_seconds":None})
    raw={"input":[{"role":"user","content":[{"type":"input_text","text":"fixture"}]}]}
    body=copy.deepcopy(state.initial)
    if with_tool:
        call={"type":"function_call","call_id":"c","id":"fc","name":"inputs","arguments":"{}"}
        receipt={"type":"function_call_output","call_id":"c","output":"actual"}
        body["input"] += [call,receipt]
        raw["input"] += [call,receipt]
    state.pending={"raw":raw,"body":body,"turn":1}
    resumed={**raw,"input":raw["input"]+[{"role":"user","content":[{"type":"input_text","text":relay.RESUME_MARKER}]}]}
    assert state.request(resumed)==body
    assert len(state.last_calls)==int(with_tool)
    assert state.pending is None

def test_resume_attempts_append_after_every_existing_number(tmp_path):
    from src.dsh_runtime.launcher import next_attempt
    assert next_attempt(tmp_path)==1
    (tmp_path/"events.jsonl").write_text("")
    assert next_attempt(tmp_path)==2
    (tmp_path/"attempt-002").mkdir()
    assert next_attempt(tmp_path)==3
    (tmp_path/"attempt-007").mkdir()
    assert next_attempt(tmp_path)==8


def test_host_start_failure_keeps_execution_receipt_without_a_journal(tmp_path):
    from src.dsh_runtime.launcher import execute_host
    (tmp_path/"shared").mkdir()
    (tmp_path/"shared/task.txt").write_text("fixture",encoding="utf8")
    session=tmp_path/"session"
    config={"dsh_executable":str(tmp_path/"missing.exe"),"model":"gpt-6.1-sol"}
    assert execute_host(tmp_path,session,config,"http://127.0.0.1:1/dsh/v1","fixture-token",1,False)==1
    receipt=json.loads((session/"execution.json").read_text())
    assert receipt["native_session_id"] is None
    assert receipt["startup_error"]["type"]=="FileNotFoundError"

@pytest.mark.parametrize("during_stream",[False,True])
def test_safe_connect_retry_does_not_survive_later_uncertain_send(during_stream):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("after response headers")
            yield b""
    count=0
    class Sequence(httpx.BaseTransport):
        def handle_request(self,request):
            nonlocal count
            count+=1
            trace=request.extensions["trace"]
            trace("connection.connect_tcp.started",{})
            if count==1:
                raise httpx.ConnectError("before write",request=request)
            trace("http11.send_request_headers.started",{})
            if during_stream:
                return httpx.Response(200,stream=Interrupted())
            raise httpx.WriteError("possibly submitted",request=request)
    with httpx.Client(transport=Sequence()) as client:
        pool=PooledTransport("https://api.openai.com/v1/responses",lambda *a,**k:None,
                             client=client,wait=lambda _:None)
        with pytest.raises(httpx.TransportError):
            with pool.stream("POST",pool.endpoint,content=b"{}",headers={},timeout=httpx.Timeout(1)) as response:
                response.read()
    assert count==2
    assert pool.safe_to_repeat is False

@pytest.mark.parametrize("status,pending",[(429,True),(503,True),(400,False)])
@pytest.mark.parametrize('problem', [{'error': {'type': 'fixture'}}, [], {'error': 'temporarily unavailable'}, {'error': None}])
def test_http_rejection_is_recorded_with_classified_recovery(tmp_path,monkeypatch,status,pending,problem):
    import threading
    import time
    shared=tmp_path/"shared"
    shared.mkdir()
    run=tmp_path/"private/dsh/work/bim"
    run.mkdir(parents=True)
    epoch=time.time()
    (run/"inputs.json").write_text(json.dumps({"started_epoch":epoch}))
    (tmp_path/"private/start.json").write_text(json.dumps({"epoch":epoch}))
    for name,value in {
        "initial-messages.json":[{"role":"user","content":"fixture"}],"tools.json":[],
        "limits.json":{"model_calls":10,"tool_calls":20,"wall_seconds":None}}.items():
        (shared/name).write_text(json.dumps(value))
    class Credential:
        async def get_access_token(self):return "offline-fixture-token"
    server=relay.create_server(tmp_path,tmp_path/"control","gpt-6.1-sol",Credential())
    server.auth="Bearer local-fixture"
    server.transport.client.close()
    received=[]
    def reject(request):
        received.append(request)
        return httpx.Response(status,json=problem)
    server.transport.client=httpx.Client(transport=httpx.MockTransport(reject))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with httpx.Client(trust_env=False) as client:
            response=client.post(f"http://127.0.0.1:{server.server_port}/dsh/v1/responses",
                headers={"Authorization":server.auth},
                json={"model":"gpt-6.1-sol","stream":True,"reasoning":{"effort":"high"},"input":[],"tools":[]})
        assert response.status_code==status
        assert len(received)==1 and server.state.requests==1
        assert (server.state.pending is not None)==pending
        if pending:
            assert not server.state.pending["safe_to_repeat"]
            assert json.loads((tmp_path/"control/pending.json").read_text())["reason"]==f"HTTP {status}"
        assert "offline-fixture-token" not in (tmp_path/"control/relay.jsonl").read_text()
    finally:
        server.shutdown()
        server.server_close()
        server.transport.client.close()


def image_body():
    import base64
    def image(value):
        return {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(value).decode(), 'detail': 'high'}
    initial = {'role': 'user', 'content': [image(b'user-image'), {'type': 'input_text', 'text': 'original scope'}]}
    body = {'input': [initial], 'reasoning': {'effort': 'high'}}
    for batch in range(5):
        count = 4 if batch == 4 else 1
        calls = [{'type': 'function_call', 'name': 'view_image', 'call_id': f'call_{batch}_{i}',
                  'arguments': json.dumps({'name': f'image-{batch}-{i}'})} for i in range(count)]
        body['input'].extend(calls)
        body['input'].extend({'type': 'function_call_output', 'call_id': c['call_id'], 'output': [
            {'type': 'input_text', 'text': 'unaltered geometry and coordinates'}, image(c['call_id'].encode())]} for c in calls)
    body['input'].insert(1, {'type': 'reasoning', 'id': 'rs', 'encrypted_content': 'private fixture', 'summary': []})
    return body


def test_auth_disconnect_is_external_without_submitting_or_replaying_model_request(tmp_path):
    import threading
    import time
    from src.agent_runtime.openai_subscription import SubscriptionAuthError
    shared = tmp_path / 'shared'
    shared.mkdir()
    run = tmp_path / 'private/dsh/work/bim'
    run.mkdir(parents=True)
    epoch = time.time()
    (run / 'inputs.json').write_text(json.dumps({'started_epoch': epoch}))
    (tmp_path / 'private/start.json').write_text(json.dumps({'epoch': epoch}))
    for name, value in {
        'initial-messages.json': [{'role': 'user', 'content': 'fixture'}], 'tools.json': [],
        'limits.json': {'model_calls': 10, 'tool_calls': 20, 'wall_seconds': None},
    }.items():
        (shared / name).write_text(json.dumps(value))
    class Credential:
        attempts = 0
        async def get_access_token(self):
            self.attempts += 1
            try:
                raise httpx.ReadTimeout('private-credential-fixture')
            except httpx.HTTPError:
                raise SubscriptionAuthError('Token refresh could not reach OpenAI.') from None
    credential = Credential()
    server = relay.create_server(tmp_path, tmp_path / 'control', 'gpt-6.1-sol', credential)
    server.auth = 'Bearer local-fixture'
    server.transport.client.close()
    # An old Responses diagnostic must not overwrite the new auth evidence.
    server.transport.last_failure = {'category': 'local_transport', 'reason_code': 'UnsupportedProtocol'}
    def unexpected(request):
        pytest.fail('No model request may be submitted after credential acquisition failed')
    server.transport.client = httpx.Client(transport=httpx.MockTransport(unexpected))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(f'http://127.0.0.1:{server.server_port}/dsh/v1/responses',
                headers={'Authorization': server.auth},
                json={'model': 'gpt-6.1-sol', 'stream': True, 'reasoning': {'effort': 'high'}, 'input': [], 'tools': []})
        assert response.status_code == 502
        assert credential.attempts == 1 and server.state.requests == 0
        assert server.state.pending is None
        assert server.state.failure['category'] == 'external_auth'
        assert server.state.failure['diagnostic']['phase'] == 'authentication'
        audit = (tmp_path / 'control/relay.jsonl').read_text()
        assert 'private-credential-fixture' not in audit
        assert not (tmp_path / 'control/wire/dsh').exists()
    finally:
        server.shutdown()
        server.server_close()
        server.transport.client.close()


def test_image_projection_keeps_initial_and_three_complete_batches_and_exact_retrieval(tmp_path):
    import base64
    from src.dsh_runtime.image_history import ImageHistory, sha
    history = ImageHistory(tmp_path, 3)
    body = image_body()
    before = copy.deepcopy(body)
    projected, audit = history.project(body)
    assert body == before
    assert projected['input'][:2] == body['input'][:2]
    assert audit['tool_images_before'] == 8 and audit['tool_images_after'] == 6
    assert audit['retained_image_batches'] == [2, 3, 4]
    assert history.restore_full(projected) == body
    for record in history.records():
        result = history.call({'view_id': record['view_id'], 'sha256': record['sha256']})
        assert sha(base64.b64decode(result[1].data)) == record['sha256']
    with pytest.raises(ValueError, match='Exact registered'):
        history.call({'view_id': history.records()[0]['view_id'], 'sha256': '0' * 64})
    listed = json.loads(history.call({'action': 'list', 'limit': 2})[0].text)
    assert len(listed['images']) == 2
    assert listed['next_arguments']['before_index'] == 6
    # A subsequent exact retrieval is a new image-bearing batch, even if its bytes repeat.
    record = history.records()[0]
    original = json.loads((tmp_path / 'blocks' / record['block_sha256']).read_bytes())
    body['input'] += [
        {'type': 'function_call', 'call_id': 'read_1', 'name': 'read_context_image', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'read_1', 'output': [original]},
    ]
    next_projected, audit = history.project(body)
    assert next_projected['input'][-1] == body['input'][-1]
    assert audit['retained_image_batches'] == [3, 4, 5]
    assert history.restore_full(next_projected) == body


def test_image_overlay_resume_is_exact_and_projected_pending_restores_full_history(tmp_path, monkeypatch):
    from src.dsh_runtime.image_history import ImageHistory, native_tool
    monkeypatch.setattr(relay, 'CONTROL', tmp_path)
    monkeypatch.setattr(relay, 'audit', lambda *a, **k: None)
    history = ImageHistory(tmp_path / 'images')
    state = relay.Session([{'role': 'user', 'content': 'fixture'}], [],
                          {'model_calls': 10, 'tool_calls': 20, 'wall_seconds': None}, history)
    original = {**state.initial, 'input': image_body()['input'], 'tools': []}
    raw = copy.deepcopy(original)
    marker = {'role': 'user', 'content': [{'type': 'input_text', 'text': relay.RESUME_MARKER}]}
    resumed = {**raw, 'tools': [native_tool()], 'input': raw['input'] + [marker]}
    pending = {'raw': relay.clean(raw), 'body': relay.clean(original), 'redacted': True,
               'body_sha256': relay.digest(relay.encoded(original)), 'turn': 28}
    state.pending = pending
    full = state.request(resumed)
    assert full['input'] == original['input']
    assert full['tools'] == state.initial['tools']
    broken = copy.deepcopy(resumed)
    broken['tools'][0]['description'] += 'changed'
    with pytest.raises(ValueError, match='registered overlay'):
        verified_resume(pending, broken, relay.RESUME_MARKER, runtime_tool=native_tool())
    projected, _ = history.project(full)
    state.pending = {'raw': relay.clean(resumed), 'body': relay.clean(projected), 'redacted': True,
                     'body_sha256': relay.digest(relay.encoded(projected)), 'turn': 29}
    resumed_again = {**resumed, 'input': resumed['input'] + [marker]}
    assert state.request(resumed_again) == full
    state.observed(full, {'output': []})
    assert state.history == original['input']


@pytest.mark.parametrize('terminal,category,retryable,external,cleanup_error', [
    (None, 'external_transport', True, True, False),
    ({'status': 'incomplete', 'output': [], 'incomplete_details': {'reason': 'max_output_tokens'}},
     'model_output_incomplete', True, False, False),
    ({'status': 'completed', 'output': []}, 'model_output_incomplete', True, False, False),
    ({'status': 'failed', 'output': [], 'error': {'code': 'server_error'}},
     'external_service', True, True, False),
    ({'status': 'incomplete', 'output': [], 'incomplete_details': {'reason': 'content_filter'}},
     'external_policy', False, True, False),
    ({'status': 'completed', 'output': [{'id': 'fc_partial', 'type': 'function_call',
       'call_id': 'call_partial', 'name': 'build_bim', 'arguments': '{}', 'namespace': 'runtime'}]},
     None, False, None, False),
    ({'status': 'completed', 'output': [{'id': 'fc_partial', 'type': 'function_call',
       'call_id': 'call_partial', 'name': 'build_bim', 'arguments': '{}', 'namespace': 'runtime'}]},
     None, False, None, True),
])
def test_stream_interruptions_never_forward_an_executable_terminal(
        tmp_path, monkeypatch, terminal, category, retryable, external, cleanup_error):
    """A partial function proposal must not become an executed native tool."""
    import threading
    import time
    shared = tmp_path / 'shared'
    shared.mkdir()
    run = tmp_path / 'private/dsh/work/bim'
    run.mkdir(parents=True)
    epoch = time.time()
    (run / 'inputs.json').write_text(json.dumps({'started_epoch': epoch}))
    (tmp_path / 'private/start.json').write_text(json.dumps({'epoch': epoch}))
    for name, value in {
        'initial-messages.json': [{'role': 'user', 'content': 'fixture'}], 'tools.json': [],
        'limits.json': {'model_calls': 10, 'tool_calls': 20, 'wall_seconds': None},
    }.items():
        (shared / name).write_text(json.dumps(value))
    class Credential:
        async def get_access_token(self):
            return 'offline-fixture-token'
    server = relay.create_server(tmp_path, tmp_path / 'control', 'gpt-6.1-sol', Credential())
    server.auth = 'Bearer local-fixture'
    server.transport.client.close()
    events = [{'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress', 'output': []}},
              {'type': 'response.output_item.added', 'output_index': 0,
               'item': {'id': 'fc_partial', 'type': 'function_call', 'call_id': 'call_partial',
                        'name': 'build_bim', 'arguments': '', 'namespace': 'runtime'}}]
    if terminal is not None:
        if category is None:
            events.append({'type': 'response.output_item.done', 'output_index': 0,
                           'item': terminal['output'][0]})
        events.append({'type': 'response.' + terminal['status'], 'response': terminal})
    content = b''.join(relay.wire_frame(event['type'], event) for event in events)
    class TrailingDisconnect(httpx.SyncByteStream):
        def __iter__(self):
            yield content
            raise httpx.RemoteProtocolError('peer closed incomplete body after completed terminal')
        def close(self):
            if cleanup_error:
                raise httpx.CloseError('fixture close failed')
    server.transport.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=TrailingDisconnect(), headers={'Content-Type': 'text/event-stream'})))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    raw = {'model': 'gpt-6.1-sol', 'stream': True, 'reasoning': {'effort': 'high'}, 'input': [], 'tools': []}
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(f'http://127.0.0.1:{server.server_port}/dsh/v1/responses',
                                   headers={'Authorization': server.auth}, json=raw)
        assert response.status_code == 200
        if category is None:
            assert 'response.completed' in response.text
            assert server.state.failure is None and server.state.pending is None
            assert server.state.tool_calls == 1
            assert server.state.last_calls[0]['call_id'] == 'call_partial'
            return
        assert 'response.completed' not in response.text
        assert 'response.incomplete' not in response.text
        assert 'response.failed' not in response.text
        assert server.state.failure['category'] == category
        assert server.state.failure['external'] is external
        assert (server.state.pending is not None) is retryable
        assert server.state.last_calls == [] and server.state.tool_calls == 0
        if retryable:
            from src.dsh_runtime.lifecycle_evidence import work_snapshot
            assert server.state.pending['work_snapshot'] == work_snapshot(run)
            pending_body = copy.deepcopy(server.state.pending['body'])
            resumed = {**raw, 'input': [{'role': 'user', 'content': [
                {'type': 'input_text', 'text': relay.RESUME_MARKER}]}]}
            restored = server.state.request(resumed)
            if category == 'model_output_incomplete':
                assert restored['tool_choice'] == 'required'
                assert restored['input'][:-1] == pending_body['input']
                assert restored['input'][-1]['role'] == 'developer'
            else:
                assert restored == pending_body
        assert 'offline-fixture-token' not in (tmp_path / 'control/relay.jsonl').read_text()
    finally:
        server.shutdown()
        server.server_close()
        server.transport.client.close()
