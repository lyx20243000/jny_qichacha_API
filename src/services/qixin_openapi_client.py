"""启信宝开放平台 API 客户端。

主数据源只允许调用项目批准的启信宝 API ID；企查查 MCP 仍作为补充数据源。
"""

import hashlib
import json
import logging
import os
from pathlib import Path
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

# ===== 启动诊断日志：部署后通过生产日志确认启信宝配置状态 =====
if QIXIN_APPKEY and QIXIN_SECRET_KEY:
    logger.info(
        "Qixin API configured: appkey=%s...(%d chars), auth_version=%s",
        QIXIN_APPKEY[:6], len(QIXIN_APPKEY), QIXIN_AUTH_VERSION,
    )
else:
    logger.warning(
        "Qixin API NOT configured: QIXIN_APPKEY=%s, QIXIN_SECRET_KEY=%s. "
        "All Qixin API calls will return config_missing.",
        "set" if QIXIN_APPKEY else "missing",
        "set" if QIXIN_SECRET_KEY else "missing",
    )
QIXIN_AUTH_VERSION = os.getenv("QIXIN_AUTH_VERSION", "2.0")
try:
    QIXIN_CACHE_TTL_SECONDS = int(os.getenv("QIXIN_CACHE_TTL_SECONDS", "259200"))
except ValueError:
    logger.warning("QIXIN_CACHE_TTL_SECONDS 配置无效，已回退为 259200 秒")
    QIXIN_CACHE_TTL_SECONDS = 259200
try:
    QIXIN_CIRCUIT_BREAKER_SECONDS = int(os.getenv("QIXIN_CIRCUIT_BREAKER_SECONDS", "600"))
except ValueError:
    logger.warning("QIXIN_CIRCUIT_BREAKER_SECONDS 配置无效，已回退为 600 秒")
    QIXIN_CIRCUIT_BREAKER_SECONDS = 600
try:
    QIXIN_PERSISTENT_CACHE_TTL_SECONDS = int(os.getenv("QIXIN_PERSISTENT_CACHE_TTL_SECONDS", "86400"))
except ValueError:
    logger.warning("QIXIN_PERSISTENT_CACHE_TTL_SECONDS 配置无效，已回退为 86400 秒")
    QIXIN_PERSISTENT_CACHE_TTL_SECONDS = 86400

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
_QIXIN_CIRCUIT_STATE = {
    "open_until": 0.0,
    "error_code": "",
    "message": "",
    "api_id": "",
}
_QIXIN_CACHE_DIR = Path(os.getenv("QIXIN_CACHE_DIR", ".cache/qixin"))


def is_qixin_configured() -> bool:
    return bool(QIXIN_APPKEY and QIXIN_SECRET_KEY)


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


def _build_qixin_error_result(
    api_id: str,
    error_code: str,
    message: str,
    *,
    status: str = "",
    retryable: bool = False,
    fatal: bool = False,
    raw_response: Any = None,
) -> str:
    spec = QIXIN_API_SPECS.get(api_id, {})
    payload: dict[str, Any] = {
        "ok": False,
        "provider": "qixin",
        "api_id": api_id,
        "api_name": spec.get("name", api_id),
        "error_code": error_code,
        "message": message,
        "status": status,
        "retryable": retryable,
        "fatal": fatal,
    }
    if raw_response not in (None, ""):
        payload["raw_response"] = raw_response
    return json.dumps(payload, ensure_ascii=False)


def is_qixin_result_ok(raw_result) -> bool:
    parsed = parse_qixin_result(raw_result)
    if parsed.get("provider") == "qixin" and parsed.get("ok") is False:
        return False
    if isinstance(raw_result, str):
        return not raw_result.startswith("查询失败:")
    return bool(raw_result)


def is_qixin_unavailable(raw_result) -> bool:
    parsed = parse_qixin_result(raw_result)
    if parsed.get("provider") == "qixin" and parsed.get("ok") is False:
        return bool(parsed.get("fatal"))
    if isinstance(raw_result, str):
        return any(keyword in raw_result for keyword in ("未配置", "账户未激活", "余额不足", "积分余额不足", "签名"))
    return False


def get_qixin_error_info(raw_result) -> dict[str, Any]:
    parsed = parse_qixin_result(raw_result)
    if parsed.get("provider") == "qixin" and parsed.get("ok") is False:
        return parsed
    if isinstance(raw_result, str) and raw_result.startswith("查询失败:"):
        return {
            "ok": False,
            "provider": "qixin",
            "error_code": "query_failed",
            "message": raw_result,
            "fatal": False,
            "retryable": True,
        }
    return {}


def _classify_qixin_payload(api_id: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    status = str(payload.get("status", "")).strip()
    message = str(payload.get("message", "")).strip()
    normalized_message = message.replace(" ", "")

    if not status:
        return True, None
    if status in {"200", "201", "202"}:
        return True, None

    if status == "113" or "账户未激活" in normalized_message:
        return False, _build_qixin_error_result(
            api_id,
            "account_inactive",
            message or "启信宝账户未激活",
            status=status,
            retryable=False,
            fatal=True,
            raw_response=payload,
        )
    if any(keyword in normalized_message for keyword in ("未配置QIXIN_APPKEY", "未配置QIXIN_SECRET_KEY", "appkey不能为空", "secret")):
        return False, _build_qixin_error_result(
            api_id,
            "config_invalid",
            message or "启信宝鉴权配置无效",
            status=status,
            retryable=False,
            fatal=True,
            raw_response=payload,
        )
    if any(keyword in normalized_message for keyword in ("签名错误", "签名无效", "鉴权失败", "认证失败", "无权限")):
        return False, _build_qixin_error_result(
            api_id,
            "auth_failed",
            message or "启信宝鉴权失败",
            status=status,
            retryable=False,
            fatal=True,
            raw_response=payload,
        )
    if any(keyword in normalized_message for keyword in ("余额不足", "积分余额不足", "额度不足", "次数不足")):
        return False, _build_qixin_error_result(
            api_id,
            "quota_exhausted",
            message or "启信宝额度不足",
            status=status,
            retryable=False,
            fatal=True,
            raw_response=payload,
        )
    return False, _build_qixin_error_result(
        api_id,
        "api_error",
        message or f"启信宝接口返回异常状态 {status}",
        status=status,
        retryable=True,
        fatal=False,
        raw_response=payload,
    )


def _open_qixin_circuit(error_info: dict[str, Any], api_id: str) -> None:
    if not error_info.get("fatal"):
        return
    _QIXIN_CIRCUIT_STATE["open_until"] = time.time() + max(QIXIN_CIRCUIT_BREAKER_SECONDS, 0)
    _QIXIN_CIRCUIT_STATE["error_code"] = str(error_info.get("error_code", "") or "")
    _QIXIN_CIRCUIT_STATE["message"] = str(error_info.get("message", "") or "")
    _QIXIN_CIRCUIT_STATE["api_id"] = api_id


def _get_qixin_circuit_error(api_id: str) -> str | None:
    open_until = float(_QIXIN_CIRCUIT_STATE.get("open_until", 0.0) or 0.0)
    if open_until <= time.time():
        if open_until:
            _QIXIN_CIRCUIT_STATE.update({"open_until": 0.0, "error_code": "", "message": "", "api_id": ""})
        return None
    message = _QIXIN_CIRCUIT_STATE.get("message", "") or "启信宝熔断中，暂时跳过后续接口"
    return _build_qixin_error_result(
        api_id,
        str(_QIXIN_CIRCUIT_STATE.get("error_code", "") or "circuit_open"),
        message,
        retryable=False,
        fatal=True,
        raw_response={
            "open_until": int(open_until),
            "source_api_id": _QIXIN_CIRCUIT_STATE.get("api_id", ""),
        },
    )


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


def _persistent_cache_file(key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return _QIXIN_CACHE_DIR / f"{digest}.json"


def _get_persistent_cached(key: str) -> str | None:
    if QIXIN_PERSISTENT_CACHE_TTL_SECONDS <= 0:
        return None
    cache_file = _persistent_cache_file(key)
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    cached_at = float(payload.get("cached_at", 0) or 0)
    value = payload.get("value")
    if not isinstance(value, str) or not cached_at:
        return None
    if time.time() - cached_at > QIXIN_PERSISTENT_CACHE_TTL_SECONDS:
        try:
            cache_file.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    return value


def _set_persistent_cached(key: str, value: str) -> None:
    if QIXIN_PERSISTENT_CACHE_TTL_SECONDS <= 0 or not is_qixin_result_ok(value):
        return
    cache_file = _persistent_cache_file(key)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cached_at": time.time(),
            "value": value,
        }
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("Skip writing qixin persistent cache: %s", exc)


def _get_cached(key: str) -> str | None:
    if QIXIN_CACHE_TTL_SECONDS <= 0:
        return _get_persistent_cached(key)
    cached = _QIXIN_CACHE.get(key)
    if cached:
        cached_at, value = cached
        if time.time() - cached_at <= QIXIN_CACHE_TTL_SECONDS:
            return value
        _QIXIN_CACHE.pop(key, None)
    persistent = _get_persistent_cached(key)
    if persistent is not None:
        _QIXIN_CACHE[key] = (time.time(), persistent)
    return persistent


def _set_cached(key: str, value: str) -> None:
    if not is_qixin_result_ok(value):
        return
    if QIXIN_CACHE_TTL_SECONDS > 0:
        _QIXIN_CACHE[key] = (time.time(), value)
    _set_persistent_cached(key, value)


def query_qixin_api(api_id: str, params: dict[str, Any] | None = None, timeout: int = 30) -> str:
    """调用启信宝白名单 API，返回紧凑 JSON 字符串。"""
    api_id = str(api_id or "").strip()
    if api_id not in ALLOWED_QIXIN_API_IDS:
        allowed = "/".join(sorted(ALLOWED_QIXIN_API_IDS))
        return _build_qixin_error_result(api_id, "api_not_allowed", f"当前仅允许调用启信宝 API ID {allowed}", fatal=False)
    if not is_qixin_configured():
        return _build_qixin_error_result(api_id, "config_missing", "未配置 QIXIN_APPKEY 或 QIXIN_SECRET_KEY", fatal=True)

    circuit_error = _get_qixin_circuit_error(api_id)
    if circuit_error is not None:
        return circuit_error

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
        logger.info(
            "Qixin API response: api_id=%s status=%d data_keys=%s ok=%s",
            api_id, resp.status_code,
            list(data.keys())[:5] if isinstance(data, dict) else "non-dict",
            str(data.get("Status", "")) if isinstance(data, dict) else "unknown",
        )
        if resp.status_code != 200:
            return _build_qixin_error_result(
                api_id,
                "http_error",
                f"启信宝 API 请求失败: status={resp.status_code}",
                status=str(resp.status_code),
                retryable=True,
                fatal=False,
                raw_response=data,
            )
        ok, normalized_error = _classify_qixin_payload(api_id, data)
        if not ok and normalized_error is not None:
            error_info = get_qixin_error_info(normalized_error)
            _open_qixin_circuit(error_info, api_id)
            return normalized_error
        result = json.dumps(data, ensure_ascii=False)
        _set_cached(key, result)
        return result
    except Exception as exc:
        logger.warning("Qixin API query failed: api_id=%s error=%s", api_id, exc)
        return _build_qixin_error_result(
            api_id,
            "request_exception",
            f"启信宝请求异常: {exc}",
            retryable=True,
            fatal=False,
        )


def query_qixin_by_search_key(api_id: str, search_key: str, timeout: int = 30, **extra_params: Any) -> str:
    spec = QIXIN_API_SPECS[api_id]
    params = dict(extra_params)
    params[spec["param"]] = search_key
    return query_qixin_api(api_id, params=params, timeout=timeout)


def query_qixin_basic(search_key: str, timeout: int = 30) -> str:
    return query_qixin_by_search_key("1.41", search_key, timeout=timeout)


def query_qixin_fuzzy_search(search_key: str, timeout: int = 30) -> str:
    return query_qixin_by_search_key("1.31", search_key, timeout=timeout)
