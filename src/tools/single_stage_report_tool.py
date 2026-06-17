"""Enterprise report tool using full evidence collection and one LLM call."""

from __future__ import annotations

import logging
import time
from typing import Any

from coze_coding_utils.log.write_log import request_context
from langchain.tools import tool

from services.llm_json_pipeline import compact_json, load_llm_config
from services.single_stage_llm_pipeline import build_single_stage_scoring_json
from tools.enterprise_evidence_tool import collect_enterprise_evidence
from tools.report_tool import generate_enterprise_report
from tools.tool_runtime_helpers import as_json_object, invoke_langchain_tool, stage_timing

logger = logging.getLogger(__name__)

FULL_REPORT_COLLECTION_MODE = "deep"


@tool
def generate_enterprise_report_single(user_input: str, collection_mode: str = "deep") -> str:
    """单次 LLM 生成企业分析报告。

    内部流程：完整采集证据 -> 一次性 LLM 输出完整 scoring_json
    -> 调用 generate_enterprise_report 生成 PDF。
    """
    started_at = time.monotonic()
    ctx = request_context.get()
    cfg = load_llm_config()
    requested_collection_mode = collection_mode
    effective_collection_mode = FULL_REPORT_COLLECTION_MODE
    timings: dict[str, Any] = {
        "requested_collection_mode": requested_collection_mode,
        "effective_collection_mode": effective_collection_mode,
    }

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

    enterprise_name = identity.get("enterprise_name") or user_input
    qcc_data = evidence_payload.get("qcc_data_json", {})
    collection_diagnostics = evidence_payload.get("collection_diagnostics", {})

    try:
        scoring_started = time.monotonic()
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
            import re
            m = re.search(r'\((https?://[^\)]+)\)', line)
            if m:
                report_url = m.group(1)
            break

    # 提取评级和总分
    import re
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
