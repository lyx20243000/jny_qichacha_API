"""固定企业证据采集工具。

先用启信宝 API 确认企业主体，未命中时回退到企查查 MCP，再按采集模式采集公开搜索、启信宝白名单接口和必要的企查查 MCP 补缺数据，
让 AI 只负责解读、评分和生成报告。
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import Any
from langchain.tools import tool

logger = logging.getLogger(__name__)

from tools.enterprise_disambiguate_tool import (
    _confirm_target_by_mcp,
    _confirm_target_by_openapi,
    _do_candidate_search,
    _extract_company_names,
    _ensure_candidate_identity_fields,
    _find_exact_credit_code_match,
    _find_exact_normalized_match,
    _format_candidate_result,
    _search_credit_code_fallback_candidates,
    _is_probably_short_name,
    _is_social_credit_code,
    _normalize_social_credit_code,
    _strip_admin_prefix,
)
from tools.enterprise_search_tool import _do_web_search, _do_web_search_structured
from tools.qcc_mcp_tool import _safe_call
from services.qixin_openapi_client import (
    get_qixin_error_info,
    is_qixin_unavailable,
    query_qixin_by_search_key,
)
try:
    from services.qixin_openapi_client import parse_qixin_result
except ImportError:
    def parse_qixin_result(raw_result) -> dict[str, Any]:
        if isinstance(raw_result, dict):
            return raw_result
        if not isinstance(raw_result, str):
            return {}
        text = raw_result.strip()
        if not text or text[0] not in "[{":
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
from services.qcc_mcp_client import (
    is_qcc_mcp_available,
    query_company_registration,
    query_shareholder_info,
    query_actual_controller,
    query_external_investments,
    query_financial_data,
    query_key_personnel,
    query_listing_info,
    query_company_profile,
    query_dishonest_info,
    query_administrative_penalty,
    query_business_exception,
    query_serious_violation,
    query_high_consumption_restriction,
    query_company_risk_scan,
    query_case_filing_info,
    query_executed_person,
    query_judicial_documents,
    query_court_announcement,
    query_final_case,
    query_environmental_penalty,
    query_tax_abnormal,
    query_tax_arrears,
    query_tax_violation,
    query_equity_pledge,
    query_equity_freeze,
    query_chattel_mortgage,
    query_land_mortgage,
    query_history_dishonest,
    query_history_executed_person,
    query_history_business_exception,
    query_history_administrative_penalty,
    query_patent_info,
    query_trademark_info,
    query_software_copyright,
    query_bidding_info,
    query_credit_evaluation,
    query_qualifications,
    query_honor_info,
    query_news_sentiment,
    query_recruitment_info,
    query_administrative_license,
    query_taxpayer_qualification,
    query_product_check,
    query_state_owned_land_transfer,
)

try:
    EVIDENCE_FIELD_MAX_CHARS = int(os.getenv("EVIDENCE_FIELD_MAX_CHARS", "2500"))
except ValueError:
    EVIDENCE_FIELD_MAX_CHARS = 2500
try:
    EVIDENCE_ITEM_TIMEOUT_SECONDS = int(os.getenv("EVIDENCE_ITEM_TIMEOUT_SECONDS", "12"))
except ValueError:
    EVIDENCE_ITEM_TIMEOUT_SECONDS = 12
try:
    EVIDENCE_GROUP_TIMEOUT_SECONDS = int(os.getenv("EVIDENCE_GROUP_TIMEOUT_SECONDS", "35"))
except ValueError:
    EVIDENCE_GROUP_TIMEOUT_SECONDS = 35
try:
    QIXIN_API_CHECK_TIMEOUT_SECONDS = int(os.getenv("QIXIN_API_CHECK_TIMEOUT_SECONDS", "10"))
except ValueError:
    QIXIN_API_CHECK_TIMEOUT_SECONDS = 10

COLLECTION_MODES = {"quick", "standard", "deep"}
DEFAULT_COLLECTION_MODE = os.getenv("ENTERPRISE_COLLECTION_MODE", "standard").strip().lower()
if DEFAULT_COLLECTION_MODE not in COLLECTION_MODES:
    DEFAULT_COLLECTION_MODE = "standard"


def _identity_result(status: str, **kwargs) -> dict:
    result = {"status": status}
    result.update(kwargs)
    return result


def _debug_text(value, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _confirmed_identity(source: str, target: dict, match_reason: str) -> dict:
    enterprise_name = target.get("企业名称", "")
    credit_code = target.get("统一社会信用代码", "")
    if not _is_social_credit_code(credit_code):
        credit_code = ""
    return _identity_result(
        "confirmed",
        enterprise_name=enterprise_name,
        unified_social_credit_code=credit_code,
        mcp_search_key=credit_code or enterprise_name,
        confidence="high" if credit_code else "medium",
        match_source=source,
        match_reason=match_reason,
        enterprise_profile=target,
        candidates=[],
    )


def _confirm_target_with_fallback(search_key: str) -> tuple[str, dict]:
    """主体确认优先启信宝，未命中时回退到企查查 MCP。"""
    logger.info("Subject confirmation: search_key=%s, trying Qixin API 1.41 first", search_key[:20])
    source, target = _confirm_target_by_openapi(search_key)
    if target:
        logger.info("Subject confirmed by Qixin API: name=%s", target.get("企业名称", "")[:30])
        return source, target
    logger.info("Qixin API 1.41 not matched, falling back to QCC MCP: search_key=%s", search_key[:20])
    source, target = _confirm_target_by_mcp(search_key)
    if target:
        logger.info("Subject confirmed by QCC MCP: name=%s", target.get("企业名称", "")[:30])
        return source, target
    logger.warning("Subject not confirmed by any channel: search_key=%s", search_key[:20])
    return "", {}


def _confirm_enterprise_identity(user_input: str) -> dict:
    """固定主体确认：启信宝 API 1.41 优先，未命中时回退到企查查 MCP。"""
    normalized_code = _normalize_social_credit_code(user_input)
    if _is_social_credit_code(user_input):
        source, target = _confirm_target_with_fallback(normalized_code)
        if target:
            if not _is_social_credit_code(target.get("统一社会信用代码", "")):
                target["统一社会信用代码"] = normalized_code
            if source == "mcp":
                return _confirmed_identity(source, target, "统一社会信用代码经企查查 MCP 在启信宝 API 1.41 未命中后确认")
            return _confirmed_identity(source, target, "统一社会信用代码经启信宝 API 1.41 工商照面优先确认")

        debug_trace = [
            {
                "channel": "qixin_api_1_41",
                "matched": False,
                "source": "qixin",
                "skipped": False,
                "reason": "用户输入为统一社会信用代码，已优先调用启信宝 API 1.41，但未确认主体。",
                "enterprise_name": "",
                "credit_code": normalized_code,
            },
            {
                "channel": "qcc_mcp_registration",
                "matched": False,
                "source": "mcp",
                "skipped": not is_qcc_mcp_available(),
                "reason": "启信宝 API 1.41 未命中后尝试企查查 MCP 主体确认；若未命中或不可用，再继续使用 Coze/公开搜索兜底。",
                "enterprise_name": "",
                "credit_code": normalized_code,
            }
        ]
        search_query = f'"{normalized_code}" 统一社会信用代码 企业名称 工商信息'
        search_result = _do_candidate_search(search_query, count=10)
        candidates = _ensure_candidate_identity_fields(
            _extract_company_names(search_result, normalized_code),
            search_result,
        )
        if not candidates:
            search_result, candidates = _search_credit_code_fallback_candidates(normalized_code, search_result)
        debug_trace.append(
            {
                "channel": "coze_public_search",
                "matched": bool(candidates),
                "candidate_count": len(candidates),
                "candidate_names": [item.get("企业名称", "") for item in candidates[:5]],
                "search_preview": _debug_text(search_result),
            }
        )
        exact_match = _find_exact_credit_code_match(candidates, normalized_code)
        if exact_match:
            if not _is_social_credit_code(exact_match.get("统一社会信用代码", "")):
                exact_match["统一社会信用代码"] = normalized_code
            return _confirmed_identity("coze_public_search", exact_match, "统一社会信用代码经公开搜索候选唯一匹配")
        if len(candidates) == 1:
            if not _is_social_credit_code(candidates[0].get("统一社会信用代码", "")):
                candidates[0]["统一社会信用代码"] = normalized_code
            return _confirmed_identity("coze_public_search", candidates[0], "统一社会信用代码仅搜索到一个候选")

        return _identity_result(
            "not_found",
            input=user_input,
            message="统一社会信用代码未能确认对应企业。该结果表示启信宝 API 1.41、企查查 MCP 和公开搜索均未返回可解析主体；请查看 debug_trace 判断各通道是否缺失。",
            candidates=[],
            debug_trace=debug_trace,
        )

    if not _is_probably_short_name(user_input):
        source, target = _confirm_target_with_fallback(user_input)
        if target:
            if source == "mcp":
                return _confirmed_identity(source, target, "企业名称经企查查 MCP 在启信宝 API 1.41 未命中后确认")
            return _confirmed_identity(source, target, "企业名称经启信宝 API 1.41 工商照面优先确认")

        stripped_input = _strip_admin_prefix(user_input)
        if stripped_input and stripped_input != user_input:
            source, target = _confirm_target_with_fallback(stripped_input)
            if target:
                if source == "mcp":
                    return _confirmed_identity(source, target, "企业名称去省份/地区前缀后经企查查 MCP 在启信宝 API 1.41 未命中后确认")
                return _confirmed_identity(source, target, "企业名称去省份/地区前缀后经启信宝 API 1.41 工商照面优先确认")

    search_query = f'"{user_input}" 公司 工商信息 企业名称'
    search_result = _do_candidate_search(search_query, count=10)
    candidates = _ensure_candidate_identity_fields(
        _extract_company_names(search_result, user_input),
        search_result,
    )

    exact_match = _find_exact_normalized_match(candidates, user_input)
    if exact_match:
        return _confirmed_identity("coze_public_search", exact_match, "企业名称去省份/地区前缀后唯一完全匹配")
    if len(candidates) == 1:
        return _confirmed_identity("coze_public_search", candidates[0], "公开搜索仅提取到一个候选企业")

    stripped_input = _strip_admin_prefix(user_input)
    if stripped_input and stripped_input != user_input:
        stripped_search_query = f'"{stripped_input}" 公司 工商信息 企业名称'
        stripped_search_result = _do_candidate_search(stripped_search_query, count=10)
        stripped_candidates = _ensure_candidate_identity_fields(
            _extract_company_names(stripped_search_result, stripped_input),
            stripped_search_result,
        )
        stripped_exact_match = _find_exact_normalized_match(stripped_candidates, stripped_input)
        if stripped_exact_match:
            return _confirmed_identity("coze_public_search", stripped_exact_match, "企业名称去省份/地区前缀后唯一完全匹配")
        if len(stripped_candidates) == 1:
            return _confirmed_identity("coze_public_search", stripped_candidates[0], "去省份/地区前缀后公开搜索仅提取到一个候选企业")

    return _identity_result(
        "need_user_confirmation",
        input=user_input,
        message="存在多个相似企业或无法唯一确认，请让用户选择编号、完整企业名称或统一社会信用代码。",
        candidates=candidates,
        display_text=_format_candidate_result(user_input, search_result, candidates),
    )


def _normalize_collection_mode(collection_mode: str = "") -> str:
    """标准化采集模式。默认 standard，兼顾速度和报告质量。"""
    mode = (collection_mode or DEFAULT_COLLECTION_MODE or "standard").strip().lower()
    aliases = {
        "lite": "standard",
        "standard-lite": "standard",
        "standard_lite": "standard",
        "full": "deep",
        "kyb": "deep",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in COLLECTION_MODES else "standard"


def _collect_search_evidence(enterprise_name: str, collection_mode: str = "standard") -> dict:
    """固定采集公开搜索证据。"""
    all_queries = {
        "industry": {
            "query": f"{enterprise_name} 所属行业 行业规模 增速 竞争格局 绿电需求 碳足迹 出口 欧盟 日本 韩国 ESG 绿色工厂 绿色供应链 用电量 政策",
            "profile_name": "industry",
        },
        "basic": {
            "query": f"{enterprise_name} 工商信息 统一社会信用代码 成立时间 注册资本 法定代表人 经营范围 企业性质 外资 台资 港澳台投资 母公司 实际控制人 最终受益人 股权穿透 企查查",
            "profile_name": "basic",
        },
        "risk": {
            "query": f"{enterprise_name} 失信 行政处罚 经营异常 法律诉讼 信用评级 被执行 股权出质 动产抵押 欠税 环保处罚 安全生产处罚 税务处罚 产品质量处罚 劳动纠纷",
            "profile_name": "risk",
        },
        "finance": {
            "query": f"{enterprise_name} 年营收 营业收入 年产值 销售额 总投资 固定资产投资 资产规模 净利润 现金流 资产负债率 年报 参保人数 纳税证明 融资 担保",
            "profile_name": "finance",
        },
        "development": {
            "query": f"{enterprise_name} 发展动态 核心客户 核心供应商 生产基地 厂房 冷链 仓储 产能 招投标 中标 出口 龙头企业 创新型中小企业 高成长性企业 绿色工厂 ISO HACCP 对外投资 知识产权 专利 技术实力",
            "profile_name": "development",
        },
        "gsxt": {
            "query": f"site:gsxt.gov.cn {enterprise_name}",
            "profile_name": "gsxt_official",
        },
        "gsxt_risk": {
            "query": f"{enterprise_name} 行政处罚 经营异常 严重违法失信 国家企业信用信息公示系统",
            "profile_name": "gsxt_risk",
        },
    }

    mode_keys = {
        "quick": ("industry", "basic", "risk", "finance"),
        "standard": ("industry", "basic", "finance", "development"),
        "deep": tuple(all_queries.keys()),
    }
    queries = {key: all_queries[key] for key in mode_keys.get(collection_mode, mode_keys["standard"])}

    def _search_count(name: str) -> int:
        if collection_mode == "deep":
            return 8 if name not in {"gsxt", "gsxt_risk"} else 5
        if collection_mode == "quick":
            return 3
        return 4 if name not in {"gsxt", "gsxt_risk"} else 3

    return _run_named_calls(
        {
            name: (
                lambda payload=query_payload, n=name: _do_web_search_structured(
                    payload["query"],
                    profile_name=payload["profile_name"],
                    count=_search_count(n),
                )
            )
            for name, query_payload in queries.items()
        },
        max_workers=3,
    )


def _collect_qixin_api_evidence(search_key: str, enterprise_name: str, collection_mode: str = "standard") -> dict:
    """按模式补充启信宝白名单 API，优先核心字段，异常时及时熔断降级。"""
    q = search_key or enterprise_name
    p0_calls = {
        "工商照面(API 1.41)": lambda: query_qixin_by_search_key("1.41", q, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "企业模糊搜索(API 1.31)": lambda: query_qixin_by_search_key("1.31", q, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
    }
    p1_calls = {
        "科技型企业(API 79.14)": lambda: query_qixin_by_search_key("79.14", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "股权穿透(API 55.2)": lambda: query_qixin_by_search_key("55.2", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "企业资质(API 22.1)": lambda: query_qixin_by_search_key("22.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "失信被执行(API 5.5)": lambda: query_qixin_by_search_key("5.5", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "被执行企业(API 17.5)": lambda: query_qixin_by_search_key("17.5", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "限制高消费(API 66.1)": lambda: query_qixin_by_search_key("66.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "经营异常(API 1.55)": lambda: query_qixin_by_search_key("1.55", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "严重违法(API 56.1)": lambda: query_qixin_by_search_key("56.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "环保处罚(API 51.1)": lambda: query_qixin_by_search_key("51.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "欠税信息(API 20.1)": lambda: query_qixin_by_search_key("20.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "重大税收违法(API 20.3)": lambda: query_qixin_by_search_key("20.3", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
    }
    p2_calls = {
        "购地信息(API 61.1)": lambda: query_qixin_by_search_key("61.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "案件串联(API 85.71)": lambda: query_qixin_by_search_key("85.71", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "地产行政处罚(API 32.1)": lambda: query_qixin_by_search_key("32.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "非正常户(API 63.2)": lambda: query_qixin_by_search_key("63.2", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "股权出质(API 26.1)": lambda: query_qixin_by_search_key("26.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "股权冻结(API 34.1)": lambda: query_qixin_by_search_key("34.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
        "动产抵押(API 25.1)": lambda: query_qixin_by_search_key("25.1", enterprise_name, timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS),
    }
    stage_calls = [("p0", p0_calls), ("p1", p1_calls)]
    if collection_mode == "deep":
        stage_calls.append(("p2", p2_calls))

    result: dict[str, Any] = {}
    stage_meta = []
    for stage_name, calls in stage_calls:
        stage_result = _run_named_calls(calls, max_workers=5, item_timeout=QIXIN_API_CHECK_TIMEOUT_SECONDS, group_timeout=35)
        stage_meta.append(
            {
                "stage": stage_name,
                "apis": [key for key in calls.keys()],
                "elapsed_seconds": stage_result.get("_meta", {}).get("elapsed_seconds", 0),
            }
        )
        for key, value in stage_result.items():
            if key != "_meta":
                result[key] = value
        fatal_errors = [get_qixin_error_info(value) for value in stage_result.values() if is_qixin_unavailable(value)]
        if fatal_errors:
            error_info = next((item for item in fatal_errors if item), {})
            result["_collection_note"] = (
                "Qixin collection stopped early because the provider became unavailable; "
                "fallback sources should be preferred for the remaining fields."
            )
            result["_fatal_error"] = error_info
            result["_meta"] = {
                "collection_mode": collection_mode,
                "stages_completed": [item["stage"] for item in stage_meta],
                "stage_meta": stage_meta,
                "stopped_early": True,
            }
            return result

    if collection_mode == "standard":
        result["_collection_note"] = (
            "Qixin standard mode collects subject/basic/risk/core qualification fields first; "
            "extended encumbrance and land/case details are deferred to deep mode or MCP补查."
        )
    elif collection_mode == "quick":
        result["_collection_note"] = (
            "Qixin quick mode keeps only basic subject confirmation plus core risk checks."
        )
    else:
        result["_collection_note"] = (
            "Qixin deep mode queried the approved whitelist in layered stages; "
            "1.41 prefers unified social credit code when available, other APIs use confirmed enterprise name."
        )
    result["_meta"] = {
        "collection_mode": collection_mode,
        "stages_completed": [item["stage"] for item in stage_meta],
        "stage_meta": stage_meta,
        "stopped_early": False,
    }
    return result


def _run_named_calls(
    call_map: dict,
    max_workers: int = 3,
    item_timeout: int = EVIDENCE_ITEM_TIMEOUT_SECONDS,
    group_timeout: int = EVIDENCE_GROUP_TIMEOUT_SECONDS,
) -> dict:
    """有限并发执行固定采集，避免 Coze 上被单个慢搜索/MCP 子接口拖住。"""
    results = {}
    started_at = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_name = {executor.submit(func): name for name, func in call_map.items()}
        try:
            completed_futures = as_completed(future_to_name, timeout=group_timeout)
            for future in completed_futures:
                name = future_to_name[future]
                try:
                    results[name] = future.result(timeout=item_timeout)
                except FuturesTimeoutError:
                    results[name] = f"查询超时({item_timeout}s)，已跳过"
                except Exception as e:
                    results[name] = f"查询失败: {e}"
        except FuturesTimeoutError:
            pass

        for future, name in future_to_name.items():
            if name in results:
                continue
            if future.done():
                try:
                    results[name] = future.result(timeout=0)
                except Exception as e:
                    results[name] = f"查询失败: {e}"
            else:
                future.cancel()
                results[name] = f"查询超时({group_timeout}s)，已跳过"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    results["_meta"] = {
        "elapsed_seconds": round(time.monotonic() - started_at, 2),
        "max_workers": max_workers,
        "timeout_policy": f"单项结果等待{item_timeout}秒，单组整体等待{group_timeout}秒；超时项标记为已跳过",
    }
    return results


def _progress_event(stage: str, status: str, started_at: float, detail: str = "") -> dict:
    return {
        "stage": stage,
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started_at, 2),
        "detail": detail,
    }


def _truncate_evidence_value(value, max_chars: int = EVIDENCE_FIELD_MAX_CHARS):
    """限制单字段证据长度，避免 Coze 后续模型解析超大 evidence_json 时卡住。"""
    if isinstance(value, dict):
        return {key: _truncate_evidence_value(item, max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_evidence_value(item, max_chars) for item in value[:20]]
    if isinstance(value, str) and len(value) > max_chars:
        return f"{value[:max_chars]}\n...（内容过长已截断，原始长度 {len(value)} 字符；如需详情请人工复核原始数据源）"
    return value


def _is_unknown_or_error(value) -> bool:
    text = str(value or "")
    if not text:
        return True
    if isinstance(value, dict) and value.get("provider") == "qixin" and value.get("ok") is False:
        return True
    return any(keyword in text for keyword in ("错误:", "查询失败", "查询超时", "未配置", "余额不足", "积分余额不足", '"ok": false'))


def _looks_like_risk(value) -> bool:
    """保守判断核心风险是否需要补查详情。"""
    text = str(value or "")
    if not text or _is_unknown_or_error(text):
        return False
    safe_keywords = ("未查询到", "无相关", "暂无", "没有相关", "无记录", "未发现")
    if any(keyword in text for keyword in safe_keywords):
        return False
    return any(keyword in text for keyword in ("有", "存在", "共", "条", "被执行", "处罚", "异常", "限制高消费", "严重违法"))


def _should_collect_risk_detail(qcc_mcp: dict, collection_mode: str) -> bool:
    if collection_mode == "deep":
        return True
    risk = qcc_mcp.get("risk", {})
    risk_keys = ("失信被执行人", "行政处罚", "经营异常", "严重违法失信", "限制高消费")
    return any(_looks_like_risk(risk.get(key)) for key in risk_keys)


def _should_collect_operation_detail(qcc_mcp: dict, search_evidence: dict, collection_mode: str) -> bool:
    if collection_mode == "deep":
        return True
    operation = qcc_mcp.get("operation", {})
    if any(operation.get(key) and not _is_unknown_or_error(operation.get(key)) for key in ("招投标记录", "资质认证", "荣誉信息")):
        return False
    development = json.dumps(search_evidence.get("development", {}), ensure_ascii=False)
    return any(keyword in development for keyword in ("专利", "知识产权", "招聘", "中标", "荣誉", "资质", "高新", "专精特新"))


def _search_items(search_evidence: dict, key: str) -> list[dict]:
    group = search_evidence.get(key, {})
    if isinstance(group, dict):
        return group.get("items", []) or []
    return []


def _search_stats(search_evidence: dict, key: str) -> dict:
    group = search_evidence.get(key, {})
    if isinstance(group, dict):
        return group.get("stats", {}) or {}
    return {}


def _search_texts(search_evidence: dict, *keys: str) -> str:
    parts = []
    for key in keys:
        for item in _search_items(search_evidence, key):
            parts.extend(
                str(item.get(field, "") or "")
                for field in ("title", "snippet", "summary", "content")
            )
    return " ".join(parts)


def _should_collect_mcp_standard_seed(qixin_api: dict) -> bool:
    if not qixin_api:
        return True
    if qixin_api.get("_fatal_error"):
        return True
    critical_keys = (
        "工商照面(API 1.41)",
        "股权穿透(API 55.2)",
        "企业资质(API 22.1)",
        "经营异常(API 1.55)",
        "严重违法(API 56.1)",
        "失信被执行(API 5.5)",
        "被执行企业(API 17.5)",
        "限制高消费(API 66.1)",
        "欠税信息(API 20.1)",
    )
    failed = 0
    seen = 0
    for key in critical_keys:
        if key not in qixin_api:
            continue
        seen += 1
        if _is_unknown_or_error(qixin_api.get(key)):
            failed += 1
    return seen == 0 or failed >= max(3, seen // 2 + 1)


def _collect_triggered_mcp_evidence(
    search_key: str,
    qcc_mcp: dict,
    search_evidence: dict,
    collection_mode: str,
    qixin_api: dict | None = None,
) -> dict:
    """按需补查重字段，避免普通评估默认全量尽调。"""
    if not is_qcc_mcp_available():
        return {
            "_meta": {
                "policy": "MCP skipped: no configured key or all configured keys are exhausted today.",
                "triggered_sections": [],
            }
        }

    if collection_mode == "deep":
        return {
            "_meta": {
                "policy": "deep 模式已在主 MCP 采集中采集全量 KYB、历史风险、税务环保、资产负担、知识产权、经营资质和舆情，不再重复触发补查。",
                "triggered_sections": [],
            }
        }

    triggered = {}
    qixin_api = qixin_api or {}
    if _should_collect_risk_detail(qcc_mcp, collection_mode):
        triggered["法律诉讼"] = _run_named_calls(
            {
                "被执行人": lambda: _safe_call(query_executed_person, search_key),
                "裁判文书": lambda: _safe_call(query_judicial_documents, search_key),
                "终本案件": lambda: _safe_call(query_final_case, search_key),
            }
        )
        triggered["风险详情"] = _run_named_calls(
            {
                "风险扫描": lambda: _safe_call(query_company_risk_scan, search_key),
                "立案信息": lambda: _safe_call(query_case_filing_info, search_key),
                "信用评价": lambda: _safe_call(query_credit_evaluation, search_key),
            }
        )

    if _should_collect_operation_detail(qcc_mcp, search_evidence, collection_mode):
        triggered["经营补充"] = _run_named_calls(
            {
                "专利信息": lambda: _safe_call(query_patent_info, search_key),
                "招聘信息": lambda: _safe_call(query_recruitment_info, search_key),
                "新闻舆情": lambda: _safe_call(query_news_sentiment, search_key),
            }
        )

    triggered["_meta"] = {
        "policy": "按需补查：核心风险命中时补司法/风险详情；经营证据不足时补专利/招聘/舆情。若启信宝不可用或核心字段缺失较多，standard 模式会在主 MCP 采集中自动补位基础结构化数据。",
        "triggered_sections": [key for key in triggered.keys() if key != "_meta"],
    }
    return triggered


def _shorten(value, limit: int = 220) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _build_collection_diagnostics(
    qixin_api: dict,
    qcc_mcp: dict,
    triggered_mcp: dict,
    search_evidence: dict,
    qcc_data_json: dict,
    collection_mode: str,
) -> dict:
    """紧凑的采集诊断摘要，让人一眼看出数据获取健康度。"""
    qixin_meta = qixin_api.get("_meta", {}) if isinstance(qixin_api, dict) else {}
    qixin_fatal = qixin_api.get("_fatal_error", {}) if isinstance(qixin_api, dict) else {}
    qixin_stages = qixin_meta.get("stages_completed", [])
    qixin_stopped_early = qixin_meta.get("stopped_early", False)

    qixin_api_keys = [k for k in qixin_api.keys() if k not in ("_meta", "_fatal_error", "_collection_note")]
    qixin_hit = sum(1 for k in qixin_api_keys if not _is_unknown_or_error(qixin_api.get(k)))
    qixin_miss = sum(1 for k in qixin_api_keys if _is_unknown_or_error(qixin_api.get(k)))

    # 检查启信宝 API 返回中是否有缓存命中标记（由 qixin_openapi_client 添加）
    # _cache_source 可能是 "memory"（进程内缓存）或 "persistent"（持久化缓存）
    memory_cache = False
    persisted_cache = False
    if isinstance(qixin_api, dict):
        for k, v in qixin_api.items():
            if k.startswith("_"):
                continue
            parsed_value = v if isinstance(v, dict) else parse_qixin_result(v)
            if isinstance(parsed_value, dict):
                cache_src = parsed_value.get("_cache_source", "")
                if cache_src == "memory":
                    memory_cache = True
                elif cache_src == "persistent":
                    persisted_cache = True

    mcp_note = qcc_mcp.get("_collection_note", "")
    mcp_seed_triggered = "promoted" in mcp_note.lower() or "seed" in mcp_note.lower()
    mcp_groups = []
    for group in ("basic", "finance", "risk", "extended_risk", "ip", "operation", "news"):
        if qcc_mcp.get(group):
            mcp_groups.append(group)

    field_sources = qcc_data_json.get("field_sources", {})
    source_counts: dict[str, int] = {}
    for src in field_sources.values():
        short = src.split(".")[0] if "." in src else src
        source_counts[short] = source_counts.get(short, 0) + 1

    missing_fields = [k for k, v in qcc_data_json.items() if k not in ("field_sources", "source_conflicts", "provider", "qixin_api", "qixin_basic", "qixin_fuzzy_search", "qixin_tech_enterprise", "qixin_equity_penetration", "qixin_qualification", "qixin_land_purchase", "qixin_case_relation", "qixin_real_estate_admin_penalty", "history_risk") and _is_unknown_or_error(v)]
    missing_count = len(missing_fields)

    conflicts = qcc_data_json.get("source_conflicts", [])
    conflict_count = len(conflicts)
    high_risk_fields = {"registration", "dishonest", "business_exception", "serious_violation", "high_consumption"}
    critical_conflict = any(c.get("field") in high_risk_fields for c in conflicts)

    needs_review = bool(qixin_fatal) or qixin_stopped_early or missing_count >= 10 or critical_conflict
    reasons = []
    if qixin_fatal:
        reasons.append("启信宝熔断")
    if qixin_stopped_early:
        reasons.append("启信宝提前终止")
    if missing_count >= 10:
        reasons.append(f"缺失字段≥10({missing_count})")
    if critical_conflict:
        reasons.append("核心字段来源冲突")

    search_groups = [key for key, value in search_evidence.items() if key != "_meta" and isinstance(value, dict)]
    official_hits = sum(_search_stats(search_evidence, key).get("official_hits", 0) for key in search_groups)
    high_auth_hits = sum(_search_stats(search_evidence, key).get("high_auth_hits", 0) for key in search_groups)
    content_hits = sum(_search_stats(search_evidence, key).get("content_hits", 0) for key in search_groups)
    website_hit = any("官网" in _search_texts(search_evidence, "basic", "development") for _ in [0])
    gsxt_hit = _search_stats(search_evidence, "gsxt").get("result_count", 0) > 0

    completeness = {
        "subject": round(max(0, min(1, 1 - (1 if _is_unknown_or_error(qcc_data_json.get("registration")) else 0) / 1)), 2),
        "risk": round(max(0, min(1, 1 - (sum(1 for key in ("dishonest", "business_exception", "serious_violation", "high_consumption") if _is_unknown_or_error(qcc_data_json.get(key))) / 4))), 2),
        "finance": round(max(0, min(1, 1 - (sum(1 for key in ("financial", "credit_eval") if _is_unknown_or_error(qcc_data_json.get(key))) / 2))), 2),
        "operation": round(max(0, min(1, 1 - (sum(1 for key in ("bidding", "qualifications", "recruitment", "news_sentiment") if _is_unknown_or_error(qcc_data_json.get(key))) / 4))), 2),
        "related_party": round(max(0, min(1, 1 - (sum(1 for key in ("shareholder", "actual_controller") if _is_unknown_or_error(qcc_data_json.get(key))) / 2))), 2),
    }

    recommended_next_step = "continue_scoring"
    if needs_review:
        recommended_next_step = "human_review"
    elif collection_mode != "deep" and (missing_count >= 6 or official_hits == 0 and high_auth_hits == 0):
        recommended_next_step = "trigger_deep"

    return {
        "qixin": {
            "fatal": bool(qixin_fatal),
            "fatal_reason": (qixin_fatal.get("message", "") or qixin_fatal.get("error_msg", "")) if qixin_fatal else "",
            "stages": qixin_stages,
            "stopped_early": qixin_stopped_early,
            "hit_count": qixin_hit,
            "miss_count": qixin_miss,
            "memory_cache": memory_cache,
            "persisted_cache": persisted_cache,
        },
        "qcc_mcp": {
            "seed_triggered": mcp_seed_triggered,
            "collected_groups": mcp_groups,
        },
        "search": {
            "groups": search_groups,
            "official_hits": official_hits,
            "high_auth_hits": high_auth_hits,
            "content_hits": content_hits,
            "website_hit": website_hit,
            "gsxt_hit": gsxt_hit,
        },
        "field_source_summary": source_counts,
        "module_completeness": completeness,
        "missing_or_unknown_fields_count": missing_count,
        "missing_or_unknown_fields": missing_fields[:20],
        "source_conflict_count": conflict_count,
        "needs_human_review": needs_review,
        "review_reasons": reasons,
        "recommended_next_step": recommended_next_step,
        "collection_mode": collection_mode,
    }


def _build_evidence_summary(
    identity: dict,
    qixin_api: dict,
    search_evidence: dict,
    qcc_mcp: dict,
    triggered_mcp: dict,
    collection_mode: str,
) -> dict:
    """给 AI 优先阅读的紧凑证据摘要，减少大块原始证据拖慢分析。"""
    risk = qcc_mcp.get("risk", {})
    basic = qcc_mcp.get("basic", {})
    finance = qcc_mcp.get("finance", {})
    operation = qcc_mcp.get("operation", {})
    risk_keys = ("失信被执行人", "行政处罚", "经营异常", "严重违法失信", "限制高消费")
    missing_or_unknown = []
    for group_name, group in (("qixin_api", qixin_api), ("basic", basic), ("finance", finance), ("risk", risk), ("operation", operation)):
        for key, value in group.items():
            if key == "_meta":
                continue
            if _is_unknown_or_error(value):
                missing_or_unknown.append(f"{group_name}.{key}")

    qixin_basic_text = qixin_api.get("工商照面(API 1.41)", "")
    finance_text = finance.get("财务数据", "")
    development_items = _search_items(search_evidence, "development")
    industry_items = _search_items(search_evidence, "industry")
    risk_items = _search_items(search_evidence, "risk")
    gsxt_items = _search_items(search_evidence, "gsxt")
    qcc_data_json = _build_qcc_data_json(qcc_mcp, qixin_api, triggered_mcp)
    conflict_flags = [
        {
            "field": item.get("field", ""),
            "sources": [src.get("source", "") for src in item.get("sources", []) if isinstance(src, dict)],
        }
        for item in qcc_data_json.get("source_conflicts", [])[:8]
        if isinstance(item, dict)
    ]

    return {
        "collection_mode": collection_mode,
        "subject_profile": {
            "enterprise_name": identity.get("enterprise_name", ""),
            "unified_social_credit_code": identity.get("unified_social_credit_code", ""),
            "match_source": identity.get("match_source", ""),
            "match_reason": identity.get("match_reason", ""),
            "confidence": identity.get("confidence", ""),
            "registration": _shorten(qixin_basic_text or basic.get("工商登记", "")),
        },
        "official_structured_summary": {
            "qixin_basic": _shorten(qixin_basic_text),
            "qixin_core_risk": {
                "business_exception": _shorten(qixin_api.get("经营异常(API 1.55)", "")),
                "dishonest": _shorten(qixin_api.get("失信被执行(API 5.5)", "")),
                "executed": _shorten(qixin_api.get("被执行企业(API 17.5)", "")),
                "serious_violation": _shorten(qixin_api.get("严重违法(API 56.1)", "")),
                "tax_environment": _shorten(qixin_api.get("欠税信息(API 20.1)", "") or qixin_api.get("环保处罚(API 51.1)", "")),
            },
            "mcp_structured": {
                "shareholder": _shorten(basic.get("股东结构", "")),
                "actual_controller": _shorten(basic.get("实际控制人", "")),
                "financial": _shorten(finance_text),
                "operation": _shorten(operation.get("资质认证", "") or operation.get("招投标记录", "")),
            },
        },
        "official_search_summary": {
            "gsxt_hits": [
                {
                    "title": item.get("title", ""),
                    "site_name": item.get("site_name", ""),
                    "publish_time": item.get("publish_time", ""),
                    "summary": _shorten(item.get("summary") or item.get("snippet", ""), 160),
                }
                for item in gsxt_items[:3]
            ],
            "risk_hits": [
                {
                    "title": item.get("title", ""),
                    "site_name": item.get("site_name", ""),
                    "publish_time": item.get("publish_time", ""),
                    "summary": _shorten(item.get("summary") or item.get("snippet", ""), 160),
                }
                for item in risk_items[:3]
            ],
        },
        "operation_signal_summary": {
            "development_signals": [
                {
                    "title": item.get("title", ""),
                    "site_name": item.get("site_name", ""),
                    "publish_time": item.get("publish_time", ""),
                    "summary": _shorten(item.get("summary") or item.get("snippet", ""), 160),
                }
                for item in development_items[:4]
            ],
            "operation_structured": {
                "bidding": _shorten(operation.get("招投标记录", "")),
                "qualifications": _shorten(operation.get("资质认证", "")),
                "honor": _shorten(operation.get("荣誉信息", "")),
                "recruitment": _shorten(operation.get("招聘信息", "")),
            },
        },
        "finance_signal_summary": {
            "financial_structured": _shorten(finance_text),
            "public_finance_signals": [
                {
                    "title": item.get("title", ""),
                    "site_name": item.get("site_name", ""),
                    "publish_time": item.get("publish_time", ""),
                    "summary": _shorten(item.get("summary") or item.get("snippet", ""), 160),
                }
                for item in _search_items(search_evidence, "finance")[:3]
            ],
        },
        "risk_signal_summary": {
            "core_risks": {key: _shorten(risk.get(key, "")) for key in risk_keys},
            "triggered_collection": triggered_mcp.get("_meta", {}),
        },
        "search_signal_summary": {
            "industry_signals": [
                {
                    "title": item.get("title", ""),
                    "site_name": item.get("site_name", ""),
                    "publish_time": item.get("publish_time", ""),
                    "summary": _shorten(item.get("summary") or item.get("snippet", ""), 160),
                }
                for item in industry_items[:3]
            ],
            "basic_signals": [
                {
                    "title": item.get("title", ""),
                    "site_name": item.get("site_name", ""),
                    "publish_time": item.get("publish_time", ""),
                    "summary": _shorten(item.get("summary") or item.get("snippet", ""), 160),
                }
                for item in _search_items(search_evidence, "basic")[:3]
            ],
        },
        "field_gaps": missing_or_unknown[:20],
        "conflict_flags": conflict_flags,
        "scoring_hints": {
            "priority_sources": ["启信宝API 1.41主体确认", "企查查MCP主体确认回退", "启信宝白名单API固定结构化采集", "国家企业信用信息公示系统/公开搜索", "企查查MCP缺失字段补查"],
            "analysis_hint": "优先基于本摘要评分；只有摘要证据不足或冲突时，再读取 search_evidence/qcc_mcp/triggered_mcp 原文。",
            "qixin_collection_meta": qixin_api.get("_meta", {}) if isinstance(qixin_api, dict) else {},
            "qixin_fatal_error": qixin_api.get("_fatal_error", {}) if isinstance(qixin_api, dict) else {},
        },
    }


def _collect_qcc_mcp_evidence(search_key: str, collection_mode: str = "standard", qixin_api: dict | None = None) -> dict:
    """固定采集企查查 MCP 证据，search_key 优先为统一社会信用代码。"""
    if not is_qcc_mcp_available():
        return {
            "basic": {},
            "finance": {},
            "risk": {},
            "extended_risk": {},
            "ip": {},
            "operation": {},
            "news": {},
            "_collection_note": "MCP skipped: no configured key or all configured keys are exhausted today.",
        }

    basic_calls_all = {
        "工商登记": lambda: _safe_call(query_company_registration, search_key),
        "股东结构": lambda: _safe_call(query_shareholder_info, search_key),
        "实际控制人": lambda: _safe_call(query_actual_controller, search_key),
        "企业简介": lambda: _safe_call(query_company_profile, search_key),
        "上市信息": lambda: _safe_call(query_listing_info, search_key),
        "主要人员": lambda: _safe_call(query_key_personnel, search_key),
    }
    basic_calls_lite = {
        "工商登记": basic_calls_all["工商登记"],
        "股东结构": basic_calls_all["股东结构"],
        "企业简介": basic_calls_all["企业简介"],
    }
    finance_calls_all = {
        "财务数据": lambda: _safe_call(query_financial_data, search_key),
        "对外投资": lambda: _safe_call(query_external_investments, search_key),
    }
    risk_calls_all = {
        "失信被执行人": lambda: _safe_call(query_dishonest_info, search_key),
        "行政处罚": lambda: _safe_call(query_administrative_penalty, search_key),
        "经营异常": lambda: _safe_call(query_business_exception, search_key),
        "严重违法失信": lambda: _safe_call(query_serious_violation, search_key),
        "限制高消费": lambda: _safe_call(query_high_consumption_restriction, search_key),
        "风险扫描": lambda: _safe_call(query_company_risk_scan, search_key),
        "立案信息": lambda: _safe_call(query_case_filing_info, search_key),
        "信用评价": lambda: _safe_call(query_credit_evaluation, search_key),
    }
    risk_calls_lite = {
        "失信被执行人": risk_calls_all["失信被执行人"],
        "行政处罚": risk_calls_all["行政处罚"],
        "经营异常": risk_calls_all["经营异常"],
        "严重违法失信": risk_calls_all["严重违法失信"],
        "限制高消费": risk_calls_all["限制高消费"],
    }
    legal_calls = {
        "被执行人": lambda: _safe_call(query_executed_person, search_key),
        "裁判文书": lambda: _safe_call(query_judicial_documents, search_key),
        "法院公告": lambda: _safe_call(query_court_announcement, search_key),
        "终本案件": lambda: _safe_call(query_final_case, search_key),
    }
    tax_environment_calls = {
        "环保处罚": lambda: _safe_call(query_environmental_penalty, search_key),
        "税务非正常户": lambda: _safe_call(query_tax_abnormal, search_key),
        "欠税公告": lambda: _safe_call(query_tax_arrears, search_key),
        "税收违法": lambda: _safe_call(query_tax_violation, search_key),
    }
    asset_equity_calls = {
        "股权出质": lambda: _safe_call(query_equity_pledge, search_key),
        "股权冻结": lambda: _safe_call(query_equity_freeze, search_key),
        "动产抵押": lambda: _safe_call(query_chattel_mortgage, search_key),
        "土地抵押": lambda: _safe_call(query_land_mortgage, search_key),
    }
    history_calls = {
        "历史失信": lambda: _safe_call(query_history_dishonest, search_key),
        "历史被执行": lambda: _safe_call(query_history_executed_person, search_key),
        "历史经营异常": lambda: _safe_call(query_history_business_exception, search_key),
        "历史行政处罚": lambda: _safe_call(query_history_administrative_penalty, search_key),
    }
    ip_calls = {
        "专利信息": lambda: _safe_call(query_patent_info, search_key),
        "商标信息": lambda: _safe_call(query_trademark_info, search_key),
        "软件著作权": lambda: _safe_call(query_software_copyright, search_key),
    }
    operation_calls = {
        "招投标记录": lambda: _safe_call(query_bidding_info, search_key),
        "资质认证": lambda: _safe_call(query_qualifications, search_key),
        "荣誉信息": lambda: _safe_call(query_honor_info, search_key),
        "招聘信息": lambda: _safe_call(query_recruitment_info, search_key),
        "行政许可": lambda: _safe_call(query_administrative_license, search_key),
        "纳税人资质": lambda: _safe_call(query_taxpayer_qualification, search_key),
        "产品抽查": lambda: _safe_call(query_product_check, search_key),
        "国有土地受让": lambda: _safe_call(query_state_owned_land_transfer, search_key),
    }
    news_calls = {
        "新闻舆情": lambda: _safe_call(query_news_sentiment, search_key),
    }

    if collection_mode == "quick":
        return {
            "basic": _run_named_calls({"工商登记": basic_calls_all["工商登记"]}),
            "finance": {},
            "risk": _run_named_calls(risk_calls_lite),
            "extended_risk": {},
            "ip": {},
            "operation": {},
            "news": {},
            "_collection_note": "quick 模式仅采集主体工商和核心风险，跳过财务、知识产权、经营资质、历史和专项风险。",
        }

    if collection_mode == "standard":
        if _should_collect_mcp_standard_seed(qixin_api or {}):
            return {
                "basic": _run_named_calls(basic_calls_lite),
                "finance": _run_named_calls({"财务数据": finance_calls_all["财务数据"]}),
                "risk": _run_named_calls(risk_calls_lite),
                "extended_risk": {},
                "ip": {},
                "operation": _run_named_calls(
                    {
                        "资质认证": operation_calls["资质认证"],
                        "招投标记录": operation_calls["招投标记录"],
                    }
                ),
                "news": {},
                "_collection_note": "standard mode promoted MCP seed collection because Qixin was unavailable or critical structured fields were missing.",
            }
        return {
            "basic": {},
            "finance": {},
            "risk": {},
            "extended_risk": {},
            "ip": {},
            "operation": {},
            "news": {},
            "_collection_note": "standard mode skips default MCP calls; Qixin whitelisted APIs and public search are used first, MCP is only used by triggered collection or deep mode.",
        }

    return {
        "basic": _run_named_calls(basic_calls_all),
        "finance": _run_named_calls(finance_calls_all),
        "risk": _run_named_calls(risk_calls_all),
        "extended_risk": {
            "法律诉讼": _run_named_calls(legal_calls),
            "税务环保风险": _run_named_calls(tax_environment_calls),
            "资产及股权负担": _run_named_calls(asset_equity_calls),
            "历史风险": _run_named_calls(history_calls),
        },
        "ip": _run_named_calls(ip_calls),
        "operation": _run_named_calls(operation_calls),
        "news": _run_named_calls(news_calls),
        "_collection_note": "deep 模式采集全量 KYB、历史风险、税务环保、资产负担、知识产权、经营资质和舆情。",
    }


def _build_qcc_data_json(
    qcc_mcp: dict,
    qixin_api: dict | None = None,
    triggered_mcp: dict | None = None,
) -> dict:
    """整理成 report_tool 可复用的 qcc_data_json 字段。

    参数名继续保留 qcc_data_json 以兼容报告工具，但主结构化来源已切换为启信宝 API。
    """
    qixin_api = qixin_api or {}
    triggered_mcp = triggered_mcp or {}
    extended_risk = qcc_mcp.get("extended_risk", {})
    legal = extended_risk.get("法律诉讼", {}) or triggered_mcp.get("法律诉讼", {})
    tax_environment = extended_risk.get("税务环保风险", {}) or triggered_mcp.get("税务环保风险", {})
    asset_equity = extended_risk.get("资产及股权负担", {}) or triggered_mcp.get("资产及股权负担", {})
    history_risk = extended_risk.get("历史风险", {}) or triggered_mcp.get("历史风险", {})
    operation_land = extended_risk.get("经营资质与土地", {})
    operation = qcc_mcp.get("operation", {})
    risk = qcc_mcp.get("risk", {})
    basic = qcc_mcp.get("basic", {})
    finance = qcc_mcp.get("finance", {})
    ip = qcc_mcp.get("ip", {})
    risk_detail = triggered_mcp.get("风险详情", {})
    operation_supplement = triggered_mcp.get("经营补充", {})

    def _pick_with_source(*candidates):
        for source_name, value in candidates:
            if value not in (None, "", [], {}):
                return value, source_name
        return "", ""

    field_sources: dict[str, str] = {}
    source_conflicts: list[dict] = []

    def _normalize_conflict_text(value) -> str:
        text = str(value or "").strip()
        import re
        text = re.sub(r"\s+", "", text)
        text = text[:120]
        return text

    def _assign(field: str, *candidates):
        value, source_name = _pick_with_source(*candidates)
        if source_name:
            field_sources[field] = source_name
        non_empty = [(sn, v) for sn, v in candidates if v not in (None, "", [], {})]
        if len(non_empty) >= 2:
            normalized = [_normalize_conflict_text(v) for _, v in non_empty]
            if len(set(normalized)) > 1:
                source_conflicts.append({
                    "field": field,
                    "sources": [
                        {"source": sn, "preview": _normalize_conflict_text(v)[:80]}
                        for sn, v in non_empty
                    ],
                })
        return value

    return {
        "provider": "qixin_primary_qcc_mcp_fallback",
        "qixin_api": qixin_api,
        "registration": _assign("registration", ("qixin_api_1_41", qixin_api.get("工商照面(API 1.41)", "")), ("qcc_mcp_basic.registration", basic.get("工商登记", ""))),
        "qixin_basic": qixin_api.get("工商照面(API 1.41)", ""),
        "qixin_fuzzy_search": qixin_api.get("企业模糊搜索(API 1.31)", ""),
        "qixin_tech_enterprise": qixin_api.get("科技型企业(API 79.14)", ""),
        "qixin_equity_penetration": qixin_api.get("股权穿透(API 55.2)", ""),
        "qixin_qualification": qixin_api.get("企业资质(API 22.1)", ""),
        "qixin_land_purchase": qixin_api.get("购地信息(API 61.1)", ""),
        "qixin_case_relation": qixin_api.get("案件串联(API 85.71)", ""),
        "qixin_real_estate_admin_penalty": qixin_api.get("地产行政处罚(API 32.1)", ""),
        "shareholder": _assign("shareholder", ("qcc_mcp_basic.shareholder", basic.get("股东结构", ""))),
        "actual_controller": _assign("actual_controller", ("qcc_mcp_basic.actual_controller", basic.get("实际控制人", ""))),
        "company_profile": _assign("company_profile", ("qcc_mcp_basic.company_profile", basic.get("企业简介", ""))),
        "listing_info": _assign("listing_info", ("qcc_mcp_basic.listing_info", basic.get("上市信息", ""))),
        "key_personnel": _assign("key_personnel", ("qcc_mcp_basic.key_personnel", basic.get("主要人员", ""))),
        "financial": _assign("financial", ("qcc_mcp_finance.financial", finance.get("财务数据", ""))),
        "investment": _assign("investment", ("qcc_mcp_finance.investment", finance.get("对外投资", ""))),
        "dishonest": _assign("dishonest", ("qixin_api_5_5", qixin_api.get("失信被执行(API 5.5)", "")), ("qcc_mcp_risk.dishonest", risk.get("失信被执行人", ""))),
        "admin_penalty": _assign("admin_penalty", ("qcc_mcp_risk.admin_penalty", risk.get("行政处罚", ""))),
        "business_exception": _assign("business_exception", ("qixin_api_1_55", qixin_api.get("经营异常(API 1.55)", "")), ("qcc_mcp_risk.business_exception", risk.get("经营异常", ""))),
        "serious_violation": _assign("serious_violation", ("qixin_api_56_1", qixin_api.get("严重违法(API 56.1)", "")), ("qcc_mcp_risk.serious_violation", risk.get("严重违法失信", ""))),
        "high_consumption": _assign("high_consumption", ("qixin_api_66_1", qixin_api.get("限制高消费(API 66.1)", "")), ("qcc_mcp_risk.high_consumption", risk.get("限制高消费", ""))),
        "risk_scan": _assign("risk_scan", ("qcc_mcp_risk.risk_scan", risk.get("风险扫描", "")), ("qcc_mcp_triggered.risk_scan", risk_detail.get("风险扫描", ""))),
        "case_filing": _assign("case_filing", ("qcc_mcp_risk.case_filing", risk.get("立案信息", "")), ("qcc_mcp_triggered.case_filing", risk_detail.get("立案信息", ""))),
        "credit_eval": _assign("credit_eval", ("qcc_mcp_risk.credit_eval", risk.get("信用评价", "")), ("qcc_mcp_triggered.credit_eval", risk_detail.get("信用评价", ""))),
        "executed_person": _assign("executed_person", ("qixin_api_17_5", qixin_api.get("被执行企业(API 17.5)", "")), ("qcc_mcp_legal.executed_person", legal.get("被执行人", ""))),
        "judicial_documents": _assign("judicial_documents", ("qcc_mcp_legal.judicial_documents", legal.get("裁判文书", ""))),
        "court_announcement": _assign("court_announcement", ("qcc_mcp_legal.court_announcement", legal.get("法院公告", ""))),
        "final_case": _assign("final_case", ("qcc_mcp_legal.final_case", legal.get("终本案件", ""))),
        "environmental_penalty": _assign("environmental_penalty", ("qixin_api_51_1", qixin_api.get("环保处罚(API 51.1)", "")), ("qcc_mcp_tax.environmental_penalty", tax_environment.get("环保处罚", ""))),
        "tax_abnormal": _assign("tax_abnormal", ("qixin_api_63_2", qixin_api.get("非正常户(API 63.2)", "")), ("qcc_mcp_tax.tax_abnormal", tax_environment.get("税务非正常户", ""))),
        "tax_arrears": _assign("tax_arrears", ("qixin_api_20_1", qixin_api.get("欠税信息(API 20.1)", "")), ("qcc_mcp_tax.tax_arrears", tax_environment.get("欠税公告", ""))),
        "tax_violation": _assign("tax_violation", ("qixin_api_20_3", qixin_api.get("重大税收违法(API 20.3)", "")), ("qcc_mcp_tax.tax_violation", tax_environment.get("税收违法", ""))),
        "equity_pledge": _assign("equity_pledge", ("qixin_api_26_1", qixin_api.get("股权出质(API 26.1)", "")), ("qcc_mcp_asset.equity_pledge", asset_equity.get("股权出质", ""))),
        "equity_freeze": _assign("equity_freeze", ("qixin_api_34_1", qixin_api.get("股权冻结(API 34.1)", "")), ("qcc_mcp_asset.equity_freeze", asset_equity.get("股权冻结", ""))),
        "chattel_mortgage": _assign("chattel_mortgage", ("qixin_api_25_1", qixin_api.get("动产抵押(API 25.1)", "")), ("qcc_mcp_asset.chattel_mortgage", asset_equity.get("动产抵押", ""))),
        "land_mortgage": _assign("land_mortgage", ("qcc_mcp_asset.land_mortgage", asset_equity.get("土地抵押", ""))),
        "history_risk": history_risk,
        "history_dishonest": _assign("history_dishonest", ("qcc_mcp_history.dishonest", history_risk.get("历史失信", ""))),
        "history_executed_person": _assign("history_executed_person", ("qcc_mcp_history.executed_person", history_risk.get("历史被执行", ""))),
        "history_business_exception": _assign("history_business_exception", ("qcc_mcp_history.business_exception", history_risk.get("历史经营异常", ""))),
        "history_admin_penalty": _assign("history_admin_penalty", ("qcc_mcp_history.admin_penalty", history_risk.get("历史行政处罚", ""))),
        "patent": _assign("patent", ("qcc_mcp_ip.patent", ip.get("专利信息", "")), ("qcc_mcp_triggered.patent", operation_supplement.get("专利信息", ""))),
        "trademark": _assign("trademark", ("qcc_mcp_ip.trademark", ip.get("商标信息", ""))),
        "software_copyright": _assign("software_copyright", ("qcc_mcp_ip.software_copyright", ip.get("软件著作权", ""))),
        "bidding": _assign("bidding", ("qcc_mcp_operation.bidding", operation.get("招投标记录", ""))),
        "qualifications": _assign("qualifications", ("qixin_api_22_1", qixin_api.get("企业资质(API 22.1)", "")), ("qcc_mcp_operation.qualifications", operation.get("资质认证", ""))),
        "honor": _assign("honor", ("qcc_mcp_operation.honor", operation.get("荣誉信息", ""))),
        "recruitment": _assign("recruitment", ("qcc_mcp_operation.recruitment", operation.get("招聘信息", "")), ("qcc_mcp_triggered.recruitment", operation_supplement.get("招聘信息", ""))),
        "administrative_license": _assign("administrative_license", ("qcc_mcp_operation.administrative_license", operation.get("行政许可", "")), ("qcc_mcp_land.administrative_license", operation_land.get("行政许可", ""))),
        "taxpayer_qualification": _assign("taxpayer_qualification", ("qcc_mcp_operation.taxpayer_qualification", operation.get("纳税人资质", "")), ("qcc_mcp_land.taxpayer_qualification", operation_land.get("纳税人资质", ""))),
        "product_check": _assign("product_check", ("qcc_mcp_operation.product_check", operation.get("产品抽查", "")), ("qcc_mcp_land.product_check", operation_land.get("产品抽查", ""))),
        "state_owned_land_transfer": _assign("state_owned_land_transfer", ("qixin_api_61_1", qixin_api.get("购地信息(API 61.1)", "")), ("qcc_mcp_operation.state_owned_land_transfer", operation.get("国有土地受让", "")), ("qcc_mcp_land.state_owned_land_transfer", operation_land.get("国有土地受让", ""))),
        "news_sentiment": _assign("news_sentiment", ("qcc_mcp_news.news_sentiment", qcc_mcp.get("news", {}).get("新闻舆情", "")), ("qcc_mcp_triggered.news_sentiment", operation_supplement.get("新闻舆情", ""))),
        "field_sources": field_sources,
        "source_conflicts": source_conflicts if source_conflicts else [],
    }


@tool
def collect_enterprise_evidence(user_input: str, collection_mode: str = "") -> str:
    """【固定数据采集】先确认企业名称和统一社会信用代码，再按模式采集证据。

    返回 JSON 字符串。若 status=need_user_confirmation，必须先让用户确认候选；
    若 status=confirmed，AI 应基于 evidence_json 解读、评分并调用 generate_enterprise_report。
    collection_mode 可选：quick / standard / deep。默认 standard。
    """
    collection_mode = _normalize_collection_mode(collection_mode)
    collection_started_at = time.monotonic()
    logger.info(
        "collect_enterprise_evidence started: user_input=%s, collection_mode=%s",
        user_input[:30], collection_mode,
    )
    progress = [
        _progress_event("subject_identity", "running", collection_started_at, f"正在确认企业名称和统一社会信用代码，采集模式={collection_mode}")
    ]
    identity = _confirm_enterprise_identity(user_input)
    progress.append(
        _progress_event(
            "subject_identity",
            identity.get("status", "unknown"),
            collection_started_at,
            identity.get("enterprise_name") or identity.get("message", ""),
        )
    )
    if identity.get("status") != "confirmed":
        logger.warning(
            "Subject identity not confirmed: status=%s, message=%s",
            identity.get("status"), identity.get("message", "")[:80],
        )
        return json.dumps({"identity": identity, "collection_progress": progress}, ensure_ascii=False)

    enterprise_name = identity["enterprise_name"]
    api_search_key = identity.get("unified_social_credit_code") or identity.get("mcp_search_key") or enterprise_name
    mcp_search_key = api_search_key
    logger.info(
        "Subject confirmed: name=%s, credit_code=%s, match_source=%s",
        enterprise_name[:30],
        identity.get("unified_social_credit_code", "")[:18],
        identity.get("match_source", ""),
    )
    progress.append(
        _progress_event("public_search", "running", collection_started_at, f"正在采集公开搜索、官方公示和行业资料，模式={collection_mode}")
    )
    search_evidence = _collect_search_evidence(enterprise_name, collection_mode)
    progress.append(
        _progress_event("public_search", "completed", collection_started_at, "公开搜索资料采集完成")
    )
    progress.append(
        _progress_event("qixin_api_checks", "running", collection_started_at, "正在查询启信宝白名单 API，工商照面优先使用统一社会信用代码，其余接口使用企业全称")
    )
    qixin_api = _collect_qixin_api_evidence(api_search_key, enterprise_name, collection_mode)
    qixin_hit = sum(1 for k in qixin_api if k not in ("_meta", "_fatal_error", "_collection_note") and not _is_unknown_or_error(qixin_api.get(k)))
    qixin_miss = sum(1 for k in qixin_api if k not in ("_meta", "_fatal_error", "_collection_note") and _is_unknown_or_error(qixin_api.get(k)))
    logger.info(
        "Qixin API collection done: hit=%d, miss=%d, fatal=%s, stopped_early=%s",
        qixin_hit, qixin_miss,
        bool(qixin_api.get("_fatal_error")) if isinstance(qixin_api, dict) else False,
        qixin_api.get("_meta", {}).get("stopped_early", False) if isinstance(qixin_api, dict) else False,
    )
    progress.append(
        _progress_event("qixin_api_checks", "completed", collection_started_at, "启信宝白名单 API 查询完成")
    )
    progress.append(
        _progress_event("qcc_mcp", "running", collection_started_at, f"正在采集企查查 MCP 结构化资料，模式={collection_mode}")
    )
    qcc_mcp = _collect_qcc_mcp_evidence(mcp_search_key, collection_mode, qixin_api=qixin_api)
    progress.append(
        _progress_event("qcc_mcp", "completed", collection_started_at, "企查查 MCP 结构化资料采集完成")
    )
    progress.append(
        _progress_event("triggered_collection", "running", collection_started_at, "正在判断是否需要按风险/证据缺口补查重字段")
    )
    triggered_mcp = _collect_triggered_mcp_evidence(mcp_search_key, qcc_mcp, search_evidence, collection_mode, qixin_api=qixin_api)
    progress.append(
        _progress_event(
            "triggered_collection",
            "completed",
            collection_started_at,
            f"按需补查完成：{','.join(triggered_mcp.get('_meta', {}).get('triggered_sections', [])) or '未触发'}",
        )
    )
    progress.append(
        _progress_event("normalize_evidence", "running", collection_started_at, "正在压缩证据字段并整理报告可复用数据")
    )
    search_evidence = _truncate_evidence_value(search_evidence)
    qixin_api = _truncate_evidence_value(qixin_api)
    qcc_mcp = _truncate_evidence_value(qcc_mcp)
    triggered_mcp = _truncate_evidence_value(triggered_mcp)
    evidence_summary = _truncate_evidence_value(
        _build_evidence_summary(identity, qixin_api, search_evidence, qcc_mcp, triggered_mcp, collection_mode),
        max_chars=1200,
    )
    qcc_data_json = _truncate_evidence_value(_build_qcc_data_json(qcc_mcp, qixin_api, triggered_mcp))
    collection_diagnostics = _build_collection_diagnostics(
        qixin_api, qcc_mcp, triggered_mcp, search_evidence, qcc_data_json, collection_mode,
    )
    logger.info(
        "Evidence collection completed: total_elapsed=%.1fs, qixin_hit=%d, qixin_miss=%d, "
        "mcp_groups=%s, missing_fields=%d, needs_review=%s",
        time.monotonic() - collection_started_at,
        collection_diagnostics.get("qixin", {}).get("hit_count", 0),
        collection_diagnostics.get("qixin", {}).get("miss_count", 0),
        collection_diagnostics.get("qcc_mcp", {}).get("collected_groups", []),
        collection_diagnostics.get("missing_or_unknown_fields_count", 0),
        collection_diagnostics.get("needs_human_review", False),
    )
    progress.append(
        _progress_event("normalize_evidence", "completed", collection_started_at, "证据整理完成，AI 可进入评分和报告生成")
    )

    result = {
        "identity": identity,
        "collection_progress": progress,
        "collection_policy": {
            "mode": collection_mode,
            "qixin_allowed_api_ids": "1.41/1.31/79.14/55.2/22.1/61.1/5.5/17.5/66.1/85.71/32.1/1.55/56.1/51.1/63.2/20.1/20.3/26.1/34.1/25.1",
            "qixin_search_key": api_search_key,
            "available_modes": "quick=主体+核心风险；standard=默认轻量评估；deep=全量KYB尽调。",
            "public_search_key": enterprise_name,
            "subject_confirmation_priority": "启信宝 API 1.41 工商照面优先确认企业名称和统一社会信用代码；未命中时回退到企查查 MCP 工商登记，再使用 Coze/公开搜索候选确认。",
            "qixin_api_checks": "主体确认后固定查询启信宝白名单 API；1.41 优先使用统一社会信用代码，其余接口使用确认后的企业全称。",
            "qcc_mcp_search_key": mcp_search_key,
            "qcc_mcp_key_rule": "企查查 MCP 的工商登记可在启信宝 API 1.41 未命中时参与主体确认回退；其余 MCP 只在启信宝未覆盖字段、缺失核心风险、深度尽调或触发补查时使用。已取得统一社会信用代码时，MCP 查询优先使用统一社会信用代码。若未配置 Key 或当天额度耗尽，直接跳过 MCP。",
            "performance_guard": f"默认 standard 不再全量采集历史/税务环保/资产负担/司法详情；单字段证据超过 {EVIDENCE_FIELD_MAX_CHARS} 字符会截断，避免 Coze 运行阶段长时间卡顿。",
            "triggered_collection": triggered_mcp.get("_meta", {}) if isinstance(triggered_mcp, dict) else {},
            "ai_role": "AI只负责基于 evidence_json 解读、评分、生成 scoring_json 和报告。",
        },
        "evidence_summary": evidence_summary,
        "search_evidence": search_evidence,
        "qixin_api": qixin_api,
        "qcc_mcp": qcc_mcp,
        "triggered_mcp": triggered_mcp,
        "qcc_data_json": qcc_data_json,
        "collection_diagnostics": collection_diagnostics,
        "required_scoring_sections": [
            "enterprise_profile",
            "subject_verification",
            "green_power_fit",
            "performance_capability",
            "related_party_risk",
            "supplementary_data_collection",
            "data_source_summary",
            "financial_assessment_notes",
            "missing_financial_fields",
            "action_recommendation",
        ],
    }
    return json.dumps(result, ensure_ascii=False)
