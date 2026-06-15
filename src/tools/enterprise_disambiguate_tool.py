"""企业主体消歧工具 - 支持企业名称和统一社会信用代码确认目标企业"""

import json
import re
from langchain.tools import tool
from coze_coding_dev_sdk import SearchClient
from coze_coding_utils.runtime_ctx.context import new_context
from coze_coding_utils.log.write_log import request_context
from services.qcc_mcp_client import query_company_registration
from services.qixin_openapi_client import query_qixin_basic
from services.cnbizapi_client import query_company_basic, query_company_search


USCC_PATTERN = re.compile(r"^[0-9A-Z]{18}$")
USCC_SEARCH_PATTERN = re.compile(r"(?<![0-9A-Z])([0-9A-Z]{18})(?![0-9A-Z])")

ADMIN_PREFIXES = (
    "北京市", "天津市", "上海市", "重庆市",
    "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省", "江苏省", "浙江省",
    "安徽省", "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省",
    "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省", "甘肃省",
    "青海省", "台湾省",
    "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "香港特别行政区", "澳门特别行政区",
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)


def _normalize_social_credit_code(text: str) -> str:
    """去除空白和分隔符后标准化统一社会信用代码。"""
    return re.sub(r"[\s\-－_]", "", text or "").upper()


def _is_social_credit_code(text: str) -> bool:
    """判断用户输入是否像统一社会信用代码。"""
    return bool(USCC_PATTERN.fullmatch(_normalize_social_credit_code(text)))


def _first_value(data: dict, keys: tuple[str, ...]) -> str:
    """按常见中英文字段名提取企查查返回值。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return ""


def _extract_qcc_payload(qcc_result) -> dict:
    """兼容 MCP 返回 JSON 字符串或 dict 的情况。"""
    if isinstance(qcc_result, str):
        try:
            parsed = json.loads(qcc_result)
        except json.JSONDecodeError:
            return _extract_qcc_payload_from_text(qcc_result)
    else:
        parsed = qcc_result
    return parsed if isinstance(parsed, dict) else {}


def _extract_qcc_payload_from_text(text: str) -> dict:
    """从 MCP/搜索返回的普通文本中尽量提取主体字段。"""
    if not text:
        return {}

    payload = {}
    field_patterns = {
        "企业名称": (
            r"(?:企业名称|公司名称|名称)[:：]\s*([^\n\r；;，,]+)",
            r"([\u4e00-\u9fa5A-Za-z0-9（）()·\-]+(?:股份有限公司|有限责任公司|有限公司))",
        ),
        "统一社会信用代码": (
            r"(?:统一社会信用代码|社会信用代码|信用代码|统一信用代码)[:：]\s*([0-9A-Z]{18})",
        ),
        "登记状态": (
            r"(?:登记状态|经营状态)[:：]\s*([^\n\r；;，,]+)",
        ),
        "法定代表人": (
            r"(?:法定代表人|法人)[:：]\s*([^\n\r；;，,]+)",
        ),
        "注册资本": (
            r"(?:注册资本)[:：]\s*([^\n\r；;，,]+)",
        ),
        "成立日期": (
            r"(?:成立日期|成立时间)[:：]\s*([^\n\r；;，,]+)",
        ),
    }
    for field, patterns in field_patterns.items():
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                payload[field] = match.group(1).strip()
                break

    if "统一社会信用代码" not in payload:
        code_match = USCC_SEARCH_PATTERN.search(text.upper())
        if code_match:
            payload["统一社会信用代码"] = code_match.group(1)
    return payload


def _iter_dicts(value):
    """递归遍历结构化返回中的 dict，兼容 Result/Data/List 包装。"""
    value = _try_parse_json(value)
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _try_parse_json(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _extract_candidate_payloads(raw_result) -> list[dict]:
    """从结构化返回中提取可能代表企业主体的记录。"""
    if isinstance(raw_result, str):
        text_payload = _extract_qcc_payload_from_text(raw_result)
        if text_payload and (_extract_enterprise_name(text_payload) or _extract_social_credit_code(text_payload)):
            return [text_payload]

    payloads = []
    seen = set()
    for data in _iter_dicts(raw_result):
        name = _extract_enterprise_name(data)
        code = _extract_social_credit_code(data)
        if not (name or code):
            continue
        key = f"{name}|{code}"
        if key in seen:
            continue
        seen.add(key)
        payloads.append(data)
    return payloads


def _find_credit_code_near_name(search_text: str, enterprise_name: str) -> str:
    """从搜索结果中按候选名称附近文本补充统一社会信用代码。"""
    if not search_text or not enterprise_name:
        return ""
    normalized_name = _normalize_company_name(enterprise_name)
    best_code = ""
    for match in re.finditer(re.escape(enterprise_name), search_text):
        start = max(0, match.start() - 220)
        end = min(len(search_text), match.end() + 360)
        nearby = search_text[start:end].upper()
        code_match = USCC_SEARCH_PATTERN.search(nearby)
        if code_match:
            return code_match.group(1)

    # 搜索标题/摘要有时括号、空白不同，兜底按标准化名称扫描行。
    for block in re.split(r"\n{2,}|【结果\s*\d+】", search_text):
        if normalized_name and normalized_name in _normalize_company_name(block):
            code_match = USCC_SEARCH_PATTERN.search(block.upper())
            if code_match:
                best_code = code_match.group(1)
                break
    return best_code


def _ensure_candidate_identity_fields(candidates: list[dict], search_text: str = "") -> list[dict]:
    """保证多候选展示至少包含企业名称和统一社会信用代码字段。"""
    normalized = []
    for cand in candidates:
        item = dict(cand)
        name = item.get("企业名称") or item.get("公司名称") or item.get("name") or ""
        if name:
            item["企业名称"] = name
        credit_code = item.get("统一社会信用代码") or item.get("社会信用代码") or item.get("creditCode") or ""
        if not credit_code and search_text:
            credit_code = _find_credit_code_near_name(search_text, name)
        item["统一社会信用代码"] = credit_code or "未获取"
        normalized.append(item)
    return normalized


def _pick_unique_payload(search_key: str, payloads: list[dict]) -> dict:
    """从候选记录中挑出唯一匹配主体，避免嵌套返回导致误判为多主体。"""
    if not payloads:
        return {}
    if len(payloads) == 1:
        return payloads[0]

    if _is_social_credit_code(search_key):
        normalized_code = _normalize_social_credit_code(search_key)
        matches = [
            item for item in payloads
            if _normalize_social_credit_code(_extract_social_credit_code(item)) == normalized_code
        ]
        return matches[0] if len(matches) == 1 else {}

    normalized_name = _normalize_company_name(search_key)
    matches = [
        item for item in payloads
        if _normalize_company_name(_extract_enterprise_name(item)) == normalized_name
    ]
    return matches[0] if len(matches) == 1 else {}


def _extract_enterprise_name(data: dict) -> str:
    """从企查查返回中提取企业名称。"""
    return _first_value(
        data,
        (
            "企业名称",
            "公司名称",
            "名称",
            "Name",
            "name",
            "CompanyName",
            "companyName",
            "company_name",
            "ShortName",
        ),
    )


def _extract_social_credit_code(data: dict) -> str:
    """从企查查返回中提取统一社会信用代码。"""
    return _first_value(
        data,
        (
            "统一社会信用代码",
            "社会信用代码",
            "信用代码",
            "统一信用代码",
            "CreditCode",
            "creditCode",
            "creditNo",
            "CreditNo",
            "credit_code",
            "social_credit_code",
            "unified_social_credit_code",
            "统一社会信用代码",
            "No",
            "RegNo",
        ),
    )


def _build_enriched_candidate(name: str, qcc_data: dict) -> dict:
    """将企查查工商信息压缩为用于消歧展示的字段。"""
    enriched = {"企业名称": _extract_enterprise_name(qcc_data) or name}
    fields = {
        "统一社会信用代码": _extract_social_credit_code(qcc_data),
        "登记状态": _first_value(qcc_data, ("登记状态", "经营状态", "Status", "status")),
        "法定代表人": _first_value(qcc_data, ("法定代表人", "法人", "OperName", "operName", "LegalPerson")),
        "注册资本": _first_value(qcc_data, ("注册资本", "RegistCapi", "registCapi", "RegisteredCapital")),
        "成立日期": _first_value(qcc_data, ("成立日期", "成立时间", "StartDate", "startDate", "TermStart", "termStart")),
        "所属地区": _first_value(qcc_data, ("所属地区", "地区", "Province", "Area")),
        "国标行业": _first_value(qcc_data, ("国标行业", "行业", "Industry", "IndustryName")),
        "企业类型": _first_value(qcc_data, ("企业类型", "公司类型", "EconKind", "Type")),
    }
    for key, value in fields.items():
        if value:
            enriched[key] = value
    return enriched


def _is_probably_short_name(name: str) -> bool:
    normalized = _normalize_company_name(name)
    suffixes = ("有限公司", "有限责任公司", "股份有限公司", "集团有限公司")
    return len(normalized) < 6 and not any(normalized.endswith(suffix) for suffix in suffixes)


def _query_registration_by_mcp(search_key: str) -> dict:
    """优先用 MCP 查询企业主体。"""
    try:
        payloads = _extract_candidate_payloads(query_company_registration(search_key))
    except Exception:
        payloads = []
    return _pick_unique_payload(search_key, payloads)


def _query_registration_by_cnbizapi(search_key: str) -> dict:
    """用 CNBizAPI 查询企业主体。当前默认主体确认链路不再主动调用。"""
    api_calls = [
        lambda: query_company_basic(search_key),
    ]
    if not _is_social_credit_code(search_key):
        api_calls.append(lambda: query_company_search(search_key, limit=5))
    for call in api_calls:
        try:
            payloads = _extract_candidate_payloads(call())
        except Exception:
            payloads = []
        picked = _pick_unique_payload(search_key, payloads)
        if picked:
            return picked
    return {}


def _query_registration_by_qixin(search_key: str) -> dict:
    """用启信宝 API 1.41 工商照面确认企业主体。"""
    api_calls = [
        lambda: query_qixin_basic(search_key),
    ]
    for call in api_calls:
        try:
            payloads = _extract_candidate_payloads(call())
        except Exception:
            payloads = []
        picked = _pick_unique_payload(search_key, payloads)
        if picked:
            return picked
    return {}


def _format_confirmed_target(
    source: str,
    target: dict,
    search_key: str,
    *,
    credit_code_input: bool = False,
) -> str:
    source_labels = {
        "cnbizapi": "CNBizAPI",
        "mcp": "企查查MCP",
        "coze_public_search": "Coze/公开搜索",
        "coze_search_cnbizapi_mcp": "Coze/公开搜索",
        "coze_search_mcp": "Coze搜索+企查查MCP",
        "qixin": "启信宝 API 1.41 工商照面",
    }
    source_label = source_labels.get(source, source)
    title_prefix = "已通过统一社会信用代码唯一确认目标企业" if credit_code_input else "已唯一确认目标企业"
    lines = [
        f"{title_prefix}，可直接采用：{target['企业名称']}",
        "",
        f"匹配说明：优先使用{source_label}按「{search_key}」确认到唯一企业主体，无需再次让用户确认。",
        "",
        "【目标企业】",
    ]
    for k, v in target.items():
        if v:
            lines.append(f"  {k}: {v}")
    lines.append("")
    credit_code = target.get("统一社会信用代码", "")
    if _is_social_credit_code(credit_code):
        lines.extend([
            "【后续查询主键】",
            f"  企查查MCP search_key: {credit_code}",
            "",
            "请优先使用以上企业全称继续公开搜索、评分和报告生成；后续所有 qcc_get_* 企查查MCP工具必须优先使用统一社会信用代码作为 search_key，避免同名/近似名企业误查。",
        ])
    else:
        lines.append("请优先使用以上企业全称继续后续信息收集、评分和报告生成；如后续补齐统一社会信用代码，所有 qcc_get_* 企查查MCP工具必须优先改用统一社会信用代码查询。")
    return "\n".join(lines)


def _confirm_target_by_mcp(search_key: str) -> tuple[str, dict]:
    """MCP 主体确认兼容函数。当前默认主体确认链路不再主动调用。"""
    mcp_data = _query_registration_by_mcp(search_key)
    if mcp_data:
        return "mcp", _build_enriched_candidate(_extract_enterprise_name(mcp_data) or search_key, mcp_data)
    return "", {}


def _confirm_target_by_cnbizapi(search_key: str) -> tuple[str, dict]:
    """CNBizAPI 主体确认兼容函数。当前默认主体确认链路不再主动调用。"""
    cnbizapi_data = _query_registration_by_cnbizapi(search_key)
    if cnbizapi_data:
        return "cnbizapi", _build_enriched_candidate(_extract_enterprise_name(cnbizapi_data) or search_key, cnbizapi_data)
    return "", {}


def _confirm_target_by_openapi(search_key: str) -> tuple[str, dict]:
    """兼容旧函数名：企业主体确认优先使用启信宝 API 1.41 工商照面。"""
    api_data = _query_registration_by_qixin(search_key)
    if api_data:
        return "qixin", _build_enriched_candidate(_extract_enterprise_name(api_data) or search_key, api_data)
    return "", {}


def _enrich_candidates_by_cnbizapi(candidates: list[dict]) -> list[dict]:
    enriched_candidates = []
    for cand in candidates[:6]:
        name = cand["企业名称"]
        try:
            payloads = _extract_candidate_payloads(query_company_basic(name))
            cnbizapi_data = _pick_unique_payload(name, payloads)
            enriched = _build_enriched_candidate(name, cnbizapi_data)
            if cnbizapi_data and (enriched.get("登记状态") or enriched.get("统一社会信用代码")):
                enriched_candidates.append(enriched)
            else:
                enriched_candidates.append(cand)
        except Exception:
            enriched_candidates.append(cand)
    return enriched_candidates


def _enrich_candidates_by_mcp(candidates: list[dict]) -> list[dict]:
    enriched_candidates = []
    for cand in candidates[:6]:  # 最多展示6个候选
        name = cand["企业名称"]
        try:
            qcc_data = _extract_qcc_payload(query_company_registration(name))
            enriched = _build_enriched_candidate(name, qcc_data)
            if qcc_data and (enriched.get("登记状态") or enriched.get("统一社会信用代码")):
                enriched_candidates.append(enriched)
            else:
                enriched_candidates.append(cand)
        except Exception:
            enriched_candidates.append(cand)
    return enriched_candidates


def _search_and_enrich_candidates(search_key: str, query: str) -> tuple[str, list[dict]]:
    """用 Coze/公开搜索找候选，并从搜索文本中提取统一社会信用代码等主体字段。"""
    search_result = _do_candidate_search(query, count=10)
    candidates = _extract_company_names(search_result, search_key)
    return search_result, _ensure_candidate_identity_fields(candidates, search_result)


def _extract_stock_short_names(search_text: str) -> list[str]:
    """从证券/行情页面标题中提取股票简称，用于信用代码搜索二次扩展。"""
    if not search_text:
        return []

    names = []
    seen = set()
    patterns = [
        r"【结果\s*\d+】\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,30})\(\d{6}\)",
        r"【结果\s*\d+】\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,30})（\d{6}）",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, search_text):
            name = match.strip()
            if name and name not in seen and not name.endswith(("有限公司", "股份有限公司")):
                seen.add(name)
                names.append(name)
    return names[:3]


def _search_credit_code_fallback_candidates(credit_code: str, first_search_result: str) -> tuple[str, list[dict]]:
    """信用代码公开搜索未直接提取全称时，按股票简称等线索二次搜索。"""
    combined_search_result = first_search_result or ""
    candidates = []
    seen_names = set()
    for short_name in _extract_stock_short_names(first_search_result):
        query = f'"{short_name}" "{credit_code}" 企业名称 统一社会信用代码 工商信息'
        search_result = _do_candidate_search(query, count=10)
        if search_result:
            combined_search_result = f"{combined_search_result}\n\n{search_result}" if combined_search_result else search_result
        for cand in _extract_company_names(search_result, credit_code):
            name = cand.get("企业名称", "")
            if name and name not in seen_names:
                seen_names.add(name)
                candidates.append(cand)

    return combined_search_result, _ensure_candidate_identity_fields(candidates, combined_search_result)


def _format_candidate_result(
    enterprise_name: str,
    search_result: str,
    enriched_candidates: list[dict],
    *,
    allow_single_auto_adopt: bool = True,
) -> str:
    """格式化 Coze/公开搜索后的主体确认结果。"""
    if not enriched_candidates:
        return (
            f"未从搜索结果中提取到包含「{enterprise_name}」的完整企业名称。\n\n"
            f"原始搜索结果如下，请从中判断用户可能想查询的企业，或询问用户提供更完整的企业名称：\n\n"
            f"{search_result}"
        )

    enriched_candidates = _ensure_candidate_identity_fields(enriched_candidates, search_result)

    exact_match = _find_exact_normalized_match(enriched_candidates, enterprise_name)
    if exact_match:
        return _format_confirmed_target("coze_search_mcp", exact_match, enterprise_name)

    if allow_single_auto_adopt and len(enriched_candidates) == 1:
        cand = enriched_candidates[0]
        lines = [
            f"仅搜索到 1 家匹配企业，可直接采用：{cand['企业名称']}",
            "",
            "匹配说明：Coze搜索只提取到一个候选，并已尽量通过企查查MCP补充统一社会信用代码和工商状态。",
            "",
            "【目标企业】",
        ]
        for k, v in cand.items():
            if v:
                lines.append(f"  {k}: {v}")
        lines.append("")
        credit_code = cand.get("统一社会信用代码", "")
        if _is_social_credit_code(credit_code):
            lines.extend([
                "【后续查询主键】",
                f"  企查查MCP search_key: {credit_code}",
                "",
                "请直接使用以上企业全称继续公开搜索、评分和报告生成；后续所有 qcc_get_* 企查查MCP工具必须优先使用统一社会信用代码作为 search_key。",
            ])
        else:
            lines.append("请直接使用以上企业全称继续后续信息收集、评分和报告生成；如后续补齐统一社会信用代码，所有 qcc_get_* 企查查MCP工具必须优先改用统一社会信用代码查询。")
        return "\n".join(lines)

    output_lines = [
        f"根据「{enterprise_name}」搜索到以下 {len(enriched_candidates)} 家匹配企业，请让用户确认要分析哪一家：\n"
    ]
    for i, cand in enumerate(enriched_candidates, 1):
        output_lines.append(f"【候选 {i}】")
        for k, v in cand.items():
            if v:
                output_lines.append(f"  {k}: {v}")
        output_lines.append("")

    output_lines.append(
        "请将以上候选企业列表展示给用户，请用户回复编号、完整企业名称或统一社会信用代码。"
        "展示时必须包含企业名称和统一社会信用代码；未获取到代码时也要显示“统一社会信用代码：未获取”，确认后再使用完整企业名称或统一社会信用代码调用其他搜索和分析工具。"
    )
    return "\n".join(output_lines)


def _normalize_company_name(name: str) -> str:
    """标准化企业名，用于判断省份前缀/空白/括号差异下的唯一匹配。"""
    normalized = re.sub(r"[\s（）()《》\"'“”‘’]", "", name or "")
    changed = True
    while changed:
        changed = False
        for prefix in ADMIN_PREFIXES:
            if normalized.startswith(prefix) and len(normalized) > len(prefix) + 4:
                normalized = normalized[len(prefix):]
                changed = True
                break
    return normalized


def _strip_admin_prefix(name: str) -> str:
    """保留原企业名格式，仅去掉开头省份/地区前缀，用于再次精确查询。"""
    stripped = (name or "").strip()
    changed = True
    while changed:
        changed = False
        compact = re.sub(r"^\s+", "", stripped)
        for prefix in ADMIN_PREFIXES:
            if compact.startswith(prefix) and len(compact) > len(prefix) + 4:
                stripped = compact[len(prefix):].strip()
                changed = True
                break
    return stripped


def _find_exact_normalized_match(candidates: list[dict], enterprise_name: str) -> dict | None:
    """如果去掉省份等前缀后能唯一完全匹配，则直接采用该候选。"""
    normalized_input = _normalize_company_name(enterprise_name)
    matches = [
        cand for cand in candidates
        if _normalize_company_name(cand.get("企业名称", "")) == normalized_input
    ]
    return matches[0] if len(matches) == 1 else None


def _find_exact_credit_code_match(candidates: list[dict], credit_code: str) -> dict | None:
    """如果候选中有唯一统一社会信用代码精确匹配，则直接采用。"""
    normalized_code = _normalize_social_credit_code(credit_code)
    matches = [
        cand for cand in candidates
        if _normalize_social_credit_code(cand.get("统一社会信用代码", "")) == normalized_code
    ]
    return matches[0] if len(matches) == 1 else None


def _do_candidate_search(query: str, count: int = 10) -> str:
    """执行联网搜索获取候选企业列表"""
    ctx = request_context.get() or new_context(method="enterprise_candidate_search")
    client = SearchClient(ctx=ctx)
    response = client.web_search(query=query, count=count, need_summary=True)

    if not response.web_items:
        return ""

    results = []
    for i, item in enumerate(response.web_items, 1):
        result = f"【结果 {i}】{item.title}\n来源: {item.site_name or '未知'}\n链接: {item.url or '无'}\n摘要: {item.snippet or '无'}"
        if item.summary:
            result += f"\nAI总结: {item.summary}"
        results.append(result)

    return "\n\n".join(results)


def _extract_company_names(search_text: str, keyword: str) -> list[dict]:
    """从搜索结果中提取匹配的公司名称和基本信息"""
    candidates = []
    seen_names = set()
    normalized_keyword = _normalize_company_name(keyword)
    keyword_is_credit_code = _is_social_credit_code(keyword)

    # 常见的句子残片前缀——如果公司名以此开头，说明是从句子中截取的，不是真实公司名
    bad_prefixes = (
        '也是', '包括', '其中', '例如', '如', '以及', '发现', '显示', '查询',
        '搜索', '风险方面', '根据', '关于', '通过', '另外', '此外', '还有',
        '目前', '现有', '新增', '新增了', '这家', '该', '上述', '以下',
        '关联', '旗下', '参股', '控股', '投资', '被', '将', '已', '现',
        '为', '与', '及', '或', '和', '在', '对', '向', '由', '从',
        '是', '有', '无', '不', '未', '共', '含', '包', '分',
        '经', '据', '按', '依', '照', '称', '说',
        '基本信息', '工商信息', '详情', '更多', '查看', '点击', '了解',
        '担任', '任职', '控股', '全资', '隶属', '参股', '隶属于',
    )

    # 匹配中文公司名模式：必须以有限公司/股份公司结尾
    patterns = [
        r'([\u4e00-\u9fa5A-Za-z0-9（）()·\-]+(?:股份有限公司|有限责任公司|有限公司))',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, search_text)
        for name in matches:
            # 过滤条件
            if name in seen_names:
                continue
            normalized_name = _normalize_company_name(name)
            if not keyword_is_credit_code and keyword not in name and normalized_keyword not in normalized_name:
                continue
            if len(name) < 5:
                continue
            # 检查是否以句子残片前缀开头
            if any(name.startswith(prefix) for prefix in bad_prefixes):
                continue
            # 检查名称中是否包含明显的非名称用词（如"方面""发现"等）
            sentence_words = ['方面', '发现', '显示', '查询', '搜索', '来看', '来说', '而言', '之后', '之前']
            if any(w in name[:6] for w in sentence_words):
                continue

            seen_names.add(name)
            candidate = {"企业名称": name}
            if keyword_is_credit_code:
                nearby_code = _find_credit_code_near_name(search_text, name)
                if _normalize_social_credit_code(nearby_code) == _normalize_social_credit_code(keyword):
                    candidate["统一社会信用代码"] = _normalize_social_credit_code(keyword)
            candidates.append(candidate)

    return candidates


@tool
def search_enterprise_candidates(enterprise_name: str) -> str:
    """【第一步必调】搜索匹配的企业候选列表。支持企业名称、简称、不完整名称或统一社会信用代码。此工具必须在开始正式分析之前调用，确认目标企业后再进行后续分析。"""
    normalized_code = _normalize_social_credit_code(enterprise_name)
    if _is_social_credit_code(enterprise_name):
        source, target = _confirm_target_by_openapi(normalized_code)
        if target:
            if not _is_social_credit_code(target.get("统一社会信用代码", "")):
                target["统一社会信用代码"] = normalized_code
            return _format_confirmed_target(source, target, normalized_code, credit_code_input=True)

        search_query = f'"{normalized_code}" 统一社会信用代码 企业名称 工商信息'
        search_result, enriched_candidates = _search_and_enrich_candidates(normalized_code, search_query)
        if not enriched_candidates:
            search_result, enriched_candidates = _search_credit_code_fallback_candidates(normalized_code, search_result)
        exact_code_match = _find_exact_credit_code_match(enriched_candidates, normalized_code)
        if exact_code_match:
            return _format_confirmed_target("coze_public_search", exact_code_match, normalized_code, credit_code_input=True)

        if len(enriched_candidates) == 1:
            cand = enriched_candidates[0]
            if not _is_social_credit_code(cand.get("统一社会信用代码", "")):
                cand["统一社会信用代码"] = normalized_code
            return _format_candidate_result(normalized_code, search_result, enriched_candidates)

        return (
            f"用户输入「{enterprise_name}」符合统一社会信用代码格式；已优先调用启信宝 API 1.41，但启信宝和 Coze/公开搜索均未能确认对应企业。\n\n"
            "请提示用户核对统一社会信用代码是否正确，或补充企业全称后再查询。"
        )

    # 对看起来比较完整的企业名称，先用启信宝 API 1.41 工商照面确认。
    # 对很短的简称保留搜索消歧，避免直接把“华为”“万达”等简称误判为唯一主体。
    if not _is_probably_short_name(enterprise_name):
        source, target = _confirm_target_by_openapi(enterprise_name)
        if target:
            return _format_confirmed_target(source, target, enterprise_name)

    # 第二步：用 Coze 搜索/公开搜索查找匹配企业。
    search_query = f'"{enterprise_name}" 公司 工商信息 企业名称'
    search_result, enriched_candidates = _search_and_enrich_candidates(enterprise_name, search_query)

    exact_match = _find_exact_normalized_match(enriched_candidates, enterprise_name)
    if exact_match:
        return _format_confirmed_target("coze_public_search", exact_match, enterprise_name)

    if len(enriched_candidates) == 1:
        return _format_candidate_result(enterprise_name, search_result, enriched_candidates)

    return _format_candidate_result(
        enterprise_name,
        search_result,
        enriched_candidates,
        allow_single_auto_adopt=True,
    )
