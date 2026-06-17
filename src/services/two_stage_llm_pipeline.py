"""Two-stage LLM helpers for enterprise scoring and report enrichment.

The first stage produces compact scoring JSON. The second stage enriches
report-facing sections without changing scores.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

LLM_CONFIG = "config/agent_llm_config.json"
SCORING_DIMENSIONS = ("industry", "operation", "finance", "credit")
PROTECTED_CORE_KEYS = set(SCORING_DIMENSIONS) | {
    "red_line_data",
    "enterprise_profile",
    "subject_verification",
    "assessment_goal",
    "missing_financial_fields",
    "financial_assessment_notes",
}


SCORING_CORE_SYSTEM_PROMPT = """你是企业评分引擎，只负责生成紧凑、合法的 scoring_core_json。

输入包含 evidence_summary、collection_diagnostics 和 collection_policy。请优先使用 evidence_summary 中的 subject_profile、official_structured_summary、operation_signal_summary、finance_signal_summary、risk_signal_summary、field_gaps、conflict_flags 和 scoring_hints。

硬性规则：
- 只输出一个 JSON object，不要 Markdown，不要解释过程。
- 不写长报告，不输出章节正文。
- 每个评分项必须包含 score、reliability、basis。
- basis 不超过 60 个中文字符。
- 缺失数据不得编造；缺失时 reliability 用 red 或 yellow，并在 missing_financial_fields / financial_assessment_notes 中说明。
- green_power_demand 原则上不低于 3 分，除非有明确证据表明无法落地绿电合作。
- 信用维度只保留 dishonest_status 和 credit_rating 两项；行政处罚、经营异常、欠税、限高、资产负担等进入风险和行动建议，不作为独立信用计分项，除非构成严重失信。
- red_line_data 必须使用 JSON boolean / number，不要使用中文字符串。

必须输出这些顶层字段：
assessment_goal, enterprise_profile, subject_verification, industry, operation, finance, credit,
red_line_data, missing_financial_fields, financial_assessment_notes, overall_summary_short,
action_recommendation_core。

评分项 ID 必须使用：
industry: industry_advancement, industry_scale, industry_growth, competition_pattern, green_power_demand, downstream_market, industry_barrier
operation: enterprise_nature, industry_status, survival_years, recent_development, core_customers
finance: registered_capital, staff_count, annual_revenue, asset_scale, net_profit_margin, cash_flow, asset_liability_ratio
credit: dishonest_status, credit_rating
"""


REPORT_ENRICHMENT_SYSTEM_PROMPT = """你是企业报告整理引擎，只负责补全报告表达字段。

输入包含 scoring_core_json、qcc_data_json 和 collection_diagnostics_json。你不能重新打分，不能修改 industry/operation/finance/credit/red_line_data 中的任何分数或核心结论。若报告表达与 scoring_core_json 冲突，以 scoring_core_json 为准。

硬性规则：
- 只输出一个 JSON object，不要 Markdown，不要解释过程。
- 可以补全风险说明、绿电适配、履约能力、数据来源、行动建议和综合评价。
- 如果信息不足，写“未获取/需复核”，不得编造。
- green_power_fit.适配等级 只能是“高适配”或“中适配”。
- overall_summary 建议 180-300 字，行动建议要可执行。
- key_risks 最多 5 条，required_materials 最多 6 条。

必须输出这些顶层字段：
green_power_fit, performance_capability, related_party_risk, kyb_risk_overview,
legal_litigation_review, administrative_operation_risk_review, tax_environment_risk_review,
asset_equity_encumbrance_review, history_risk_review, operation_qualification_land_review,
data_source_summary, overall_summary, action_recommendation。
"""


def load_llm_config() -> dict[str, Any]:
    workspace_path = os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects")
    config_path = os.path.join(workspace_path, LLM_CONFIG)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_stage_llm_config(cfg: dict[str, Any], stage: str) -> dict[str, Any]:
    base = dict(cfg.get("config", {}))
    two_stage = cfg.get("two_stage_generation", {})
    if isinstance(two_stage, dict):
        stage_cfg = two_stage.get(stage, {})
        if isinstance(stage_cfg, dict):
            base.update(stage_cfg)
    base.setdefault("thinking", "disabled")
    return base


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
        "streaming": False,
        "timeout": stage_config.get("timeout", 180),
        "extra_body": {"thinking": {"type": stage_config.get("thinking", "disabled")}},
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
    llm = _build_chat_openai(stage_config, ctx=ctx)
    response = llm.invoke(
        [
            ("system", system_prompt),
            ("human", compact_json(payload)),
        ]
    )
    return extract_json_object(_response_text(response))


def build_scoring_core_json(
    *,
    evidence_payload: dict[str, Any],
    cfg: dict[str, Any],
    ctx: Any = None,
) -> dict[str, Any]:
    payload = {
        "evidence_summary": evidence_payload.get("evidence_summary", {}),
        "collection_diagnostics": evidence_payload.get("collection_diagnostics", {}),
        "collection_policy": evidence_payload.get("collection_policy", {}),
    }
    return invoke_stage_json(
        system_prompt=SCORING_CORE_SYSTEM_PROMPT,
        payload=payload,
        stage_config=get_stage_llm_config(cfg, "scoring_llm"),
        ctx=ctx,
    )


def build_report_enrichment_json(
    *,
    scoring_core_json: dict[str, Any],
    qcc_data_json: dict[str, Any],
    collection_diagnostics_json: dict[str, Any],
    cfg: dict[str, Any],
    ctx: Any = None,
) -> dict[str, Any]:
    payload = {
        "scoring_core_json": scoring_core_json,
        "qcc_data_json": qcc_data_json,
        "collection_diagnostics_json": collection_diagnostics_json,
    }
    return invoke_stage_json(
        system_prompt=REPORT_ENRICHMENT_SYSTEM_PROMPT,
        payload=payload,
        stage_config=get_stage_llm_config(cfg, "report_llm"),
        ctx=ctx,
    )


def merge_scoring_payload(
    scoring_core_json: dict[str, Any],
    report_enrichment_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge two-stage outputs. Scores and core identifiers stay protected."""
    final_payload = copy.deepcopy(scoring_core_json or {})
    enrichment = copy.deepcopy(report_enrichment_json or {})

    core_action = final_payload.pop("action_recommendation_core", None)
    short_summary = final_payload.pop("overall_summary_short", "")

    for key, value in enrichment.items():
        if key in PROTECTED_CORE_KEYS or key in SCORING_DIMENSIONS:
            continue
        if key == "action_recommendation" and isinstance(value, dict) and isinstance(core_action, dict):
            merged_action = copy.deepcopy(core_action)
            for action_key, action_value in value.items():
                if action_key in ("conclusion", "next_action") and merged_action.get(action_key):
                    continue
                if action_value not in (None, "", [], {}):
                    merged_action[action_key] = action_value
            final_payload[key] = merged_action
        elif value not in (None, "", [], {}):
            final_payload[key] = value

    if "action_recommendation" not in final_payload and isinstance(core_action, dict):
        final_payload["action_recommendation"] = core_action
    if "overall_summary" not in final_payload and short_summary:
        final_payload["overall_summary"] = short_summary

    return final_payload

