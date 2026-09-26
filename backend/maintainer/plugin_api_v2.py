from __future__ import annotations

import asyncio
import copy
import re
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal


class PluginAPIError(RuntimeError):
    pass


class PluginRegistrationError(PluginAPIError):
    pass


class PluginSchemaError(PluginAPIError):
    pass


_PRIMITIVES = {"object", "array", "string", "number", "integer", "boolean"}
_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    "default", "title", "description", "secret",
}


def validate_schema(schema: Any, *, root_object: bool = False) -> None:
    if not isinstance(schema, dict) or set(schema) - _SCHEMA_KEYS:
        raise PluginSchemaError("Plugin schema is not in the supported JSON Schema subset")
    kind = schema.get("type")
    if kind not in _PRIMITIVES:
        raise PluginSchemaError("Plugin schema requires a supported type")
    if root_object and kind != "object":
        raise PluginSchemaError("Tool input schema must be an object")
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values or len(values) > 100:
            raise PluginSchemaError("Schema enum must contain 1 to 100 values")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or len(properties) > 100 or not isinstance(required, list):
            raise PluginSchemaError("Invalid object schema")
        if len(set(required)) != len(required) or any(name not in properties for name in required):
            raise PluginSchemaError("Object schema has an invalid required list")
        if not isinstance(schema.get("additionalProperties", False), bool):
            raise PluginSchemaError("additionalProperties must be boolean")
        for child in properties.values():
            validate_schema(child)
    if kind == "array":
        if not isinstance(schema.get("items"), dict):
            raise PluginSchemaError("Array schema requires an items schema")
        validate_schema(schema["items"])
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if lower in schema and (not isinstance(schema[lower], int) or schema[lower] < 0):
            raise PluginSchemaError(f"Invalid {lower}")
        if upper in schema and (not isinstance(schema[upper], int) or schema[upper] < 0):
            raise PluginSchemaError(f"Invalid {upper}")
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise PluginSchemaError(f"{lower} exceeds {upper}")
    for key in ("minimum", "maximum"):
        if key in schema and (not isinstance(schema[key], (int, float)) or isinstance(schema[key], bool)):
            raise PluginSchemaError(f"Invalid {key}")


def validate_value(schema: dict[str, Any], value: Any, path: str = "input") -> None:
    validate_schema(schema)
    kind = schema["type"]
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }[kind]
    if not valid:
        raise PluginSchemaError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise PluginSchemaError(f"{path} is outside the allowed values")
    if kind == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise PluginSchemaError(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise PluginSchemaError(f"{path} contains an unknown property")
        for name, child_value in value.items():
            if name in properties:
                validate_value(properties[name], child_value, f"{path}.{name}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10_000):
            raise PluginSchemaError(f"{path} has an invalid item count")
        for index, child in enumerate(value):
            validate_value(schema["items"], child, f"{path}[{index}]")
    elif kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            raise PluginSchemaError(f"{path} has an invalid length")
    elif kind in {"number", "integer"}:
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise PluginSchemaError(f"{path} is outside the allowed range")


@dataclass(frozen=True)
class HookSpec:
    name: str
    stability: Literal["stable", "experimental"]
    behavior: Literal["notification", "pipeline", "replaceable"]
    concurrency: Literal["serial", "parallel"]
    failure: Literal["open", "closed"]
    timeout: float
    max_timeout: float
    retry_safe: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


_OBJECT = {"type": "object", "properties": {}, "additionalProperties": True}
_ISSUE_PROMPT = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "repository": {"type": "string", "minLength": 3, "maxLength": 200},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
    },
    "required": ["task_id", "repository", "prompt"],
    "additionalProperties": False,
}
_PIPELINE_OUTPUT = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "modify", "cancel"]},
        "payload": _ISSUE_PROMPT,
    },
    "required": ["action"],
    "additionalProperties": False,
}
_TASK_FINAL = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "status": {"type": "string", "minLength": 1, "maxLength": 40},
        "attempt": {"type": "integer", "minimum": 1},
        "summary": {"type": "string", "maxLength": 8000},
    },
    "required": ["task_id", "status", "attempt"],
    "additionalProperties": True,
}
_REVIEW_VERDICT = {"type": "string", "enum": ["approved", "changes_required", "owner_decision"]}
_REVIEW_FINDING = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["blocking", "suggestion"]},
        "path": {"type": "string", "maxLength": 1000},
        "line": {"type": "integer", "minimum": 1},
        "message": {"type": "string", "minLength": 1, "maxLength": 8000},
        "issue_type": {"type": "string", "maxLength": 100},
    },
    "required": ["severity", "message"],
    "additionalProperties": False,
}
_REVIEW_OUTPUT = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "replace", "cancel", "wait"]},
        "reason": {"type": "string", "maxLength": 1000},
        "result": {
            "type": "object",
            "properties": {
                "head_sha": {"type": "string", "minLength": 7, "maxLength": 64},
                "verdict": _REVIEW_VERDICT,
                "summary": {"type": "string", "minLength": 1, "maxLength": 12000},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "blocking_issues": {"type": "array", "items": _REVIEW_FINDING, "maxItems": 50},
                "suggestions": {"type": "array", "items": _REVIEW_FINDING, "maxItems": 50},
            },
            "required": ["head_sha", "verdict", "summary", "risk", "blocking_issues", "suggestions"],
            "additionalProperties": False,
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

HOOKS: dict[str, HookSpec] = {
    "task.started": HookSpec("task.started", "stable", "notification", "parallel", "open", 2, 10, True, _OBJECT),
    "task.finally": HookSpec("task.finally", "stable", "notification", "serial", "open", 5, 30, False, _TASK_FINAL),
    "issue.analysis_prompt": HookSpec("issue.analysis_prompt", "experimental", "pipeline", "serial", "open", 5, 30, False, _ISSUE_PROMPT, _PIPELINE_OUTPUT),
    "pr.review": HookSpec("pr.review", "experimental", "replaceable", "serial", "open", 30, 120, False, _OBJECT, _REVIEW_OUTPUT),
}


@dataclass(frozen=True)
class ExtensionRegistration:
    plugin_id: str
    kind: str
    name: str
    identifier: str
    load_order: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class HookDispatch:
    action: str
    payload: dict[str, Any]
    original: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    waiting_for_plugin: str | None = None
    waiting_invocation_id: str | None = None
    cancelled_by: str | None = None


Invoker = Callable[[ExtensionRegistration, dict[str, Any], str, float], Awaitable[Any]]


class PluginRegistry:
    """Validated runtime registrations; public identifiers are namespaced by Core."""

    def __init__(self):
        self._items: dict[tuple[str, str], ExtensionRegistration] = {}
        self._next_order = 0

    def register_plugin(self, plugin_id: str, values: Any) -> list[ExtensionRegistration]:
        if not isinstance(values, list) or len(values) > 256:
            raise PluginRegistrationError("Plugin registration list is invalid")
        parsed: list[ExtensionRegistration] = []
        staged: set[tuple[str, str]] = set()
        for value in values:
            if not isinstance(value, dict):
                raise PluginRegistrationError("Plugin registration must be an object")
            kind, name = value.get("kind"), value.get("name")
            if kind not in {"hook", "tool", "service", "provider", "route"} or not isinstance(name, str):
                raise PluginRegistrationError("Unsupported plugin registration")
            if not name or len(name) > 64 or not name[0].islower() or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for c in name):
                raise PluginRegistrationError("Invalid extension name")
            key = (plugin_id, f"{kind}:{name}")
            if key in staged:
                raise PluginRegistrationError("Duplicate extension registration")
            metadata = {key: val for key, val in value.items() if key not in {"kind", "name"}}
            if kind == "hook":
                hook = HOOKS.get(name)
                if not hook:
                    raise PluginRegistrationError(f"Hook {name!r} is not in the public Hook catalog")
                priority = metadata.get("priority", 100)
                if not isinstance(priority, int) or not -1000 <= priority <= 1000:
                    raise PluginRegistrationError("Invalid Hook priority")
            elif kind == "tool":
                schema = metadata.get("input_schema")
                validate_schema(schema, root_object=True)
                description = metadata.get("description")
                if not isinstance(description, str) or not description.strip() or len(description) > 1000:
                    raise PluginRegistrationError("Tool description is required")
                recommended = metadata.get("recommended_agents", [])
                if not isinstance(recommended, list) or len(recommended) > 64 or any(not isinstance(item, str) for item in recommended):
                    raise PluginRegistrationError("Invalid recommended_agents")
            elif kind == "service":
                version = metadata.get("version")
                if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version)):
                    raise PluginRegistrationError("Service version must be semantic versioning")
                for schema_name in ("input_schema", "output_schema"):
                    schema = metadata.get(schema_name)
                    if schema is not None:
                        validate_schema(schema)
            elif kind == "provider":
                if metadata.get("provider_kind") not in {"model", "sandbox"}:
                    raise PluginRegistrationError("Unsupported provider kind")
                validate_schema(metadata.get("config_schema"), root_object=True)
                if metadata["provider_kind"] == "model":
                    models = metadata.get("models")
                    if not isinstance(models, list) or not 1 <= len(models) <= 100 or any(not isinstance(model, str) or not 1 <= len(model) <= 200 for model in models) or len(set(models)) != len(models):
                        raise PluginRegistrationError("Model provider requires 1 to 100 unique model IDs")
            elif kind == "route":
                methods = metadata.get("methods", [])
                if not isinstance(methods, list) or not methods or any(method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} for method in methods):
                    raise PluginRegistrationError("Invalid plugin HTTP methods")
                if metadata.get("access") not in {"authenticated", "external"}:
                    raise PluginRegistrationError("Invalid plugin route access")
            parsed.append(ExtensionRegistration(plugin_id, kind, name, f"{plugin_id}/{name}", -1, metadata))
            staged.add(key)
        for item in parsed:
            self._next_order += 1
            registration = ExtensionRegistration(item.plugin_id, item.kind, item.name, item.identifier, self._next_order, item.metadata)
            self._items[(plugin_id, f"{item.kind}:{item.name}")] = registration
        return [self._items[(plugin_id, f"{item.kind}:{item.name}")] for item in parsed]

    def unregister_plugin(self, plugin_id: str) -> None:
        self._items = {key: value for key, value in self._items.items() if value.plugin_id != plugin_id}

    def list(self, kind: str | None = None) -> list[ExtensionRegistration]:
        return sorted((item for item in self._items.values() if kind is None or item.kind == kind), key=lambda item: item.load_order)

    def get(self, identifier: str, kind: str | None = None) -> ExtensionRegistration:
        matches = [item for item in self._items.values() if item.identifier == identifier and (kind is None or item.kind == kind)]
        if len(matches) != 1:
            raise PluginRegistrationError("Extension is not registered")
        return matches[0]

    def recommended_tools(self, agent_id: str) -> list[ExtensionRegistration]:
        return [item for item in self.list("tool") if agent_id in item.metadata.get("recommended_agents", [])]

    async def invoke_tool(self, identifier: str, arguments: dict[str, Any], invoker: Invoker, *, invocation_id: str | None = None, timeout: float | None = None) -> Any:
        registration = self.get(identifier, "tool")
        validate_value(registration.metadata["input_schema"], arguments)
        limit = min(timeout or 30, 120)
        return await invoker(registration, arguments, invocation_id or uuid.uuid4().hex, limit)

    async def invoke_service(self, identifier: str, arguments: dict[str, Any], invoker: Invoker, *, invocation_id: str | None = None) -> Any:
        registration = self.get(identifier, "service")
        schema = registration.metadata.get("input_schema")
        if schema:
            validate_value(schema, arguments)
        result = await invoker(registration, arguments, invocation_id or uuid.uuid4().hex, 30)
        output = registration.metadata.get("output_schema")
        if output:
            validate_value(output, result, "result")
        return result

    async def dispatch_hook(self, name: str, payload: dict[str, Any], invoker: Invoker) -> HookDispatch:
        spec = HOOKS.get(name)
        if not spec:
            raise PluginRegistrationError("Hook is not in the public Hook catalog")
        validate_value(spec.input_schema, payload)
        original = copy.deepcopy(payload)
        current = copy.deepcopy(payload)
        steps: list[dict[str, Any]] = []
        entries = sorted(
            (item for item in self.list("hook") if item.name == name),
            key=lambda item: (item.metadata.get("priority", 100), item.load_order),
        )
        async def call_with_retry(item: ExtensionRegistration, invocation_id: str):
            attempts = 2 if spec.retry_safe else 1
            for attempt in range(1, attempts + 1):
                try:
                    return await asyncio.wait_for(invoker(item, current, invocation_id, spec.timeout), spec.timeout), attempt
                except Exception:
                    if attempt == attempts:
                        raise
            raise AssertionError("unreachable")
        if spec.concurrency == "parallel" and spec.behavior == "notification":
            async def call_one(item):
                invocation_id = uuid.uuid4().hex
                try:
                    _, attempt = await call_with_retry(item, invocation_id)
                    return {"plugin_id": item.plugin_id, "status": "completed", "invocation_id": invocation_id, "attempts": attempt}
                except Exception as error:
                    return {"plugin_id": item.plugin_id, "status": "failed", "error_type": type(error).__name__, "invocation_id": invocation_id, "attempts": 2 if spec.retry_safe else 1}
            steps.extend(await asyncio.gather(*(call_one(item) for item in entries)))
            return HookDispatch("continue", current, original, tuple(steps))

        for item in entries:
            invocation_id = uuid.uuid4().hex
            try:
                response, attempt = await call_with_retry(item, invocation_id)
                if spec.behavior == "notification":
                    steps.append({"plugin_id": item.plugin_id, "status": "completed", "invocation_id": invocation_id, "attempts": attempt})
                    continue
                if response is None:
                    response = {"action": "continue"}
                validate_value(spec.output_schema or {}, response, "hook_result")
                action = response["action"]
                previous = copy.deepcopy(current)
                if spec.behavior == "pipeline" and action == "modify":
                    updated = response.get("payload")
                    validate_value(spec.input_schema, updated, "modified_payload")
                    current = copy.deepcopy(updated)
                if action == "replace":
                    result = response["result"]
                    if result["head_sha"] != original.get("head_sha"):
                        raise PluginSchemaError("Replacement review head SHA is stale")
                    current["review_result"] = result
                step = {"plugin_id": item.plugin_id, "status": "completed", "action": action, "invocation_id": invocation_id}
                if spec.behavior == "pipeline":
                    step.update({"input": previous, "output": copy.deepcopy(current)})
                steps.append(step)
                if action == "cancel":
                    return HookDispatch("cancel", current, original, tuple(steps), cancelled_by=item.plugin_id)
                elif action == "wait":
                    return HookDispatch("wait", current, original, tuple(steps), waiting_for_plugin=item.plugin_id, waiting_invocation_id=invocation_id)
            except Exception as error:
                steps.append({"plugin_id": item.plugin_id, "status": "failed", "error_type": type(error).__name__, "invocation_id": invocation_id, "attempts": 2 if spec.retry_safe else 1})
                if spec.failure == "closed":
                    raise
                # Fail-open means the Core keeps its last validated value. No
                # invalid plugin output is ever passed to the next extension.
        return HookDispatch("continue", current, original, tuple(steps))
