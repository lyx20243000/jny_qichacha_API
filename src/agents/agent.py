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

# 默认保留最近 20 轮对话 (40 条消息)
MAX_MESSAGES = 40


def _windowed_messages(old, new):
    """滑动窗口: 只保留最近 MAX_MESSAGES 条消息"""
    return add_messages(old, new)[-MAX_MESSAGES:]  # type: ignore


class AgentState(MessagesState):
    messages: Annotated[list[AnyMessage], _windowed_messages]


def build_agent(ctx=None):
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, LLM_CONFIG)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    llm = ChatOpenAI(
        model=cfg["config"].get("model"),
        api_key=api_key,
        base_url=base_url,
        temperature=cfg["config"].get("temperature", 0.4),
        streaming=True,
        timeout=cfg["config"].get("timeout", 600),
        extra_body={
            "thinking": {
                "type": cfg["config"].get("thinking", "disabled")
            }
        },
        default_headers=default_headers(ctx) if ctx else {},
    )

    # 免费渠道工具：固定采集、Coze 搜索、公开互联网搜索、页面抓取和报告生成
    tools = [
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
