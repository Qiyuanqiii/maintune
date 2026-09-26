"""Opt-in Model Provider registration sketch; never loaded by the example plugin.

Copy this into a real provider plugin, implement the transport and response DTO,
then call register_model_provider_example(api) from that plugin's register(api).
"""

from __future__ import annotations

from maintune_plugin_sdk import PluginAPI, PluginContext


def model_call(context: PluginContext, request: dict) -> dict:
    raise NotImplementedError("Implement your Model Provider transport before registering it")


def register_model_provider_example(api: PluginAPI) -> None:
    api.register_model_provider(
        "model_example", model_call,
        models=["example-model"],
        config_schema={
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "minLength": 1},
                "api_key": {"type": "string", "secret": True, "minLength": 1},
            },
            "required": ["model_id", "api_key"],
            "additionalProperties": False,
        },
    )
