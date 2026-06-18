"""CNBizAPI 免费/低成本结构化数据工具。"""

from langchain.tools import tool

from services.cnbizapi_client import query_company_basic, query_company_search


@tool
def cnbizapi_search_company(keyword: str, limit: int = 5) -> str:
    """通过 CNBizAPI 模糊搜索企业候选，返回企业名称和统一社会信用代码等基础字段。"""
    return query_company_search(keyword, limit=limit)


@tool
def cnbizapi_get_company_basic(q: str) -> str:
    """通过 CNBizAPI 查询企业基础工商信息，q 可为企业名称或统一社会信用代码。"""
    return query_company_basic(q)
