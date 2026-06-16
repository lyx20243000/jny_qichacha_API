"""报告生成工具 - 由 Agent 调用，完成评分计算和 PDF 报告生成
严格复用 collect_enterprise_evidence 已采集的数据，报告阶段不再主动查询 MCP，避免数据漂移和额度重复消耗。"""

import json
import logging
from langchain.tools import tool
from services.scoring_engine import evaluate
from services.report_generator import generate_pdf_report
from services.qcc_mcp_client import (
    is_qcc_mcp_available,
    query_company_registration,
    query_shareholder_info,
    query_dishonest_info,
    query_administrative_penalty,
    query_business_exception,
    query_high_consumption_restriction,
    query_patent_info,
    query_credit_evaluation,
    query_qualifications,
    query_external_investments,
    query_financial_data,
)

logger = logging.getLogger(__name__)

QCC_SAFE_KEYWORDS = ("未发现", "未查询到", "无相关", "暂无", "安全", "未查到", "无数据")
QCC_UNKNOWN_KEYWORDS = ("查询失败", "错误:", "超时", "异常", "失败", "积分余额不足", "额度不足")
QCC_QUOTA_KEYWORDS = ("code=300008", "积分余额不足", "余额不足")
QCC_QUOTA_MESSAGE = "企查查MCP积分余额不足，报告阶段已停止自动补查MCP；本次报告仅基于已取得的免费搜索、公开信息和已传入数据生成。"
GENERIC_REPORT_NAME_KEYWORDS = ("完整分析报告", "分析报告PDF", "报告PDF", "PDF报告")


def _is_qcc_quota_error(value) -> bool:
    if not isinstance(value, str):
        return False
    return any(keyword in value for keyword in QCC_QUOTA_KEYWORDS)


def _try_parse_json_text(value):
    """企查查 MCP 有时返回 JSON 字符串，这里统一转成结构化对象。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _is_empty_record_payload(value) -> bool:
    """识别企查查返回的空记录结构，避免误判为风险或展示原始 JSON。"""
    value = _try_parse_json_text(value)
    if isinstance(value, list):
        return len(value) == 0
    if not isinstance(value, dict):
        return False

    for key in ("count", "total", "totalCount", "total_count", "num"):
        if key in value:
            try:
                if int(value.get(key) or 0) == 0:
                    return True
            except (TypeError, ValueError):
                pass

    for key in ("records", "items", "list", "data", "result"):
        records = value.get(key)
        if isinstance(records, list) and len(records) == 0:
            return True
        if isinstance(records, dict) and _is_empty_record_payload(records):
            return True

    return False


def _is_error_text(value) -> bool:
    if not isinstance(value, str):
        return False
    return any(kw in value[:300] for kw in QCC_UNKNOWN_KEYWORDS)


def _compact_text(value, limit: int = 220) -> str:
    value = _try_parse_json_text(value)
    if isinstance(value, (dict, list)):
        value = _format_qcc_value(value, max_items=4)
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _format_qcc_value(value, max_items: int = 6) -> str:
    """将结构化企查查数据转成面向客户的简明文本，不直接暴露 JSON。"""
    value = _try_parse_json_text(value)
    if value in (None, ""):
        return ""
    if _is_empty_record_payload(value):
        return "未查询到相关记录。"
    if isinstance(value, str):
        if _is_error_text(value):
            return ""
        return value.strip()
    if isinstance(value, list):
        rows = []
        for item in value[:max_items]:
            rows.append(_compact_text(item))
        suffix = f"（仅展示前{max_items}条）" if len(value) > max_items else ""
        return "；".join(rows) + suffix
    if not isinstance(value, dict):
        return str(value)

    lines = []
    count = value.get("count") or value.get("total") or value.get("totalCount") or value.get("total_count")
    if count not in (None, "", 0, "0"):
        lines.append(f"共查询到 {count} 条相关记录。")

    # 优先展示常见中文/英文业务字段。
    preferred_keys = [
        "企业名称", "统一社会信用代码", "登记状态", "法定代表人", "注册资本", "成立日期",
        "所属地区", "国标行业", "企业类型", "经营范围", "公司简介",
        "name", "creditCode", "status", "legalPerson", "registeredCapital",
        "establishDate", "industry", "companyType", "businessScope", "description",
    ]
    for key in preferred_keys:
        if key in value and value[key] not in (None, "", [], {}):
            lines.append(f"- {key}: {_compact_text(value[key], 180)}")

    # 展示列表型明细，但只取少量，避免报告膨胀。
    for key in ("records", "items", "list", "data", "result"):
        records = value.get(key)
        if isinstance(records, list) and records:
            for i, item in enumerate(records[:max_items], 1):
                lines.append(f"- 记录{i}: {_compact_text(item, 220)}")
            if len(records) > max_items:
                lines.append(f"- 另有 {len(records) - max_items} 条记录未展示。")
            break

    if lines:
        return "\n".join(lines)

    # 兜底：挑选少量简单字段输出，仍不暴露 JSON 结构。
    for key, val in list(value.items())[:max_items]:
        if val not in (None, "", [], {}):
            lines.append(f"- {key}: {_compact_text(val, 180)}")

    return "\n".join(lines)


def _iter_values(value):
    """递归遍历结构化数据，便于从不同包装中提取企业字段。"""
    value = _try_parse_json_text(value)
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_values(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_values(item)


def _first_nested_value(value, keys: tuple[str, ...]) -> str:
    for item in _iter_values(value):
        if not isinstance(item, dict):
            continue
        for key in keys:
            field_value = item.get(key)
            if field_value not in (None, "", [], {}):
                return str(field_value).strip()
    return ""


def _extract_social_credit_code_from_qcc(qcc_data: dict) -> str:
    """从 Agent 传入或报告阶段查询到的企查查数据中提取统一社会信用代码。"""
    keys = (
        "统一社会信用代码",
        "社会信用代码",
        "信用代码",
        "统一信用代码",
        "CreditCode",
        "creditCode",
        "CreditNo",
        "creditNo",
        "No",
        "RegNo",
    )
    for preferred_key in ("registration", "basic", "工商登记", "工商登记信息"):
        value = qcc_data.get(preferred_key)
        code = _first_nested_value(value, keys)
        if code:
            return code
    return _first_nested_value(qcc_data, keys)


def _ensure_enterprise_profile(scoring_result: dict, enterprise_name: str, qcc_data: dict) -> None:
    """确保报告头部能展示企业名称和统一社会信用代码。"""
    profile = scoring_result.get("enterprise_profile") or {}
    if isinstance(profile, str):
        profile = {"企业简介": profile}
    if not isinstance(profile, dict):
        profile = {}

    profile.setdefault("企业名称", enterprise_name)

    existing_code = (
        profile.get("统一社会信用代码")
        or profile.get("社会信用代码")
        or scoring_result.get("统一社会信用代码")
        or scoring_result.get("social_credit_code")
        or scoring_result.get("credit_code")
    )
    if existing_code:
        profile["统一社会信用代码"] = str(existing_code).strip()
    elif qcc_data:
        code = _extract_social_credit_code_from_qcc(qcc_data)
        if code:
            profile["统一社会信用代码"] = code

    scoring_result["enterprise_profile"] = profile


def _looks_like_generic_report_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    return any(keyword in text for keyword in GENERIC_REPORT_NAME_KEYWORDS)


def _resolve_report_enterprise_name(scoring_result: dict, fallback_name: str) -> str:
    """优先使用评分 JSON 中的真实企业名，避免把泛化报告名当作企业名。"""
    profile = scoring_result.get("enterprise_profile") or {}
    if isinstance(profile, dict):
        for key in ("企业名称", "company_name", "enterprise_name", "name"):
            value = str(profile.get(key) or "").strip()
            if value and not _looks_like_generic_report_name(value):
                return value

    fallback = str(fallback_name or "").strip()
    return fallback if fallback else "企业"


def _fetch_qcc_data(enterprise_name: str) -> dict:
    """[已废弃] 自动调用企查查 MCP 获取核心结构化数据
    
    ⚠️ 该函数已不再被报告阶段调用，原因：
    1. 数据漂移：报告阶段查到的数据可能和 collect_enterprise_evidence 阶段不一致
    2. 额度消耗：同一次分析会消耗两次 MCP 额度
    3. 策略不一致：报告阶段应该消费固定采集结果
    
    保留该函数是为了可能的未来用途（如独立调试），但不应在正常流程中调用。
    """
    qcc_data = {}
    if not is_qcc_mcp_available():
        qcc_data["mcp_status"] = "企查查MCP未配置可用 Key 或当天额度已耗尽，报告阶段跳过自动补查。"
        return qcc_data
    
    # 必查：工商基础信息
    try:
        qcc_data["registration"] = query_company_registration(enterprise_name)
        if _is_qcc_quota_error(qcc_data["registration"]):
            qcc_data["mcp_status"] = QCC_QUOTA_MESSAGE
            return qcc_data
    except Exception as e:
        logger.warning(f"QCC registration query failed: {e}")
        qcc_data["registration"] = "查询失败"
    
    # 必查：红线信息（失信、行政处罚、经营异常、限高）
    for name, func in [
        ("dishonest", query_dishonest_info),
        ("admin_penalty", query_administrative_penalty),
        ("business_exception", query_business_exception),
        ("high_consumption", query_high_consumption_restriction),
    ]:
        try:
            qcc_data[name] = func(enterprise_name)
            if _is_qcc_quota_error(qcc_data[name]):
                qcc_data[name] = QCC_QUOTA_MESSAGE
                qcc_data["mcp_status"] = QCC_QUOTA_MESSAGE
                return qcc_data
        except Exception as e:
            logger.warning(f"QCC {name} query failed: {e}")
            qcc_data[name] = "查询失败"
    
    # 重要：知识产权、资质、信用评价
    for name, func in [
        ("patent", query_patent_info),
        ("qualifications", query_qualifications),
        ("credit_eval", query_credit_evaluation),
        ("shareholder", query_shareholder_info),
        ("investment", query_external_investments),
        ("financial", query_financial_data),
    ]:
        try:
            qcc_data[name] = func(enterprise_name)
            if _is_qcc_quota_error(qcc_data[name]):
                qcc_data[name] = QCC_QUOTA_MESSAGE
                qcc_data["mcp_status"] = QCC_QUOTA_MESSAGE
                return qcc_data
        except Exception as e:
            logger.warning(f"QCC {name} query failed: {e}")
            qcc_data[name] = "查询失败"
    
    return qcc_data


def _qcc_data_to_summary(qcc_data: dict) -> str:
    """将启信宝主数据源和企查查 MCP 补充数据整理为文本摘要，供报告引用。"""
    parts = ["【结构化数据：启信宝 API 主数据源 + 企查查 MCP 补充】"]
    
    field_names = {
        "registration": "工商登记信息",
        "company_profile": "企业简介",
        "actual_controller": "实际控制人",
        "listing_info": "上市信息",
        "key_personnel": "主要人员",
        "dishonest": "失信被执行人信息",
        "qixin_basic": "启信宝 API 1.41 工商照面",
        "qixin_fuzzy_search": "启信宝 API 1.31 企业模糊搜索",
        "qixin_tech_enterprise": "启信宝 API 79.14 科技型企业",
        "qixin_equity_penetration": "启信宝 API 55.2 股权穿透",
        "qixin_qualification": "启信宝 API 22.1 企业资质",
        "qixin_land_purchase": "启信宝 API 61.1 购地信息",
        "qixin_case_relation": "启信宝 API 85.71 案件串联",
        "qixin_real_estate_admin_penalty": "启信宝 API 32.1 地产行政处罚",
        "admin_penalty": "行政处罚记录",
        "business_exception": "经营异常名录",
        "serious_violation": "严重违法失信信息",
        "high_consumption": "限制高消费信息",
        "risk_scan": "风险扫描",
        "case_filing": "立案信息",
        "executed_person": "被执行人信息",
        "judicial_documents": "裁判文书详情",
        "court_announcement": "法院公告信息",
        "final_case": "终本案件信息",
        "environmental_penalty": "环保处罚信息",
        "tax_abnormal": "税务非正常户信息",
        "tax_arrears": "欠税公告信息",
        "tax_violation": "税收违法信息",
        "equity_pledge": "股权出质信息",
        "equity_freeze": "股权冻结信息",
        "chattel_mortgage": "动产抵押信息",
        "land_mortgage": "土地抵押信息",
        "history_risk": "历史风险信息",
        "history_dishonest": "历史失信信息",
        "history_executed_person": "历史被执行人信息",
        "history_business_exception": "历史经营异常信息",
        "history_admin_penalty": "历史行政处罚信息",
        "patent": "专利信息",
        "trademark": "商标信息",
        "software_copyright": "软件著作权",
        "bidding": "招投标记录",
        "qualifications": "资质认证",
        "honor": "荣誉信息",
        "recruitment": "招聘信息",
        "administrative_license": "行政许可信息",
        "taxpayer_qualification": "纳税人资质信息",
        "product_check": "产品抽查信息",
        "state_owned_land_transfer": "国有土地受让信息",
        "credit_eval": "信用评价",
        "shareholder": "股东信息",
        "investment": "对外投资",
        "financial": "财务数据",
        "news_sentiment": "新闻舆情",
        "mcp_status": "企查查MCP状态",
    }
    
    for key, label in field_names.items():
        data = qcc_data.get(key, "")
        formatted = _format_qcc_value(data)
        if formatted:
            parts.append(f"\n### {label}\n{formatted}")
    
    return "\n".join(parts)


def _parse_qcc_data_json(qcc_data_json: str) -> dict:
    """解析 Agent 传入的企查查数据 JSON；为空时返回空字典。"""
    if not qcc_data_json or not qcc_data_json.strip():
        return {}

    parsed = json.loads(qcc_data_json)
    if not isinstance(parsed, dict):
        raise ValueError("qcc_data_json 必须是 JSON object")
    return parsed


def _qcc_risk_status(text: str) -> str:
    """判断企查查风险文本状态：safe/risk/unknown。"""
    parsed = _try_parse_json_text(text)

    if _is_empty_record_payload(parsed):
        return "safe"

    if isinstance(parsed, dict):
        for key in ("records", "items", "list", "data", "result"):
            records = parsed.get(key)
            if isinstance(records, list) and records:
                return "risk"
        count = parsed.get("count") or parsed.get("total") or parsed.get("totalCount") or parsed.get("total_count")
        if count not in (None, "", 0, "0"):
            try:
                return "risk" if int(count) > 0 else "safe"
            except (TypeError, ValueError):
                pass
    elif isinstance(parsed, list):
        return "risk" if parsed else "safe"

    if not isinstance(parsed, str):
        text = _format_qcc_value(parsed)
    else:
        text = parsed

    if not text or not text.strip():
        return "unknown"

    head = text[:300]
    if any(kw in head for kw in QCC_UNKNOWN_KEYWORDS):
        return "unknown"
    if any(kw in head for kw in QCC_SAFE_KEYWORDS):
        return "safe"
    return "risk"


def _append_triggered_risks_to_recommendation(scoring_result: dict, triggered: list[dict]) -> None:
    """把触发风险项并入行动建议里的重点关注风险。"""
    if not triggered:
        return

    recommendation = scoring_result.setdefault("action_recommendation", {})
    if isinstance(recommendation, str):
        recommendation = {"cooperation_advice": recommendation}
        scoring_result["action_recommendation"] = recommendation
    if not isinstance(recommendation, dict):
        return

    key_risks = recommendation.get("key_risks") or []
    if isinstance(key_risks, str):
        key_risks = [key_risks]

    seen = {str(item) for item in key_risks if item}
    for item in triggered:
        description = item.get("description") or item.get("field") or "风险项"
        value = item.get("value")
        if value not in (None, ""):
            text = f"{description}：当前值为 {value}，建议结合权威数据源进行人工复核。"
        else:
            text = f"{description}：建议结合权威数据源进行人工复核。"
        if text not in seen:
            key_risks.append(text)
            seen.add(text)

    recommendation["key_risks"] = key_risks


def _qcc_status_label(value) -> str:
    """把企查查专项数据转为报告可读状态。"""
    status = _qcc_risk_status(_format_qcc_value(value))
    if status == "safe":
        return "未查询到相关记录"
    if status == "risk":
        return "查询到相关记录，建议人工复核详情"
    return "未获取或查询失败，需复核"


def _ensure_kyb_review_sections(scoring_result: dict, qcc_data: dict) -> None:
    """根据 qcc_data_json 兜底生成 KYB 专项核查模块，避免采集后报告不展示。"""
    if not qcc_data:
        return

    scoring_result.setdefault(
        "legal_litigation_review",
        {
            "失信被执行人": _qcc_status_label(qcc_data.get("dishonest", "")),
            "被执行人": _qcc_status_label(qcc_data.get("executed_person", "")),
            "限制高消费": _qcc_status_label(qcc_data.get("high_consumption", "")),
            "裁判文书": _qcc_status_label(qcc_data.get("judicial_documents", "")),
            "法院公告": _qcc_status_label(qcc_data.get("court_announcement", "")),
            "终本案件": _qcc_status_label(qcc_data.get("final_case", "")),
        },
    )
    scoring_result.setdefault(
        "administrative_operation_risk_review",
        {
            "行政处罚": _qcc_status_label(qcc_data.get("admin_penalty", "")),
            "经营异常": _qcc_status_label(qcc_data.get("business_exception", "")),
            "严重违法": _qcc_status_label(qcc_data.get("serious_violation", "")),
        },
    )
    scoring_result.setdefault(
        "tax_environment_risk_review",
        {
            "环保处罚": _qcc_status_label(qcc_data.get("environmental_penalty", "")),
            "税务非正常户": _qcc_status_label(qcc_data.get("tax_abnormal", "")),
            "欠税公告": _qcc_status_label(qcc_data.get("tax_arrears", "")),
            "税收违法": _qcc_status_label(qcc_data.get("tax_violation", "")),
        },
    )
    scoring_result.setdefault(
        "asset_equity_encumbrance_review",
        {
            "股权出质": _qcc_status_label(qcc_data.get("equity_pledge", "")),
            "股权冻结": _qcc_status_label(qcc_data.get("equity_freeze", "")),
            "动产抵押": _qcc_status_label(qcc_data.get("chattel_mortgage", "")),
            "土地抵押": _qcc_status_label(qcc_data.get("land_mortgage", "")),
        },
    )
    history_risk = qcc_data.get("history_risk", {})
    if not isinstance(history_risk, dict):
        history_risk = {"历史风险": history_risk}
    scoring_result.setdefault(
        "history_risk_review",
        {key: _qcc_status_label(value) for key, value in history_risk.items()},
    )
    scoring_result.setdefault(
        "operation_qualification_land_review",
        {
            "行政许可": _qcc_status_label(qcc_data.get("administrative_license", "")),
            "纳税人资质": _qcc_status_label(qcc_data.get("taxpayer_qualification", "")),
            "产品抽查": _qcc_status_label(qcc_data.get("product_check", "")),
            "国有土地受让": _qcc_status_label(qcc_data.get("state_owned_land_transfer", "")),
        },
    )

    risk_sections = [
        scoring_result.get("legal_litigation_review", {}),
        scoring_result.get("administrative_operation_risk_review", {}),
        scoring_result.get("tax_environment_risk_review", {}),
        scoring_result.get("asset_equity_encumbrance_review", {}),
        scoring_result.get("history_risk_review", {}),
        scoring_result.get("operation_qualification_land_review", {}),
    ]
    risk_hits = []
    unknowns = []
    for section in risk_sections:
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            text = str(value)
            if "查询到相关记录" in text or "有记录" in text or "存在" in text:
                risk_hits.append(str(key))
            elif "未获取" in text or "查询失败" in text or "需复核" in text:
                unknowns.append(str(key))
    if risk_hits:
        conclusion = "专项风险发现需重点复核"
        main_risks = risk_hits[:8]
    elif unknowns:
        conclusion = "部分专项风险字段未获取，需补充复核"
        main_risks = []
    else:
        conclusion = "未发现明显专项风险记录"
        main_risks = []
    scoring_result.setdefault(
        "kyb_risk_overview",
        {
            "总体结论": conclusion,
            "主要风险": main_risks or ["未发现明确专项风险记录"],
            "复核建议": unknowns[:8] if unknowns else ["结合业务金额和合作模式决定是否人工复核原始记录"],
        },
    )


@tool
def generate_enterprise_report(enterprise_name: str = "", scoring_json: str = "", qcc_data_json: str = "") -> str:
    """根据评分结果计算加权总分、确定评级并生成 PDF 分析报告。
    优先使用 qcc_data_json 中的启信宝主数据和企查查 MCP 补充数据来验证和修正评分。
    如果未传入 qcc_data_json，工具不会在报告阶段自动回查企查查MCP，而是仅基于 scoring_json 和已传入数据生成报告。

    Args:
        enterprise_name: 企业名称
        scoring_json: 评分结果的JSON字符串，格式为:
            {
                "industry": {"industry_advancement": {"score": 5, "reliability": "green", "basis": "..."}, ...},
                "operation": {...},
                "finance": {...},
                "credit": {...},
                "red_line_data": {"is_dishonest": false, "major_penalty_count": 0, "has_abnormal_operation": false, "legal_person_restricted": false},
                "industry_summary": "...",
                "operation_summary": "...",
                "finance_summary": "...",
                "credit_summary": "...",
                "overall_summary": "..."
            }
        qcc_data_json: 可选，Agent 已获取的结构化数据 JSON 字符串，字段建议包括:
            registration, dishonest, admin_penalty, business_exception, high_consumption,
            patent, qualifications, credit_eval, shareholder, investment, financial

    Returns:
        包含评级、总分和报告链接的结果文本
    """
    if not enterprise_name or not scoring_json:
        return (
            "报告生成参数缺失：必须同时传入 enterprise_name 和 scoring_json。"
            "请先完成企业主体确认、信息收集和评分JSON构建，再调用 generate_enterprise_report；"
            "不要使用空参数调用本工具。"
        )

    try:
        scoring_result = json.loads(scoring_json)
    except json.JSONDecodeError as e:
        return f"评分结果JSON解析失败: {str(e)}"

    # 0. 严格复用 collect_enterprise_evidence 已采集的 MCP 数据，报告阶段不再主动查询
    # 避免数据漂移和 MCP 额度重复消耗
    qcc_data = {}
    qcc_summary = ""
    if qcc_data_json:
        try:
            qcc_data = _parse_qcc_data_json(qcc_data_json)
            qcc_summary = _qcc_data_to_summary(qcc_data)
            logger.info(f"QCC MCP data reused for {enterprise_name}, fields: {list(qcc_data.keys())}")
        except Exception as e:
            logger.warning(f"QCC MCP data parse failed for {enterprise_name}: {e}")
            qcc_summary = "【企查查MCP数据解析失败，仅基于评分JSON生成报告】"
    else:
        # 报告阶段不再主动查询 MCP，避免数据漂移和额度消耗
        logger.warning(f"QCC MCP data not provided for {enterprise_name}, report will be generated without MCP data. "
                       f"This indicates collect_enterprise_evidence did not pass qcc_data_json.")
        qcc_summary = "【未提供企查查MCP数据，仅基于评分JSON生成报告】"

    if qcc_data:
        _ensure_enterprise_profile(scoring_result, enterprise_name, qcc_data)
        _ensure_kyb_review_sections(scoring_result, qcc_data)

        try:
            dishonest_info = qcc_data.get("dishonest", "")
            if _qcc_risk_status(dishonest_info) == "risk":
                scoring_result.setdefault("red_line_data", {})["is_dishonest"] = True
                logger.info("QCC: detected dishonest status, updating red_line_data")
        except Exception:
            pass

        try:
            penalty_info = qcc_data.get("admin_penalty", "")
            if _qcc_risk_status(penalty_info) == "risk":
                import re
                counts = re.findall(r"共(\d+)条", penalty_info)
                if counts:
                    major_count = min(int(counts[0]), 10)
                    if major_count >= 3:
                        scoring_result.setdefault("red_line_data", {})["major_penalty_count"] = major_count
                        logger.info(f"QCC: detected {major_count} admin penalties, updating red_line_data")
        except Exception:
            pass

        try:
            exception_info = qcc_data.get("business_exception", "")
            if _qcc_risk_status(exception_info) == "risk":
                scoring_result.setdefault("red_line_data", {})["has_abnormal_operation"] = True
                logger.info("QCC: detected business exception, updating red_line_data")
        except Exception:
            pass

        try:
            high_consumption_info = qcc_data.get("high_consumption", "")
            if _qcc_risk_status(high_consumption_info) == "risk":
                scoring_result.setdefault("red_line_data", {})["legal_person_restricted"] = True
                logger.info("QCC: detected high consumption restriction, updating red_line_data")
        except Exception:
            pass
    else:
        _ensure_enterprise_profile(scoring_result, enterprise_name, {})

    report_enterprise_name = _resolve_report_enterprise_name(scoring_result, enterprise_name)

    # 1. 评分引擎计算
    try:
        evaluation_result = evaluate(scoring_result)
    except Exception as e:
        logger.error(f"Scoring engine error: {e}")
        return f"评分计算失败: {str(e)}"

    # 2. 生成 PDF 报告
    summary = scoring_result.get("overall_summary", "")
    try:
        report_url = generate_pdf_report(
            enterprise_name=report_enterprise_name,
            evaluation_result=evaluation_result,
            scoring_result=scoring_result,
            summary=summary,
            qcc_data_summary=qcc_summary,
        )
    except Exception as e:
        logger.error(f"Report generation error: {e}")
        # 报告生成失败时仍返回评分结果
        report_url = "报告生成失败，请稍后重试"

    # 3. 构建返回结果
    final_grade = evaluation_result["final_grade"]
    total_score = evaluation_result["weighted_total_score"]
    triggered = evaluation_result["triggered_red_lines"]
    dimensions = evaluation_result["dimensions"]
    _append_triggered_risks_to_recommendation(scoring_result, triggered)

    result_lines = [
        f"## {report_enterprise_name} 企业分析报告",
        f"",
        f"**企业评级：{final_grade}**",
        f"**加权总分：{total_score}分（满分100分）**",
        f"",
        f"**数据来源：启信宝API + 联网搜索 + 企查查MCP结构化数据 + 国家企业信用信息公示系统**",
        f"",
    ]

    # 各维度得分
    dim_names = {"industry": "行业", "operation": "企业经营", "finance": "财务", "credit": "信用"}
    for dim_key, dim_name in dim_names.items():
        dim_data = dimensions.get(dim_key, {})
        dim_score = dim_data.get("total_score", 0)
        dim_max = dim_data.get("max_score", 0)
        result_lines.append(f"**{dim_name}：{dim_score}/{dim_max}分**")

    result_lines.append("")
    result_lines.append(f"**综合评价：**{summary}")
    result_lines.append("")

    action_recommendation = scoring_result.get("action_recommendation")
    if action_recommendation:
        result_lines.append("**行动建议：**")
        if isinstance(action_recommendation, str):
            result_lines.append(action_recommendation)
        elif isinstance(action_recommendation, dict):
            for label, key in [
                ("综合建议", "conclusion"),
                ("建议动作", "next_action"),
                ("合作建议", "cooperation_advice"),
            ]:
                value = action_recommendation.get(key)
                if value:
                    result_lines.append(f"- {label}: {value}")
            for label, key in [
                ("重点关注风险", "key_risks"),
                ("需补充资料", "required_materials"),
            ]:
                values = action_recommendation.get(key)
                if values:
                    if isinstance(values, str):
                        values = [values]
                    result_lines.append(f"- {label}: {'；'.join(values)}")
        result_lines.append("")

    report_name = f"{report_enterprise_name} 分析报告"
    if str(report_url).startswith(("http://", "https://")):
        result_lines.append(f"📄 [{report_name}]({report_url})")
    else:
        result_lines.append(f"📄 **PDF报告：**{report_url}")

    return "\n".join(result_lines)
