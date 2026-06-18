"""Staggered parallel LLM pipeline for enterprise dimension scoring.

The pipeline starts dimension-specific LLM calls at a fixed interval, waits for
all dimensions to finish, then runs a final summary LLM. It keeps score fields
owned by the dimension calls and lets the summary call write report-facing
sections only.
"""

from __future__ import annotations

import copy
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from services.two_stage_llm_pipeline import compact_json, get_stage_llm_config, invoke_stage_json

logger = logging.getLogger(__name__)

DIMENSION_KEYS = ("industry", "operation", "finance", "credit")
SUMMARY_PROTECTED_KEYS = set(DIMENSION_KEYS) | {
    "red_line_data",
    "missing_financial_fields",
    "financial_assessment_notes",
}


COMMON_DIMENSION_RULES = """You are an enterprise scoring engine. Return only one valid JSON object.
Write all text values in Chinese. Do not output Markdown or explanations.
Each scoring item must include score, reliability, and basis. reliability must be green, yellow, or red.
Each basis must be no more than 60 Chinese characters. Summaries must be no more than 120 Chinese characters.
Do not invent missing facts. Mark uncertain or missing facts as yellow/red and explain briefly.
"""

INDUSTRY_PROMPT = COMMON_DIMENSION_RULES + """
Score only the industry dimension. Required output keys:
industry, industry_summary, industry_key_findings.
Required industry item ids:
industry_advancement, industry_scale, industry_growth, competition_pattern,
green_power_demand, downstream_market, industry_barrier.
green_power_demand should not be lower than 3 unless evidence clearly shows green power cooperation is impossible.
"""

OPERATION_PROMPT = COMMON_DIMENSION_RULES + """
Score only the operation dimension. Required output keys:
operation, operation_summary, operation_key_findings.
Required operation item ids:
enterprise_nature, industry_status, survival_years, recent_development, core_customers.
Use company type, shareholder background, qualifications, honors, bids, recruitment, development signals, and customer/order evidence.
"""

FINANCE_PROMPT = COMMON_DIMENSION_RULES + """
Score only the finance dimension. Required output keys:
finance, finance_summary, finance_key_findings, missing_financial_fields, financial_assessment_notes.
Required finance item ids:
registered_capital, staff_count, annual_revenue, asset_scale, net_profit_margin, cash_flow, asset_liability_ratio.
If revenue, profit, cash flow, assets, or liabilities are missing, mark the fields and use conservative scores.
"""

CREDIT_PROMPT = COMMON_DIMENSION_RULES + """
Score only the credit/risk dimension. Required output keys:
credit, red_line_data, credit_summary, risk_key_findings.
Required credit item ids: dishonest_status, credit_rating.
red_line_data must use JSON booleans/numbers only:
is_dishonest, major_penalty_count, has_abnormal_operation, legal_person_restricted.
Administrative penalties, business exceptions, tax, environmental, and encumbrance risks must be reflected in risk findings.
"""

SUMMARY_PROMPT = """You are an enterprise report summary engine. Return only one valid JSON object.
Write all text values in Chinese. Do not output Markdown or explanations.
You must not change scores, score bases, red_line_data, missing_financial_fields, or financial_assessment_notes.
Use the four dimension results and collection diagnostics to write only report-facing sections.
Required output keys:
assessment_goal, enterprise_profile, subject_verification, green_power_fit,
performance_capability, related_party_risk, kyb_risk_overview,
data_source_summary, overall_summary, action_recommendation.
green_power_fit level can only be 高适配 or 中适配.
overall_summary should be 180-260 Chinese characters.
action_recommendation must include conclusion, next_action, cooperation_advice, key_risks, required_materials.
key_risks max 5 items. required_materials max 6 items.
"""


DIMENSION_SPECS = {
    "industry": {
        "prompt": INDUSTRY_PROMPT,
        "result_keys": ("industry", "industry_summary", "industry_key_findings"),
        "qcc_fields": ("registration", "company_profile", "qixin_basic"),
        "search_groups": ("industry", "development"),
    },
    "operation": {
        "prompt": OPERATION_PROMPT,
        "result_keys": ("operation", "operation_summary", "operation_key_findings"),
        "qcc_fields": (
            "registration",
            "company_profile",
            "shareholder",
            "actual_controller",
            "qixin_tech_enterprise",
            "qixin_qualification",
            "qualifications",
            "honor",
            "bidding",
            "recruitment",
            "news_sentiment",
        ),
        "search_groups": ("basic", "development"),
    },
    "finance": {
        "prompt": FINANCE_PROMPT,
        "result_keys": (
            "finance",
            "finance_summary",
            "finance_key_findings",
            "missing_financial_fields",
            "financial_assessment_notes",
        ),
        "qcc_fields": ("registration", "financial", "investment", "bidding", "recruitment"),
        "search_groups": ("finance", "development"),
    },
    "credit": {
        "prompt": CREDIT_PROMPT,
        "result_keys": ("credit", "red_line_data", "credit_summary", "risk_key_findings"),
        "qcc_fields": (
            "dishonest",
            "admin_penalty",
            "business_exception",
            "serious_violation",
            "high_consumption",
            "executed_person",
            "judicial_documents",
            "environmental_penalty",
            "tax_abnormal",
            "tax_arrears",
            "tax_violation",
            "equity_pledge",
            "equity_freeze",
            "chattel_mortgage",
        ),
        "search_groups": ("risk", "gsxt"),
    },
}


def get_parallel_llm_config(cfg: dict[str, Any], stage: str) -> dict[str, Any]:
    base = get_stage_llm_config(cfg, "scoring_llm")
    parallel = cfg.get("parallel_generation", {})
    if isinstance(parallel, dict):
        shared_dimension = parallel.get("dimension_llm", {})
        if stage in DIMENSION_KEYS and isinstance(shared_dimension, dict):
            base.update(shared_dimension)
        stage_cfg = parallel.get(f"{stage}_llm", {})
        if isinstance(stage_cfg, dict):
            base.update(stage_cfg)
    base.setdefault("thinking", "disabled")
    return base


def get_parallel_runtime_config(cfg: dict[str, Any]) -> dict[str, Any]:
    parallel = cfg.get("parallel_generation", {})
    if not isinstance(parallel, dict):
        parallel = {}
    return {
        "max_parallel_workers": int(parallel.get("max_parallel_workers", 4) or 4),
        "dimension_launch_interval_seconds": float(
            parallel.get("dimension_launch_interval_seconds", 3) or 0
        ),
        "max_input_chars": int(parallel.get("max_input_chars", 7000) or 7000),
        "summary_max_input_chars": int(parallel.get("summary_max_input_chars", 8000) or 8000),
    }


def _shorten_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _clip_value(value: Any, max_chars: int = 300, max_items: int = 8) -> Any:
    if isinstance(value, dict):
        return {key: _clip_value(item, max_chars, max_items) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_value(item, max_chars, max_items) for item in value[:max_items]]
    if isinstance(value, str):
        return _shorten_text(value, max_chars)
    return value


def _take_dict_keys(source: Any, keys: tuple[str, ...], max_chars: int = 300) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: _clip_value(source.get(key), max_chars=max_chars) for key in keys if key in source}


def _take_search_groups(search_evidence: Any, groups: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(search_evidence, dict):
        return {}
    selected: dict[str, Any] = {}
    for group in groups:
        value = search_evidence.get(group)
        if not isinstance(value, dict):
            continue
        items = value.get("items", [])
        selected[group] = {
            "stats": _clip_value(value.get("stats", {}), max_chars=160),
            "items": [
                {
                    "title": _shorten_text(item.get("title", ""), 80),
                    "snippet": _shorten_text(
                        item.get("snippet") or item.get("summary") or item.get("content") or "",
                        180,
                    ),
                    "url": _shorten_text(item.get("url", ""), 160),
                }
                for item in items[:8]
                if isinstance(item, dict)
            ],
        }
    return selected


def _bounded_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text = compact_json(payload)
    if len(text) <= max_chars:
        return payload
    trimmed = copy.deepcopy(payload)
    if "search_evidence" in trimmed:
        trimmed["search_evidence"] = _clip_value(trimmed["search_evidence"], max_chars=120, max_items=4)
    if "qcc_data_json" in trimmed:
        trimmed["qcc_data_json"] = _clip_value(trimmed["qcc_data_json"], max_chars=180, max_items=4)
    if len(compact_json(trimmed)) <= max_chars:
        return trimmed
    trimmed["evidence_summary"] = _clip_value(trimmed.get("evidence_summary", {}), max_chars=240, max_items=6)
    return trimmed


def build_dimension_payload(
    evidence_payload: dict[str, Any],
    dimension: str,
    max_input_chars: int = 7000,
) -> dict[str, Any]:
    spec = DIMENSION_SPECS[dimension]
    payload = {
        "dimension": dimension,
        "identity": _clip_value(evidence_payload.get("identity", {}), max_chars=220),
        "evidence_summary": _clip_value(evidence_payload.get("evidence_summary", {}), max_chars=420),
        "collection_diagnostics": _clip_value(
            evidence_payload.get("collection_diagnostics", {}),
            max_chars=220,
            max_items=8,
        ),
        "qcc_data_json": _take_dict_keys(
            evidence_payload.get("qcc_data_json", {}),
            spec["qcc_fields"],
            max_chars=300,
        ),
        "search_evidence": _take_search_groups(
            evidence_payload.get("search_evidence", {}),
            spec["search_groups"],
        ),
    }
    return _bounded_payload(payload, max_input_chars)


def build_summary_payload(
    evidence_payload: dict[str, Any],
    dimension_results: dict[str, dict[str, Any]],
    max_input_chars: int = 8000,
) -> dict[str, Any]:
    payload = {
        "identity": _clip_value(evidence_payload.get("identity", {}), max_chars=260),
        "dimension_results": dimension_results,
        "collection_diagnostics": _clip_value(
            evidence_payload.get("collection_diagnostics", {}),
            max_chars=240,
            max_items=8,
        ),
        "evidence_summary": _clip_value(evidence_payload.get("evidence_summary", {}), max_chars=320),
        "qcc_risk_subject_brief": _take_dict_keys(
            evidence_payload.get("qcc_data_json", {}),
            (
                "registration",
                "shareholder",
                "actual_controller",
                "dishonest",
                "business_exception",
                "serious_violation",
                "high_consumption",
                "financial",
            ),
            max_chars=220,
        ),
    }
    return _bounded_payload(payload, max_input_chars)


def _fallback_dimension_result(dimension: str, error: Exception) -> dict[str, Any]:
    reason = f"维度LLM失败，已按缺失数据兜底: {error}"
    if dimension == "industry":
        ids = (
            "industry_advancement",
            "industry_scale",
            "industry_growth",
            "competition_pattern",
            "green_power_demand",
            "downstream_market",
            "industry_barrier",
        )
        return {
            "industry": {item: {"score": 0, "reliability": "red", "basis": "维度分析失败，需复核"} for item in ids},
            "industry_summary": reason[:120],
            "industry_key_findings": [reason],
        }
    if dimension == "operation":
        ids = ("enterprise_nature", "industry_status", "survival_years", "recent_development", "core_customers")
        return {
            "operation": {item: {"score": 0, "reliability": "red", "basis": "维度分析失败，需复核"} for item in ids},
            "operation_summary": reason[:120],
            "operation_key_findings": [reason],
        }
    if dimension == "finance":
        ids = (
            "registered_capital",
            "staff_count",
            "annual_revenue",
            "asset_scale",
            "net_profit_margin",
            "cash_flow",
            "asset_liability_ratio",
        )
        return {
            "finance": {item: {"score": 1, "reliability": "red", "basis": "财务分析失败，需补充材料"} for item in ids},
            "finance_summary": reason[:120],
            "finance_key_findings": [reason],
            "missing_financial_fields": ["年营收", "净利润率", "现金流", "资产负债率"],
            "financial_assessment_notes": [reason],
        }
    return {
        "credit": {
            "dishonest_status": {"score": 0, "reliability": "red", "basis": "信用分析失败，需复核"},
            "credit_rating": {"score": 1, "reliability": "red", "basis": "信用分析失败，需复核"},
        },
        "red_line_data": {
            "is_dishonest": False,
            "major_penalty_count": 0,
            "has_abnormal_operation": False,
            "legal_person_restricted": False,
        },
        "credit_summary": reason[:120],
        "risk_key_findings": [reason],
    }


def _fallback_summary_result(evidence_payload: dict[str, Any], error: Exception) -> dict[str, Any]:
    identity = evidence_payload.get("identity", {}) if isinstance(evidence_payload, dict) else {}
    enterprise_name = identity.get("enterprise_name") or "未获取"
    credit_code = identity.get("unified_social_credit_code") or "未获取"
    reason = f"汇总LLM失败，已使用模板兜底: {error}"
    return {
        "assessment_goal": "企业综合评估",
        "enterprise_profile": {
            "企业名称": enterprise_name,
            "统一社会信用代码": credit_code,
        },
        "subject_verification": {
            "核验结论": "主体已确认" if identity.get("status") == "confirmed" else "需复核",
            "登记状态": "需结合工商照面复核",
            "经营范围匹配": "需结合合作场景复核",
            "地址一致性": "需人工复核",
        },
        "green_power_fit": {
            "适配等级": "中适配",
            "适配依据": ["基于已采集资料初步判断，仍需补充用电侧资料"],
            "建议合作方式": "绿电交易或绿证合作，待资料补充后确认",
            "需补充资料": ["月度用电量", "峰谷电价", "变压器容量", "厂房或屋顶条件"],
        },
        "performance_capability": {
            "履约判断": "需复核",
            "依据": ["已完成结构化采集和维度评分"],
            "短板": ["部分报告汇总字段由模板兜底"],
        },
        "related_party_risk": {
            "风险结论": "需复核",
            "关注对象": ["控股股东", "实际控制人", "法定代表人", "关联企业"],
            "风险说明": [reason],
        },
        "kyb_risk_overview": {
            "总体结论": "需结合专项风险字段复核",
            "主要风险": [reason],
            "复核建议": ["人工复核启信宝、企查查MCP和公开搜索原始记录"],
        },
        "data_source_summary": {
            "official_or_structured": "已使用启信宝API、企查查MCP补充字段和公开搜索资料。",
            "public_disclosure": "公开披露资料以采集摘要为准。",
            "search_or_media": "搜索结果仅作为辅助证据。",
            "inference": reason,
        },
        "overall_summary": "已完成企业主体确认、结构化数据采集和四个维度的并发评分。由于汇总模型未能完成，本报告采用模板生成综合结论，建议重点复核财务透明度、信用风险、关联方风险和绿电合作落地资料后再进入正式合作判断。",
        "action_recommendation": {
            "conclusion": "谨慎合作",
            "next_action": "进入人工复核",
            "cooperation_advice": "先补充关键资料并小额试单，再决定正式合作额度。",
            "key_risks": [reason],
            "required_materials": ["近两年财报", "纳税证明", "主要客户合同", "月度用电量证明"],
        },
    }


def _run_dimension_llm(
    *,
    evidence_payload: dict[str, Any],
    dimension: str,
    cfg: dict[str, Any],
    ctx: Any = None,
    max_input_chars: int = 7000,
) -> dict[str, Any]:
    spec = DIMENSION_SPECS[dimension]
    payload = build_dimension_payload(evidence_payload, dimension, max_input_chars=max_input_chars)
    result = invoke_stage_json(
        system_prompt=spec["prompt"],
        payload=payload,
        stage_config=get_parallel_llm_config(cfg, dimension),
        ctx=ctx,
    )
    return {key: result[key] for key in spec["result_keys"] if key in result}


def build_summary_json(
    *,
    evidence_payload: dict[str, Any],
    dimension_results: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    ctx: Any = None,
    max_input_chars: int = 8000,
) -> dict[str, Any]:
    payload = build_summary_payload(evidence_payload, dimension_results, max_input_chars=max_input_chars)
    return invoke_stage_json(
        system_prompt=SUMMARY_PROMPT,
        payload=payload,
        stage_config=get_parallel_llm_config(cfg, "summary"),
        ctx=ctx,
    )


def build_staggered_dimension_json(
    *,
    evidence_payload: dict[str, Any],
    cfg: dict[str, Any],
    ctx: Any = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    runtime = get_parallel_runtime_config(cfg)
    max_workers = max(1, min(4, runtime["max_parallel_workers"]))
    launch_interval = max(0.0, runtime["dimension_launch_interval_seconds"])
    max_input_chars = runtime["max_input_chars"]
    timings: dict[str, Any] = {
        "dimension_launch_interval_seconds": launch_interval,
        "max_parallel_workers": max_workers,
        "dimension_order": list(DIMENSION_KEYS),
        "dimension_started": {},
        "dimension_elapsed": {},
    }
    started_at = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    futures = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, dimension in enumerate(DIMENSION_KEYS):
            timings["dimension_started"][dimension] = round(time.monotonic() - started_at, 2)
            logger.info("Starting staggered dimension LLM: %s", dimension)
            futures[
                executor.submit(
                    _run_dimension_llm,
                    evidence_payload=evidence_payload,
                    dimension=dimension,
                    cfg=cfg,
                    ctx=ctx,
                    max_input_chars=max_input_chars,
                )
            ] = (dimension, time.monotonic())
            if index < len(DIMENSION_KEYS) - 1 and launch_interval > 0:
                time.sleep(launch_interval)

        for future in as_completed(futures):
            dimension, dimension_started_at = futures[future]
            try:
                results[dimension] = future.result()
                timings["dimension_elapsed"][dimension] = round(time.monotonic() - dimension_started_at, 2)
            except Exception as exc:
                logger.exception("Dimension LLM failed: %s", dimension)
                results[dimension] = _fallback_dimension_result(dimension, exc)
                timings["dimension_elapsed"][dimension] = -1
                timings.setdefault("dimension_errors", {})[dimension] = str(exc)

    timings["dimension_llm_total_wall_time"] = round(time.monotonic() - started_at, 2)
    return results, timings


def merge_parallel_payload(
    dimension_results: dict[str, dict[str, Any]],
    summary_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_payload: dict[str, Any] = {}
    for dimension in DIMENSION_KEYS:
        part = dimension_results.get(dimension, {})
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if value not in (None, "", [], {}):
                final_payload[key] = copy.deepcopy(value)

    summary = copy.deepcopy(summary_json or {})
    for key, value in summary.items():
        if key in SUMMARY_PROTECTED_KEYS:
            continue
        if value not in (None, "", [], {}):
            final_payload[key] = value

    return final_payload


def build_parallel_scoring_json(
    *,
    evidence_payload: dict[str, Any],
    cfg: dict[str, Any],
    ctx: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    dimension_results, timings = build_staggered_dimension_json(
        evidence_payload=evidence_payload,
        cfg=cfg,
        ctx=ctx,
    )

    summary_started_at = time.monotonic()
    try:
        summary = build_summary_json(
            evidence_payload=evidence_payload,
            dimension_results=dimension_results,
            cfg=cfg,
            ctx=ctx,
            max_input_chars=get_parallel_runtime_config(cfg)["summary_max_input_chars"],
        )
        timings["summary_llm"] = round(time.monotonic() - summary_started_at, 2)
    except Exception as exc:
        logger.exception("Summary LLM failed; falling back to template")
        summary = _fallback_summary_result(evidence_payload, exc)
        timings["summary_llm"] = -1
        timings["summary_error"] = str(exc)

    return merge_parallel_payload(dimension_results, summary), timings
