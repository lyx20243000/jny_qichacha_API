"""
企查查 MCP 工具 - 精简聚合版
将24个细分工具合并为6个核心维度工具，降低LLM选择负担
"""
import json
import logging
from langchain.tools import tool
from services.qcc_mcp_client import (
    is_qcc_mcp_available,
    is_qcc_mcp_quota_error,
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


QCC_MCP_QUOTA_MESSAGE = "企查查MCP积分余额不足，本项已跳过；请优先使用Coze搜索/公开搜索，并复用启信宝白名单API已采集结果。"

logger = logging.getLogger(__name__)


def _safe_call(func, search_key: str) -> str:
    """安全调用，捕获异常避免单个查询失败导致整体中断。额度轮换由 MCP 客户端统一维护。"""
    if not is_qcc_mcp_available():
        logger.warning("QCC MCP not available (no key or quota exhausted), skipping: func=%s", func.__name__)
        return QCC_MCP_QUOTA_MESSAGE
    try:
        result = func(search_key)
        if is_qcc_mcp_quota_error(result):
            logger.warning("QCC MCP quota exhausted: func=%s, search_key=%s", func.__name__, search_key[:20])
            return QCC_MCP_QUOTA_MESSAGE
        if not result:
            logger.debug("QCC MCP no data: func=%s, search_key=%s", func.__name__, search_key[:20])
        return result if result else "无数据"
    except Exception as e:
        error = str(e)
        logger.error("QCC MCP call failed: func=%s, search_key=%s, error=%s", func.__name__, search_key[:20], error[:100])
        if is_qcc_mcp_quota_error(error):
            return QCC_MCP_QUOTA_MESSAGE
        return f"查询失败: {error}"


@tool
def qcc_get_basic_info(search_key: str) -> str:
    """【企查查】查询企业基础工商信息。包括：工商登记、股东结构、实际控制人、企业简介、上市信息、主要人员。这是获取企业经营维度评分数据的核心工具。search_key为企业全称或统一社会信用代码。"""
    results = {
        "工商登记": _safe_call(query_company_registration, search_key),
        "股东结构": _safe_call(query_shareholder_info, search_key),
        "实际控制人": _safe_call(query_actual_controller, search_key),
        "企业简介": _safe_call(query_company_profile, search_key),
        "上市信息": _safe_call(query_listing_info, search_key),
        "主要人员": _safe_call(query_key_personnel, search_key),
    }
    return json.dumps(results, ensure_ascii=False)


@tool
def qcc_get_finance_info(search_key: str) -> str:
    """【企查查】查询企业财务与投资信息。包括：核心财务数据、对外投资企业。这是获取财务维度评分数据的核心工具。search_key为企业全称或统一社会信用代码。"""
    results = {
        "财务数据": _safe_call(query_financial_data, search_key),
        "对外投资": _safe_call(query_external_investments, search_key),
    }
    return json.dumps(results, ensure_ascii=False)


@tool
def qcc_get_risk_info(search_key: str) -> str:
    """【企查查】查询企业风险与信用信息（风险必查！）。包括：失信被执行人、行政处罚、经营异常、严重违法失信、限制高消费、风险扫描、立案信息、信用评价。这是获取信用维度评分数据和风险判断的核心工具。search_key为企业全称或统一社会信用代码。"""
    results = {
        "失信被执行人": _safe_call(query_dishonest_info, search_key),
        "行政处罚": _safe_call(query_administrative_penalty, search_key),
        "经营异常": _safe_call(query_business_exception, search_key),
        "严重违法失信": _safe_call(query_serious_violation, search_key),
        "限制高消费": _safe_call(query_high_consumption_restriction, search_key),
        "风险扫描": _safe_call(query_company_risk_scan, search_key),
        "立案信息": _safe_call(query_case_filing_info, search_key),
        "信用评价": _safe_call(query_credit_evaluation, search_key),
    }
    return json.dumps(results, ensure_ascii=False)


@tool
def qcc_get_ip_info(search_key: str) -> str:
    """【企查查】查询企业知识产权信息。包括：专利、商标、软件著作权。这是获取企业经营维度中知识产权/技术实力评分项的核心工具。search_key为企业全称或统一社会信用代码。"""
    results = {
        "专利信息": _safe_call(query_patent_info, search_key),
        "商标信息": _safe_call(query_trademark_info, search_key),
        "软件著作权": _safe_call(query_software_copyright, search_key),
    }
    return json.dumps(results, ensure_ascii=False)


@tool
def qcc_get_operation_info(search_key: str) -> str:
    """【企查查】查询企业经营动态信息。包括：招投标记录、资质认证（高新技术企业、专精特新等）、荣誉信息、招聘信息。这是获取企业经营维度中核心客户/订单和近期发展评分项的核心工具。search_key为企业全称或统一社会信用代码。"""
    results = {
        "招投标记录": _safe_call(query_bidding_info, search_key),
        "资质认证": _safe_call(query_qualifications, search_key),
        "荣誉信息": _safe_call(query_honor_info, search_key),
        "招聘信息": _safe_call(query_recruitment_info, search_key),
    }
    return json.dumps(results, ensure_ascii=False)


@tool
def qcc_get_news_info(search_key: str) -> str:
    """【企查查】查询企业新闻舆情信息。包括近期正面/负面新闻报道。这是辅助行业分析和近期发展评估的工具。search_key为企业全称或统一社会信用代码。"""
    results = {
        "新闻舆情": _safe_call(query_news_sentiment, search_key),
    }
    return json.dumps(results, ensure_ascii=False)


@tool
def qcc_get_extended_risk_info(search_key: str) -> str:
    """【企查查】查询企业专项风险、历史风险、税务环保、资产及股权负担信息。包括：被执行人、裁判文书、法院公告、终本案件、环保处罚、税务非正常户、欠税公告、税收违法、股权出质、股权冻结、动产抵押、土地抵押、历史失信、历史被执行、历史经营异常、历史行政处罚、行政许可、纳税人资质、产品抽查、国有土地受让。search_key优先使用统一社会信用代码。"""
    results = {
        "法律诉讼": {
            "被执行人": _safe_call(query_executed_person, search_key),
            "裁判文书": _safe_call(query_judicial_documents, search_key),
            "法院公告": _safe_call(query_court_announcement, search_key),
            "终本案件": _safe_call(query_final_case, search_key),
        },
        "税务环保风险": {
            "环保处罚": _safe_call(query_environmental_penalty, search_key),
            "税务非正常户": _safe_call(query_tax_abnormal, search_key),
            "欠税公告": _safe_call(query_tax_arrears, search_key),
            "税收违法": _safe_call(query_tax_violation, search_key),
        },
        "资产及股权负担": {
            "股权出质": _safe_call(query_equity_pledge, search_key),
            "股权冻结": _safe_call(query_equity_freeze, search_key),
            "动产抵押": _safe_call(query_chattel_mortgage, search_key),
            "土地抵押": _safe_call(query_land_mortgage, search_key),
        },
        "历史风险": {
            "历史失信": _safe_call(query_history_dishonest, search_key),
            "历史被执行": _safe_call(query_history_executed_person, search_key),
            "历史经营异常": _safe_call(query_history_business_exception, search_key),
            "历史行政处罚": _safe_call(query_history_administrative_penalty, search_key),
        },
        "经营资质与土地": {
            "行政许可": _safe_call(query_administrative_license, search_key),
            "纳税人资质": _safe_call(query_taxpayer_qualification, search_key),
            "产品抽查": _safe_call(query_product_check, search_key),
            "国有土地受让": _safe_call(query_state_owned_land_transfer, search_key),
        },
    }
    return json.dumps(results, ensure_ascii=False)
