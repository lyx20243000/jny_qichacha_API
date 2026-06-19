"""Single total-entry tool for the fixed enterprise analysis flow."""

from __future__ import annotations

from langchain.tools import tool

from services.enterprise_analysis_runner import run_enterprise_analysis_sync


@tool
def analyze_enterprise_report(user_input: str = "", collection_mode: str = "") -> str:
    """Run the complete enterprise analysis flow in one tool call."""
    if not user_input or not str(user_input).strip():
        return "请提供企业名称或统一社会信用代码。"

    return run_enterprise_analysis_sync(
        user_input=str(user_input).strip(),
        collection_mode=str(collection_mode or "").strip().lower(),
    )
