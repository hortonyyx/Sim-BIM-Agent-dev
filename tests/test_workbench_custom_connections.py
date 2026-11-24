import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.testclient import TestClient

from src.workbench import custom_connections as custom
from src.workbench.connections import Connections
from src.workbench.storage import InputError


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_BIM_WORKBENCH_HOME", str(tmp_path))
    state = {"requests": [], "code": 200, "data": [
        {"id": "lab/vision-v1:local", "display_name": "Lab Vision"}, {"id": "lab/text"}]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Authorization")))
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": state["data"]}).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/proxy/v1", state
        finally:
            server.shutdown()
            thread.join()


def test_real_catalog_keyless_model_and_manual_fallback(catalog, monkeypatch):
    base, state = catalog
    manager = Connections()
    # This test exercises its own local endpoint and ignores unrelated signed-in accounts.
    monkeypatch.setattr(manager, "configured", lambda provider: False)
    manager.manage({"action": "create", "label": "Local studio", "protocol": "openai-chat",
                    "baseUrl": base + "/", "modelIds": ["lab/text", "manual-v2"]})
    available = manager.view("en")
    assert [row["provider"] for row in available[:2]] == ["chatgpt-subscription", "deepseek"]
    row = available[2]
    assert custom.is_custom_provider(row["provider"])
    assert not row["hasApiKey"] and row["connected"]
    assert row["contextWindow"] == 128000 and row["outputTokens"] == 8192
    assert row["models"] == [{"id": "lab/vision-v1:local", "name": "Lab Vision"},
                             {"id": "lab/text", "name": "lab/text"},
                             {"id": "manual-v2", "name": "manual-v2"}]
    assert state["requests"] == [("/proxy/v1/models", None)]
    state["code"] = 404
    manager.manage({"action": "update", "provider": row["provider"], "revision": row["revision"],
                    "baseUrl": base + "/missing", "modelIds": ["manual-only"], "outputTokens": 512})
    updated = custom.custom_catalog("en", ())[0]
    assert updated["connected"] and updated["models"] == [{"id": "manual-only", "name": "manual-only"}]
    assert "catalog" in updated["message"] and "目录" in custom.custom_catalog("zh-CN", ())[0]["message"]
    assert state["requests"][-1] == ("/proxy/v1/missing/models", None)


def test_freeze_credentials_edit_and_delete(catalog):
    base, state = catalog
    manager = Connections()
    manager.manage({"action": "create", "label": "My API", "baseUrl": base, "apiKey": "test-key-original"})
    row = custom.custom_catalog("en", ())[0]
    provider = row["provider"]
    assert state["requests"][-1][1] == "Bearer test-key-original"
    assert row["hasApiKey"] and "test-key" not in json.dumps(row)
    directory = custom.custom_connections_directory()
    assert b"test-key" not in (directory / "connections.json").read_bytes()
    first = custom.freeze_custom_connection(provider, expected_revision=row["revision"])

    with pytest.raises(InputError, match="changing servers"):
        manager.manage({"action": "update", "provider": provider, "baseUrl": "https://example.com/v1"})
    manager.manage({"action": "update", "provider": provider, "label": "Updated", "apiKey": ""})
    assert custom.custom_connection(provider)["api_key"] == "test-key-original"
    with pytest.raises(InputError, match="changed"):
        custom.freeze_custom_connection(provider, expected_revision=first)
    with pytest.raises(InputError, match="changed"):
        manager.manage({"action": "delete", "provider": provider, "revision": first})

    manager.manage({"action": "update", "provider": provider, "baseUrl": base + "/two",
                    "apiKey": "test-key-next", "contextWindow": 32768, "outputTokens": 2048})
    second = custom.freeze_custom_connection(provider)
    manager.manage({"action": "update", "provider": provider, "clearApiKey": True})
    assert state["requests"][-1][1] is None
    manager.manage({"action": "delete", "provider": provider})
    assert custom.custom_catalog("en", ()) == []
    with pytest.raises(InputError, match="not found"):
        custom.custom_connection(provider)
    original = custom.custom_connection(provider, directory=directory, revision=first)
    updated = custom.custom_connection(provider, directory=directory, revision=second)
    assert (original["base_url"], original["api_key"], original["context_window"]) == (base, "test-key-original", 128000)
    assert (updated["base_url"], updated["api_key"], updated["output_tokens"]) == (base + "/two", "test-key-next", 2048)
    assert not list((directory / "current").rglob("*.bin"))


def test_settings_api_survives_restart_and_rejects_invalid_updates(catalog, monkeypatch):
    from src.workbench.server import Workbench, create_app
    base, state = catalog
    monkeypatch.setattr(Connections, "configured", lambda self, provider: False)
    app = Workbench()
    with TestClient(create_app(app, warm=False), base_url="http://127.0.0.1") as client:
        endpoint = "/api/connections"
        saved = client.post(endpoint, json={"action": "create", "baseUrl": base, "label": "Gateway",
                                           "apiKey": "local-test-only", "protocol": "openai-chat"})
        assert saved.status_code == 200 and "local-test-only" not in saved.text
        row = saved.json()["connections"][-1]
        request = {"action": "update", "provider": row["provider"], "revision": row["revision"]}
        for invalid in ({"outputTokens": row["contextWindow"]}, {"contextWindow": True},
                        {"modelIds": "name"}, {"protocol": "unsupported"}):
            assert client.post(endpoint, json={**request, **invalid}).status_code == 400
        assert len(state["requests"]) == 1
        fresh = Workbench()
        assert fresh.connections.view("en")[-1] == row
        revised = client.post(endpoint, json={**request, "label": "Revised"})
        assert revised.status_code == 200 and revised.json()["connections"][-1]["label"] == "Revised"
        assert client.post(endpoint, json={**request, "label": "Stale edit"}).status_code == 409


def test_slow_catalog_cannot_overwrite_edited_or_deleted_connection(catalog, monkeypatch):
    base, _ = catalog
    provider, revision = custom.save_custom({"label": "First", "baseUrl": base})
    started = threading.Event()
    release = threading.Event()
    failures = []

    def delayed(connection):
        started.set()
        assert release.wait(5)
        return [{"id": "stale", "name": "stale"}]

    def refresh():
        try:
            custom.refresh_custom(provider, revision)
        except InputError as exc:
            failures.append(exc.status)

    monkeypatch.setattr(custom, "discover_custom", delayed)
    thread = threading.Thread(target=refresh)
    thread.start()
    try:
        assert started.wait(5)
        custom.save_custom({"label": "Updated", "revision": revision, "modelIds": ["new"]}, provider=provider)
    finally:
        release.set()
        thread.join(5)
    assert failures == [409]
    assert custom.custom_catalog("en", ())[0]["models"] == [{"id": "new", "name": "new"}]


@pytest.mark.parametrize("url", ["https://llm.example.net/reverse/proxy/v1", "http://localhost:8000/v1",
                                  "http://[::1]:8080/v1", "http://192.168.1.42:1234/v1"])
def test_compatible_base_urls(url):
    assert custom.base_url(url + "/") == url


@pytest.mark.parametrize("url", ["http://untrusted.example/api", "ftp://localhost:1234",
                                  "https://user:pass@example.org/v1", "https://example.org/v1?key=private",
                                  "https://example.org/#secret", "https://localhost:65536"])
def test_base_urls_keep_credentials_out_of_public_settings(url):
    with pytest.raises(InputError):
        custom.base_url(url)
