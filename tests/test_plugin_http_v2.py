import json
import zipfile

from test_foundation import app, client
from maintainer.providers import Completion


def test_plugin_routes_are_namespaced_and_ui_requires_scoped_session(app, client):
    manifest = {
        "id": "example.http-test", "name": "HTTP Test", "version": "0.1.0-dev",
        "plugin_api": 2, "publisher": "mcxianyujun", "license": "MIT",
        "maintune": {"min_version": "0.1.0"},
        "entrypoint": {"python": "http_test.main"},
        "ui": {"mode": "bundled", "entrypoint": "ui/index.html"},
    }
    source = '''
def ping(context, payload):
    return {"ok": True, "method": payload["method"]}

def webhook(context, payload):
    if payload["headers"].get("authorization") != "Bearer test-plugin-token":
        return {"status_code": 401, "body": {"detail": "Unauthorized"}}
    return {"received": True}

def register(api):
    api.register_route("ping", ping)
    api.register_route("webhook", webhook, methods=("POST",), access="external")
'''
    archive = app.state.plugins.packages.inbox / "http-test.mtp"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.yaml", json.dumps(manifest))
        bundle.writestr("src/http_test/__init__.py", "")
        bundle.writestr("src/http_test/main.py", source)
        bundle.writestr("ui/index.html", "<!doctype html><title>Plugin UI</title>")
    assert client.post("/api/plugins/install/http-test.mtp").status_code == 201
    assert client.post("/api/plugins/example.http-test/enable").status_code == 200
    assert client.get("/api/plugins/example.http-test/http/ping").json() == {"ok": True, "method": "GET"}
    assert client.get("/api/plugins/example.http-test/http/ping", headers={"Authorization": ""}).status_code == 401
    assert client.post("/api/plugins/example.http-test/public/webhook", headers={"Authorization": ""}).status_code == 401
    assert client.post("/api/plugins/example.http-test/public/webhook", headers={"Authorization": "Bearer test-plugin-token"}).json() == {"received": True}
    assert client.get("/api/plugins/example.http-test/ui/index.html", headers={"Authorization": ""}).status_code == 401
    session = client.post("/api/plugins/example.http-test/ui-session")
    assert session.status_code == 200 and session.json()["url"].endswith("/ui/index.html")
    asset = client.get(session.json()["url"], headers={"Authorization": ""})
    assert asset.status_code == 200 and "Plugin UI" in asset.text
    assert asset.headers["x-frame-options"] == "SAMEORIGIN"
    assert "maintune_plugin_ui" not in session.text
    assert client.post("/api/plugins/example.http-test/disable").status_code == 200
    assert client.get(session.json()["url"], headers={"Authorization": ""}).status_code == 401


def test_plugin_sandbox_is_selectable_and_probe_uses_plugin(app, client):
    manifest = {
        "id": "example.sandbox-http", "name": "Sandbox HTTP", "version": "0.1.0-dev",
        "plugin_api": 2, "publisher": "mcxianyujun", "license": "MIT",
        "maintune": {"min_version": "0.1.0"}, "entrypoint": {"python": "sandbox_http.main"},
    }
    source = '''
files = {}

def sandbox(context, payload):
    action = payload["action"]
    if action == "create": return {"id": payload["task_id"]}
    if action == "write_file":
        files[(payload["sandbox"], payload["path"])] = payload["content"]
        return {}
    if action == "read_file": return {"content": files[(payload["sandbox"], payload["path"])]}
    if action == "destroy":
        files.clear()
        return {}
    return {"exit_code": 0, "output": "ok"}

def register(api):
    api.register_sandbox_provider("worker", sandbox, config_schema={"type": "object", "properties": {}})
'''
    archive = app.state.plugins.packages.inbox / "sandbox-http.mtp"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.yaml", json.dumps(manifest))
        bundle.writestr("src/sandbox_http/__init__.py", "")
        bundle.writestr("src/sandbox_http/main.py", source)
    assert client.post("/api/plugins/install/sandbox-http.mtp").status_code == 201
    selected = {"provider": "example.sandbox-http/worker", "base_url": "http://127.0.0.1:8123", "profile": "python-default"}
    assert client.put("/api/sandbox", json=selected).status_code == 409
    assert client.post("/api/plugins/example.sandbox-http/enable").status_code == 200
    assert client.put("/api/sandbox", json=selected).status_code == 200
    assert client.get("/api/setup/status").json()["steps"]["sandbox"] is True
    assert client.post("/api/sandbox/test").json()["ok"] is True
    assert client.post("/api/plugins/example.sandbox-http/disable").status_code == 200
    assert client.get("/api/setup/status").json()["steps"]["sandbox"] is False
    assert client.post("/api/sandbox/test").status_code == 502


def test_plugin_model_provider_is_selectable_and_used_by_diagnostic(app, client):
    manifest = {
        "id": "example.model-http", "name": "Model HTTP", "version": "0.1.0-dev",
        "plugin_api": 2, "publisher": "mcxianyujun", "license": "MIT",
        "maintune": {"min_version": "0.1.0"}, "entrypoint": {"python": "model_http.main"},
        "config_schema": {"type": "object", "properties": {"endpoint_key": {"type": "string", "secret": True}}, "required": ["endpoint_key"], "additionalProperties": False},
    }
    source = '''
def endpoint(context, payload):
    if payload["action"] != "resolve": raise ValueError("Unsupported action")
    return {"base_url": "https://models.example/v1", "api_key": context.config["endpoint_key"], "model": payload["model"]}

def register(api):
    api.register_model_provider("test", endpoint, config_schema={"type": "object", "properties": {"endpoint_key": {"type": "string", "secret": True}}, "required": ["endpoint_key"]}, models=["example-model"])
'''
    archive = app.state.plugins.packages.inbox / "model-http.mtp"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.yaml", json.dumps(manifest))
        bundle.writestr("src/model_http/__init__.py", "")
        bundle.writestr("src/model_http/main.py", source)
    assert client.post("/api/plugins/install/model-http.mtp").status_code == 201
    assert client.put("/api/plugins/example.model-http/config", json={"endpoint_key": "fake-provider-key"}).status_code == 200
    assert client.post("/api/plugins/example.model-http/enable").status_code == 200
    provider = next(item for item in client.get("/api/providers").json() if item["type"] == "plugin")
    assert provider["models"][0]["id"] == "example-model"
    assert "fake-provider-key" not in str(provider)
    settings = client.get("/api/settings").json()
    settings["model"] = {"provider": provider["id"], "model": "example-model"}
    assert client.put("/api/settings", json=settings).status_code == 200
    assert client.get("/api/setup/status").json()["steps"]["model"] is True
    worker_config, worker_key = client.portal.call(app.state.processor.model_endpoint, "code_worker")
    assert (worker_config.base_url, worker_config.model, worker_key) == ("https://models.example/v1", "example-model", "fake-provider-key")

    calls = []
    class FakeModel:
        def __init__(self, base_url, key):
            calls.append((base_url, key))
        async def complete(self, model, system, prompt, timeout, parameters=None):
            assert model == "example-model"
            return Completion(text="diagnostic-ok", input_tokens=2, output_tokens=1, total_tokens=3, usage_reported=True)

    app.state.provider_factory = FakeModel
    result = client.post("/api/runs", json={"agent": "main", "prompt": "diagnostic"})
    assert result.status_code == 201 and result.json()["result"] == "diagnostic-ok"
    assert calls == [("https://models.example/v1", "fake-provider-key")]
    assert client.put(f"/api/providers/{provider['id']}", json={"name": "Wrong", "base_url": "https://example.test", "models": ["example-model"]}).status_code == 409
    assert client.delete(f"/api/providers/{provider['id']}").status_code == 409
    assert client.post("/api/plugins/example.model-http/disable").status_code == 200
    assert client.get("/api/setup/status").json()["steps"]["model"] is False
    assert next(item for item in client.get("/api/providers").json() if item["id"] == provider["id"])["available"] is False
    assert client.put("/api/settings", json=settings).status_code == 409
