"""Offline DeepSeek Flash route, image/tool history and request validation."""
import asyncio
import json

import httpx
import pytest

from src.agent_runtime.adapter import HttpChatAdapter
from src.agent_runtime.connections import resolve_connection
from src.agent_runtime.failures import ModelServiceError
from src.agent_runtime.providers import provider_parameters, validate_provider_model
from test_agent_runtime import MESSAGES, response, runtime

FLASH_ROUTES = [("deepseek", "deepseek-flash"), ("opencode-go", "deepseek-v4.1-flash"),
                ("paratera", "DeepSeek-V4.1-Flash")]


def credentials(tmp_path, base="https://api.deepseek.com"):
    path = tmp_path / "deepseek.env"
    path.write_text(f"DEEPSEEK_BASE_URL={base}\nDEEPSEEK_API_KEY=fixture-secret\n")
    return path


def test_credentials_are_explicit_isolated_and_never_redirected(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-must-not-be-used")
    with pytest.raises(ValueError, match="explicit existing"):
        resolve_connection("deepseek", None, model="deepseek-flash")
    with pytest.raises(ValueError, match="official HTTPS"):
        resolve_connection("deepseek", credentials(tmp_path, "https://unreviewed.invalid"), model="deepseek-flash")
    route = resolve_connection("deepseek", credentials(tmp_path), model="deepseek-flash")
    assert route.descriptor.route_id == "deepseek" and route.descriptor.billing_mode == "metered"
    assert "fixture-secret" not in repr(route)
    assert "fixture-secret" not in route.descriptor.model_dump_json()

    async def scenario():
        seen = []
        def handler(request):
            seen.append(request)
            assert str(request.url) == "https://api.deepseek.com/chat/completions"
            assert request.headers["authorization"] == "Bearer fixture-secret"
            return httpx.Response(307, headers={"location": "https://unreviewed.invalid"})
        adapter = HttpChatAdapter(base_url=route.descriptor.base_url, api_key=route.api_key,
                                  transport=httpx.MockTransport(handler))
        from types import SimpleNamespace
        with pytest.raises(ModelServiceError) as error:
            await adapter.send(SimpleNamespace(wire_bytes=b"{}"), timeout=5)
        assert len(seen) == 1 and "fixture-secret" not in str(error.value)
        await adapter.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [{"reasoning_effort": "medium"}, {"temperature": 0},
    {"thinking": False, "reasoning_effort": "high"}, {"output_tokens": 400000}])
@pytest.mark.parametrize("provider,model", FLASH_ROUTES)
def test_unsupported_parameters_fail_before_requests(kwargs, provider, model):
    with pytest.raises(ValueError):
        provider_parameters(provider, model=model, **{"output_tokens": 32000, **kwargs})


def test_model_identity_and_role_config_require_explicit_provider(tmp_path):
    from src.agent.runtime_configuration import argv_for, load_configuration
    from test_role_configuration import case, configuration, role, ROLE_NAMES
    for provider, model in [("paratera", "deepseek-flash"), ("deepseek", "deepseek-v4-pro")]:
        with pytest.raises(ValueError, match="DeepSeek"):
            validate_provider_model(provider, model)
    selected = case()
    selected.update(provider="deepseek", model="deepseek-flash", reasoning_effort="high",
                    credentials_file=str(credentials(tmp_path)))
    selected["roles"] = {name: role("deepseek", "deepseek-flash", "high") for name in ROLE_NAMES}
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(configuration(selected)))
    loaded = load_configuration(path)["cases"][0]
    command = argv_for(loaded)
    assert command[command.index("--provider") + 1] == "deepseek"
    assert all(row["model"] == "deepseek-flash" for row in loaded["roles"].values())


def deepseek_engine(tmp_path, provider="deepseek", model="deepseek-flash"):
    replies = [response(("view-1", "view", {}), reasoning=True), response(text="Done", reasoning=True)]
    for reply in replies:
        reply["usage"]["prompt_cache_hit_tokens"] = 12
    engine = runtime(tmp_path, replies)
    engine.model = model
    engine.parameters = provider_parameters(provider, model=engine.model, output_tokens=32000)
    engine.strict_model_profile = True
    engine.versions = engine.versions.model_copy(update={"remote_model": engine.versions.remote_model.model_copy(
        update={"route_id": provider, "remote_alias": engine.model})})
    return engine


@pytest.mark.parametrize("provider,model", FLASH_ROUTES)
def test_actual_runtime_replays_reasoning_and_tool_image_without_resizing(tmp_path, provider, model):
    engine = deepseek_engine(tmp_path, provider, model)
    with engine.store:
        result = asyncio.run(engine.run(MESSAGES))
        assert result["status"] == "completed" and result["model_calls"] == 2
        assert result["reported_tokens"] == 60
        assert result["billing_usd"] is None and result["estimated_cost_cny"] is None
        requests = [e.payload for e in engine.store.events if e.payload.event_type == "adapter_request"]
        for sent, captured in zip(engine.adapter.requests, requests):
            assert sent == engine.store.capture_bytes(captured.final_request_body)
            body = json.loads(sent)
            assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
            assert "enable_thinking" not in body and "temperature" not in body and "n" not in body
        second = json.loads(engine.adapter.requests[1])
        assert next(m for m in second["messages"] if m["role"] == "assistant")["reasoning_content"] == "Public protocol test explanation."
        assert [m["role"] for m in second["messages"][-2:]] == ["tool", "user"]
        assert requests[1].images[0].original.sha256 == requests[1].images[0].sent.sha256
        from src.agent_runtime.estimation import estimate_chat_request
        estimate = estimate_chat_request(second, strict=True)
        assert estimate.image_tokens == 1024 and estimate.context_window_tokens == 1048576
        assert estimate.images[0].width == estimate.images[0].resized_width == 4
        from src.agent_runtime.accounting import request_accounting_from_store
        records = [request_accounting_from_store(engine.store, e.event_id).accounting
                   for e in engine.store.events if e.payload.event_type == "adapter_request"]
        assert all(r.reported_cache_read_tokens == 12 for r in records)
        if provider != "paratera":
            assert records[1].budget_charge_tokens == 30 and records[1].additional_image_tokens == 0
        else:
            # Gateway image billing is uncalibrated: keep the conservative separate image charge.
            assert records[1].additional_image_tokens == 1024
            assert records[1].estimated_cost_cny is None
        engine.store.validate()


@pytest.mark.parametrize("provider,model", FLASH_ROUTES)
def test_partial_reasoning_history_is_rejected_without_send(tmp_path, provider, model):
    engine = deepseek_engine(tmp_path, provider, model)
    engine.reasoning_history = "current_tool_chain"
    with engine.store, pytest.raises(ValueError, match="reasoning_history=all"):
        asyncio.run(engine.run(MESSAGES))
    assert not engine.adapter.requests


@pytest.mark.parametrize("provider,model", FLASH_ROUTES)
def test_flash_profile_and_effort_limits_are_explicit(provider, model):
    from src.agent_runtime.estimation import get_model_profile
    profile = get_model_profile(model, strict=True)
    assert profile.context_window_tokens == 1048576
    assert profile.image_estimator == "deepseek_vision_max1024_v1"
    for effort in ("low", "high", "max"):
        parameters = provider_parameters(provider, model=model, output_tokens=393216, reasoning_effort=effort)
        assert parameters == {"max_tokens": 393216, "thinking": {"type": "enabled"}, "reasoning_effort": effort}
    for wrong in ("deepseek-v4-pro", "DeepSeek-V4-Flash-Vision-Exp", "deepseek-latest"):
        with pytest.raises(ValueError):
            provider_parameters(provider, model=wrong, output_tokens=32000)


def test_go_flash_uses_confirmed_chat_endpoint_and_subscription_identity(tmp_path):
    from src.agent_runtime.opencode_go import BASE_URL, HttpGoAdapter, conversation_id
    path = tmp_path / "go.env"
    path.write_text(f"OPENCODE_GO_BASE_URL={BASE_URL}\nOPENCODE_GO_API_KEY=fixture-secret\n")
    connection = resolve_connection("opencode-go", path, model="deepseek-v4.1-flash")
    assert connection.descriptor.billing_mode == "subscription"
    assert connection.descriptor.adapter_kind == "openai_chat_completions"
    async def scenario():
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=response(text="Done"))
        adapter = HttpGoAdapter(base_url=connection.descriptor.base_url, api_key=connection.api_key,
            protocol=connection.descriptor.adapter_kind, session_id=conversation_id(tmp_path, "reader"),
            transport=httpx.MockTransport(handler))
        from types import SimpleNamespace
        await adapter.send(SimpleNamespace(wire_bytes=b'{"model":"deepseek-v4.1-flash","messages":[]}'), timeout=5)
        assert str(seen[0].url) == "https://opencode.ai/inference/go/openai/v1/chat/completions"
        assert seen[0].headers["authorization"] == "Bearer fixture-secret"
        await adapter.close()
    asyncio.run(scenario())
