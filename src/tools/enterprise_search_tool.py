"""企业信息搜索工具 - 通过联网搜索获取企业公开信息（免费渠道）"""

import logging
from typing import Any
from langchain.tools import tool
from coze_coding_dev_sdk import SearchClient
from coze_coding_utils.runtime_ctx.context import new_context
from coze_coding_utils.log.write_log import request_context

logger = logging.getLogger(__name__)

SEARCH_PROFILES: dict[str, dict[str, Any]] = {
    "industry": {
        "search_type": "web_summary",
        "count": 6,
        "need_summary": True,
        "need_content": False,
        "need_url": True,
        "sites": "stats.gov.cn;gov.cn;ndrc.gov.cn;nea.gov.cn",
        "block_hosts": "toutiao.com;sohu.com;163.com",
        "time_range": "1y",
    },
    "basic": {
        "search_type": "web",
        "count": 6,
        "need_summary": True,
        "need_content": False,
        "need_url": True,
        "sites": None,
        "block_hosts": "b2b.baidu.com;huangye88.com;qy6.com",
        "time_range": None,
    },
    "risk": {
        "search_type": "web",
        "count": 8,
        "need_summary": True,
        "need_content": False,
        "need_url": True,
        "sites": "gsxt.gov.cn;court.gov.cn;chinatax.gov.cn;mee.gov.cn",
        "block_hosts": "toutiao.com;sohu.com;163.com",
        "time_range": "3y",
    },
    "finance": {
        "search_type": "web_summary",
        "count": 6,
        "need_summary": True,
        "need_content": False,
        "need_url": True,
        "sites": "gov.cn;cninfo.com.cn",
        "block_hosts": "toutiao.com;sohu.com;163.com",
        "time_range": "2y",
    },
    "development": {
        "search_type": "web_summary",
        "count": 8,
        "need_summary": True,
        "need_content": True,
        "need_url": True,
        "sites": "gov.cn;cninfo.com.cn",
        "block_hosts": "toutiao.com;sohu.com;163.com",
        "time_range": "2y",
    },
    "gsxt_official": {
        "search_type": "web",
        "count": 4,
        "need_summary": False,
        "need_content": False,
        "need_url": True,
        "sites": "gsxt.gov.cn",
        "block_hosts": None,
        "time_range": None,
    },
    "gsxt_risk": {
        "search_type": "web",
        "count": 4,
        "need_summary": True,
        "need_content": False,
        "need_url": True,
        "sites": "gov.cn",
        "block_hosts": "toutiao.com;sohu.com;163.com",
        "time_range": "3y",
    },
}


def _profile_value(profile_name: str, key: str, default: Any = None) -> Any:
    return SEARCH_PROFILES.get(profile_name, {}).get(key, default)


def _search_client_call(client: SearchClient, query: str, profile_name: str, count: int | None = None):
    search_type = _profile_value(profile_name, "search_type", "web")
    final_count = count if count is not None else _profile_value(profile_name, "count", 10)
    kwargs = {
        "query": query,
        "search_type": search_type,
        "count": final_count,
        "need_summary": _profile_value(profile_name, "need_summary", True),
        "need_content": _profile_value(profile_name, "need_content", False),
        "need_url": _profile_value(profile_name, "need_url", False),
        "sites": _profile_value(profile_name, "sites"),
        "block_hosts": _profile_value(profile_name, "block_hosts"),
        "time_range": _profile_value(profile_name, "time_range"),
    }
    return client.search(**kwargs)


def _structured_item_dict(item, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "title": item.title,
        "site_name": item.site_name,
        "url": item.url,
        "snippet": item.snippet,
        "summary": item.summary,
        "publish_time": item.publish_time,
        "rank_score": item.rank_score,
        "auth_info_level": item.auth_info_level,
        "auth_info_des": item.auth_info_des,
    }
    if include_content:
        payload["content"] = item.content
    return payload


def _format_structured_search_result(payload: dict[str, Any]) -> str:
    items = payload.get("items", [])
    if not items:
        return f"未找到与「{payload.get('query', '')}」相关的搜索结果。"

    results = []
    for i, item in enumerate(items, 1):
        result = (
            f"【结果 {i}】{item.get('title') or '无标题'}\n"
            f"来源: {item.get('site_name') or '未知'}\n"
            f"链接: {item.get('url') or '无'}\n"
            f"摘要: {item.get('snippet') or '无'}"
        )
        if item.get("summary"):
            result += f"\nAI总结: {item['summary']}"
        if item.get("publish_time"):
            result += f"\n发布时间: {item['publish_time']}"
        if item.get("auth_info_des"):
            result += f"\n权威度: {item['auth_info_des']}"
        results.append(result)

    top_summary = payload.get("summary")
    if top_summary:
        return f"【搜索总摘要】\n{top_summary}\n\n" + "\n\n".join(results)
    return "\n\n".join(results)


def _do_web_search_structured(query: str, profile_name: str = "basic", count: int | None = None) -> dict[str, Any]:
    """执行联网搜索，返回结构化结果，保留时效、权威度和正文等字段。"""
    logger.info("Web search: query=%s, profile=%s, count=%s", query[:50], profile_name, count)
    ctx = request_context.get() or new_context(method="enterprise_search")
    client = SearchClient(ctx=ctx)
    response = _search_client_call(client, query=query, profile_name=profile_name, count=count)

    include_content = bool(_profile_value(profile_name, "need_content", False))
    items = [_structured_item_dict(item, include_content=include_content) for item in response.web_items]

    official_hits = sum(1 for item in items if "gov.cn" in (item.get("url") or "") or item.get("site_name") == "国家企业信用信息公示系统")
    high_auth_hits = sum(1 for item in items if (item.get("auth_info_level") or 0) >= 3)
    has_content = sum(1 for item in items if item.get("content"))

    if not items:
        logger.info("Web search no results: query=%s", query[:50])
    else:
        logger.info("Web search returned %d results: query=%s", len(items), query[:50])

    return {
        "query": query,
        "profile_name": profile_name,
        "search_type": _profile_value(profile_name, "search_type", "web"),
        "summary": response.summary,
        "items": items,
        "stats": {
            "result_count": len(items),
            "official_hits": official_hits,
            "high_auth_hits": high_auth_hits,
            "content_hits": has_content,
        },
    }


def _do_web_search(query: str, count: int = 10, profile_name: str = "basic") -> str:
    """执行联网搜索的公共逻辑，兼容文本输出。"""
    payload = _do_web_search_structured(query=query, profile_name=profile_name, count=count)
    return _format_structured_search_result(payload)


@tool
def search_industry_info(enterprise_name: str) -> str:
    """搜索企业所属行业的信息，包括行业规模、增速、竞争格局、政策支持、绿电需求和行业壁垒。当需要评估企业所在行业的整体情况时调用此工具。"""
    query = f"{enterprise_name} 所属行业 行业规模 增速 竞争格局 绿电需求 碳足迹 出口 欧盟 日本 韩国 ESG 绿色工厂 绿色供应链 用电量 政策"
    return _do_web_search(query, count=8, profile_name="industry")


@tool
def search_enterprise_basic(enterprise_name: str) -> str:
    """搜索企业的工商注册基本信息，包括企业性质、成立时间、注册资本、法定代表人、经营范围等。当需要了解企业基础工商数据时调用此工具。"""
    query = f"{enterprise_name} 工商信息 统一社会信用代码 成立时间 注册资本 法定代表人 经营范围 企业性质 外资 台资 港澳台投资 母公司 实际控制人 最终受益人 股权穿透 企查查"
    return _do_web_search(query, count=8, profile_name="basic")


@tool
def search_enterprise_risk(enterprise_name: str) -> str:
    """搜索企业的风险和信用信息，包括失信情况、行政处罚、经营异常、法律诉讼、信用评级。当需要评估企业风险和信用时调用此工具。"""
    query = f"{enterprise_name} 失信 行政处罚 经营异常 法律诉讼 信用评级 被执行 股权出质 动产抵押 欠税 环保处罚 安全生产处罚 税务处罚 产品质量处罚 劳动纠纷"
    return _do_web_search(query, count=8, profile_name="risk")


@tool
def search_enterprise_finance(enterprise_name: str) -> str:
    """搜索企业的财务数据，包括年营收、资产规模、净利润、现金流、资产负债率等。当需要评估企业财务状况时调用此工具。"""
    query = f"{enterprise_name} 年营收 营业收入 年产值 销售额 总投资 固定资产投资 资产规模 净利润 现金流 资产负债率 年报 参保人数 纳税证明 融资 担保"
    return _do_web_search(query, count=8, profile_name="finance")


@tool
def search_enterprise_development(enterprise_name: str) -> str:
    """搜索企业的发展动态、核心客户、对外投资控股、知识产权和技术实力。当需要了解企业发展能力和技术实力时调用此工具。"""
    query = f"{enterprise_name} 发展动态 核心客户 核心供应商 生产基地 厂房 冷链 仓储 产能 招投标 中标 出口 龙头企业 创新型中小企业 高成长性企业 绿色工厂 ISO HACCP 对外投资 知识产权 专利 技术实力"
    return _do_web_search(query, count=8, profile_name="development")


@tool
def search_gsxt_info(enterprise_name: str) -> str:
    """搜索国家企业信用信息公示系统(gsxt.gov.cn)上的企业官方数据，包括行政处罚、经营异常名录、严重违法失信、行政许可等权威信息。这是官方最权威的数据源，在评估企业信用红线（失信、经营异常、行政处罚）时必须调用此工具进行核实。"""
    query = f"site:gsxt.gov.cn {enterprise_name}"
    result1 = _do_web_search(query, count=5, profile_name="gsxt_official")
    # 补充搜索：企业+经营异常+行政处罚（不限定站点，扩大覆盖）
    query2 = f"{enterprise_name} 行政处罚 经营异常 严重违法失信 国家企业信用信息公示系统"
    result2 = _do_web_search(query2, count=5, profile_name="gsxt_risk")
    return f"【gsxt.gov.cn 官方数据】\n{result1}\n\n【补充搜索-信用监管信息】\n{result2}"
