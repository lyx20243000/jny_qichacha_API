"""Shared helpers for JSON-only LLM pipeline calls."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

LLM_CONFIG = "config/agent_llm_config.json"


def load_llm_config() -> dict[str, Any]:
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, LLM_CONFIG)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from an LLM response."""
    if not isinstance(text, str):
        raise ValueError("LLM response is not text")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()

    decoder = json.JSONDecoder()
    for idx, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("LLM response does not contain a valid JSON object")


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _resolve_streaming_flag(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on", "stream", "streaming", "enabled"}:
        return True
    if normalized in {"false", "0", "no", "off", "non-stream", "non_stream", "disabled"}:
        return False
    return default


def _build_chat_openai(stage_config: dict[str, Any], ctx: Any = None):
    from coze_coding_utils.runtime_ctx.context import default_headers
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")
    common_kwargs = {
        "model": stage_config.get("model"),
        "api_key": api_key,
        "base_url": base_url,
        "temperature": stage_config.get("temperature", 0.2),
        "streaming": _resolve_streaming_flag(stage_config.get("streaming"), default=False),
        "timeout": stage_config.get("timeout", 600),
        "extra_body": {"thinking": {"type": stage_config.get("thinking", "enabled")}},
        "default_headers": default_headers(ctx) if ctx else {},
    }
    if stage_config.get("top_p") is not None:
        common_kwargs["top_p"] = stage_config.get("top_p")

    max_tokens = stage_config.get("max_completion_tokens")
    if max_tokens:
        try:
            return ChatOpenAI(**common_kwargs, max_completion_tokens=max_tokens)
        except TypeError:
            try:
                return ChatOpenAI(**common_kwargs, max_tokens=max_tokens)
            except TypeError:
                logger.warning("ChatOpenAI does not accept max token kwargs; continuing without token cap")
    return ChatOpenAI(**common_kwargs)


def invoke_stage_json(
    *,
    system_prompt: str,
    payload: dict[str, Any],
    stage_config: dict[str, Any],
    ctx: Any = None,
) -> dict[str, Any]:
    logger.info(
        "invoke_stage_json config: model=%s streaming=%s thinking=%s timeout=%s max_completion_tokens=%s payload_chars=%s",
        stage_config.get("model"),
        _resolve_streaming_flag(stage_config.get("streaming"), default=False),
        stage_config.get("thinking", "enabled"),
        stage_config.get("timeout", 600),
        stage_config.get("max_completion_tokens"),
        len(compact_json(payload)),
    )
    llm = _build_chat_openai(stage_config, ctx=ctx)
    response = llm.invoke(
        [
            ("system", system_prompt),
            ("human", compact_json(payload)),
        ]
    )
    return extract_json_object(_response_text(response))
