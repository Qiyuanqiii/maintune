"""Small Plugin API v2 example: Hooks, finalizer, Service, Tool, and data_dir."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Literal

from maintune_plugin_sdk import PluginAPI, PluginContext


def _event_dir(context: PluginContext) -> Path:
    directory = context.data_dir / "events"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _store(context: PluginContext, kind: str, payload: dict) -> None:
    """One file per invocation makes replay of the same call harmless."""
    if not context.invocation_id:
        raise ValueError("Core invocation_id is required for an event write")
    directory = _event_dir(context)
    identifier = hashlib.sha256(f"{kind}:{context.invocation_id}".encode()).hexdigest()
    destination = directory / f"{identifier}.json"
    if destination.exists():
        return
    # Store a stable, deliberately small DTO; never persist the full Hook payload.
    record = {"kind": kind, "task_id": str(payload.get("task_id", ""))[:64]}
    if kind == "finalized":
        record["status"] = str(payload.get("status", ""))[:40]
        record["attempt"] = int(payload.get("attempt", 1))
    temporary = directory / f".{identifier}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def on_task_started(context: PluginContext, payload: dict) -> None:
    if context.config.get("record_started", True):
        _store(context, "started", payload)


def on_task_finally(context: PluginContext, payload: dict) -> None:
    _store(context, "finalized", payload)


def on_pr_review(context: PluginContext, payload: dict) -> dict[str, str]:
    """Observe the experimental review seam without changing Core's decision."""
    return {"action": "continue"}


def _counts(context: PluginContext) -> dict[str, int]:
    counts = {"started": 0, "finalized": 0}
    for path in _event_dir(context).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        kind = record.get("kind") if isinstance(record, dict) else None
        if kind in counts:
            counts[kind] += 1
    return counts


def activity_stats(context: PluginContext, payload: dict) -> dict[str, int]:
    """Public Service: other declared-dependency plugins can read counters."""
    return _counts(context)


def activity_count(context: PluginContext, kind: Literal["started", "finalized"]) -> dict[str, str | int]:
    """Agent Tool using type hints to generate its small input schema."""
    return {"label": str(context.config.get("label", "Maintune")), "kind": kind, "count": _counts(context)[kind]}


def register(api: PluginAPI) -> None:
    api.register_hook("task.started", on_task_started)
    api.on_task_finally(on_task_finally)
    api.register_hook("pr.review", on_pr_review)
    api.register_service(
        "activity.stats", activity_stats,
        description="Read counts of task lifecycle events stored by this plugin.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"started": {"type": "integer"}, "finalized": {"type": "integer"}},
            "required": ["started", "finalized"],
            "additionalProperties": False,
        },
    )
    api.register_tool(
        "activity_count", activity_count,
        description="Count task start or finalization observations recorded by this plugin.",
        recommended_agents=["code_worker"],
    )
