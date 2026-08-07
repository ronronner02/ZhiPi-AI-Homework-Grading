"""公网访问风控：让 Demo 能安全地挂成一个所有人可点的链接。

本机演示时这些机制**全部默认关闭**，不配置任何环境变量就和以前一模一样；
只有部署到公网、并且配置了真实 VLM Key 时才需要逐项打开。

四道闸：
    1. 访问口令  ZHIPI_ACCESS_CODE —— 链接可以转发，但没口令打不开；
    2. 请求体上限 ZHIPI_MAX_BODY_MB —— 挡住超大 body 把内存撑爆（FastAPI
       默认不限制请求体大小，公网裸奔时这是最现实的一条攻击面）；
    3. 单 IP 频率  ZHIPI_RATE_LIMIT —— 挡住有人拿脚本刷识别 / 批改接口；
    4. VLM 日配额 ZHIPI_VLM_DAILY_LIMIT —— 兜底保护 API 余额，超限后自动
       回落离线样例匹配，页面明确告知，而不是把错误甩给体验者。
"""
import os
import secrets
import threading
import time

_lock = threading.RLock()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


# ---------- 1. 访问口令 ----------

COOKIE_NAME = "zhipi_pass"


def access_code() -> str:
    """当前访问口令；空字符串表示不启用（本机演示默认如此）。"""
    return str(os.environ.get("ZHIPI_ACCESS_CODE", "")).strip()


def check_code(candidate: str | None) -> bool:
    """校验口令。用等时比较，避免通过响应耗时逐字符试探口令。"""
    expected = access_code()
    if not expected:
        return True
    if not candidate:
        return False
    return secrets.compare_digest(str(candidate), expected)


# ---------- 2. 请求体上限 ----------

MAX_BODY_BYTES = _env_int("ZHIPI_MAX_BODY_MB", 12, 1, 64) * 1024 * 1024
# 图片走 base64 传输，编码后约膨胀 4/3；这里给原始图片的上限。
MAX_IMAGE_BYTES = _env_int("ZHIPI_MAX_IMAGE_MB", 8, 1, 32) * 1024 * 1024


# ---------- 3. 单 IP 频率限制 ----------

def _parse_rate(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    """解析 "次数/窗口秒" 形式的限流配置，配错时退回默认值。"""
    try:
        count, window = str(raw).split("/", 1)
        parsed = (int(count), int(window))
    except (AttributeError, TypeError, ValueError):
        return default
    if parsed[0] < 1 or parsed[1] < 1:
        return default
    return parsed


RATE_LIMIT = _parse_rate(os.environ.get("ZHIPI_RATE_LIMIT", ""), (30, 600))
RATE_LIMIT_ENABLED = _env_flag("ZHIPI_RATE_LIMIT_ON") or bool(os.environ.get("ZHIPI_RATE_LIMIT"))

# (bucket, ip) -> [时间戳, ...]，滑动窗口日志。
_hits: dict[tuple[str, str], list[float]] = {}
_last_sweep = 0.0


def client_ip(request) -> str:
    """取客户端 IP。

    默认只信任直连地址；套了 nginx / 云负载均衡时置 ZHIPI_TRUST_PROXY=1
    才读 X-Forwarded-For 首跳。无条件信任该头等于把限流关掉——任何人
    都能自己伪造一个头换一个「新 IP」。
    """
    if _env_flag("ZHIPI_TRUST_PROXY"):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "unknown")[:64]


def _sweep(now: float) -> None:
    """清理过期窗口，防止限流表随访问 IP 无限增长（调用方须持锁）。"""
    global _last_sweep
    if now - _last_sweep < 60:
        return
    _last_sweep = now
    window = RATE_LIMIT[1]
    for key in list(_hits):
        kept = [t for t in _hits[key] if now - t < window]
        if kept:
            _hits[key] = kept
        else:
            del _hits[key]


def rate_limit(bucket: str, ip: str) -> tuple[bool, int]:
    """滑动窗口限流。返回 (是否放行, 建议重试秒数)。"""
    if not RATE_LIMIT_ENABLED:
        return True, 0
    limit, window = RATE_LIMIT
    now = time.time()
    with _lock:
        _sweep(now)
        key = (bucket, ip)
        hits = [t for t in _hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _hits[key] = hits
            retry = int(window - (now - hits[0])) + 1
            return False, max(1, retry)
        hits.append(now)
        _hits[key] = hits
        return True, 0


# ---------- 4. 大模型日调用配额 ----------
# 两个独立桶：vlm = 多模态识别（qwen-vl-max 等），grade = 文本批改（DeepSeek 等）。
# 0 表示不限（本机演示默认）。公开部署时按 API 余额设一个自己能接受的数字，
# 用尽后自动回落离线链路，而不是把报错甩给体验者。

VLM_DAILY_LIMIT = _env_int("ZHIPI_VLM_DAILY_LIMIT", 0, 0, 1000000)
GRADE_DAILY_LIMIT = _env_int("ZHIPI_GRADE_DAILY_LIMIT", 0, 0, 1000000)
_LIMITS = {"vlm": VLM_DAILY_LIMIT, "grade": GRADE_DAILY_LIMIT}

_quota: dict[str, dict] = {}


def _today() -> tuple:
    """按服务器本地日期分桶。容器请设置 TZ=Asia/Shanghai，否则按 UTC 跨日。"""
    t = time.localtime()
    return (t.tm_year, t.tm_mon, t.tm_mday)


def quota_take(bucket: str) -> bool:
    """占用一次配额；返回 False 表示今日额度已用尽。未设限时恒为 True。"""
    limit = _LIMITS.get(bucket, 0)
    if limit <= 0:
        return True
    with _lock:
        today = _today()
        state = _quota.setdefault(bucket, {"day": today, "used": 0})
        if state["day"] != today:
            state["day"], state["used"] = today, 0
        if state["used"] >= limit:
            return False
        state["used"] += 1
        return True


def quota_refund(bucket: str) -> None:
    """归还额度：仅用于「占了额度但确认没发出任何调用」的分支。"""
    if _LIMITS.get(bucket, 0) <= 0:
        return
    with _lock:
        state = _quota.get(bucket)
        if state and state["day"] == _today() and state["used"] > 0:
            state["used"] -= 1


def _bucket_snapshot(bucket: str) -> dict:
    limit = _LIMITS.get(bucket, 0)
    if limit <= 0:
        return {"enabled": False}
    with _lock:
        state = _quota.get(bucket)
        used = state["used"] if state and state["day"] == _today() else 0
        return {"enabled": True, "limit": limit, "used": used,
                "remaining": max(0, limit - used)}


def quota_snapshot() -> dict:
    """各配额桶使用情况（供 /api/demo/config 与运维排查用）。"""
    return {bucket: _bucket_snapshot(bucket) for bucket in _LIMITS}


def config_snapshot() -> dict:
    """风控开关总览（不含口令原文）。"""
    return {
        "access_code_required": bool(access_code()),
        "rate_limit": ("%d/%ds" % RATE_LIMIT) if RATE_LIMIT_ENABLED else None,
        "max_image_mb": MAX_IMAGE_BYTES // (1024 * 1024),
        "quota": quota_snapshot(),
    }

