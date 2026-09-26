"""Minimal, side-effect-free Maintune Plugin API v2 example."""

from __future__ import annotations

from maintune_plugin_sdk import PluginAPI, PluginContext


def greet(context: PluginContext, name: str) -> dict[str, str]:
    """Return a greeting for the supplied name."""
    return {"message": f"Hello, {name}!"}


def register(api: PluginAPI) -> None:
    """Register this plugin's extensions with Maintune."""
    api.register_tool(
        "greet",
        greet,
        description="Return a greeting for a name.",
    )
