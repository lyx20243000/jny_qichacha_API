"""Tests for evidence diagnostics and summary helpers."""

import sys
import types
from unittest.mock import MagicMock


for mod_name in (
    "langchain",
    "langchain.tools",
    "services.qixin_openapi_client",
    "services.qcc_mcp_client",
    "tools.enterprise_disambiguate_tool",
    "tools.enterprise_search_tool",
    "tools.qcc_mcp_tool",
):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)


if not hasattr(sys.modules["langchain.tools"], "tool"):
    sys.modules["langchain.tools"].tool = lambda fn=None, **kw: (fn if fn is not None else (lambda f: f))


qixin_mod = sys.modules["services.qixin_openapi_client"]
if not callable(getattr(qixin_mod, "is_qixin_unavailable", None)):
    qixin_mod.is_qixin_unavailable = lambda v: isinstance(v, dict) and v.get("ok") is False
if not callable(getattr(qixin_mod, "get_qixin_error_info", None)):
    qixin_mod.get_qixin_error_info = lambda v: v if isinstance(v, dict) and v.get("ok") is False else {}
if not callable(getattr(qixin_mod, "query_qixin_by_search_key", None)):
    qixin_mod.query_qixin_by_search_key = MagicMock(return_value="")


qcc_mod = sys.modules["services.qcc_mcp_client"]
for fn_name in (
    "is_qcc_mcp_available",
    "query_company_registration",
    "query_shareholder_info",
    "query_actual_controller",
    "query_external_investments",
    "query_financial_data",
    "query_key_personnel",
    "query_listing_info",
    "query_company_profile",
    "query_dishonest_info",
    "query_administrative_penalty",
    "query_business_exception",
    "query_serious_violation",
    "query_high_consumption_restriction",
    "query_company_risk_scan",
    "query_case_filing_info",
    "query_executed_person",
    "query_judicial_documents",
    "query_court_announcement",
    "query_final_case",
    "query_environmental_penalty",
    "query_tax_abnormal",
    "query_tax_arrears",
    "query_tax_violation",
    "query_equity_pledge",
    "query_equity_freeze",
    "query_chattel_mortgage",
    "query_land_mortgage",
    "query_history_dishonest",
    "query_history_executed_person",
    "query_history_business_exception",
    "query_history_administrative_penalty",
    "query_patent_info",
    "query_trademark_info",
    "query_software_copyright",
    "query_bidding_info",
    "query_credit_evaluation",
    "query_qualifications",
    "query_honor_info",
    "query_news_sentiment",
    "query_recruitment_info",
    "query_administrative_license",
    "query_taxpayer_qualification",
    "query_product_check",
    "query_state_owned_land_transfer",
):
    if not callable(getattr(qcc_mod, fn_name, None)):
        setattr(qcc_mod, fn_name, MagicMock(return_value=""))


tools_disambig_mod = sys.modules["tools.enterprise_disambiguate_tool"]
for fn_name in (
    "_confirm_target_by_mcp",
    "_confirm_target_by_openapi",
    "_do_candidate_search",
    "_extract_company_names",
    "_ensure_candidate_identity_fields",
    "_find_exact_credit_code_match",
    "_find_exact_normalized_match",
    "_format_candidate_result",
    "_search_credit_code_fallback_candidates",
    "_is_probably_short_name",
    "_is_social_credit_code",
    "_normalize_social_credit_code",
    "_strip_admin_prefix",
):
    if not callable(getattr(tools_disambig_mod, fn_name, None)):
        setattr(tools_disambig_mod, fn_name, MagicMock(return_value=("", {})))


tools_search_mod = sys.modules["tools.enterprise_search_tool"]
if not callable(getattr(tools_search_mod, "_do_web_search", None)):
    tools_search_mod._do_web_search = MagicMock(return_value="")
if not callable(getattr(tools_search_mod, "_do_web_search_structured", None)):
    tools_search_mod._do_web_search_structured = MagicMock(return_value={})


tools_qcc_mod = sys.modules["tools.qcc_mcp_tool"]
if not callable(getattr(tools_qcc_mod, "_safe_call", None)):
    tools_qcc_mod._safe_call = MagicMock(return_value="")


sys.path.insert(0, "src")


from tools.enterprise_evidence_tool import (  # noqa: E402
    _build_collection_diagnostics,
    _build_evidence_summary,
    _build_qcc_data_json,
)


def _sample_search_evidence():
    return {
        "basic": {
            "items": [
                {
                    "title": "企业官网",
                    "site_name": "企业官网",
                    "url": "https://example.com",
                    "summary": "公司介绍",
                    "snippet": "官网介绍",
                    "publish_time": "2026-01-05",
                }
            ],
            "stats": {"result_count": 1, "official_hits": 0, "high_auth_hits": 0, "content_hits": 0},
        },
        "development": {
            "items": [
                {
                    "title": "扩产项目",
                    "site_name": "gov.cn",
                    "url": "https://gov.cn/project",
                    "summary": "新增产线",
                    "snippet": "项目建设",
                    "publish_time": "2026-01-01",
                }
            ],
            "stats": {"result_count": 1, "official_hits": 1, "high_auth_hits": 1, "content_hits": 0},
        },
        "industry": {
            "items": [
                {
                    "title": "行业报告",
                    "site_name": "stats.gov.cn",
                    "url": "https://stats.gov.cn/report",
                    "summary": "行业增长",
                    "snippet": "规模上升",
                    "publish_time": "2026-01-02",
                }
            ],
            "stats": {"result_count": 1, "official_hits": 1, "high_auth_hits": 1, "content_hits": 0},
        },
        "risk": {
            "items": [
                {
                    "title": "风险公告",
                    "site_name": "court.gov.cn",
                    "url": "https://court.gov.cn/case",
                    "summary": "涉诉线索",
                    "snippet": "公开信息",
                    "publish_time": "2026-01-03",
                }
            ],
            "stats": {"result_count": 1, "official_hits": 1, "high_auth_hits": 1, "content_hits": 0},
        },
        "finance": {
            "items": [
                {
                    "title": "项目投资",
                    "site_name": "gov.cn",
                    "url": "https://gov.cn/investment",
                    "summary": "总投资2亿元",
                    "snippet": "项目公示",
                    "publish_time": "2026-01-06",
                }
            ],
            "stats": {"result_count": 1, "official_hits": 1, "high_auth_hits": 1, "content_hits": 0},
        },
        "gsxt": {
            "items": [
                {
                    "title": "工商公示",
                    "site_name": "国家企业信用信息公示系统",
                    "url": "https://www.gsxt.gov.cn",
                    "summary": "登记状态正常",
                    "snippet": "工商公示",
                    "publish_time": "2026-01-04",
                }
            ],
            "stats": {"result_count": 1, "official_hits": 1, "high_auth_hits": 1, "content_hits": 0},
        },
    }


def test_qcc_data_json_has_field_sources_and_source_conflicts():
    qcc_mcp = {
        "basic": {"工商登记": "企查查工商登记"},
        "risk": {},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
    }
    qixin_api = {
        "工商照面(API 1.41)": "启信宝工商登记",
        "失信被执行(API 5.5)": "启信宝失信数据",
    }
    result = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    assert isinstance(result["field_sources"], dict)
    assert isinstance(result["source_conflicts"], list)


def test_source_conflict_detected_when_sources_differ():
    qcc_mcp = {
        "basic": {},
        "risk": {"失信被执行人": "MCP失信记录A"},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
    }
    qixin_api = {"失信被执行(API 5.5)": "启信宝失信记录B内容完全不同"}
    result = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    dishonest_conflicts = [c for c in result["source_conflicts"] if c["field"] == "dishonest"]
    assert len(dishonest_conflicts) == 1
    source_names = {s["source"] for s in dishonest_conflicts[0]["sources"]}
    assert source_names == {"qixin_api_5_5", "qcc_mcp_risk.dishonest"}


def test_no_conflict_when_sources_same():
    qcc_mcp = {
        "basic": {},
        "risk": {"失信被执行人": "无失信记录"},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
    }
    qixin_api = {"失信被执行(API 5.5)": "无失信记录"}
    result = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    dishonest_conflicts = [c for c in result["source_conflicts"] if c["field"] == "dishonest"]
    assert dishonest_conflicts == []


def test_field_sources_records_winning_source():
    qcc_mcp = {
        "basic": {"工商登记": "MCP工商数据", "股东结构": "MCP股东数据"},
        "risk": {},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
    }
    qixin_api = {"工商照面(API 1.41)": "启信宝工商数据"}
    result = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    assert result["field_sources"]["registration"] == "qixin_api_1_41"
    assert result["field_sources"]["shareholder"] == "qcc_mcp_basic.shareholder"


def test_diagnostics_basic_structure():
    qixin_api = {
        "_meta": {"stages_completed": ["p0", "p1"], "stopped_early": False},
        "工商照面(API 1.41)": "有效数据",
        "失信被执行(API 5.5)": "无失信",
        "_collection_note": "standard mode",
    }
    qcc_mcp = {
        "basic": {"工商登记": "MCP数据"},
        "risk": {},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
        "_collection_note": "standard mode skips default MCP calls",
    }
    qcc_data_json = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    diag = _build_collection_diagnostics(
        qixin_api,
        qcc_mcp,
        {},
        _sample_search_evidence(),
        qcc_data_json,
        "standard",
    )
    assert diag["collection_mode"] == "standard"
    assert diag["qixin"]["stages"] == ["p0", "p1"]
    assert diag["qixin"]["fatal"] is False
    assert diag["qixin"]["stopped_early"] is False
    assert diag["search"]["gsxt_hit"] is True
    assert diag["search"]["high_auth_hits"] >= 1
    assert isinstance(diag["field_source_summary"], dict)
    assert isinstance(diag["module_completeness"], dict)
    assert isinstance(diag["missing_or_unknown_fields_count"], int)
    assert isinstance(diag["needs_human_review"], bool)


def test_diagnostics_qixin_fatal_error():
    qixin_api = {
        "_meta": {"stages_completed": ["p0"], "stopped_early": True},
        "_fatal_error": {"ok": False, "error_code": "BALANCE_NOT_ENOUGH", "error_msg": "积分余额不足"},
        "工商照面(API 1.41)": {"ok": False, "error_code": "BALANCE_NOT_ENOUGH"},
    }
    qcc_mcp = {
        "basic": {},
        "risk": {},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
        "_collection_note": "MCP skipped",
    }
    qcc_data_json = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    diag = _build_collection_diagnostics(qixin_api, qcc_mcp, {}, {}, qcc_data_json, "standard")
    assert diag["qixin"]["fatal"] is True
    assert diag["qixin"]["stopped_early"] is True
    assert "启信宝熔断" in diag["review_reasons"]
    assert diag["needs_human_review"] is True


def test_diagnostics_mcp_seed_triggered():
    qixin_api = {
        "_meta": {"stages_completed": ["p0"], "stopped_early": True},
        "_fatal_error": {"ok": False, "error_msg": "余额不足"},
    }
    qcc_mcp = {
        "basic": {"工商登记": "MCP数据"},
        "risk": {"失信被执行人": "MCP失信"},
        "finance": {},
        "operation": {},
        "ip": {},
        "news": {},
        "extended_risk": {},
        "_collection_note": "standard mode promoted MCP seed collection because Qixin was unavailable",
    }
    qcc_data_json = _build_qcc_data_json(qcc_mcp, qixin_api=qixin_api)
    diag = _build_collection_diagnostics(qixin_api, qcc_mcp, {}, {}, qcc_data_json, "standard")
    assert diag["qcc_mcp"]["seed_triggered"] is True
    assert "basic" in diag["qcc_mcp"]["collected_groups"]
    assert "risk" in diag["qcc_mcp"]["collected_groups"]


def test_evidence_summary_uses_structured_search_and_conflict_flags():
    identity = {
        "enterprise_name": "测试企业",
        "unified_social_credit_code": "913100000000000000",
        "match_source": "qixin",
        "match_reason": "工商照面确认",
        "confidence": "high",
    }
    qixin_api = {
        "工商照面(API 1.41)": "启信工商信息",
        "经营异常(API 1.55)": "无经营异常",
        "失信被执行(API 5.5)": "启信失信记录A",
        "_meta": {"stages_completed": ["p0"]},
    }
    qcc_mcp = {
        "basic": {"股东结构": "MCP股东结构", "实际控制人": "MCP实控人", "工商登记": "MCP工商登记"},
        "risk": {"失信被执行人": "MCP失信记录B"},
        "finance": {"财务数据": "MCP财务摘要"},
        "operation": {"资质认证": "高新技术企业", "招聘信息": "正在招聘"},
        "ip": {},
        "news": {},
        "extended_risk": {},
    }
    triggered_mcp = {"_meta": {"triggered_sections": ["经营补充"]}}

    summary = _build_evidence_summary(
        identity,
        qixin_api,
        _sample_search_evidence(),
        qcc_mcp,
        triggered_mcp,
        "standard",
    )
    assert summary["subject_profile"]["enterprise_name"] == "测试企业"
    assert summary["official_search_summary"]["gsxt_hits"][0]["title"] == "工商公示"
    assert summary["operation_signal_summary"]["development_signals"][0]["title"] == "扩产项目"
    assert summary["finance_signal_summary"]["public_finance_signals"][0]["title"] == "项目投资"
    conflict_fields = {item["field"] for item in summary["conflict_flags"]}
    assert "dishonest" in conflict_fields
