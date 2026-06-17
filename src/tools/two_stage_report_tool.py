"""Two-stage enterprise report tool.

This tool orchestrates evidence collection, compact scoring, report enrichment,
payload merge, and the existing PDF report generation tool.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from langchain.tools import tool
from coze_coding_utils.log.write_log import request_context

from services.two_stage_llm_pipeline import (
    build_report_enrichment_json,
    build_scoring_core_json,
    compact_json,
    load_llm_config,
    merge_scoring_payload,
)
from tools.enterprise_evidence_tool import collect_enterprise_evidence
from tools.report_tool import generate_enterprise_report

logger = logging.getLogger(__name__)


def _invoke_langchain_tool(tool_obj: Any, kwargs: dict[str, Any]) -> Any:
    if hasattr(tool_obj, "func") and callable(tool_obj.func):
        return tool_obj.func(**kwargs)
    if hasattr(tool_obj, "invoke") and callable(tool_obj.invoke):
        return tool_obj.invoke(kwargs)
    if callable(tool_obj):
        return tool_obj(**kwargs)
    raise TypeError(f"Unsupported tool object: {tool_obj!r}")


def _as_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} must be a JSON object")


def _stage_timing(started_at: float) -> float:
    return round(time.monotonic() - started_at, 2)


@tool
def generate_enterprise_report_two_stage(user_input: str, collection_mode: str = "standard") -> str:
    """两阶段生成企业分析报告。

    内部流程：固定采集证据 -> 第 1 轮 LLM 输出紧凑评分 JSON -> 第 2 轮 LLM 补全报告表达
    -> 合并为最终 scoring_json -> 调用 generate_enterprise_report 生成 PDF。
    """
    started_at = time.monotonic()
    ctx = request_context.get()
    cfg = load_llm_config()
    timings: dict[str, float] = {}

    try:
        evidence_started = time.monotonic()
        evidence_text = _invoke_langchain_tool(
            collect_enterprise_evidence,
            {"user_input": user_input, "collection_mode": collection_mode},
        )
        timings["evidence_collection"] = _stage_timing(evidence_started)
        evidence_payload = _as_json_object(evidence_text, "collect_enterprise_evidence result")
    except Exception as exc:
        logger.exception("two-stage evidence collection failed")
        return f"两阶段报告生成失败：证据采集失败：{exc}"

    identity = evidence_payload.get("identity", {})
    if identity.get("status") != "confirmed":
        return compact_json(evidence_payload)

    enterprise_name = identity.get("enterprise_name") or user_input
    qcc_data = evidence_payload.get("qcc_data_json", {})
    collection_diagnostics = evidence_payload.get("collection_diagnostics", {})

    try:
        scoring_started = time.monotonic()
        scoring_core = build_scoring_core_json(
            evidence_payload=evidence_payload,
            cfg=cfg,
            ctx=ctx,
        )
        timings["llm_scoring"] = _stage_timing(scoring_started)
    except Exception as exc:
        logger.exception("two-stage scoring LLM failed")
        return f"两阶段报告生成失败：评分轮 LLM 未能生成合法 scoring_core_json：{exc}"

    enrichment = {}
    try:
        report_started = time.monotonic()
        enrichment = build_report_enrichment_json(
            scoring_core_json=scoring_core,
            qcc_data_json=qcc_data if isinstance(qcc_data, dict) else {},
            collection_diagnostics_json=collection_diagnostics if isinstance(collection_diagnostics, dict) else {},
            cfg=cfg,
            ctx=ctx,
        )
        timings["llm_report_enrichment"] = _stage_timing(report_started)
    except Exception as exc:
        timings["llm_report_enrichment"] = -1
        logger.warning("report enrichment failed; falling back to scoring core only: %s", exc)

    final_scoring = merge_scoring_payload(scoring_core, enrichment)
    qcc_data_json = compact_json(qcc_data) if isinstance(qcc_data, dict) else "{}"
    collection_diagnostics_json = compact_json(collection_diagnostics) if isinstance(collection_diagnostics, dict) else "{}"

    report_started = time.monotonic()
    report_result = _invoke_langchain_tool(
        generate_enterprise_report,
        {
            "enterprise_name": enterprise_name,
            "scoring_json": compact_json(final_scoring),
            "qcc_data_json": qcc_data_json,
            "collection_diagnostics_json": collection_diagnostics_json,
        },
    )
    timings["pdf_report"] = _stage_timing(report_started)
    timings["total"] = _stage_timing(started_at)

    return f"{report_result}\n\n两阶段耗时诊断：{compact_json(timings)}"

