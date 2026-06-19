import json
import logging
import os
from typing import Annotated

from coze_coding_utils.runtime_ctx.context import default_headers
from langchain.agents import create_agent
from langchain_core.messages import AnyMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages

from storage.memory.memory_saver import get_memory_saver
from tools.enterprise_analysis_tool import analyze_enterprise_report

logger = logging.getLogger(__name__)

LLM_CONFIG = "config/agent_llm_config.json"

FIXED_RUNNER_DEFAULT_PROMPT_PREFIX = (
    "# 默认入口兜底\n"
    "企业分析、评分和 PDF 报告默认优先调用 analyze_enterprise_report。\n"
    "该工具内部固定执行主体确认 -> 证据采集 -> scoring_json -> PDF 报告，"
    "不要让外层 Agent 自己搬运大型 evidence_json。\n"
)

MAX_MESSAGES = 40


def _resolve_streaming_flag(value, *, default: bool) -> bool:
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


def _windowed_messages(old, new):
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class AgentState(MessagesState):
    messages: Annotated[list[AnyMessage], _windowed_messages]


def _ensure_fixed_runner_default_prompt(sp: str) -> str:
    if "analyze_enterprise_report" in sp:
        return sp
    return FIXED_RUNNER_DEFAULT_PROMPT_PREFIX + "\n" + sp


def _build_chat_openai(cfg: dict, api_key: str, base_url: str, ctx=None):
    agent_streaming = _resolve_streaming_flag(cfg["config"].get("streaming"), default=True)
    kwargs = {
        "model": cfg["config"].get("model"),
        "api_key": api_key,
        "base_url": base_url,
        "temperature": cfg["config"].get("temperature", 0.4),
        "streaming": agent_streaming,
        "timeout": cfg["config"].get("timeout", 600),
        "extra_body": {
            "thinking": {
                "type": cfg["config"].get("thinking", "enabled")
            }
        },
        "default_headers": default_headers(ctx) if ctx else {},
    }
    if cfg["config"].get("top_p") is not None:
        kwargs["top_p"] = cfg["config"].get("top_p")

    max_tokens = cfg["config"].get("max_completion_tokens")
    if max_tokens:
        try:
            return ChatOpenAI(**kwargs, max_completion_tokens=max_tokens)
        except TypeError:
            try:
                return ChatOpenAI(**kwargs, max_tokens=max_tokens)
            except TypeError:
                logger.warning("ChatOpenAI does not accept max token kwargs; continuing without token cap")
    return ChatOpenAI(**kwargs)


def build_agent(ctx=None):
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, LLM_CONFIG)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["sp"] = _ensure_fixed_runner_default_prompt(str(cfg.get("sp") or ""))

    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    llm = _build_chat_openai(cfg, api_key, base_url, ctx=ctx)

    tools = [
        analyze_enterprise_report,
    ]

    agent = create_agent(
        model=llm,
        system_prompt=cfg.get("sp"),
        tools=tools,
        checkpointer=get_memory_saver(),
        state_schema=AgentState,
    )

    return agent
