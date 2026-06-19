"""Backward-compatible wrapper for the fixed enterprise analysis flow."""

from __future__ import annotations

import logging
import time

from langchain.tools import tool

from services.enterprise_analysis_runner import run_enterprise_analysis_sync

logger = logging.getLogger(__name__)


@tool
def generate_enterprise_report_single(user_input: str, collection_mode: str = "standard") -> str:
    """Backward-compatible alias of the fixed enterprise analysis flow."""
    started_at = time.monotonic()
    try:
        return run_enterprise_analysis_sync(user_input=user_input, collection_mode=collection_mode)
    finally:
        logger.info(
            "generate_enterprise_report_single delegated to fixed runner, elapsed=%.2fs",
            time.monotonic() - started_at,
        )
