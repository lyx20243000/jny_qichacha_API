"""Single-stage LLM pipeline for enterprise scoring JSON generation."""

from __future__ import annotations

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

SUMMARY_SECTION_LIMITS = {
    "subject_profile": 1200,
    "official_structured_summary": 1200,
    "official_search_summary": 1000,
    "operation_signal_summary": 1200,
    "finance_signal_summary": 1200,
    "risk_signal_summary": 1200,
    "search_signal_summary": 1000,
}

QCC_DATA_PRIORITY_FIELDS = (
    "registration",
    "company_profile",
    "shareholder",
    "actual_controller",
    "listing_info",
    "key_personnel",
    "financial",
    "investment",
    "dishonest",
    "admin_penalty",
    "business_exception",
    "serious_violation",
    "high_consumption",
    "risk_scan",
    "case_filing",
    "credit_eval",
    "executed_person",
    "judicial_documents",
    "court_announcement",
    "final_case",
    "environmental_penalty",
    "tax_abnormal",
    "tax_arrears",
    "tax_violation",
    "equity_pledge",
    "equity_freeze",
    "chattel_mortgage",
    "land_mortgage",
    "history_risk",
    "patent",
    "trademark",
    "software_copyright",
    "bidding",
    "qualifications",
    "honor",
    "recruitment",
    "administrative_license",
    "taxpayer_qualification",
    "product_check",
    "state_owned_land_transfer",
    "news_sentiment",
    "field_sources",
    "source_conflicts",
)


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


def _clip_simple_value(value: Any, max_chars: int = 1000, max_items: int = 24) -> Any:
    if isinstance(value, dict):
        return {key: _clip_simple_value(item, max_chars, max_items) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_simple_value(item, max_chars, max_items) for item in value[:max_items]]
    if isinstance(value, str):
        return _shorten_text(value, max_chars)
    return value


def _clip_list_items(items: list[Any], *, max_items: int, max_chars_per_item: int) -> list[Any]:
    clipped = []
    for item in items[:max_items]:
        clipped.append(_clip_simple_value(item, max_chars=max_chars_per_item, max_items=max_items))
    return clipped


def _resolve_optional_positive_int(value: Any) -> int | None:
    if value in (None, "", False):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _trim_identity(identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        return {}
    keys = (
        "status",
        "enterprise_name",
        "unified_social_credit_code",
        "match_source",
        "match_reason",
        "confidence",
    )
    return {key: _shorten_text(identity.get(key), 320) for key in keys if identity.get(key) not in (None, "", [], {})}


def _trim_collection_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        return {}
    trimmed = {
        "mode": policy.get("mode", ""),
        "available_modes": _shorten_text(policy.get("available_modes", ""), 240),
        "qixin_search_key": _shorten_text(policy.get("qixin_search_key", ""), 240),
        "public_search_key": _shorten_text(policy.get("public_search_key", ""), 240),
        "qcc_mcp_search_key": _shorten_text(policy.get("qcc_mcp_search_key", ""), 240),
        "subject_confirmation_priority": _shorten_text(policy.get("subject_confirmation_priority", ""), 440),
        "triggered_collection": _clip_simple_value(policy.get("triggered_collection", {}), max_chars=360, max_items=12),
    }
    return {key: value for key, value in trimmed.items() if value not in (None, "", [], {})}


def _trim_collection_diagnostics(diagnostics: Any) -> dict[str, Any]:
    if not isinstance(diagnostics, dict):
        return {}
    trimmed = {
        "qixin": _clip_simple_value(diagnostics.get("qixin", {}), max_chars=440, max_items=20),
        "qcc_mcp": _clip_simple_value(diagnostics.get("qcc_mcp", {}), max_chars=440, max_items=20),
        "search": _clip_simple_value(diagnostics.get("search", {}), max_chars=440, max_items=20),
        "field_source_summary": _clip_simple_value(diagnostics.get("field_source_summary", {}), max_chars=240, max_items=20),
        "module_completeness": _clip_simple_value(diagnostics.get("module_completeness", {}), max_chars=240, max_items=20),
        "missing_or_unknown_fields_count": diagnostics.get("missing_or_unknown_fields_count", 0),
        "missing_or_unknown_fields": _clip_list_items(
            diagnostics.get("missing_or_unknown_fields", []) if isinstance(diagnostics.get("missing_or_unknown_fields"), list) else [],
            max_items=24,
            max_chars_per_item=240,
        ),
        "source_conflict_count": diagnostics.get("source_conflict_count", 0),
        "needs_human_review": diagnostics.get("needs_human_review", False),
        "review_reasons": _clip_list_items(
            diagnostics.get("review_reasons", []) if isinstance(diagnostics.get("review_reasons"), list) else [],
            max_items=12,
            max_chars_per_item=240,
        ),
        "recommended_next_step": diagnostics.get("recommended_next_step", ""),
        "collection_mode": diagnostics.get("collection_mode", ""),
    }
    return {key: value for key, value in trimmed.items() if value not in (None, "", [], {})}


def _trim_evidence_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    trimmed: dict[str, Any] = {}
    for key, limit in SUMMARY_SECTION_LIMITS.items():
        value = summary.get(key)
        if value not in (None, "", [], {}):
            trimmed[key] = _shorten_text(value, limit)
    for key in ("field_gaps", "conflict_flags", "scoring_hints"):
        value = summary.get(key)
        if isinstance(value, list):
            trimmed[key] = _clip_list_items(value, max_items=16, max_chars_per_item=280)
        elif value not in (None, "", [], {}):
            trimmed[key] = _clip_simple_value(value, max_chars=360, max_items=16)
    return trimmed


def _trim_qixin_api(qixin_api: Any, *, max_chars_per_api: int, max_items_per_list: int) -> dict[str, Any]:
    if not isinstance(qixin_api, dict):
        return {}
    trimmed = {}
    for key, value in qixin_api.items():
        if key == "_meta":
            trimmed[key] = _clip_simple_value(value, max_chars=480, max_items=20)
        elif key == "_fatal_error":
            trimmed[key] = _clip_simple_value(value, max_chars=440, max_items=16)
        else:
            trimmed[key] = _clip_simple_value(value, max_chars=max_chars_per_api, max_items=max_items_per_list)
    return trimmed


def _trim_qcc_group(group: Any, *, max_chars_per_item: int, max_items_per_list: int) -> Any:
    return _clip_simple_value(group, max_chars=max_chars_per_item, max_items=max_items_per_list)


def _trim_qcc_mcp(qcc_mcp: Any, *, tight: bool = False) -> dict[str, Any]:
    if not isinstance(qcc_mcp, dict):
        return {}
    list_limit = 16 if tight else 24
    char_limit = 480 if tight else 720
    return {
        "basic": _trim_qcc_group(qcc_mcp.get("basic", {}), max_chars_per_item=840 if not tight else 640, max_items_per_list=list_limit),
        "finance": _trim_qcc_group(qcc_mcp.get("finance", {}), max_chars_per_item=840 if not tight else 640, max_items_per_list=list_limit),
        "risk": _trim_qcc_group(qcc_mcp.get("risk", {}), max_chars_per_item=720 if not tight else 560, max_items_per_list=list_limit),
        "extended_risk": _trim_qcc_group(qcc_mcp.get("extended_risk", {}), max_chars_per_item=640 if not tight else 480, max_items_per_list=list_limit),
        "ip": _trim_qcc_group(qcc_mcp.get("ip", {}), max_chars_per_item=440 if not tight else 320, max_items_per_list=20 if not tight else 16),
        "operation": _trim_qcc_group(qcc_mcp.get("operation", {}), max_chars_per_item=520 if not tight else 360, max_items_per_list=20 if not tight else 16),
        "news": _trim_qcc_group(qcc_mcp.get("news", {}), max_chars_per_item=360 if not tight else 240, max_items_per_list=16 if not tight else 10),
        "_collection_note": _shorten_text(qcc_mcp.get("_collection_note", ""), char_limit),
    }


def _trim_triggered_mcp(triggered_mcp: Any, *, tight: bool = False) -> dict[str, Any]:
    if not isinstance(triggered_mcp, dict):
        return {}
    trimmed = {}
    max_sections = 6 if not tight else 4
    kept = 0
    for key, value in triggered_mcp.items():
        if key == "_meta":
            trimmed[key] = _clip_simple_value(value, max_chars=440, max_items=20)
            continue
        if kept >= max_sections:
            continue
        trimmed[key] = _clip_simple_value(value, max_chars=640 if not tight else 440, max_items=20 if not tight else 12)
        kept += 1
    return trimmed


def _trim_search_group(group: Any, *, tight: bool = False) -> dict[str, Any]:
    if not isinstance(group, dict):
        return {}
    items = group.get("items", [])
    max_items = 12 if not tight else 8
    summary_limit = 480 if not tight else 360
    trimmed_items = []
    if isinstance(items, list):
        for item in items[:max_items]:
            if not isinstance(item, dict):
                continue
            trimmed_items.append(
                {
                    "title": _shorten_text(item.get("title", ""), 240),
                    "site_name": _shorten_text(item.get("site_name", ""), 120),
                    "publish_time": _shorten_text(item.get("publish_time", ""), 80),
                    "snippet": _shorten_text(item.get("snippet", "") or item.get("summary", ""), 360 if not tight else 240),
                    "authority": item.get("authority", ""),
                }
            )
    trimmed = {
        "query": _shorten_text(group.get("query", ""), 360),
        "profile_name": _shorten_text(group.get("profile_name", ""), 120),
        "search_type": _shorten_text(group.get("search_type", ""), 120),
        "summary": _shorten_text(group.get("summary", ""), summary_limit),
        "items": trimmed_items,
        "stats": _clip_simple_value(group.get("stats", {}), max_chars=160, max_items=24),
    }
    return {key: value for key, value in trimmed.items() if value not in (None, "", [], {})}


def _trim_search_evidence(search_evidence: Any, *, tight: bool = False) -> dict[str, Any]:
    if not isinstance(search_evidence, dict):
        return {}
    preferred_order = ("industry", "basic", "risk", "finance", "development", "gsxt", "gsxt_risk")
    trimmed = {}
    for key in preferred_order:
        if key in search_evidence:
            trimmed[key] = _trim_search_group(search_evidence.get(key, {}), tight=tight)
    return trimmed


def _trim_qcc_data_json(qcc_data_json: Any, *, tight: bool = False) -> dict[str, Any]:
    if not isinstance(qcc_data_json, dict):
        return {}
    trimmed = {}
    for key in QCC_DATA_PRIORITY_FIELDS:
        if key not in qcc_data_json:
            continue
        value = qcc_data_json.get(key)
        if key == "field_sources":
            trimmed[key] = _clip_simple_value(value, max_chars=160, max_items=60)
        elif key == "source_conflicts":
            trimmed[key] = _clip_simple_value(value, max_chars=280 if not tight else 200, max_items=16 if not tight else 10)
        elif key == "history_risk":
            trimmed[key] = _clip_simple_value(value, max_chars=520 if not tight else 360, max_items=20 if not tight else 12)
        else:
            trimmed[key] = _clip_simple_value(value, max_chars=840 if not tight else 560, max_items=24 if not tight else 16)
    return trimmed


def _compose_payload(evidence_payload: dict[str, Any], *, tight: bool = False) -> dict[str, Any]:
    payload = {
        "identity": _trim_identity(evidence_payload.get("identity", {})),
        "collection_policy": _trim_collection_policy(evidence_payload.get("collection_policy", {})),
        "collection_diagnostics": _trim_collection_diagnostics(evidence_payload.get("collection_diagnostics", {})),
        "evidence_summary": _trim_evidence_summary(evidence_payload.get("evidence_summary", {})),
        "qixin_api": _trim_qixin_api(
            evidence_payload.get("qixin_api", {}),
            max_chars_per_api=2400 if not tight else 1800,
            max_items_per_list=40 if not tight else 28,
        ),
        "qcc_mcp": _trim_qcc_mcp(evidence_payload.get("qcc_mcp", {}), tight=tight),
        "triggered_mcp": _trim_triggered_mcp(evidence_payload.get("triggered_mcp", {}), tight=tight),
        "qcc_data_json": _trim_qcc_data_json(evidence_payload.get("qcc_data_json", {}), tight=tight),
        "search_evidence": _trim_search_evidence(evidence_payload.get("search_evidence", {}), tight=tight),
    }
    return payload


def build_single_stage_payload(
    evidence_payload: dict[str, Any],
    max_input_chars: int | None = None,
) -> dict[str, Any]:
    _ = max_input_chars
    return _compose_payload(evidence_payload, tight=False)


def build_single_stage_scoring_json(
    *,
    evidence_payload: dict[str, Any],
    cfg: dict[str, Any],
    ctx: Any = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    single_stage = cfg.get("single_stage_generation", {})
    max_input_chars = _resolve_optional_positive_int(single_stage.get("max_input_chars")) if isinstance(single_stage, dict) else None
    payload = payload or build_single_stage_payload(evidence_payload, max_input_chars=max_input_chars)
    return invoke_stage_json(
        system_prompt=SINGLE_STAGE_SYSTEM_PROMPT,
        payload=payload,
        stage_config=get_single_stage_llm_config(cfg),
        ctx=ctx,
    )
