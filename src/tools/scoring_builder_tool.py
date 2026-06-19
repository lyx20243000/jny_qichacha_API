"""Dedicated enterprise scoring builder tool.

This stage mirrors the fixed enterprise flow:
collect evidence -> build scoring_json -> generate report.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from coze_coding_utils.log.write_log import request_context
from langchain.tools import tool

from services.llm_json_pipeline import compact_json, load_llm_config
from services.single_stage_llm_pipeline import (
    build_single_stage_payload,
    build_single_stage_scoring_json,
)

logger = logging.getLogger(__name__)


def _ensure_evidence_payload(evidence_json: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(evidence_json, dict):
        return evidence_json
    if not isinstance(evidence_json, str) or not evidence_json.strip():
        raise ValueError("evidence_json is empty")
    payload = json.loads(evidence_json)
    if not isinstance(payload, dict):
        raise ValueError("evidence_json is not a JSON object")
    return payload


@tool
def build_enterprise_scoring_json(user_input: str, evidence_json: str) -> str:
    """Build the final scoring_json from fixed collected evidence."""
    _ = user_input
    cfg = load_llm_config()
    ctx = request_context.get()

    try:
        evidence_payload = _ensure_evidence_payload(evidence_json)
    except Exception as exc:
        logger.exception("build_enterprise_scoring_json evidence parse failed")
        return compact_json({"error": f"evidence_json parse failed: {exc}"})

    identity = evidence_payload.get("identity", {})
    if not isinstance(identity, dict) or identity.get("status") != "confirmed":
        return compact_json({"error": "enterprise identity is not confirmed"})

    try:
        llm_payload = build_single_stage_payload(evidence_payload)
        scoring_json = build_single_stage_scoring_json(
            evidence_payload=evidence_payload,
            cfg=cfg,
            ctx=ctx,
            payload=llm_payload,
        )
        return compact_json(scoring_json)
    except Exception as exc:
        logger.exception("build_enterprise_scoring_json failed")
        return compact_json({"error": f"scoring build failed: {exc}"})
