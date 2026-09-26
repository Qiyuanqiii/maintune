from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select

from .db import Config, Provider, Repository, Task, Timeline
from .plugin_api_v2 import HOOKS, ExtensionRegistration, PluginRegistry, validate_value
from .schemas import ProviderInput
from .plugin_system import (
    MAX_MESSAGE_BYTES,
    PLUGIN_PROTOCOL,
    PluginCapabilityBroker,
    PluginError,
    PluginEvent,
    PluginManifest,
    InProcessPlugin,
    PluginPackageError,
    PluginPackageManager,
    PluginProcess,
    generate_bridge_token,
    load_manifest,
    make_event,
)


class PluginManager:
    def __init__(self, root: Path, maintune_version: str, sessions, vault):
        self.packages = PluginPackageManager(root, maintune_version)
        self.sessions = sessions
        self.vault = vault
        self.processes: dict[str, PluginProcess | InProcessPlugin] = {}
        self.registry = PluginRegistry()
        self.connections: dict[str, set[WebSocket]] = {}
        self.connection_state: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, set[str]] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.restart_attempts: dict[str, int] = {}
        self.restart_after: dict[str, float] = {}
        self.ui_sessions: dict[str, tuple[str, float]] = {}
        self.controller = None
        self.active_invocations: dict[str, int] = {}
        self.draining_plugins: set[str] = set()

    def _config_id(self, plugin_id: str) -> str:
        return f"plugin:{plugin_id}"

    def _manifest(self, plugin_id: str) -> PluginManifest:
        path = self.packages.installed / plugin_id / "manifest.yaml"
        if not path.is_file():
            raise PluginPackageError("Plugin is not installed")
        return load_manifest(path)

    def _record(self, plugin_id: str) -> dict[str, Any]:
        with self.sessions() as db:
            row = db.get(Config, self._config_id(plugin_id))
            return dict(row.data) if row else {"enabled": False, "config": {}, "secrets": {}, "error": ""}

    def _save_record(self, plugin_id: str, data: dict[str, Any]) -> None:
        with self.sessions.begin() as db:
            row = db.get(Config, self._config_id(plugin_id))
            if row:
                row.data = data
            else:
                db.add(Config(id=self._config_id(plugin_id), data=data))

    async def start(self) -> None:
        self._stop.clear()
        for manifest in self.packages.discover():
            if self._record(manifest.id).get("enabled"):
                try:
                    await self.enable(manifest.id)
                except Exception as error:
                    record = self._record(manifest.id)
                    record["error"] = self._sanitize(str(error))
                    self._save_record(manifest.id, record)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        await asyncio.gather(*(process.stop() for process in list(self.processes.values())), return_exceptions=True)
        self.processes.clear()
        self.registry = PluginRegistry()

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(15)
            for manifest in self.packages.discover():
                if not self._record(manifest.id).get("enabled") or manifest.id in self.processes:
                    continue
                if time.time() < self.restart_after.get(manifest.id, 0):
                    continue
                try:
                    await self.enable(manifest.id)
                    self.restart_attempts[manifest.id] = 0
                except Exception as error:
                    attempts = min(self.restart_attempts.get(manifest.id, 0) + 1, 6)
                    self.restart_attempts[manifest.id] = attempts
                    self.restart_after[manifest.id] = time.time() + min(2 ** attempts, 60)
                    record = self._record(manifest.id)
                    record["error"] = self._sanitize(str(error))
                    self._save_record(manifest.id, record)
            for plugin_id, process in list(self.processes.items()):
                try:
                    await process.request("health", {}, timeout=5)
                except Exception as error:
                    record = self._record(plugin_id)
                    record["error"] = self._sanitize(str(error))
                    self._save_record(plugin_id, record)
                    self.registry.unregister_plugin(plugin_id)
                    try:
                        await process.stop()
                    except Exception as stop_error:
                        record["error"] = self._sanitize(str(stop_error))
                        self._save_record(plugin_id, record)
                    finally:
                        self.processes.pop(plugin_id, None)
                    attempts = min(self.restart_attempts.get(plugin_id, 0) + 1, 6)
                    self.restart_attempts[plugin_id] = attempts
                    self.restart_after[plugin_id] = time.time() + min(2 ** attempts, 60)

    def scan(self) -> list[dict[str, Any]]:
        results = []
        for archive in sorted(self.packages.inbox.glob("*.mtp")):
            try:
                manifest = self.packages.validate(archive)
                results.append({"file": archive.name, "valid": True, "manifest": manifest.model_dump()})
            except Exception as error:
                results.append({"file": archive.name, "valid": False, "error": self._sanitize(str(error))})
        return results

    def install(self, filename: str) -> dict[str, Any]:
        if Path(filename).name != filename:
            raise PluginPackageError("Invalid package filename")
        archive = self.packages.inbox / filename
        manifest = self.packages.install(archive)
        config: dict[str, Any] = {}
        if manifest.is_legacy:
            for name, field in manifest.config.items():
                if field.type != "secret" and field.default is not None:
                    config[name] = field.default
        else:
            for name, field in (manifest.config_schema or {}).get("properties", {}).items():
                if not field.get("secret") and "default" in field:
                    config[name] = field["default"]
        self._save_record(manifest.id, {"enabled": False, "config": config, "secrets": {}, "error": "", "installed_at": time.time(), "package_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
        return self.view(manifest.id)

    async def upgrade(self, filename: str) -> dict[str, Any]:
        if Path(filename).name != filename:
            raise PluginPackageError("Invalid package filename")
        archive = self.packages.inbox / filename
        incoming = self.packages.validate(archive)
        old = self._manifest(incoming.id)
        record = self._record(incoming.id)
        was_enabled = bool(record.get("enabled"))
        if was_enabled:
            await self._stop_runtime(incoming.id)
        self.packages.install(archive, replace=True)
        runtime_root = (self.packages.runtime / incoming.id).resolve()
        venv_path = (runtime_root / "venv").resolve()
        if venv_path.is_dir() and venv_path.parent == runtime_root:
            shutil.rmtree(venv_path)
        marker = runtime_root / "requirements.sha256"
        if marker.is_file() and marker.resolve().parent == runtime_root:
            marker.unlink()
        try:
            if incoming.is_legacy:
                if not old.is_legacy:
                    raise PluginError("Downgrading a v2 plugin to Legacy v1 is not supported")
                migrated = record
            else:
                current = dict(record.get("config", {}))
                for name, encrypted in record.get("secrets", {}).items():
                    if encrypted:
                        current[name] = self.vault.decrypt(encrypted)
                data_dir = self.packages.root / "data" / incoming.id
                data_dir.mkdir(parents=True, exist_ok=True)
                async def no_core(_method: str, _params: dict[str, Any]):
                    raise PluginError("Core calls are unavailable during plugin migration")
                mode = record.get("runtime_mode", incoming.runtime.default)
                if mode not in incoming.runtime.supported:
                    mode = incoming.runtime.default
                runtime_type = InProcessPlugin if mode == "in_process" else PluginProcess
                process = runtime_type(incoming, self.packages.installed / incoming.id, runtime_root, no_core)
                try:
                    await process.start(context={
                        "plugin_id": incoming.id, "plugin_version": incoming.version,
                        "data_dir": str(data_dir), "config": current,
                        "secret_fields": {name: True for name in record.get("secrets", {})},
                    })
                    result = await process.request("config.migrate", {"old_config": current, "old_version": old.version, "new_version": incoming.version}, timeout=30)
                    migrated = self._reconcile_config(incoming, record, result)
                    await process.request("lifecycle.upgrade", {"old_version": old.version, "new_version": incoming.version}, timeout=30)
                finally:
                    await process.stop()
            migrated = {**migrated, "enabled": was_enabled, "error": "", "version": incoming.version, "package_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
            self._save_record(incoming.id, migrated)
            if was_enabled:
                return await self.enable(incoming.id)
            return self.view(incoming.id)
        except Exception as error:
            failed = {**record, "enabled": False, "error": self._sanitize(str(error))}
            self._save_record(incoming.id, failed)
            raise

    def _reconcile_config(self, manifest: PluginManifest, old_record: dict[str, Any], migrated: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(migrated, dict):
            raise PluginError("Plugin migration must return an object")
        schema = manifest.config_schema or {"type": "object", "properties": {}}
        properties = schema.get("properties", {})
        config: dict[str, Any] = {}
        secrets: dict[str, str] = {}
        complete: dict[str, Any] = {}
        for name, field in properties.items():
            if name in migrated:
                value = migrated[name]
            elif "default" in field and not field.get("secret"):
                value = field["default"]
            else:
                continue
            validate_value(field, value, f"config.{name}")
            complete[name] = value
            if field.get("secret"):
                secrets[name] = self.vault.encrypt(value)
            else:
                config[name] = value
        validate_value(schema, complete, "config")
        return {**old_record, "config": config, "secrets": secrets}

    async def enable(self, plugin_id: str) -> dict[str, Any]:
        manifest = self._manifest(plugin_id)
        record = self._record(plugin_id)
        if manifest.is_legacy:
            missing = [name for name, field in manifest.config.items() if field.required and not (record.get("secrets", {}).get(name) if field.type == "secret" else record.get("config", {}).get(name))]
        else:
            schema = manifest.config_schema or {"type": "object", "properties": {}}
            missing = [name for name in schema.get("required", []) if not (record.get("secrets", {}).get(name) if schema.get("properties", {}).get(name, {}).get("secret") else record.get("config", {}).get(name))]
        if missing:
            raise PluginError("Required plugin configuration is missing: " + ", ".join(missing))
        if plugin_id not in self.processes:
            if not manifest.is_legacy:
                self._check_dependencies(manifest)
            broker = PluginCapabilityBroker(self.sessions, plugin_id, set(manifest.capabilities))
            async def capability(capability: str, params: dict[str, Any]):
                if manifest.is_legacy:
                    result = await broker.call(capability, params)
                    if capability == "event.subscribe" and params.get("action") == "subscribe":
                        self.set_subscriptions(plugin_id, list(params.get("events") or []))
                    return result
                return await self._v2_core_call(plugin_id, capability, params.get("params") or {})
            mode = record.get("runtime_mode", manifest.runtime.default) if not manifest.is_legacy else "isolated"
            if mode not in manifest.runtime.supported:
                raise PluginError("Selected plugin runtime is not supported by this package")
            runtime_type = InProcessPlugin if mode == "in_process" else PluginProcess
            process = runtime_type(manifest, self.packages.installed / plugin_id, self.packages.runtime / plugin_id, capability)
            context = self._v2_context(manifest, record) if not manifest.is_legacy else None
            await process.start(context=context)
            if not manifest.is_legacy:
                try:
                    self.registry.register_plugin(plugin_id, process.registrations)
                    self._validate_provider_config(manifest, context or {})
                    self._sync_model_providers(manifest)
                except Exception:
                    self.registry.unregister_plugin(plugin_id)
                    await process.stop()
                    raise
            self.processes[plugin_id] = process
        record.update({"enabled": True, "error": ""})
        self._save_record(plugin_id, record)
        return self.view(plugin_id)

    @staticmethod
    def model_provider_row_id(identifier: str) -> str:
        return "plugin-" + hashlib.sha256(identifier.encode()).hexdigest()[:29]

    def _sync_model_providers(self, manifest: PluginManifest) -> None:
        registrations = [item for item in self.registry.list("provider") if item.plugin_id == manifest.id and item.metadata.get("provider_kind") == "model"]
        with self.sessions.begin() as db:
            for item in registrations:
                identifier = self.model_provider_row_id(item.identifier)
                existing = db.get(Provider, identifier)
                if existing and existing.data.get("plugin_provider") != item.identifier:
                    raise PluginError("Plugin Model Provider identifier collides with an existing provider")
                data = ProviderInput(
                    name=(manifest.name + " · " + item.name)[:80],
                    type="plugin", plugin_provider=item.identifier, models=item.metadata["models"],
                ).model_dump(exclude={"api_key"})
                if existing:
                    existing.data = data
                else:
                    db.add(Provider(id=identifier, data=data))

    def _validate_provider_config(self, manifest: PluginManifest, context: dict[str, Any]) -> None:
        manifest_properties = (manifest.config_schema or {}).get("properties", {})
        values = context.get("config", {})
        for item in self.registry.list("provider"):
            if item.plugin_id != manifest.id:
                continue
            schema = item.metadata["config_schema"]
            properties = schema.get("properties", {})
            for name, field in properties.items():
                source = manifest_properties.get(name)
                if not source or source.get("type") != field.get("type") or bool(source.get("secret")) != bool(field.get("secret")):
                    raise PluginError("Provider config must be declared with the same type and secret status in the plugin manifest")
            validate_value(schema, {name: values[name] for name in properties if name in values}, "provider_config")

    def _check_dependencies(self, manifest: PluginManifest) -> None:
        for dependency in manifest.dependencies:
            if dependency.requirement != "required":
                continue
            try:
                installed = self._manifest(dependency.id)
            except PluginPackageError as error:
                raise PluginError(f"Required plugin {dependency.id} is not installed") from error
            process = self.processes.get(dependency.id)
            if not process or process.state() != "running":
                raise PluginError(f"Required plugin {dependency.id} is not running")
            if dependency.version not in {"*", installed.version}:
                raise PluginError(f"Required plugin {dependency.id} version does not match")

    def _v2_context(self, manifest: PluginManifest, record: dict[str, Any]) -> dict[str, Any]:
        schema = manifest.config_schema or {"type": "object", "properties": {}}
        properties = schema.get("properties", {})
        values = dict(record.get("config", {}))
        secret_fields: dict[str, bool] = {}
        for name, field in properties.items():
            if field.get("secret"):
                secret_fields[name] = True
                encrypted = record.get("secrets", {}).get(name)
                if encrypted:
                    values[name] = self.vault.decrypt(encrypted)
        validate_value(schema, values, "config")
        data_dir = self.packages.root / "data" / manifest.id
        data_dir.mkdir(parents=True, exist_ok=True)
        return {
            "plugin_id": manifest.id,
            "plugin_version": manifest.version,
            "data_dir": str(data_dir),
            "config": values,
            "secret_fields": secret_fields,
        }

    async def _v2_core_call(self, plugin_id: str, method: str, params: dict[str, Any]) -> Any:
        if not isinstance(params, dict):
            raise PluginError("Core API parameters must be an object")
        mapping = {
            "task.status": ("task.read", "status"),
            "task.list": ("task.read", "list"),
            "task.get": ("task.read", "get"),
            "repository.list": ("repository.read", "list"),
            "repository.get": ("repository.read", "get"),
            "owner_decision.submit": ("owner_decision.submit", "submit"),
            "plugin.log": ("plugin.log", "log"),
        }
        if method == "service.call":
            identifier = str(params.get("service_id", ""))
            registration = self.registry.get(identifier, "service")
            caller = self._manifest(plugin_id)
            if registration.plugin_id != plugin_id and registration.plugin_id not in {item.id for item in caller.dependencies}:
                raise PluginError("Service provider is not declared as a plugin dependency")
            requested_version = params.get("version")
            if requested_version is not None and requested_version != registration.metadata.get("version"):
                raise PluginError("Service version does not match")
            return await self.invoke_service(identifier, params.get("input") or {})
        if method == "workflow.resume":
            return self._resume_review(plugin_id, params)
        if method.startswith("github."):
            return await self._github_call(plugin_id, method, params)
        if method not in mapping:
            raise PluginError("Core API method is not available")
        capability, action = mapping[method]
        broker = PluginCapabilityBroker(self.sessions, plugin_id, {capability})
        return await broker.call(capability, {**params, "action": action})

    async def _github_call(self, plugin_id: str, method: str, params: dict[str, Any]) -> Any:
        if method not in {"github.issue.get", "github.pull.get", "github.comment", "github.privileged.comment"}:
            raise PluginError("GitHub Core Client method is not available")
        if self.controller is None:
            raise PluginError("GitHub Controller is unavailable")
        task_id = str(params.get("task_id", ""))
        with self.sessions() as db:
            task = db.get(Task, task_id)
            if not task:
                raise PluginError("Task is unavailable")
            repository = db.get(Repository, task.repository)
            installation_id = task.data.get("installation_id") or (repository.installation_id if repository else None)
            if not installation_id:
                raise PluginError("Repository installation is unavailable")
            repo, number, kind = task.repository, task.number, task.kind
            owner_approved = task.data.get("owner_decision_action") == "implement"
            status = task.status
        github = self.controller.app_client()
        if method == "github.issue.get":
            if kind != "issue":
                raise PluginError("Task is not an Issue")
            return await github.issue_context(installation_id, repo, number)
        if method == "github.pull.get":
            if kind != "pull_request":
                raise PluginError("Task is not a Pull Request")
            return await github.pull_context(installation_id, repo, number)
        body = params.get("body")
        invocation_id = str(params.get("invocation_id", ""))
        if not isinstance(body, str) or not body.strip() or len(body) > 8000:
            raise PluginError("GitHub comment body is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,200}", invocation_id):
            raise PluginError("GitHub write requires a stable invocation_id")
        privileged = method == "github.privileged.comment"
        if not privileged and (not owner_approved or status not in {"queued", "running"}):
            raise PluginError("GitHub write requires an active, owner-approved Task")
        audit = {"plugin_id": plugin_id, "method": method, "invocation_id": invocation_id, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        with self.sessions.begin() as db:
            db.add(Timeline(task_id=task_id, kind="plugin_github_write_requested", data=audit))
        result = await self.controller.github_action(
            task_id,
            "plugin_comment_privileged" if privileged else "plugin_comment",
            f"plugin-comment:{plugin_id}:{task_id}:{invocation_id}",
            audit,
            lambda: github.comment(installation_id, repo, number, body),
        )
        with self.sessions.begin() as db:
            db.add(Timeline(task_id=task_id, kind="plugin_github_write_completed", data={**audit, "url": result.get("html_url")}))
        return {"id": str(result.get("id") or ""), "html_url": result.get("html_url")}

    def _resume_review(self, plugin_id: str, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("task_id", ""))
        invocation_id = str(params.get("invocation_id", ""))
        result = params.get("result")
        validate_value(HOOKS["pr.review"].output_schema, {"action": "replace", "result": result}, "review")
        with self.sessions.begin() as db:
            task = db.get(Task, task_id)
            if not task or task.status != "waiting_for_plugin":
                raise PluginError("Task is not waiting for a plugin review")
            waiting = task.data.get("plugin_waiting") or {}
            if waiting.get("plugin_id") != plugin_id or waiting.get("invocation_id") != invocation_id:
                raise PluginError("Plugin review invocation does not match")
            if result["head_sha"] != waiting.get("head_sha"):
                raise PluginError("Plugin review is for a stale PR head")
            if task.data.get("plugin_review_result"):
                raise PluginError("Plugin review result has already been submitted")
            task.status = "queued"
            task.data = {**task.data, "plugin_review_result": result}
            db.add(Timeline(task_id=task_id, kind="plugin_review_resumed", data={"plugin_id": plugin_id, "invocation_id": invocation_id, "head_sha": result["head_sha"]}))
        return {"status": "queued", "task_id": task_id}

    async def _invoke_extension(self, registration: ExtensionRegistration, arguments: dict[str, Any], invocation_id: str, timeout: float) -> Any:
        if registration.plugin_id in self.draining_plugins:
            raise PluginError("Plugin is draining")
        process = self.processes.get(registration.plugin_id)
        if not process or process.state() != "running":
            raise PluginError("Plugin is not running")
        plugin_id = registration.plugin_id
        self.active_invocations[plugin_id] = self.active_invocations.get(plugin_id, 0) + 1
        try:
            return await process.request(
                "extension.invoke",
                {"kind": registration.kind, "name": registration.name, "input": arguments, "invocation_id": invocation_id, "context": {}},
                timeout=timeout,
            )
        finally:
            remaining = self.active_invocations.get(plugin_id, 1) - 1
            if remaining:
                self.active_invocations[plugin_id] = remaining
            else:
                self.active_invocations.pop(plugin_id, None)

    async def invoke_tool(self, identifier: str, arguments: dict[str, Any], *, invocation_id: str | None = None) -> Any:
        invocation_id = invocation_id or uuid.uuid4().hex
        try:
            return await self.registry.invoke_tool(identifier, arguments, self._invoke_extension, invocation_id=invocation_id)
        except asyncio.CancelledError:
            registration = self.registry.get(identifier, "tool")
            process = self.processes.get(registration.plugin_id)
            if process and process.state() == "running":
                try:
                    await asyncio.shield(process.notify("extension.cancel", {"invocation_id": invocation_id}))
                except Exception:
                    pass
            raise

    async def invoke_provider(self, identifier: str, kind: str, arguments: dict[str, Any]) -> Any:
        registration = self.registry.get(identifier, "provider")
        if registration.metadata.get("provider_kind") != kind:
            raise PluginError("Registered provider kind does not match")
        timeout = 120
        if kind == "sandbox" and arguments.get("action") == "exec":
            requested = arguments.get("timeout")
            if not isinstance(requested, int) or not 1 <= requested <= 7200:
                raise PluginError("Sandbox execution timeout is invalid")
            timeout = requested + 20
        return await self._invoke_extension(registration, arguments, uuid.uuid4().hex, timeout)

    async def resolve_model_endpoint(self, identifier: str, model: str) -> tuple[str, str, str]:
        registration = self.registry.get(identifier, "provider")
        if registration.metadata.get("provider_kind") != "model" or model not in registration.metadata.get("models", []):
            raise PluginError("Plugin Model Provider or model is unavailable")
        result = await self.invoke_provider(identifier, "model", {"action": "resolve", "model": model})
        if not isinstance(result, dict) or not isinstance(result.get("base_url"), str) or not isinstance(result.get("api_key"), str):
            raise PluginError("Plugin Model Provider returned an invalid endpoint")
        selected_model = result.get("model", model)
        if not isinstance(selected_model, str) or not 1 <= len(selected_model) <= 200:
            raise PluginError("Plugin Model Provider returned an invalid model ID")
        endpoint = ProviderInput(name="Plugin endpoint", base_url=result["base_url"], models=[selected_model])
        return endpoint.base_url, result["api_key"], selected_model

    async def invoke_service(self, identifier: str, arguments: dict[str, Any], *, invocation_id: str | None = None) -> Any:
        return await self.registry.invoke_service(identifier, arguments, self._invoke_extension, invocation_id=invocation_id)

    async def invoke_route(self, plugin_id: str, name: str, method: str, access: str, body: Any, headers: dict[str, str]) -> Any:
        registration = self.registry.get(f"{plugin_id}/{name}", "route")
        if registration.metadata.get("access") != access or method not in registration.metadata.get("methods", []):
            raise PluginError("Plugin HTTP route is not available")
        return await self._invoke_extension(registration, {"method": method, "body": body, "headers": headers}, uuid.uuid4().hex, 30)

    async def dispatch_hook(self, name: str, payload: dict[str, Any]):
        return await self.registry.dispatch_hook(name, payload, self._invoke_extension)

    def agent_tools(self, agent_id: str) -> list[ExtensionRegistration]:
        if agent_id != "code_worker":
            return []
        with self.sessions() as db:
            row = db.get(Config, f"plugin-tools:{agent_id}")
            selected = set(row.data.get("tools", [])) if row else set()
        return [item for item in self.registry.list("tool") if item.identifier in selected]

    def set_agent_tools(self, agent_id: str, identifiers: list[str]) -> list[str]:
        if agent_id != "code_worker" or not isinstance(identifiers, list) or len(identifiers) > 64:
            raise PluginError("Invalid Agent Tool selection")
        available = {item.identifier for item in self.registry.list("tool")}
        if any(not isinstance(value, str) or value not in available for value in identifiers):
            raise PluginError("Selected Agent Tool is not available")
        selected = list(dict.fromkeys(identifiers))
        with self.sessions.begin() as db:
            row = db.get(Config, f"plugin-tools:{agent_id}")
            if row:
                row.data = {"tools": selected}
            else:
                db.add(Config(id=f"plugin-tools:{agent_id}", data={"tools": selected}))
        return selected

    def enable_recommended_tools(self, plugin_id: str) -> dict[str, list[str]]:
        recommendations: dict[str, list[str]] = {}
        for item in self.registry.list("tool"):
            if item.plugin_id != plugin_id:
                continue
            for agent_id in item.metadata.get("recommended_agents", []):
                if agent_id == "code_worker":
                    recommendations.setdefault(agent_id, []).append(item.identifier)
        for agent_id, identifiers in recommendations.items():
            existing = [item.identifier for item in self.agent_tools(agent_id)]
            self.set_agent_tools(agent_id, existing + identifiers)
        return recommendations

    async def finalize_task(self, task_id: str) -> None:
        """Run registered task finalizers independently of business Hook results.

        Each invocation has a persisted ID. A failed or crashed plugin leaves an
        explicit incomplete record; another attempt reuses that same ID.
        """
        with self.sessions() as db:
            task = db.get(Task, task_id)
            if not task or task.status in {"queued", "running"} or task.status.startswith("waiting_"):
                return
            payload = {
                "task_id": task.id,
                "status": task.status,
                "attempt": max(task.attempts, 1),
                "summary": str(task.data.get("summary") or task.data.get("error") or "")[:8000],
            }
        for registration in [item for item in self.registry.list("hook") if item.name == "task.finally"]:
            record_id = f"plugin-finalizer:{task_id}:{payload['attempt']}:{registration.plugin_id}"
            with self.sessions.begin() as db:
                record = db.get(Config, record_id)
                if record and record.data.get("status") == "completed":
                    continue
                invocation_id = (record.data.get("invocation_id") if record else None) or uuid.uuid4().hex
                if record:
                    record.data = {"status": "pending", "invocation_id": invocation_id}
                else:
                    db.add(Config(id=record_id, data={"status": "pending", "invocation_id": invocation_id}))
            try:
                await self._invoke_extension(registration, payload, invocation_id, 5)
            except Exception as error:
                with self.sessions.begin() as db:
                    db.get(Config, record_id).data = {"status": "incomplete", "invocation_id": invocation_id, "error_type": type(error).__name__}
                    db.add(Timeline(task_id=task_id, kind="plugin_finalizer_incomplete", data={"plugin_id": registration.plugin_id, "invocation_id": invocation_id, "error_type": type(error).__name__}))
            else:
                with self.sessions.begin() as db:
                    db.get(Config, record_id).data = {"status": "completed", "invocation_id": invocation_id}
                    db.add(Timeline(task_id=task_id, kind="plugin_finalizer_completed", data={"plugin_id": registration.plugin_id, "invocation_id": invocation_id}))

    async def _stop_runtime(self, plugin_id: str) -> None:
        self.draining_plugins.add(plugin_id)
        self.registry.unregister_plugin(plugin_id)
        deadline = time.monotonic() + 5
        while self.active_invocations.get(plugin_id, 0) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.ui_sessions = {digest: session for digest, session in self.ui_sessions.items() if session[0] != plugin_id}
        sockets = list(self.connections.pop(plugin_id, set()))
        self.connection_state.pop(plugin_id, None)
        self.subscriptions.pop(plugin_id, None)
        for socket in sockets:
            try:
                await socket.close(code=1012)
            except Exception:
                pass
        process = self.processes.pop(plugin_id, None)
        try:
            if process:
                await process.stop()
        finally:
            self.draining_plugins.discard(plugin_id)

    async def disable(self, plugin_id: str) -> dict[str, Any]:
        await self._stop_runtime(plugin_id)
        record = self._record(plugin_id)
        record["enabled"] = False
        self._save_record(plugin_id, record)
        return self.view(plugin_id)

    async def reload(self, plugin_id: str) -> dict[str, Any]:
        if not self._record(plugin_id).get("enabled"):
            raise PluginError("Plugin is disabled")
        await self._stop_runtime(plugin_id)
        try:
            return await self.enable(plugin_id)
        except Exception as error:
            record = self._record(plugin_id)
            record["error"] = self._sanitize(str(error))
            self._save_record(plugin_id, record)
            raise

    def set_runtime_mode(self, plugin_id: str, mode: str) -> dict[str, Any]:
        manifest = self._manifest(plugin_id)
        if manifest.is_legacy or mode not in manifest.runtime.supported:
            raise PluginError("Plugin runtime mode is not supported")
        record = self._record(plugin_id)
        if record.get("enabled"):
            raise PluginError("Disable the plugin before changing its runtime mode")
        record["runtime_mode"] = mode
        self._save_record(plugin_id, record)
        return self.view(plugin_id)

    async def uninstall(self, plugin_id: str, *, delete_data: bool = False) -> None:
        await self.disable(plugin_id)
        self.packages.uninstall(plugin_id)
        if delete_data:
            data_root = (self.packages.root / "data").resolve()
            destination = (data_root / plugin_id).resolve()
            if destination.parent != data_root:
                raise PluginPackageError("Invalid plugin data path")
            if destination.is_dir():
                shutil.rmtree(destination)
        with self.sessions.begin() as db:
            row = db.get(Config, self._config_id(plugin_id))
            if row:
                db.delete(row)

    def configure(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        manifest = self._manifest(plugin_id)
        if not manifest.is_legacy:
            return self._configure_v2(manifest, values)
        unknown = set(values) - set(manifest.config)
        if unknown:
            raise PluginError("Unknown plugin configuration field")
        record = self._record(plugin_id)
        config, encrypted = dict(record.get("config", {})), dict(record.get("secrets", {}))
        for name, value in values.items():
            field = manifest.config[name]
            if field.type == "secret":
                if value not in (None, "", "********"):
                    encrypted[name] = self.vault.encrypt(str(value))
                continue
            config[name] = self._validate_value(field.type, value, field.options)
        record.update({"config": config, "secrets": encrypted, "error": ""})
        self._save_record(plugin_id, record)
        return self.view(plugin_id)

    def _configure_v2(self, manifest: PluginManifest, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise PluginError("Plugin configuration must be an object")
        schema = manifest.config_schema or {"type": "object", "properties": {}}
        properties = schema.get("properties", {})
        if set(values) - set(properties):
            raise PluginError("Unknown plugin configuration field")
        record = self._record(manifest.id)
        config = dict(record.get("config", {}))
        encrypted = dict(record.get("secrets", {}))
        for name, value in values.items():
            field = properties[name]
            if field.get("secret"):
                if value not in (None, "", "********"):
                    validate_value(field, value, f"config.{name}")
                    encrypted[name] = self.vault.encrypt(value)
            else:
                validate_value(field, value, f"config.{name}")
                config[name] = value
        complete = dict(config)
        for name, field in properties.items():
            if field.get("secret") and encrypted.get(name):
                complete[name] = self.vault.decrypt(encrypted[name])
        validate_value(schema, complete, "config")
        self._validate_provider_config(manifest, {"config": complete})
        record.update({"config": config, "secrets": encrypted, "error": ""})
        self._save_record(manifest.id, record)
        return self.view(manifest.id)

    def regenerate_secret(self, plugin_id: str, field_name: str) -> str:
        manifest = self._manifest(plugin_id)
        field = manifest.config.get(field_name) if manifest.is_legacy else (manifest.config_schema or {}).get("properties", {}).get(field_name)
        is_secret = field.type == "secret" if manifest.is_legacy and field else bool(field and field.get("secret"))
        if not is_secret:
            raise PluginError("Secret field not found")
        value = generate_bridge_token()
        record = self._record(plugin_id)
        encrypted = dict(record.get("secrets", {}))
        encrypted[field_name] = self.vault.encrypt(value)
        record["secrets"] = encrypted
        self._save_record(plugin_id, record)
        return value

    def _validate_value(self, kind: str, value: Any, options: list[str]) -> Any:
        if kind == "string":
            if not isinstance(value, str) or len(value) > 4000:
                raise PluginError("Invalid string configuration")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise PluginError("Invalid boolean configuration")
        elif kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise PluginError("Invalid integer configuration")
        elif kind == "select":
            if value not in options:
                raise PluginError("Invalid select configuration")
        elif kind == "string_list":
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) or len(item) > 500 for item in value):
                raise PluginError("Invalid string-list configuration")
        return value

    def list(self) -> list[dict[str, Any]]:
        return [self.view(manifest.id) for manifest in self.packages.discover()]

    def readme(self, plugin_id: str) -> str:
        self._manifest(plugin_id)
        root = (self.packages.installed / plugin_id).resolve()
        candidate = root / "README.md"
        path = candidate.resolve()
        if candidate.is_symlink() or path.parent != root or not path.is_file():
            raise PluginPackageError("Plugin README is not available")
        if path.stat().st_size > 512 * 1024:
            raise PluginPackageError("Plugin README is too large")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise PluginPackageError("Plugin README must be UTF-8") from error

    def issue_ui_session(self, plugin_id: str) -> str:
        manifest = self._manifest(plugin_id)
        if not manifest.ui or not self._record(plugin_id).get("enabled"):
            raise PluginError("Plugin UI is not available")
        token = secrets.token_urlsafe(32)
        self.ui_sessions[hashlib.sha256(token.encode()).hexdigest()] = (plugin_id, time.time() + 300)
        return token

    def valid_ui_session(self, plugin_id: str, token: str) -> bool:
        if not token:
            return False
        entry = self.ui_sessions.get(hashlib.sha256(token.encode()).hexdigest())
        return bool(entry and entry[0] == plugin_id and entry[1] > time.time() and self._record(plugin_id).get("enabled"))

    def ui_file(self, plugin_id: str, asset_path: str) -> Path:
        manifest = self._manifest(plugin_id)
        if not manifest.ui or not self._record(plugin_id).get("enabled"):
            raise PluginPackageError("Plugin UI is not available")
        root = (self.packages.installed / plugin_id).resolve()
        entrypoint = (root / manifest.ui.entrypoint).resolve()
        if root not in entrypoint.parents:
            raise PluginPackageError("Plugin UI entrypoint escapes its package")
        ui_root = entrypoint.parent
        candidate = (ui_root / asset_path).resolve()
        if ui_root != candidate and ui_root not in candidate.parents:
            raise PluginPackageError("Plugin UI path escapes its bundle")
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_MESSAGE_BYTES:
            raise PluginPackageError("Plugin UI asset is not available")
        return candidate

    def view(self, plugin_id: str) -> dict[str, Any]:
        manifest = self._manifest(plugin_id)
        record = self._record(plugin_id)
        process = self.processes.get(plugin_id)
        config = dict(record.get("config", {}))
        if manifest.is_legacy:
            for name, field in manifest.config.items():
                if field.type == "secret":
                    config[name] = "********" if record.get("secrets", {}).get(name) else ""
            ui_schema = {key: value.model_dump() for key, value in manifest.config.items()}
        else:
            ui_schema = self._v2_ui_schema(manifest.config_schema)
            for name, field in (manifest.config_schema or {}).get("properties", {}).items():
                if field.get("secret"):
                    config[name] = "********" if record.get("secrets", {}).get(name) else ""
        connection = self.connection_state.get(plugin_id, {})
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "package_sha256": record.get("package_sha256", ""),
            "publisher": manifest.publisher,
            "license": manifest.license,
            "description": manifest.description,
            "api_version": manifest.plugin_api,
            "capabilities": manifest.capabilities,
            "config_schema": ui_schema,
            "config_json_schema": manifest.config_schema if not manifest.is_legacy else None,
            "registrations": [
                {
                    "kind": item.kind,
                    "name": item.name,
                    "identifier": item.identifier,
                    "metadata": {
                        **item.metadata,
                        **({"recommended_agents": [agent for agent in item.metadata.get("recommended_agents", []) if agent == "code_worker"]} if item.kind == "tool" else {}),
                        **({"stability": HOOKS[item.name].stability, "behavior": HOOKS[item.name].behavior, "failure": HOOKS[item.name].failure, "timeout": HOOKS[item.name].timeout} if item.kind == "hook" else {}),
                    },
                }
                for item in self.registry.list() if item.plugin_id == plugin_id
            ],
            "runtime_mode": record.get("runtime_mode", manifest.runtime.default) if not manifest.is_legacy else "isolated",
            "runtime_supported": manifest.runtime.supported if not manifest.is_legacy else ["isolated"],
            "config": config,
            "enabled": bool(record.get("enabled")),
            "runtime_status": process.state() if process else "stopped",
            "error": record.get("error", ""),
            "connection_status": "connected" if self.connections.get(plugin_id) else "disconnected",
            "connected_instance": connection.get("instance", ""),
            "last_heartbeat": connection.get("last_heartbeat"),
            "event_subscriptions": sorted(self.subscriptions.get(plugin_id, set())),
            "has_readme": (self.packages.installed / plugin_id / "README.md").is_file(),
            "has_ui": bool(manifest.ui),
            "ui_entrypoint": Path(manifest.ui.entrypoint).name if manifest.ui else None,
        }

    @staticmethod
    def _v2_ui_schema(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not schema:
            return {}
        required = set(schema.get("required", []))
        result: dict[str, dict[str, Any]] = {}
        for name, field in schema.get("properties", {}).items():
            kind = "secret" if field.get("secret") else "select" if "enum" in field else "integer" if field.get("type") == "integer" else "string_list" if field.get("type") == "array" and field.get("items", {}).get("type") == "string" else "json" if field.get("type") in {"array", "object"} else field.get("type", "string")
            result[name] = {
                "type": kind,
                "title": field.get("title", name),
                "description": field.get("description", ""),
                "required": name in required,
                "default": field.get("default"),
                "options": field.get("enum", []),
            }
        return result

    def _secret(self, plugin_id: str, name: str) -> str:
        encrypted = self._record(plugin_id).get("secrets", {}).get(name)
        return self.vault.decrypt(encrypted) if encrypted else ""

    async def authenticate_transport(self, plugin_id: str, message: dict[str, Any]) -> bool:
        if message.get("protocol") != "maintune.astrbot.v1" or message.get("type") != "authenticate":
            return False
        supplied = str(message.get("token", ""))
        expected = self._secret(plugin_id, "bridge_token")
        return bool(expected) and secrets.compare_digest(supplied.encode(), expected.encode())

    async def connect(self, plugin_id: str, websocket: WebSocket, authenticate: dict[str, Any]) -> None:
        process = self.processes.get(plugin_id)
        if not process or process.state() != "running":
            raise PluginError("Plugin is not running")
        self.connections.setdefault(plugin_id, set()).add(websocket)
        self.connection_state[plugin_id] = {"instance": str(authenticate.get("instance", ""))[:200], "last_heartbeat": time.time()}
        result = await process.request("transport.connected", {key: value for key, value in authenticate.items() if key != "token"})
        await websocket.send_json(result or {"protocol": "maintune.astrbot.v1", "type": "hello", "timestamp": time.time()})
        for event in self._load_outbox(plugin_id).values():
            await websocket.send_json({"protocol": "maintune.astrbot.v1", "type": "event", "event": event})

    async def disconnect(self, plugin_id: str, websocket: WebSocket) -> None:
        self.connections.get(plugin_id, set()).discard(websocket)
        if not self.connections.get(plugin_id):
            self.connection_state.pop(plugin_id, None)
        process = self.processes.get(plugin_id)
        if process and process.state() == "running":
            await process.notify("transport.disconnected", {})

    async def transport_message(self, plugin_id: str, message: dict[str, Any]) -> list[dict[str, Any]]:
        process = self.processes.get(plugin_id)
        if not process:
            raise PluginError("Plugin is not running")
        if message.get("type") == "ack" and isinstance(message.get("event_id"), str):
            self._ack(plugin_id, message["event_id"])
        if message.get("type") == "heartbeat":
            state = self.connection_state.setdefault(plugin_id, {})
            state["last_heartbeat"] = time.time()
        result = await process.request("transport.message", message)
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    async def publish(self, event: PluginEvent) -> None:
        for manifest in self.packages.discover():
            if "event.subscribe" not in manifest.capabilities or not self._record(manifest.id).get("enabled"):
                continue
            subscribed = self.subscriptions.get(manifest.id)
            if subscribed and event.event not in subscribed:
                continue
            self._queue(manifest.id, event)
            payload = {"protocol": "maintune.astrbot.v1", "type": "event", "event": event.model_dump()}
            for websocket in list(self.connections.get(manifest.id, set())):
                try:
                    await websocket.send_json(payload)
                except Exception:
                    self.connections.get(manifest.id, set()).discard(websocket)

    async def publish_task(self, event: str, task_id: str, data: dict[str, Any] | None = None) -> None:
        with self.sessions() as db:
            task = db.get(Task, task_id)
            if task:
                task_ids = list(db.scalars(select(Task.id)))
                reference = PluginCapabilityBroker._short_task_reference(task.id, task_ids)
                await self.publish(make_event(event, task, data, reference))

    def set_subscriptions(self, plugin_id: str, events: list[str]) -> None:
        self.subscriptions[plugin_id] = {event for event in events if isinstance(event, str) and len(event) <= 80}

    def _outbox_path(self, plugin_id: str) -> Path:
        path = self.packages.runtime / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path / "event-outbox.json"

    def _load_outbox(self, plugin_id: str) -> dict[str, dict[str, Any]]:
        path = self._outbox_path(plugin_id)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _write_outbox(self, plugin_id: str, events: dict[str, dict[str, Any]]) -> None:
        path = self._outbox_path(plugin_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(events, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)

    def _queue(self, plugin_id: str, event: PluginEvent) -> None:
        events = self._load_outbox(plugin_id)
        events[event.event_id] = event.model_dump()
        if len(events) > 1000:
            events.pop(next(iter(events)))
        self._write_outbox(plugin_id, events)

    def _ack(self, plugin_id: str, event_id: str) -> None:
        events = self._load_outbox(plugin_id)
        if events.pop(event_id, None) is not None:
            self._write_outbox(plugin_id, events)

    @staticmethod
    def _sanitize(message: str) -> str:
        return message.replace("\n", " ")[:500]
