"""启信宝开放平台 API 客户端。

主数据源只允许调用项目批准的启信宝 API ID；企查查 MCP 仍作为补充数据源。
"""

import hashlib
import json
import logging
import os
import time
from typing import Any

import requests


logger = logging.getLogger(__name__)


def _load_local_env() -> None:
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
            if key and value and not os.getenv(key):
                os.environ[key] = value


_load_local_env()

QIXIN_APPKEY = os.getenv("QIXIN_APPKEY", os.getenv("QIXIN_OPENAPI_APPKEY", ""))
QIXIN_SECRET_KEY = os.getenv("QIXIN_SECRET_KEY", os.getenv("QIXIN_OPENAPI_SECRET_KEY", ""))
QIXIN_AUTH_VERSION = os.getenv("QIXIN_AUTH_VERSION", "2.0")
try:
    QIXIN_CACHE_TTL_SECONDS = int(os.getenv("QIXIN_CACHE_TTL_SECONDS", "259200"))
except ValueError:
    logger.warning("QIXIN_CACHE_TTL_SECONDS 配置无效，已回退为 259200 秒")
    QIXIN_CACHE_TTL_SECONDS = 259200

ALLOWED_QIXIN_API_IDS = {
    "1.31",
    "1.41",
    "79.14",
    "55.2",
    "22.1",
    "61.1",
    "5.5",
    "17.5",
    "66.1",
    "85.71",
    "32.1",
    "1.55",
    "56.1",
    "51.1",
    "63.2",
    "20.1",
    "20.3",
    "26.1",
    "34.1",
    "25.1",
}

QIXIN_API_SPECS: dict[str, dict[str, Any]] = {
    "1.31": {
        "name": "企业模糊搜索",
        "url": "https://api.qixin.com/APIService/v2/search/advSearch",
        "param": "keyword",
    },
    "1.41": {
        "name": "工商照面",
        "url": "https://api.qixin.com/APIService/enterprise/getBasicInfo",
        "param": "keyword",
    },
    "79.14": {
        "name": "科技型企业",
        "url": "https://api.qixin.com/APIService/enterprise/getTechEnt",
        "param": "name",
    },
    "55.2": {
        "name": "股权穿透",
        "url": "https://api.qixin.com/APIService/reportData/getReportDataByName",
        "param": "name",
    },
    "22.1": {
        "name": "企业资质信息",
        "url": "https://api.qixin.com/APIService/certificate/getCertificateByName",
        "param": "name",
    },
    "61.1": {
        "name": "购地信息",
        "url": "https://api.qixin.com/APIService/operation/getLandInfoByNameAndType",
        "param": "keyword",
        "extra_params": {"type": 1},
    },
    "5.5": {
        "name": "失信被执行企业",
        "url": "https://api.qixin.com/APIService/execution/getExecutionListByName",
        "param": "keyword",
    },
    "17.5": {
        "name": "被执行企业",
        "url": "https://api.qixin.com/APIService/execution/getExecutedpersonListByName",
        "param": "name",
    },
    "66.1": {
        "name": "限制高消费",
        "url": "https://api.qixin.com/APIService/risk/getRestrictedConsumer",
        "param": "ename",
    },
    "85.71": {
        "name": "企业案件串联",
        "url": "https://api.qixin.com/APIService/judicialcases/getRelationByName",
        "param": "keyword",
    },
    "32.1": {
        "name": "地产行政处罚",
        "url": "https://api.qixin.com/APIService/v2/adminPunish/getAdminPunishByName",
        "param": "keyword",
    },
    "1.55": {
        "name": "经营异常信息",
        "url": "https://api.qixin.com/APIService/enterprise/getAbnormals",
        "param": "keyword",
    },
    "56.1": {
        "name": "企业严重违法信息",
        "url": "https://api.qixin.com/APIService/enterprise/getSeriousIllegalByName",
        "param": "name",
    },
    "51.1": {
        "name": "企业环保处罚列表",
        "url": "https://api.qixin.com/APIService/envp/getEPByName",
        "param": "name",
    },
    "63.2": {
        "name": "企业非正常户",
        "url": "https://api.qixin.com/APIService/risk/getAbnormalEnterpriseByName",
        "param": "name",
    },
    "20.1": {
        "name": "企业欠税信息",
        "url": "https://api.qixin.com/APIService/overduetax/getOverDueTaxByName",
        "param": "name",
    },
    "20.3": {
        "name": "重大税收违法信息",
        "url": "https://api.qixin.com/APIService/overduetax/getTaxCaseByName",
        "param": "name",
    },
    "26.1": {
        "name": "股权出质信息",
        "url": "https://api.qixin.com/APIService/v2/equityPledge/getEquityQualitiesByName",
        "param": "name",
    },
    "34.1": {
        "name": "股权冻结",
        "url": "https://api.qixin.com/APIService/v2/judicialFreeze/getJudicialFreezeByName",
        "param": "name",
    },
    "25.1": {
        "name": "动产抵押信息",
        "url": "https://api.qixin.com/APIService/v2/mortgage/getMortgagesByName",
        "param": "name",
    },
}

_QIXIN_CACHE: dict[str, tuple[float, str]] = {}


def is_qixin_configured() -> bool:
    return bool(QIXIN_APPKEY and QIXIN_SECRET_KEY)


def _headers() -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    sign = hashlib.md5(f"{QIXIN_APPKEY}{timestamp}{QIXIN_SECRET_KEY}".encode("utf-8")).hexdigest()
    return {
        "Auth-Version": QIXIN_AUTH_VERSION,
        "appkey": QIXIN_APPKEY,
        "timestamp": timestamp,
        "sign": sign,
        "Accept": "application/json",
    }


def _cache_key(api_id: str, params: dict[str, Any]) -> str:
    return f"{api_id}:{json.dumps(params, ensure_ascii=False, sort_keys=True)}"


def _get_cached(key: str) -> str | None:
    if QIXIN_CACHE_TTL_SECONDS <= 0:
        return None
    cached = _QIXIN_CACHE.get(key)
    if not cached:
        return None
    cached_at, value = cached
    if time.time() - cached_at > QIXIN_CACHE_TTL_SECONDS:
        _QIXIN_CACHE.pop(key, None)
        return None
    return value


def _set_cached(key: str, value: str) -> None:
    if QIXIN_CACHE_TTL_SECONDS <= 0 or value.startswith("查询失败:"):
        return
    _QIXIN_CACHE[key] = (time.time(), value)


def query_qixin_api(api_id: str, params: dict[str, Any] | None = None, timeout: int = 30) -> str:
    """调用启信宝白名单 API，返回紧凑 JSON 字符串。"""
    api_id = str(api_id or "").strip()
    if api_id not in ALLOWED_QIXIN_API_IDS:
        allowed = "/".join(sorted(ALLOWED_QIXIN_API_IDS))
        return f"查询失败: 当前仅允许调用启信宝 API ID {allowed}"
    if not is_qixin_configured():
        return "查询失败: 未配置 QIXIN_APPKEY 或 QIXIN_SECRET_KEY"

    spec = QIXIN_API_SPECS[api_id]
    request_params = dict(spec.get("extra_params") or {})
    request_params.update(params or {})
    key = _cache_key(api_id, request_params)
    cached = _get_cached(key)
    if cached is not None:
        logger.info("Qixin API cache hit: api_id=%s", api_id)
        return cached

    try:
        resp = requests.get(spec["url"], headers=_headers(), params=request_params, timeout=timeout)
        resp.encoding = "utf-8"
        try:
            data = resp.json()
        except ValueError:
            data = {"raw_text": resp.text[:1000]}
        if resp.status_code != 200:
            raise RuntimeError(f"启信宝 API 请求失败: status={resp.status_code}, body={str(data)[:500]}")
        result = json.dumps(data, ensure_ascii=False)
        _set_cached(key, result)
        return result
    except Exception as exc:
        logger.warning("Qixin API query failed: api_id=%s error=%s", api_id, exc)
        return f"查询失败: {exc}"


def query_qixin_by_search_key(api_id: str, search_key: str, timeout: int = 30, **extra_params: Any) -> str:
    spec = QIXIN_API_SPECS[api_id]
    params = dict(extra_params)
    params[spec["param"]] = search_key
    return query_qixin_api(api_id, params=params, timeout=timeout)


def query_qixin_basic(search_key: str, timeout: int = 30) -> str:
    return query_qixin_by_search_key("1.41", search_key, timeout=timeout)


def query_qixin_fuzzy_search(search_key: str, timeout: int = 30) -> str:
    return query_qixin_by_search_key("1.31", search_key, timeout=timeout)
