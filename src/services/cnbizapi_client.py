"""CNBizAPI 客户端。

当前不进入默认固定采集链路，仅作为备用兼容客户端保留。
密钥只从环境变量读取，不在代码中保存。
"""

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urljoin

import requests


def _load_local_env():
    """加载 .env.local 文件中的环境变量（仅用于开发/测试环境）。"""
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

logger = logging.getLogger(__name__)

CNBIZAPI_BASE_URL = os.getenv("CNBIZAPI_BASE_URL", "https://api.cnbizapi.com")
CNBIZAPI_ENABLED = os.getenv("CNBIZAPI_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
CNBIZAPI_API_KEY = os.getenv("CNBIZAPI_API_KEY", "")
CNBIZAPI_EMAIL = os.getenv("CNBIZAPI_EMAIL", "")
CNBIZAPI_PASSWORD = os.getenv("CNBIZAPI_PASSWORD", "")
try:
    CNBIZAPI_CACHE_TTL_SECONDS = int(os.getenv("CNBIZAPI_CACHE_TTL_SECONDS", "259200"))
except ValueError:
    logger.warning("CNBIZAPI_CACHE_TTL_SECONDS 配置无效，已回退为 259200 秒")
    CNBIZAPI_CACHE_TTL_SECONDS = 259200

_CNBIZAPI_AUTH_CACHE: dict[str, Any] = {}
_CNBIZAPI_CACHE: dict[str, tuple[float, str]] = {}


def is_cnbizapi_configured() -> bool:
    """检查 CNBizAPI 是否可用。"""
    return bool(CNBIZAPI_ENABLED and (CNBIZAPI_API_KEY or (CNBIZAPI_EMAIL and CNBIZAPI_PASSWORD)))


def _cache_key(path: str, params: dict[str, Any] | None = None) -> str:
    params_text = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
    return f"{path}:{params_text}"


def _get_cached_result(key: str) -> str | None:
    if CNBIZAPI_CACHE_TTL_SECONDS <= 0:
        return None
    cached = _CNBIZAPI_CACHE.get(key)
    if not cached:
        return None
    cached_at, value = cached
    if time.time() - cached_at > CNBIZAPI_CACHE_TTL_SECONDS:
        _CNBIZAPI_CACHE.pop(key, None)
        return None
    return value


def _set_cached_result(key: str, value: str) -> None:
    if CNBIZAPI_CACHE_TTL_SECONDS <= 0:
        return
    if value.startswith("查询失败:"):
        return
    _CNBIZAPI_CACHE[key] = (time.time(), value)


def _extract_api_key(data: Any) -> str:
    """兼容常见 token/api_key 包装。"""
    if not isinstance(data, dict):
        return ""
    for key in ("api_key", "apiKey", "token", "access_token"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("data", "result"):
        value = data.get(key)
        found = _extract_api_key(value)
        if found:
            return found
    return ""


def _login_for_api_key(timeout: int = 20) -> str:
    cached_key = _CNBIZAPI_AUTH_CACHE.get("api_key")
    if cached_key:
        return str(cached_key)

    if CNBIZAPI_API_KEY:
        _CNBIZAPI_AUTH_CACHE["api_key"] = CNBIZAPI_API_KEY
        return CNBIZAPI_API_KEY

    if not (CNBIZAPI_EMAIL and CNBIZAPI_PASSWORD):
        raise RuntimeError("未配置 CNBIZAPI_API_KEY，且未配置 CNBIZAPI_EMAIL/CNBIZAPI_PASSWORD")

    url = urljoin(CNBIZAPI_BASE_URL.rstrip("/") + "/", "v1/auth/login")
    resp = requests.post(
        url,
        json={"email": CNBIZAPI_EMAIL, "password": CNBIZAPI_PASSWORD},
        timeout=timeout,
    )
    resp.encoding = "utf-8"
    try:
        data = resp.json()
    except ValueError:
        data = {"raw_text": resp.text[:1000]}
    if resp.status_code != 200:
        raise RuntimeError(f"CNBizAPI 登录失败: status={resp.status_code}, body={str(data)[:500]}")

    api_key = _extract_api_key(data)
    if not api_key:
        raise RuntimeError(f"CNBizAPI 登录响应未包含 api_key: {str(data)[:500]}")
    _CNBIZAPI_AUTH_CACHE["api_key"] = api_key
    return api_key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_login_for_api_key()}",
        "Accept": "application/json",
    }


def query_cnbizapi(path: str, params: dict[str, Any] | None = None, timeout: int = 20) -> str:
    """调用 CNBizAPI REST 接口，返回紧凑 JSON 字符串。"""
    if not is_cnbizapi_configured():
        return "查询失败: CNBizAPI 未配置，请设置 CNBIZAPI_API_KEY 或 CNBIZAPI_EMAIL/CNBIZAPI_PASSWORD"

    try:
        normalized_path = path.strip().lstrip("/")
        cache_key = _cache_key(normalized_path, params)
        cached = _get_cached_result(cache_key)
        if cached is not None:
            logger.info("CNBizAPI cache hit: %s", normalized_path)
            return cached

        url = urljoin(CNBIZAPI_BASE_URL.rstrip("/") + "/", normalized_path)
        resp = requests.get(url, headers=_headers(), params=params or {}, timeout=timeout)
        resp.encoding = "utf-8"
        try:
            data = resp.json()
        except ValueError:
            data = {"raw_text": resp.text[:1000]}
        if resp.status_code != 200:
            raise RuntimeError(f"CNBizAPI 请求失败: status={resp.status_code}, body={str(data)[:500]}")

        result = json.dumps(data, ensure_ascii=False)
        _set_cached_result(cache_key, result)
        return result
    except Exception as exc:
        logger.warning("CNBizAPI query failed: %s", exc)
        return f"查询失败: {exc}"


def query_company_search(keyword: str, limit: int = 5) -> str:
    """模糊搜索企业候选。"""
    return query_cnbizapi("v1/company/search", params={"keyword": keyword, "limit": limit})


def query_company_basic(q: str) -> str:
    """查询企业基础工商信息，q 可为企业名或统一社会信用代码。"""
    return query_cnbizapi("v1/company/basic", params={"q": q})
