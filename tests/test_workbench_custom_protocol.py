import json
from types import SimpleNamespace

import pytest

from src.dsh_runtime import relay
from src.dsh_runtime.chat_protocol import chat_events, chat_request
from src.dsh_runtime.continuation import verified_resume
from tests.test_orchestrator_product import project


def test_chat_tools_images_and_ordinary_text_round_trip_on_resume(monkeypatch):
    provider = "custom-" + "1" * 32
    model = "lab/unknown:model"
    monkeypatch.setattr(relay, "MODEL", model)
    tool = {"type": "function", "function": {"name": "view_image", "parameters": {"type": "object"}}}
    session = relay.Session([{"role": "user", "content": "Read the floor plans."}], [tool], {},
                            provider=provider, output_tokens=3000)
    native = {"stream": True, "model": model, "reasoning": {"effort": "high"},
              "tools": [{"type": "function", "name": relay.native_name("view_image")}]}
    first = session.request(native)
    wire = chat_request(first, 3000)
    assert set(wire) == {"model", "messages", "max_tokens", "tools", "stream"}
    assert wire["tools"] == [tool | {"function": {**tool["function"], "description": ""}}]
    calls = [{"id": name, "type": "function", "function": {"name": "view_image", "arguments": "{}"}}
             for name in ("a", "b")]
    reply = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": "fixture", "tool_calls": calls}}]}
    terminal = json.loads(list(chat_events(reply, model, 1))[-1][1])["response"]
    assert len(terminal["output"]) == 3
    session.requests = 1
    session.observed(first, terminal)
    png = "data:image/png;base64,fixture"
    receipts = [{"type": "function_call_output", "call_id": c["id"],
                 "output": [{"type": "input_text", "text": c["id"]}, {"type": "input_image", "image_url": png}]}
                for c in calls]
    native["input"] = [*relay.downstream(terminal["output"]), *receipts]
    following = session.request(native)
    request = chat_request(following, 3000)
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "tool", "tool", "user"]
    assert request["messages"][1]["tool_calls"] == calls
    assert request["messages"][1]["reasoning_content"] == "fixture"
    assert [p["image_url"]["url"] for p in request["messages"][-1]["content"] if p["type"] == "image_url"] == [png, png]
    pending = {"raw": relay.clean(native), "body": relay.clean(following), "redacted": True,
               "body_sha256": relay.digest(relay.encoded(following))}
    resumed = {**native, "input": [*native["input"], {"role": "user", "content": [
        {"type": "input_text", "text": relay.RESUME_MARKER}]}]}
    assert chat_request(verified_resume(pending, resumed, relay.RESUME_MARKER), 3000) == request
    truncated = {**reply, "choices": [{**reply["choices"][0], "finish_reason": "length"}]}
    last = json.loads(list(chat_events(truncated, model, 1))[-1][1])["response"]
    assert last["output"] == [] and last["status"] == "incomplete"


def test_custom_connection_freeze_and_worker_options(tmp_path, monkeypatch):
    from src.agent import observer_mcp
    from src.agent.runtime_configuration import load_configuration
    from src.dsh_runtime import product
    from src.workbench import connections
    provider = "custom-" + "a" * 32
    revision = "b" * 32
    model = "deepseek-custom/preview"
    workers = [{"provider": provider, "model": model}, {"provider": "chatgpt-subscription", "model": "gpt-6.1-terra"}]
    path = project(tmp_path, provider=provider, model=model, output_tokens=1024, worker=None, workers=workers)
    case = load_configuration(path)["cases"][0]
    selected = []
    def custom(p, *, directory=None, revision=None):
        selected.append(revision)
        assert p == provider
        return {"base_url": "http://localhost:8098/local/v1", "api_key": "", "output_tokens": 1024,
                "context_window": 16000}
    monkeypatch.setattr(connections, "custom_connection", custom)
    monkeypatch.setattr(connections, "freeze_custom_connection", lambda p: revision)
    monkeypatch.setattr(product, "installed_executable", lambda _: tmp_path / "dsh")
    config = product.host_config(case, tmp_path)
    assert config["connection_revisions"] == {provider: revision}
    assert config["base_url"].endswith("/local/v1") and config["context_window"] == 16000
    assert config["worker"]["output_tokens"] == 32000
    args = SimpleNamespace(custom_directory=tmp_path, connection_revisions=config["connection_revisions"],
        provider=provider, output_tokens=32000, reasoning_effort="high", script=None, background=False,
        chatgpt_auth_dir=None, credentials_file=None, protected_key=None)
    profile = observer_mcp.worker_profile(args, provider, model)
    assert profile["parameters"] == {"max_tokens": 1024}
    assert profile["route"]["context_window"] == 16000
    assert profile["route"]["billing_mode"] == "provider_defined" and profile["route"]["route_id"] == provider
    assert all(s == revision for s in selected)
    monkeypatch.setattr(connections, "freeze_custom_connection", lambda p: pytest.fail("old run used active connection"))
    assert product.host_config(case, tmp_path, resume_revisions=config["connection_revisions"]) == config
