"""
Telemetry decorators for MCP for Unity (Sora Unity MCP fork).

Telemetry was physically removed in this fork. These decorators are pure
pass-throughs kept for import/decoration compatibility — zero per-call
overhead (no signature binding, no timing, no record calls).
"""

from typing import Callable


def telemetry_tool(tool_name: str):
    """Pass-through decorator (telemetry removed)."""
    def decorator(func: Callable) -> Callable:
        return func
    return decorator


def telemetry_resource(resource_name: str):
    """Pass-through decorator (telemetry removed)."""
    def decorator(func: Callable) -> Callable:
        return func
    return decorator
