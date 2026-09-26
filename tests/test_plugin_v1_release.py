"""Compatibility check against the unmodified public AstrBot Bridge v1 package.

The fixture is the exact v0.1.0-preview.1 .mtp release artifact. Keeping the
original package catches manifest, installation, and IPC regressions that a
newly generated test plugin would miss.
"""

import asyncio
import hashlib
import shutil
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

from maintainer.db import database
from maintainer.plugin_manager import PluginManager
from maintainer.plugin_system import PluginPackageManager
from maintainer.security import Vault


PACKAGE = Path(__file__).parent / "fixtures" / "maintune-plugin-astrbot-0.1.0-preview.1.mtp"
EXPECTED_SHA256 = "a2b395fbd3907b83dd11ff0a27b558b5bf671fb0e0b8e54b394673dd0f76955d"
PLUGIN_ID = "official.astrbot-bridge"


def test_original_v1_release_package_installs_unchanged(tmp_path):
    assert hashlib.sha256(PACKAGE.read_bytes()).hexdigest() == EXPECTED_SHA256
    with zipfile.ZipFile(PACKAGE) as archive:
        assert "LICENSE" in archive.namelist()
        assert "requirements.lock" in archive.namelist()
        assert b"api_version: 1" in archive.read("manifest.yaml")

    manager = PluginPackageManager(tmp_path / "plugins", "0.1.0-preview.3")
    manifest = manager.validate(PACKAGE)
    assert manifest.id == PLUGIN_ID
    assert manifest.api_version == 1
    assert manifest.plugin_api == 1
    assert manifest.is_legacy
    assert manifest.license == "MIT"

    installed = manager.install(PACKAGE)
    assert installed.id == PLUGIN_ID
    assert [item.id for item in manager.discover()] == [PLUGIN_ID]


def test_original_v1_release_lifecycle_and_basic_rpc(tmp_path):
    engine, sessions = database(f"sqlite:///{tmp_path / 'v1-bridge.db'}")
    manager = PluginManager(tmp_path / "plugins", "0.1.0-preview.3", sessions, Vault(Fernet.generate_key().decode()))
    inbox = manager.packages.inbox / PACKAGE.name
    shutil.copyfile(PACKAGE, inbox)
    installed = manager.install(inbox.name)
    assert installed["id"] == PLUGIN_ID
    manager.configure(PLUGIN_ID, {"bridge_token": "local-test-token-" + "x" * 40})

    class Socket:
        def __init__(self):
            self.sent = []
            self.closed = []

        async def send_json(self, value):
            self.sent.append(value)

        async def close(self, code):
            self.closed.append(code)

    async def scenario():
        try:
            enabled = await manager.enable(PLUGIN_ID)
            assert enabled["enabled"] is True
            assert enabled["runtime_status"] == "running"
            assert enabled["config"]["bridge_token"] == "********"

            process = manager.processes[PLUGIN_ID]
            health = await process.request("health", {})
            assert health == {"ok": True, "connected_instance": ""}
            assert "task.started" in manager.subscriptions[PLUGIN_ID]

            auth = {
                "protocol": "maintune.astrbot.v1",
                "type": "authenticate",
                "token": "local-test-token-" + "x" * 40,
                "instance": "local-regression",
            }
            assert await manager.authenticate_transport(PLUGIN_ID, auth)
            socket = Socket()
            await manager.connect(PLUGIN_ID, socket, auth)
            assert socket.sent[0]["type"] == "hello"
            assert socket.sent[0]["instance"] == "local-regression"
            assert (await process.request("health", {}))["connected_instance"] == "local-regression"

            response = await manager.transport_message(
                PLUGIN_ID,
                {
                    "protocol": "maintune.astrbot.v1",
                    "type": "request",
                    "request_id": "local-status-1",
                    "action": "status",
                    "params": {},
                },
            )
            assert len(response) == 1
            assert response[0]["protocol"] == "maintune.astrbot.v1"
            assert response[0]["type"] == "response"
            assert response[0]["request_id"] == "local-status-1"
            assert isinstance(response[0]["data"], dict)

            disabled = await manager.disable(PLUGIN_ID)
            assert disabled["enabled"] is False
            assert disabled["runtime_status"] == "stopped"
            assert PLUGIN_ID not in manager.processes
            assert socket.closed == [1012]

            await manager.enable(PLUGIN_ID)
            before_reload = manager.processes[PLUGIN_ID]
            reloaded = await manager.reload(PLUGIN_ID)
            assert reloaded["enabled"] is True
            assert reloaded["runtime_status"] == "running"
            assert manager.processes[PLUGIN_ID] is not before_reload
            assert (await manager.processes[PLUGIN_ID].request("health", {}))["ok"] is True
        finally:
            await manager.stop()

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
