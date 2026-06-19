"""Fixed enterprise analysis orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from tools.enterprise_evidence_tool import collect_enterprise_evidence
from tools.report_tool import generate_enterprise_report
from tools.scoring_builder_tool import build_enterprise_scoring_json
from tools.tool_runtime_helpers import invoke_langchain_tool

logger = logging.getLogger(__name__)

ANALYSIS_MODE_KEY = "analysis_mode"
USCC_PATTERN = re.compile(r"^[0-9A-Z]{18}$")
ENTERPRISE_HINT_PATTERN = re.compile(
    r"(公司|企业|集团|有限责任公司|有限公司|股份有限公司|统一社会信用代码|信用代码|工商|启信宝|企查查)"
)


def extract_user_input(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""

    for key in ("user_input", "enterprise_name", "input", "text", "query"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, (list, tuple)) and len(message) >= 2:
                role, content = message[0], message[1]
                if str(role).lower() in {"user", "human"} and isinstance(content, str) and content.strip():
                    return content.strip()

            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "").lower()
            if role not in {"user", "human"}:
                continue

            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("text"):
                        parts.append(str(item["text"]))
                text = "\n".join(parts).strip()
                if text:
                    return text

    return ""


def should_use_fixed_enterprise_runner(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False

    mode = str(payload.get(ANALYSIS_MODE_KEY) or "").strip().lower()
    if mode in {"agent", "langchain", "legacy"}:
        return False
    if mode in {"enterprise", "fixed", "report"}:
        return True

    for key in ("enterprise_name", "user_input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True

    text = extract_user_input(payload)
    if not text:
        return False

    normalized = re.sub(r"[\s\-]", "", text).upper()
    if USCC_PATTERN.fullmatch(normalized):
        return True
    if len(text.strip()) <= 80 and ENTERPRISE_HINT_PATTERN.search(text):
        return True
    return False


def _ai_message(content: str) -> dict[str, str]:
    return {"type": "ai", "role": "assistant", "content": content}


def _extract_confirmed_enterprise_name(identity: dict[str, Any], fallback: str) -> str:
    return str(
        identity.get("enterprise_name")
        or identity.get("企业名称")
        or identity.get("name")
        or fallback
    ).strip()


def _extract_candidate_credit_code(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("unified_social_credit_code")
        or candidate.get("统一社会信用代码")
        or candidate.get("credit_code")
        or candidate.get("creditCode")
        or ""
    ).strip()


def _extract_candidate_region(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("region")
        or candidate.get("province")
        or candidate.get("地区")
        or ""
    ).strip()


def _extract_candidate_status(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("status")
        or candidate.get("登记状态")
        or ""
    ).strip()


def _format_identity_confirmation(identity: dict[str, Any]) -> str:
    message = identity.get("message") or "存在多个相似企业，请确认要分析哪一家。"
    candidates = identity.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        return str(message)

    lines = ["为您找到以下匹配企业，请确认要分析哪一家："]
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        name = (
            candidate.get("enterprise_name")
            or candidate.get("企业名称")
            or candidate.get("name")
            or "未获取"
        )
        code = _extract_candidate_credit_code(candidate) or "未获取"
        region = _extract_candidate_region(candidate)
        status = _extract_candidate_status(candidate)
        details = " | ".join(str(item) for item in (name, code, region, status) if item)
        lines.append(f"{index}. {details}")
    lines.append("请回复编号、完整企业名称或统一社会信用代码。")
    return "\n".join(lines)


def run_enterprise_analysis_sync(user_input: str, collection_mode: str = "") -> str:
    started_at = time.monotonic()
    logger.info("fixed enterprise analysis start, input=%s", user_input[:80])

    evidence_text = invoke_langchain_tool(
        collect_enterprise_evidence,
        {"user_input": user_input, "collection_mode": collection_mode},
    )
    logger.info(
        "fixed enterprise evidence done, elapsed=%.2fs, length=%s",
        time.monotonic() - started_at,
        len(str(evidence_text)),
    )

    try:
        evidence = json.loads(evidence_text)
    except Exception as exc:
        logger.error("fixed enterprise evidence parse failed: %s", exc, exc_info=True)
        return f"企业信息采集结果解析失败：{exc}"

    identity = evidence.get("identity") or {}
    if not isinstance(identity, dict):
        return "企业主体确认结果异常，请稍后重试。"

    status = identity.get("status")
    if status == "need_user_confirmation":
        return _format_identity_confirmation(identity)
    if status != "confirmed":
        return str(identity.get("message") or "未能确认企业主体，请提供更完整的企业名称或统一社会信用代码。")

    enterprise_name = _extract_confirmed_enterprise_name(identity, user_input)

    scoring_text = invoke_langchain_tool(
        build_enterprise_scoring_json,
        {"user_input": user_input, "evidence_json": evidence_text},
    )
    logger.info(
        "fixed enterprise scoring done, elapsed=%.2fs, length=%s",
        time.monotonic() - started_at,
        len(str(scoring_text)),
    )

    try:
        scoring_data = json.loads(scoring_text)
    except Exception as exc:
        logger.error("fixed enterprise scoring parse failed: %s", exc, exc_info=True)
        return f"企业评分结果解析失败：{exc}"

    if isinstance(scoring_data, dict) and scoring_data.get("error"):
        return f"企业评分构建失败：{scoring_data.get('error')}"

    qcc_data_json = json.dumps(evidence.get("qcc_data_json") or {}, ensure_ascii=False, separators=(",", ":"))
    collection_diagnostics_json = json.dumps(
        evidence.get("collection_diagnostics") or {},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    report_text = invoke_langchain_tool(
        generate_enterprise_report,
        {
            "enterprise_name": enterprise_name,
            "scoring_json": scoring_text,
            "qcc_data_json": qcc_data_json,
            "collection_diagnostics_json": collection_diagnostics_json,
        },
    )
    logger.info(
        "fixed enterprise report done, elapsed=%.2fs, reply_length=%s",
        time.monotonic() - started_at,
        len(str(report_text)),
    )
    return report_text


async def run_enterprise_analysis(payload: dict[str, Any], run_id: str = "") -> dict[str, Any]:
    user_input = extract_user_input(payload)
    if not user_input:
        content = "请提供企业名称或统一社会信用代码。"
        return {"messages": [_ai_message(content)], "output": content, "result": content, "run_id": run_id}

    collection_mode = str(payload.get("collection_mode") or "").strip().lower()
    started_at = time.monotonic()
    try:
        content = await asyncio.to_thread(run_enterprise_analysis_sync, user_input, collection_mode)
        status = "success"
    except Exception as exc:
        logger.error("fixed enterprise analysis failed: %s", exc, exc_info=True)
        content = f"企业分析执行失败：{exc}"
        status = "error"

    return {
        "status": status,
        "run_id": run_id,
        "messages": [_ai_message(content)],
        "output": content,
        "result": content,
        "analysis_mode": "fixed_enterprise_runner",
        "duration_seconds": round(time.monotonic() - started_at, 2),
    }
