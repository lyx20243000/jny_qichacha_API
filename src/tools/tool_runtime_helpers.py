"""Shared helpers for invoking LangChain tools and parsing JSON tool output."""

from __future__ import annotations

import json
import time
from typing import Any


def invoke_langchain_tool(tool_obj: Any, kwargs: dict[str, Any]) -> Any:
    """Call a LangChain @tool object from inside another tool.

    Prefer `.func` for tool-to-tool orchestration so the inner tool runs as a
    plain Python function and does not re-enter LangChain's invoke chain.
    """
    if hasattr(tool_obj, "func") and callable(tool_obj.func):
        return tool_obj.func(**kwargs)
    if hasattr(tool_obj, "invoke") and callable(tool_obj.invoke):
        return tool_obj.invoke(kwargs)
    if callable(tool_obj):
        return tool_obj(**kwargs)
    raise TypeError(f"Unsupported tool object: {tool_obj!r}")


def as_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} must be a JSON object")


def stage_timing(started_at: float) -> float:
    return round(time.monotonic() - started_at, 2)
