"""企业信息搜索工具 - 通过联网搜索获取企业公开信息（免费渠道）"""

import logging
from langchain.tools import tool
from coze_coding_dev_sdk import SearchClient
from coze_coding_utils.runtime_ctx.context import new_context
from coze_coding_utils.log.write_log import request_context

logger = logging.getLogger(__name__)


def _do_web_search(query: str, count: int = 10) -> str:
    """执行联网搜索的公共逻辑"""
    logger.info("Web search: query=%s, count=%d", query[:50], count)
    ctx = request_context.get() or new_context(method="enterprise_search")
    client = SearchClient(ctx=ctx)
    response = client.web_search(query=query, count=count, need_summary=True)

    if not response.web_items:
        logger.info("Web search no results: query=%s", query[:50])
        return f"未找到与「{query}」相关的搜索结果。"

    logger.info("Web search returned %d results: query=%s", len(response.web_items), query[:50])

    results = []
    for i, item in enumerate(response.web_items, 1):
        result = f"【结果 {i}】{item.title}\n来源: {item.site_name or '未知'}\n链接: {item.url or '无'}\n摘要: {item.snippet or '无'}"
        if item.summary:
            result += f"\nAI总结: {item.summary}"
        results.append(result)

    return "\n\n".join(results)


@tool
def search_industry_info(enterprise_name: str) -> str:
    """搜索企业所属行业的信息，包括行业规模、增速、竞争格局、政策支持、绿电需求和行业壁垒。当需要评估企业所在行业的整体情况时调用此工具。"""
    query = f"{enterprise_name} 所属行业 行业规模 增速 竞争格局 绿电需求 碳足迹 出口 欧盟 日本 韩国 ESG 绿色工厂 绿色供应链 用电量 政策"
    return _do_web_search(query, count=8)


@tool
def search_enterprise_basic(enterprise_name: str) -> str:
    """搜索企业的工商注册基本信息，包括企业性质、成立时间、注册资本、法定代表人、经营范围等。当需要了解企业基础工商数据时调用此工具。"""
    query = f"{enterprise_name} 工商信息 统一社会信用代码 成立时间 注册资本 法定代表人 经营范围 企业性质 外资 台资 港澳台投资 母公司 实际控制人 最终受益人 股权穿透 企查查"
    return _do_web_search(query, count=8)


@tool
def search_enterprise_risk(enterprise_name: str) -> str:
    """搜索企业的风险和信用信息，包括失信情况、行政处罚、经营异常、法律诉讼、信用评级。当需要评估企业风险和信用时调用此工具。"""
    query = f"{enterprise_name} 失信 行政处罚 经营异常 法律诉讼 信用评级 被执行 股权出质 动产抵押 欠税 环保处罚 安全生产处罚 税务处罚 产品质量处罚 劳动纠纷"
    return _do_web_search(query, count=8)


@tool
def search_enterprise_finance(enterprise_name: str) -> str:
    """搜索企业的财务数据，包括年营收、资产规模、净利润、现金流、资产负债率等。当需要评估企业财务状况时调用此工具。"""
    query = f"{enterprise_name} 年营收 营业收入 年产值 销售额 总投资 固定资产投资 资产规模 净利润 现金流 资产负债率 年报 参保人数 纳税证明 融资 担保"
    return _do_web_search(query, count=8)


@tool
def search_enterprise_development(enterprise_name: str) -> str:
    """搜索企业的发展动态、核心客户、对外投资控股、知识产权和技术实力。当需要了解企业发展能力和技术实力时调用此工具。"""
    query = f"{enterprise_name} 发展动态 核心客户 核心供应商 生产基地 厂房 冷链 仓储 产能 招投标 中标 出口 龙头企业 创新型中小企业 高成长性企业 绿色工厂 ISO HACCP 对外投资 知识产权 专利 技术实力"
    return _do_web_search(query, count=8)


@tool
def search_gsxt_info(enterprise_name: str) -> str:
    """搜索国家企业信用信息公示系统(gsxt.gov.cn)上的企业官方数据，包括行政处罚、经营异常名录、严重违法失信、行政许可等权威信息。这是官方最权威的数据源，在评估企业信用红线（失信、经营异常、行政处罚）时必须调用此工具进行核实。"""
    query = f"site:gsxt.gov.cn {enterprise_name}"
    result1 = _do_web_search(query, count=5)
    # 补充搜索：企业+经营异常+行政处罚（不限定站点，扩大覆盖）
    query2 = f"{enterprise_name} 行政处罚 经营异常 严重违法失信 国家企业信用信息公示系统"
    result2 = _do_web_search(query2, count=5)
    return f"【gsxt.gov.cn 官方数据】\n{result1}\n\n【补充搜索-信用监管信息】\n{result2}"
