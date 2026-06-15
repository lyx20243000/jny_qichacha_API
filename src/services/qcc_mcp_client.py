"""
企查查 MCP 客户端 - 通过 Streamable HTTP 协议调用企查查 MCP 服务
"""
import os
import json
import logging
import requests
import time
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# 本地开发环境变量加载（仅在环境变量为空时生效）
def _load_local_env():
    """加载 .env.local 文件中的环境变量（仅用于开发/测试环境）"""
    env_file = os.path.join(os.getenv("COZE_WORKSPACE_PATH", "/workspace/projects"), ".env.local")
    if not os.path.exists(env_file):
        return
    
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # 仅在环境变量为空时才设置
            if key and value and not os.getenv(key):
                os.environ[key] = value
                logger.debug(f"Loaded local env: {key}")

_load_local_env()

# 企查查 MCP 服务配置
QCC_MCP_SERVERS = {
    "qcc-company": "https://agent.qcc.com/mcp/company/stream",
    "qcc-risk": "https://agent.qcc.com/mcp/risk/stream",
    "qcc-ipr": "https://agent.qcc.com/mcp/ipr/stream",
    "qcc-operation": "https://agent.qcc.com/mcp/operation/stream",
    "qcc-executive": "https://agent.qcc.com/mcp/executive/stream",
    "qcc-history": "https://agent.qcc.com/mcp/history/stream",
}

QCC_MCP_KEY_ENV_NAMES = [
    "QCC_MCP_API_KEY",
    "QCC_MCP_API_KEY02",
    "QCC_MCP_API_KEY03",
    "QCC_MCP_API_KEY04",
    "QCC_MCP_API_KEY05",
    "QCC_MCP_API_KEY06",
]
QCC_MCP_QUOTA_KEYWORDS = ("code=300008", "积分余额不足", "余额不足")
QCC_MCP_EMPTY_OR_PARAM_ERROR_KEYWORDS = (
    "未查询到相关数据",
    "无数据",
    "参数错误",
    "invalid params",
    "missing",
    "required",
    "searchkey",
    "search_key",
)
QCC_MCP_TOOL_UNAVAILABLE_KEYWORDS = (
    "method not found",
    "tool not found",
    "unknown tool",
    "invalid tool",
    "not found",
    "不存在",
    "未找到",
    "工具不存在",
)
_QCC_MCP_EXHAUSTED_KEY_LABELS: set[str] = set()
_QCC_MCP_EXHAUSTED_DATE = date.today().isoformat()
_QCC_MCP_UNAVAILABLE_TOOLS: set[str] = set()


def _configured_mcp_keys() -> list[tuple[str, str]]:
    """读取当前已配置的 MCP Key，未配置或空字符串的备用 Key 自动跳过。"""
    configured_keys = []
    for env_name in QCC_MCP_KEY_ENV_NAMES:
        api_key = os.getenv(env_name, "").strip()
        if api_key:
            configured_keys.append((env_name, api_key))
    return configured_keys


if not _configured_mcp_keys():
    logger.warning(
        "环境变量 QCC_MCP_API_KEY 未配置，企查查 MCP 调用将鉴权失败。"
        "请在 Coze 项目设置中添加 QCC_MCP_API_KEY，或按需添加 QCC_MCP_API_KEY02 至 QCC_MCP_API_KEY06。"
    )
else:
    logger.info("已加载 %s 个企查查 MCP Key（仅记录数量，不输出密钥）", len(_configured_mcp_keys()))

try:
    QCC_MCP_CACHE_TTL_SECONDS = int(os.getenv("QCC_MCP_CACHE_TTL_SECONDS", "3600"))
except ValueError:
    logger.warning("QCC_MCP_CACHE_TTL_SECONDS 配置无效，已回退为 3600 秒")
    QCC_MCP_CACHE_TTL_SECONDS = 3600
_QCC_MCP_CACHE: dict[str, tuple[float, str]] = {}

try:
    QCC_MCP_TIMEOUT_SECONDS = int(os.getenv("QCC_MCP_TIMEOUT_SECONDS", "20"))
except ValueError:
    logger.warning("QCC_MCP_TIMEOUT_SECONDS 配置无效，已回退为 20 秒")
    QCC_MCP_TIMEOUT_SECONDS = 20


def _load_tool_aliases() -> dict[str, list[str]]:
    """读取 MCP 工具名别名配置，便于线上按实际挂载工具名修正。"""
    raw = os.getenv("QCC_MCP_TOOL_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("QCC_MCP_TOOL_ALIASES 不是有效 JSON，已忽略")
        return {}

    if not isinstance(parsed, dict):
        logger.warning("QCC_MCP_TOOL_ALIASES 必须是 JSON object，已忽略")
        return {}

    aliases: dict[str, list[str]] = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            names = [value]
        elif isinstance(value, list):
            names = [str(item).strip() for item in value if str(item).strip()]
        else:
            continue
        if names:
            aliases[str(key).strip()] = names
    return aliases


QCC_MCP_TOOL_ALIASES = _load_tool_aliases()


def _cache_key(server_name: str, tool_name: str, arguments: dict) -> str:
    """生成企查查 MCP 查询缓存 key。"""
    args = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return f"{server_name}:{tool_name}:{args}"


def _get_cached_result(key: str) -> Optional[str]:
    """读取进程内短期缓存，减少同一 run 内重复 MCP 查询。"""
    if QCC_MCP_CACHE_TTL_SECONDS <= 0:
        return None

    cached = _QCC_MCP_CACHE.get(key)
    if not cached:
        return None

    cached_at, value = cached
    if time.time() - cached_at > QCC_MCP_CACHE_TTL_SECONDS:
        _QCC_MCP_CACHE.pop(key, None)
        return None

    return value


def _set_cached_result(key: str, value: str) -> None:
    """缓存可复用结果；错误类结果不缓存，避免短暂故障被放大。"""
    if QCC_MCP_CACHE_TTL_SECONDS <= 0:
        return
    if value.startswith("错误:"):
        return

    _QCC_MCP_CACHE[key] = (time.time(), value)


def _is_quota_error(result: str) -> bool:
    """判断 MCP 返回是否为额度/积分不足。"""
    return isinstance(result, str) and any(keyword in result for keyword in QCC_MCP_QUOTA_KEYWORDS)


def is_qcc_mcp_quota_error(result: str) -> bool:
    """供聚合工具复用的额度不足判断，避免工具层维护第二套额度状态。"""
    return _is_quota_error(result)


def _is_tool_unavailable_error(result: str) -> bool:
    """判断 MCP 返回是否代表工具名不存在、未开通或无权限。"""
    if not isinstance(result, str):
        return False
    lowered = result.lower()
    return any(keyword in lowered for keyword in QCC_MCP_TOOL_UNAVAILABLE_KEYWORDS)


def _should_try_alternate_arguments(result: str) -> bool:
    """判断是否需要尝试 searchKey/search_key 另一种参数命名。"""
    if not isinstance(result, str):
        return False
    lowered = result.lower()
    return any(keyword in lowered for keyword in QCC_MCP_EMPTY_OR_PARAM_ERROR_KEYWORDS)


def _argument_candidates(arguments: dict) -> list[dict]:
    """兼容 agent.qcc.com MCP 不同工具对 searchKey/search_key 的命名要求。"""
    candidates = [dict(arguments)]
    if "searchKey" in arguments:
        alternate = dict(arguments)
        alternate["search_key"] = alternate.pop("searchKey")
        candidates.append(alternate)
    elif "search_key" in arguments:
        alternate = dict(arguments)
        alternate["searchKey"] = alternate.pop("search_key")
        candidates.append(alternate)
    return candidates


def _tool_key(server_name: str, tool_name: str) -> str:
    return f"{server_name}.{tool_name}"


def _tool_name_candidates(server_name: str, tool_name: str) -> list[str]:
    """返回待尝试的工具名列表，支持 server.tool 和 tool 两种别名配置键。"""
    candidates = [tool_name]
    for alias_key in (_tool_key(server_name, tool_name), tool_name):
        for alias in QCC_MCP_TOOL_ALIASES.get(alias_key, []):
            if alias not in candidates:
                candidates.append(alias)
    return candidates


def _reset_exhausted_keys_if_new_day() -> None:
    """MCP 额度按天刷新，跨天后重新尝试所有已配置 Key。"""
    global _QCC_MCP_EXHAUSTED_DATE
    today = date.today().isoformat()
    if today != _QCC_MCP_EXHAUSTED_DATE:
        _QCC_MCP_EXHAUSTED_KEY_LABELS.clear()
        _QCC_MCP_EXHAUSTED_DATE = today
        logger.info("企查查 MCP Key 每日额度日期已变化，已重置本进程内的额度耗尽标记")


def _available_mcp_keys() -> list[tuple[str, str]]:
    """返回当前进程内尚未标记为额度耗尽的 MCP Key。"""
    _reset_exhausted_keys_if_new_day()
    return [
        (label, api_key)
        for label, api_key in _configured_mcp_keys()
        if label not in _QCC_MCP_EXHAUSTED_KEY_LABELS
    ]


def is_qcc_mcp_available() -> bool:
    """Return whether at least one configured MCP key still has quota in this process."""
    return bool(_available_mcp_keys())


def _call_mcp_tool_once(
    url: str,
    server_name: str,
    tool_name: str,
    arguments: dict,
    api_key: str,
    key_label: str,
    timeout: int,
) -> str:
    """使用指定 Key 调用一次企查查 MCP。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    session = requests.Session()

    try:
        # Step 1: Initialize
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "enterprise-analyzer", "version": "1.0.0"},
            },
        }
        resp = session.post(url, headers=headers, json=init_payload, timeout=timeout)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            return f"错误: MCP initialize 失败, status={resp.status_code}, key={key_label}, body={resp.text[:500]}"

        # Step 2: Send initialized notification
        notif_payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        session.post(url, headers=headers, json=notif_payload, timeout=timeout)

        # Step 3: Call tool
        call_payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        resp = session.post(url, headers=headers, json=call_payload, timeout=timeout)
        resp.encoding = "utf-8"  # 强制UTF-8解码，避免requests默认回退ISO-8859-1导致中文乱码

        if resp.status_code != 200:
            return f"错误: MCP tool call 失败, status={resp.status_code}, key={key_label}, body={resp.text[:500]}"

        # 解析 SSE 响应
        result_text = ""
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                    if "result" in data:
                        content = data["result"].get("content", [])
                        for item in content:
                            if item.get("type") == "text":
                                result_text += item.get("text", "")
                    elif "error" in data:
                        err = data["error"]
                        return f"错误: MCP 返回错误 - code={err.get('code')}, message={err.get('message', '')}"
                except json.JSONDecodeError:
                    continue

        return result_text or "未查询到相关数据"

    except requests.exceptions.Timeout:
        return f"错误: 调用 {server_name}/{tool_name} 超时"
    except Exception as e:
        return f"错误: 调用 {server_name}/{tool_name} 异常 - {str(e)}"
    finally:
        session.close()


def _call_mcp_tool(server_name: str, tool_name: str, arguments: dict, timeout: int | None = None) -> str:
    """
    调用企查查 MCP 服务的指定工具

    Args:
        server_name: MCP服务名 (如 qcc-company, qcc-risk)
        tool_name: 工具名 (如 get_company_registration_info)
        arguments: 工具参数
        timeout: 超时时间(秒)

    Returns:
        工具返回的结果文本
    """
    if timeout is None:
        timeout = QCC_MCP_TIMEOUT_SECONDS

    url = QCC_MCP_SERVERS.get(server_name)
    if not url:
        return f"错误: 未知的服务名 {server_name}"

    available_keys = _available_mcp_keys()
    if not available_keys:
        if _configured_mcp_keys():
            return "错误: MCP 返回错误 - code=300008, message=所有已配置企查查MCP Key 当前积分余额不足"
        return "错误: 未配置企查查MCP Key，请配置 QCC_MCP_API_KEY，或按需配置 QCC_MCP_API_KEY02 至 QCC_MCP_API_KEY06"

    last_quota_error = ""
    tried_tool_names = []
    unavailable_messages = []
    for candidate_tool_name in _tool_name_candidates(server_name, tool_name):
        tried_tool_names.append(candidate_tool_name)
        candidate_key = _tool_key(server_name, candidate_tool_name)
        if candidate_key in _QCC_MCP_UNAVAILABLE_TOOLS:
            unavailable_messages.append(f"{candidate_tool_name}: 已标记为不可用")
            continue

        best_empty_result = ""
        for candidate_arguments in _argument_candidates(arguments):
            cache_key = _cache_key(server_name, candidate_tool_name, candidate_arguments)
            cached_result = _get_cached_result(cache_key)
            if cached_result is not None:
                logger.info("QCC MCP cache hit: %s/%s", server_name, candidate_tool_name)
                return cached_result

            for key_label, api_key in _available_mcp_keys():
                result = _call_mcp_tool_once(url, server_name, candidate_tool_name, candidate_arguments, api_key, key_label, timeout)
                if _is_quota_error(result):
                    _QCC_MCP_EXHAUSTED_KEY_LABELS.add(key_label)
                    last_quota_error = result
                    logger.warning("企查查 MCP Key %s 积分余额不足，自动切换下一个已配置 Key", key_label)
                    continue

                if _is_tool_unavailable_error(result):
                    _QCC_MCP_UNAVAILABLE_TOOLS.add(candidate_key)
                    unavailable_messages.append(f"{candidate_tool_name}: {result[:180]}")
                    logger.warning("企查查 MCP 工具不可用: %s/%s", server_name, candidate_tool_name)
                    break

                if _should_try_alternate_arguments(result):
                    best_empty_result = best_empty_result or result
                    logger.info(
                        "企查查 MCP 返回空/参数疑似不匹配，尝试备用参数命名: %s/%s args=%s",
                        server_name,
                        candidate_tool_name,
                        list(candidate_arguments.keys()),
                    )
                    break

                _set_cached_result(cache_key, result)
                return result

        if best_empty_result:
            _set_cached_result(_cache_key(server_name, candidate_tool_name, arguments), best_empty_result)
            return best_empty_result

        if last_quota_error and not _available_mcp_keys():
            break

    logger.warning(
        "企查查 MCP 查询未成功，本次请求: %s/%s，已尝试工具名: %s",
        server_name,
        tool_name,
        tried_tool_names,
    )
    if last_quota_error and not _available_mcp_keys():
        return "错误: MCP 返回错误 - code=300008, message=所有已配置企查查MCP Key 当前积分余额不足"
    if unavailable_messages:
        return (
            "错误: MCP 工具不可用，请核对企查查 MCP tool name；"
            f"server={server_name}, expected={tool_name}, tried={tried_tool_names}, detail={unavailable_messages[:3]}"
        )
    return "错误: MCP 返回错误 - code=300008, message=没有可用的企查查MCP Key"


# ============================================================
# 对外暴露的高层查询函数 - 每个函数对应一个 LangChain Tool
# ============================================================


def query_company_registration(search_key: str) -> str:
    """查询企业核心工商登记信息（法人、注册资本、成立日期、经营状态、行业等）"""
    return _call_mcp_tool("qcc-company", "get_company_registration_info", {"searchKey": search_key})


def query_shareholder_info(search_key: str) -> str:
    """查询企业一层直接股东构成信息（投资人、持股比例、认缴出资）"""
    return _call_mcp_tool("qcc-company", "get_shareholder_info", {"searchKey": search_key})


def query_actual_controller(search_key: str) -> str:
    """查询企业实际控制人详情（股权穿透分析后的最终控制人）"""
    return _call_mcp_tool("qcc-company", "get_actual_controller", {"searchKey": search_key})


def query_external_investments(search_key: str) -> str:
    """查询企业对外投资信息（被投资企业名称、状态、持股比例）"""
    return _call_mcp_tool("qcc-company", "get_external_investments", {"searchKey": search_key})


def query_financial_data(search_key: str) -> str:
    """查询企业核心财务数据（资产负债、利润表现、营运能力）"""
    return _call_mcp_tool("qcc-company", "get_financial_data", {"searchKey": search_key})


def query_key_personnel(search_key: str) -> str:
    """查询企业主要管理人员信息（姓名、职务等高管构成）"""
    return _call_mcp_tool("qcc-company", "get_key_personnel", {"searchKey": search_key})


def query_listing_info(search_key: str) -> str:
    """查询企业当前上市信息（上市日期、股票代码、交易所）"""
    return _call_mcp_tool("qcc-company", "get_listing_info", {"searchKey": search_key})


def query_company_profile(search_key: str) -> str:
    """查询企业简介（运营产品、业务模式、企查查行业分类）"""
    return _call_mcp_tool("qcc-company", "get_company_profile", {"searchKey": search_key})


# --- 风险类 ---

def query_dishonest_info(search_key: str) -> str:
    """查询企业失信被执行人信息（风险必查）"""
    return _call_mcp_tool("qcc-risk", "get_dishonest_info", {"searchKey": search_key})


def query_administrative_penalty(search_key: str) -> str:
    """查询企业行政处罚记录（风险必查）"""
    return _call_mcp_tool("qcc-risk", "get_administrative_penalty", {"searchKey": search_key})


def query_business_exception(search_key: str) -> str:
    """查询企业经营异常名录（风险必查）"""
    return _call_mcp_tool("qcc-risk", "get_business_exception", {"searchKey": search_key})


def query_serious_violation(search_key: str) -> str:
    """查询企业严重违法失信信息"""
    return _call_mcp_tool("qcc-risk", "get_serious_violation", {"searchKey": search_key})


def query_high_consumption_restriction(search_key: str) -> str:
    """查询企业限制高消费信息（风险必查）"""
    return _call_mcp_tool("qcc-risk", "get_high_consumption_restriction", {"searchKey": search_key})


def query_company_risk_scan(search_key: str) -> str:
    """查询企业风险概览扫描（综合风险画像）"""
    return _call_mcp_tool("qcc-risk", "get_company_risk_scan", {"searchKey": search_key})


def query_case_filing_info(search_key: str) -> str:
    """查询企业立案信息"""
    return _call_mcp_tool("qcc-risk", "get_case_filing_info", {"searchKey": search_key})


def query_executed_person(search_key: str) -> str:
    """查询企业被执行人信息"""
    return _call_mcp_tool("qcc-risk", "get_executed_person", {"searchKey": search_key})


def query_judicial_documents(search_key: str) -> str:
    """查询企业裁判文书详情"""
    return _call_mcp_tool("qcc-risk", "get_judicial_documents", {"searchKey": search_key})


def query_court_announcement(search_key: str) -> str:
    """查询企业法院公告信息"""
    return _call_mcp_tool("qcc-risk", "get_court_announcement", {"searchKey": search_key})


def query_final_case(search_key: str) -> str:
    """查询企业终本案件信息"""
    return _call_mcp_tool("qcc-risk", "get_final_case", {"searchKey": search_key})


def query_environmental_penalty(search_key: str) -> str:
    """查询企业环保处罚信息"""
    return _call_mcp_tool("qcc-risk", "get_environmental_penalty", {"searchKey": search_key})


def query_tax_abnormal(search_key: str) -> str:
    """查询企业税务非正常户信息"""
    return _call_mcp_tool("qcc-risk", "get_tax_abnormal", {"searchKey": search_key})


def query_tax_arrears(search_key: str) -> str:
    """查询企业欠税公告信息"""
    return _call_mcp_tool("qcc-risk", "get_tax_arrears", {"searchKey": search_key})


def query_tax_violation(search_key: str) -> str:
    """查询企业税收违法信息"""
    return _call_mcp_tool("qcc-risk", "get_tax_violation", {"searchKey": search_key})


def query_equity_pledge(search_key: str) -> str:
    """查询企业股权出质信息"""
    return _call_mcp_tool("qcc-risk", "get_equity_pledge", {"searchKey": search_key})


def query_equity_freeze(search_key: str) -> str:
    """查询企业股权冻结信息"""
    return _call_mcp_tool("qcc-risk", "get_equity_freeze", {"searchKey": search_key})


def query_chattel_mortgage(search_key: str) -> str:
    """查询企业动产抵押信息"""
    return _call_mcp_tool("qcc-risk", "get_chattel_mortgage", {"searchKey": search_key})


def query_land_mortgage(search_key: str) -> str:
    """查询企业土地抵押信息"""
    return _call_mcp_tool("qcc-risk", "get_land_mortgage", {"searchKey": search_key})


def query_history_dishonest(search_key: str) -> str:
    """查询企业历史失信信息"""
    return _call_mcp_tool("qcc-history", "get_history_dishonest_info", {"searchKey": search_key})


def query_history_executed_person(search_key: str) -> str:
    """查询企业历史被执行人信息"""
    return _call_mcp_tool("qcc-history", "get_history_executed_person", {"searchKey": search_key})


def query_history_business_exception(search_key: str) -> str:
    """查询企业历史经营异常信息"""
    return _call_mcp_tool("qcc-history", "get_history_business_exception", {"searchKey": search_key})


def query_history_administrative_penalty(search_key: str) -> str:
    """查询企业历史行政处罚信息"""
    return _call_mcp_tool("qcc-history", "get_history_administrative_penalty", {"searchKey": search_key})


# --- 知识产权类 ---

def query_patent_info(search_key: str) -> str:
    """查询企业专利信息"""
    return _call_mcp_tool("qcc-ipr", "get_patent_info", {"searchKey": search_key})


def query_trademark_info(search_key: str) -> str:
    """查询企业商标信息"""
    return _call_mcp_tool("qcc-ipr", "get_trademark_info", {"searchKey": search_key})


def query_software_copyright(search_key: str) -> str:
    """查询企业软件著作权信息"""
    return _call_mcp_tool("qcc-ipr", "get_software_copyright_info", {"searchKey": search_key})


# --- 经营类 ---

def query_bidding_info(search_key: str) -> str:
    """查询企业招投标信息"""
    return _call_mcp_tool("qcc-operation", "get_bidding_info", {"searchKey": search_key})


def query_credit_evaluation(search_key: str) -> str:
    """查询企业信用评价信息"""
    return _call_mcp_tool("qcc-operation", "get_credit_evaluation", {"searchKey": search_key})


def query_qualifications(search_key: str) -> str:
    """查询企业资质认证信息（高新技术企业、专精特新等）"""
    return _call_mcp_tool("qcc-operation", "get_qualifications", {"searchKey": search_key})


def query_honor_info(search_key: str) -> str:
    """查询企业荣誉信息"""
    return _call_mcp_tool("qcc-operation", "get_honor_info", {"searchKey": search_key})


def query_news_sentiment(search_key: str) -> str:
    """查询企业新闻舆情"""
    return _call_mcp_tool("qcc-operation", "get_news_sentiment", {"searchKey": search_key})


def query_recruitment_info(search_key: str) -> str:
    """查询企业招聘信息（反映经营活跃度）"""
    return _call_mcp_tool("qcc-operation", "get_recruitment_info", {"searchKey": search_key})


def query_administrative_license(search_key: str) -> str:
    """查询企业行政许可信息"""
    return _call_mcp_tool("qcc-operation", "get_administrative_license", {"searchKey": search_key})


def query_taxpayer_qualification(search_key: str) -> str:
    """查询企业纳税人资质信息"""
    return _call_mcp_tool("qcc-operation", "get_taxpayer_qualification", {"searchKey": search_key})


def query_product_check(search_key: str) -> str:
    """查询企业产品抽查/产品质量信息"""
    return _call_mcp_tool("qcc-operation", "get_product_check", {"searchKey": search_key})


def query_state_owned_land_transfer(search_key: str) -> str:
    """查询企业国有土地受让信息"""
    return _call_mcp_tool("qcc-operation", "get_state_owned_land_transfer", {"searchKey": search_key})


# --- 变更历史类 ---

def query_change_records(search_key: str) -> str:
    """查询企业工商变更记录"""
    return _call_mcp_tool("qcc-company", "get_change_records", {"searchKey": search_key})
