"""Enterprise report tool using full evidence collection and one LLM call."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from coze_coding_utils.log.write_log import request_context
from langchain.tools import tool

from services.llm_json_pipeline import compact_json, load_llm_config
from services.single_stage_llm_pipeline import build_single_stage_payload, build_single_stage_scoring_json
from tools.enterprise_evidence_tool import collect_enterprise_evidence
from tools.report_tool import generate_enterprise_report
from tools.tool_runtime_helpers import as_json_object, invoke_langchain_tool, stage_timing

logger = logging.getLogger(__name__)

DEFAULT_REPORT_COLLECTION_MODE = "standard"
DEEP_TRIGGER_KEYWORDS = (
    "deep",
    "全量",
    "全部",
    "完整",
    "深度",
    "尽调",
    "核查",
    "复核",
    "授信",
    "投资",
    "并购",
    "ipo",
    "风险排查",
)


def _looks_like_deep_request(user_input: str, collection_mode: str) -> bool:
    normalized_mode = str(collection_mode or "").strip().lower()
    if normalized_mode in {"deep", "full", "kyb"}:
        return True
    lowered = str(user_input or "").strip().lower()
    return any(keyword in lowered for keyword in DEEP_TRIGGER_KEYWORDS)


def _looks_like_risk_signal(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    safe_keywords = ("未查询到", "无相关", "暂无", "没有相关", "无记录", "未发现")
    if any(keyword in text for keyword in safe_keywords):
        return False
    return any(keyword in text for keyword in ("有", "存在", "共", "条", "被执行", "处罚", "异常", "限制高消费", "严重违法"))


def _should_escalate_to_deep(user_input: str, evidence_payload: dict[str, Any]) -> tuple[bool, str]:
    if _looks_like_deep_request(user_input, ""):
        return True, "user_requested_deep"

    diagnostics = evidence_payload.get("collection_diagnostics", {}) or {}
    next_step = str(diagnostics.get("recommended_next_step") or "").strip().lower()
    if next_step == "trigger_deep":
        return True, "diagnostics_trigger_deep"

    qixin_diag = diagnostics.get("qixin", {}) if isinstance(diagnostics, dict) else {}
    if qixin_diag.get("fatal"):
        missing_count = int(diagnostics.get("missing_or_unknown_fields_count") or 0)
        if missing_count >= 4:
            return True, "qixin_fatal_with_field_gaps"

    qcc_data = evidence_payload.get("qcc_data_json", {}) or {}
    risk_fields = (
        "dishonest",
        "admin_penalty",
        "business_exception",
        "serious_violation",
        "high_consumption",
        "executed_person",
        "tax_violation",
        "equity_pledge",
        "equity_freeze",
        "chattel_mortgage",
    )
    if any(_looks_like_risk_signal(qcc_data.get(field)) for field in risk_fields):
        return True, "core_risk_signal_detected"

    return False, ""


@tool
def generate_enterprise_report_single(user_input: str, collection_mode: str = "standard") -> str:
    """单次 LLM 生成企业分析报告。

    内部流程：完整采集证据 -> 一次性 LLM 输出完整 scoring_json
    -> 调用 generate_enterprise_report 生成 PDF。
    """
    started_at = time.monotonic()
    ctx = request_context.get()
    cfg = load_llm_config()
    requested_collection_mode = collection_mode
    effective_collection_mode = "deep" if _looks_like_deep_request(user_input, requested_collection_mode) else DEFAULT_REPORT_COLLECTION_MODE
    timings: dict[str, Any] = {
        "requested_collection_mode": requested_collection_mode,
        "effective_collection_mode": effective_collection_mode,
    }
    escalation_reason = "user_requested_deep" if effective_collection_mode == "deep" and _looks_like_deep_request(user_input, requested_collection_mode) else ""

    try:
        evidence_started = time.monotonic()
        evidence_text = invoke_langchain_tool(
            collect_enterprise_evidence,
            {"user_input": user_input, "collection_mode": effective_collection_mode},
        )
        timings["evidence_collection"] = stage_timing(evidence_started)
        evidence_payload = as_json_object(evidence_text, "collect_enterprise_evidence result")
    except Exception as exc:
        logger.exception("single-stage report evidence collection failed")
        return f"单次报告生成失败：证据采集失败：{exc}"

    identity = evidence_payload.get("identity", {})
    if identity.get("status") != "confirmed":
        return compact_json(evidence_payload)

    if effective_collection_mode != "deep":
        should_escalate, escalation_reason = _should_escalate_to_deep(user_input, evidence_payload)
        if should_escalate:
            logger.info("single-stage report escalates to deep collection: reason=%s", escalation_reason)
            deep_started = time.monotonic()
            deep_evidence_text = invoke_langchain_tool(
                collect_enterprise_evidence,
                {"user_input": user_input, "collection_mode": "deep"},
            )
            timings["evidence_collection_deep"] = stage_timing(deep_started)
            evidence_payload = as_json_object(deep_evidence_text, "collect_enterprise_evidence deep result")
            identity = evidence_payload.get("identity", {})
            if identity.get("status") != "confirmed":
                return compact_json(evidence_payload)
            effective_collection_mode = "deep"
            timings["effective_collection_mode"] = effective_collection_mode

    if escalation_reason:
        timings["deep_escalation_reason"] = escalation_reason

    enterprise_name = identity.get("enterprise_name") or user_input
    qcc_data = evidence_payload.get("qcc_data_json", {})
    collection_diagnostics = evidence_payload.get("collection_diagnostics", {})

    try:
        scoring_started = time.monotonic()
        max_input_chars = int(
            ((cfg.get("single_stage_generation", {}) or {}).get("max_input_chars", 18000)) or 18000
        )
        payload_preview = build_single_stage_payload(evidence_payload, max_input_chars=max_input_chars)
        timings["single_llm_input_chars"] = len(compact_json(payload_preview))
        timings["single_llm_input_sections"] = list(payload_preview.keys())
        final_scoring = build_single_stage_scoring_json(
            evidence_payload=evidence_payload,
            cfg=cfg,
            ctx=ctx,
        )
        timings["single_llm_scoring"] = stage_timing(scoring_started)
    except Exception as exc:
        logger.exception("single-stage LLM scoring failed")
        return f"单次报告生成失败：LLM 未能生成合法 scoring_json：{exc}"

    qcc_data_json = compact_json(qcc_data) if isinstance(qcc_data, dict) else "{}"
    collection_diagnostics_json = (
        compact_json(collection_diagnostics) if isinstance(collection_diagnostics, dict) else "{}"
    )

    report_started = time.monotonic()
    report_result = invoke_langchain_tool(
        generate_enterprise_report,
        {
            "enterprise_name": enterprise_name,
            "scoring_json": compact_json(final_scoring),
            "qcc_data_json": qcc_data_json,
            "collection_diagnostics_json": collection_diagnostics_json,
        },
    )
    timings["pdf_report"] = stage_timing(report_started)
    timings["total"] = stage_timing(started_at)

    # 精简返回：只返回核心结论 + PDF链接 + 耗时诊断
    # 避免返回超长全文导致 Agent 最后一轮 LLM 推理卡住
    report_url = None
    for line in str(report_result).splitlines():
        if line.strip().startswith("📄"):
            # 提取PDF链接
            m = re.search(r'\((https?://[^\)]+)\)', line)
            if m:
                report_url = m.group(1)
            break

    # 提取评级和总分
    rating_match = re.search(r'企业评级：([A-D][\+\-]?)', str(report_result))
    score_match = re.search(r'加权总分[：:]([\d.]+)分', str(report_result))
    rating = rating_match.group(1) if rating_match else "N/A"
    total_score = score_match.group(1) if score_match else "N/A"

    summary_lines = [
        f"✅ 企业分析报告已生成",
        f"- 企业评级：{rating}",
        f"- 加权总分：{total_score}分",
    ]
    if report_url:
        summary_lines.append(f"- 📄 [分析报告PDF]({report_url})")
    summary_lines.append(f"\n耗时诊断：{compact_json(timings)}")

    return "\n".join(summary_lines)
