import os
import json
import logging
from typing import Annotated
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from langchain_core.messages import AnyMessage
from coze_coding_utils.runtime_ctx.context import default_headers
from storage.memory.memory_saver import get_memory_saver
from tools.enterprise_evidence_tool import collect_enterprise_evidence
from tools.enterprise_disambiguate_tool import search_enterprise_candidates
from tools.enterprise_search_tool import (
    search_industry_info,
    search_enterprise_basic,
    search_enterprise_risk,
    search_enterprise_finance,
    search_enterprise_development,
    search_gsxt_info,
)
from tools.enterprise_fetch_tool import fetch_enterprise_page
from tools.report_tool import generate_enterprise_report
from tools.single_stage_report_tool import generate_enterprise_report_single
from tools.qcc_mcp_tool import (
    qcc_get_basic_info,
    qcc_get_finance_info,
    qcc_get_risk_info,
    qcc_get_ip_info,
    qcc_get_operation_info,
    qcc_get_news_info,
    qcc_get_extended_risk_info,
)


logger = logging.getLogger(__name__)

LLM_CONFIG = "config/agent_llm_config.json"

SINGLE_STAGE_DEFAULT_PROMPT_PREFIX = (
    "# 默认入口兜底\n"
    "企业分析、评分和PDF报告默认优先调用 generate_enterprise_report_single；"
    "该工具会完整采集数据后只调用一次 LLM 生成 scoring_json。\n"
)

# 默认保留最近 20 轮对话 (40 条消息)
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
    """滑动窗口: 只保留最近 MAX_MESSAGES 条消息"""
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class AgentState(MessagesState):
    messages: Annotated[list[AnyMessage], _windowed_messages]


def _ensure_single_stage_default_prompt(sp: str) -> str:
    if "generate_enterprise_report_single" in sp:
        return sp
    return SINGLE_STAGE_DEFAULT_PROMPT_PREFIX + "\n" + sp


def _build_chat_openai(cfg: dict, api_key: str, base_url: str, ctx=None):
    agent_streaming = _resolve_streaming_flag(cfg["config"].get("streaming"), default=False)
    kwargs = {
        "model": cfg["config"].get("model"),
        "api_key": api_key,
        "base_url": base_url,
        "temperature": cfg["config"].get("temperature", 0.4),
        "streaming": agent_streaming,
        "timeout": cfg["config"].get("timeout", 600),
        "extra_body": {
            "thinking": {
                "type": cfg["config"].get("thinking", "disabled")
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
    cfg["sp"] = _ensure_single_stage_default_prompt(str(cfg.get("sp") or ""))

    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    llm = _build_chat_openai(cfg, api_key, base_url, ctx=ctx)

    # 免费渠道工具：固定采集、Coze 搜索、公开互联网搜索、页面抓取和报告生成
    tools = [
        generate_enterprise_report_single,
        collect_enterprise_evidence,
        search_enterprise_candidates,
        search_industry_info,
        search_enterprise_basic,
        search_enterprise_risk,
        search_enterprise_finance,
        search_enterprise_development,
        search_gsxt_info,
        fetch_enterprise_page,
        generate_enterprise_report,
    ]

    # 企查查 MCP 仅用于启信宝未覆盖字段、缺失字段补查或 deep 尽调；主体确认已由 collect_enterprise_evidence 内部优先调用启信宝 API 1.41。
    qcc_free_tools = [
        qcc_get_basic_info,
        qcc_get_finance_info,
        qcc_get_risk_info,
        qcc_get_ip_info,
        qcc_get_operation_info,
        qcc_get_news_info,
        qcc_get_extended_risk_info,
    ]
    tools.extend(qcc_free_tools)

    agent = create_agent(
        model=llm,
        system_prompt=cfg.get("sp"),
        tools=tools,
        checkpointer=get_memory_saver(),
        state_schema=AgentState,
    )

    return agent
