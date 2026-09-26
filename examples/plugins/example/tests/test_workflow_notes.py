"""Offline tests: the example imports the public SDK and no Core modules."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from maintune_plugin_sdk import PluginAPI, PluginContext


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
import workflow_notes as plugin  # noqa: E402
from provider_example import register_model_provider_example  # noqa: E402
from build_mtp import build  # noqa: E402


class WorkflowNotesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.api = PluginAPI()
        plugin.register(self.api)

    def context(self, invocation_id="fixture-invocation", **config):
        return PluginContext(
            plugin_id="example.workflow-notes",
            plugin_version="0.1.0-dev",
            data_dir=self.data_dir,
            config={"label": "Example", "record_started": True, **config},
            invocation_id=invocation_id,
        )

    def test_registration_and_generated_tool_schema(self):
        values = {(item["kind"], item["name"]): item for item in self.api.registrations()}
        self.assertEqual(set(values), {
            ("hook", "task.started"), ("hook", "task.finally"), ("hook", "pr.review"),
            ("service", "activity.stats"), ("tool", "activity_count"),
        })
        schema = values[("tool", "activity_count")]["input_schema"]
        self.assertEqual(schema["properties"]["kind"], {"type": "string", "enum": ["started", "finalized"]})
        self.assertEqual(schema["required"], ["kind"])
        self.assertEqual(values[("tool", "activity_count")]["recommended_agents"], ["code_worker"])

    def test_hook_finalizer_service_and_tool_use_data_dir(self):
        context = self.context()
        asyncio.run(self.api.invoke("hook", "task.started", context, {"task_id": "task-7", "untrusted": "discarded"}))
        asyncio.run(self.api.invoke("hook", "task.started", context, {"task_id": "task-7"}))
        final_context = self.context(invocation_id="final-invocation")
        asyncio.run(self.api.invoke("hook", "task.finally", final_context, {"task_id": "task-7", "status": "completed", "attempt": 2, "summary": "discarded"}))
        asyncio.run(self.api.invoke("hook", "task.finally", final_context, {"task_id": "task-7", "status": "completed", "attempt": 2}))
        records = [json.loads(path.read_text(encoding="utf-8")) for path in (self.data_dir / "events").glob("*.json")]
        self.assertEqual(len(records), 2)
        self.assertFalse(any("summary" in item or "untrusted" in item for item in records))
        self.assertEqual(asyncio.run(self.api.invoke("service", "activity.stats", context, {})), {"started": 1, "finalized": 1})
        self.assertEqual(asyncio.run(self.api.invoke("tool", "activity_count", context, {"kind": "finalized"})), {
            "label": "Example", "kind": "finalized", "count": 1,
        })

    def test_config_can_disable_start_hook_without_blocking_finalizer(self):
        context = self.context(record_started=False)
        asyncio.run(self.api.invoke("hook", "task.started", context, {"task_id": "task-1"}))
        asyncio.run(self.api.invoke("hook", "task.finally", self.context(invocation_id="finally"), {
            "task_id": "task-1", "status": "failed", "attempt": 1,
        }))
        self.assertEqual(plugin._counts(context), {"started": 0, "finalized": 1})

    def test_experimental_review_hook_only_continues(self):
        result = asyncio.run(self.api.invoke("hook", "pr.review", self.context(), {"head_sha": "abc1234"}))
        self.assertEqual(result, {"action": "continue"})

    def test_provider_example_is_opt_in_and_has_secret_schema(self):
        self.assertFalse(any(item["kind"] == "provider" for item in self.api.registrations()))
        provider_api = PluginAPI()
        register_model_provider_example(provider_api)
        provider = provider_api.registrations()[0]
        self.assertEqual(provider["provider_kind"], "model")
        self.assertTrue(provider["config_schema"]["properties"]["api_key"]["secret"])

    def test_mtp_reproducible_and_declares_optional_dependency_and_ui(self):
        first, second = self.data_dir / "first.mtp", self.data_dir / "second.mtp"
        build(first)
        build(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(set(archive.namelist()), {
                "manifest.yaml", "README.md", "requirements.txt", "src/workflow_notes.py",
                "src/provider_example.py", "ui/index.html",
            })
            manifest = json.loads(archive.read("manifest.yaml"))
        self.assertEqual(manifest["plugin_api"], 2)
        self.assertEqual(manifest["runtime"]["default"], "isolated")
        self.assertEqual(manifest["dependencies"], [{"id": "example.anysearch", "version": "*", "requirement": "optional"}])
        self.assertEqual(manifest["ui"]["entrypoint"], "ui/index.html")


if __name__ == "__main__":
    unittest.main()
