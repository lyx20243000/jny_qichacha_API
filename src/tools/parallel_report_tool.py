"""Enterprise report tool using staggered parallel dimension LLM analysis."""

from __future__ import annotations

import logging
import time
from typing import Any

from coze_coding_utils.log.write_log import request_context
from langchain.tools import tool

from services.parallel_dimension_llm_pipeline import build_parallel_scoring_json
from services.two_stage_llm_pipeline import compact_json, load_llm_config
from tools.enterprise_evidence_tool import collect_enterprise_evidence
from tools.report_tool import generate_enterprise_report
from tools.tool_runtime_helpers import as_json_object, invoke_langchain_tool, stage_timing

logger = logging.getLogger(__name__)


@tool
def generate_enterprise_report_parallel(user_input: str, collection_mode: str = "standard") -> str:
    """错峰并发生成企业分析报告。

    内部流程：固定采集证据 -> 每 3 秒错峰启动一个维度 LLM -> 等全部维度完成
    -> 汇总 LLM -> 调用 generate_enterprise_report 生成 PDF。
    """
    started_at = time.monotonic()
    ctx = request_context.get()
    cfg = load_llm_config()
    timings: dict[str, Any] = {}

    try:
        evidence_started = time.monotonic()
        evidence_text = invoke_langchain_tool(
            collect_enterprise_evidence,
            {"user_input": user_input, "collection_mode": collection_mode},
        )
        timings["evidence_collection"] = stage_timing(evidence_started)
        evidence_payload = as_json_object(evidence_text, "collect_enterprise_evidence result")
    except Exception as exc:
        logger.exception("parallel report evidence collection failed")
        return f"并发维度报告生成失败：证据采集失败：{exc}"

    identity = evidence_payload.get("identity", {})
    if identity.get("status") != "confirmed":
        return compact_json(evidence_payload)

    enterprise_name = identity.get("enterprise_name") or user_input
    qcc_data = evidence_payload.get("qcc_data_json", {})
    collection_diagnostics = evidence_payload.get("collection_diagnostics", {})

    try:
        scoring_started = time.monotonic()
        final_scoring, parallel_timings = build_parallel_scoring_json(
            evidence_payload=evidence_payload,
            cfg=cfg,
            ctx=ctx,
        )
        timings["parallel_dimension_analysis"] = stage_timing(scoring_started)
        timings.update(parallel_timings)
    except Exception as exc:
        logger.exception("parallel dimension LLM pipeline failed")
        return f"并发维度报告生成失败：维度分析未能完成：{exc}"

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

    return f"{report_result}\n\n并发维度耗时诊断：{compact_json(timings)}"
