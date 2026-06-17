"""Single-stage LLM pipeline for enterprise scoring JSON generation."""

from __future__ import annotations

import copy
from typing import Any

from services.llm_json_pipeline import compact_json, invoke_stage_json


SINGLE_STAGE_SYSTEM_PROMPT = """你是企业综合评分引擎。请基于输入的完整采集结果，一次性生成最终 scoring_json。

硬性规则：
- 只输出一个合法 JSON object，不要 Markdown，不要解释过程。
- 所有文字用中文。
- 不得编造缺失事实；没有权威数据时 reliability 用 yellow/red，并写入 missing_financial_fields / financial_assessment_notes。
- 每个评分项必须包含 score、reliability、basis；basis 不超过 80 个中文字符。
- red_line_data 必须使用 JSON boolean / number，不要使用中文字符串。
- green_power_fit.适配等级 只能是“高适配”或“中适配”。
- action_recommendation 必须可执行，说明是否合作、下一步、合作方式、重点风险、需补充资料。

必须输出顶层字段：
assessment_goal, enterprise_profile, subject_verification,
industry, operation, finance, credit, red_line_data,
green_power_fit, performance_capability, related_party_risk,
kyb_risk_overview, legal_litigation_review, administrative_operation_risk_review,
tax_environment_risk_review, asset_equity_encumbrance_review, history_risk_review,
operation_qualification_land_review, data_source_summary,
missing_financial_fields, financial_assessment_notes,
industry_summary, operation_summary, finance_summary, credit_summary,
overall_summary, action_recommendation。

评分项 ID 必须使用：
industry: industry_advancement, industry_scale, industry_growth, competition_pattern, green_power_demand, downstream_market, industry_barrier
operation: enterprise_nature, industry_status, survival_years, recent_development, core_customers
finance: registered_capital, staff_count, annual_revenue, asset_scale, net_profit_margin, cash_flow, asset_liability_ratio
credit: dishonest_status, credit_rating

评分口径：
- 行业只评价客观细分行业；企业竞争力放在经营维度。
- 绿电需求原则上不低于 3 分，除非明确无法落地绿电合作。
- 企业经营重点看企业性质、行业地位、存续年限、近三年扩张、核心客户/订单。
- 财务缺失时保守评分，并明确要求补充财报、纳税证明、银行流水或审计报告。
- 信用维度只保留失信情况和信用等级；处罚、异常、税务环保、限高、资产负担等进入专项风险和重点关注风险。
"""


def get_single_stage_llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    base = dict(cfg.get("config", {}))
    single_stage = cfg.get("single_stage_generation", {})
    if isinstance(single_stage, dict):
        stage_cfg = single_stage.get("report_llm", {})
        if isinstance(stage_cfg, dict):
            base.update(stage_cfg)
    base.setdefault("thinking", "disabled")
    return base


def _shorten_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _clip_value(value: Any, max_chars: int = 500, max_items: int = 12) -> Any:
    if isinstance(value, dict):
        return {key: _clip_value(item, max_chars, max_items) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_value(item, max_chars, max_items) for item in value[:max_items]]
    if isinstance(value, str):
        return _shorten_text(value, max_chars)
    return value


def _bounded_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text = compact_json(payload)
    if len(text) <= max_chars:
        return payload

    trimmed = copy.deepcopy(payload)
    trimmed["search_evidence"] = _clip_value(trimmed.get("search_evidence", {}), max_chars=240, max_items=6)
    trimmed["qcc_data_json"] = _clip_value(trimmed.get("qcc_data_json", {}), max_chars=320, max_items=8)
    if len(compact_json(trimmed)) <= max_chars:
        return trimmed

    trimmed["evidence_summary"] = _clip_value(trimmed.get("evidence_summary", {}), max_chars=360, max_items=8)
    trimmed["collection_diagnostics"] = _clip_value(
        trimmed.get("collection_diagnostics", {}),
        max_chars=260,
        max_items=8,
    )
    return trimmed


def build_single_stage_payload(
    evidence_payload: dict[str, Any],
    max_input_chars: int = 18000,
) -> dict[str, Any]:
    payload = {
        "identity": _clip_value(evidence_payload.get("identity", {}), max_chars=300),
        "collection_policy": _clip_value(evidence_payload.get("collection_policy", {}), max_chars=260),
        "collection_diagnostics": _clip_value(
            evidence_payload.get("collection_diagnostics", {}),
            max_chars=420,
            max_items=12,
        ),
        "evidence_summary": _clip_value(evidence_payload.get("evidence_summary", {}), max_chars=650),
        "qixin_api": _clip_value(evidence_payload.get("qixin_api", {}), max_chars=450, max_items=10),
        "qcc_mcp": _clip_value(evidence_payload.get("qcc_mcp", {}), max_chars=420, max_items=10),
        "triggered_mcp": _clip_value(evidence_payload.get("triggered_mcp", {}), max_chars=360, max_items=10),
        "qcc_data_json": _clip_value(evidence_payload.get("qcc_data_json", {}), max_chars=520, max_items=12),
        "search_evidence": _clip_value(evidence_payload.get("search_evidence", {}), max_chars=360, max_items=10),
    }
    return _bounded_payload(payload, max_input_chars)


def build_single_stage_scoring_json(
    *,
    evidence_payload: dict[str, Any],
    cfg: dict[str, Any],
    ctx: Any = None,
) -> dict[str, Any]:
    single_stage = cfg.get("single_stage_generation", {})
    max_input_chars = 18000
    if isinstance(single_stage, dict):
        max_input_chars = int(single_stage.get("max_input_chars", max_input_chars) or max_input_chars)

    payload = build_single_stage_payload(evidence_payload, max_input_chars=max_input_chars)
    return invoke_stage_json(
        system_prompt=SINGLE_STAGE_SYSTEM_PROMPT,
        payload=payload,
        stage_config=get_single_stage_llm_config(cfg),
        ctx=ctx,
    )
