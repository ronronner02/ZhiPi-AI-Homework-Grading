# -*- coding: utf-8 -*-
"""页图暂存：上传的作业页 + 批改后带痕迹的图。

## 为什么需要它

批改痕迹要画在**学生自己那张纸**上，所以从上传到出图这段时间里，原图必须
还在服务端。三条替代路都不通：

- 让前端在批改时把 base64 再传一遍：一份 PDF 摊成 12 页就是十几 MB 往返，
  而这些字节服务端刚刚才见过；
- 塞进 session 字典：那里的东西会被调试接口与日志顺手打出来，图片字节混在
  里面既没意义又危险；而且「会话数上限 300」会悄悄变成「内存上限 300×N MB」，
  真正会先撑爆的那一维反而没人管；
- 落盘：Demo 要能一键起停、删干净，多一个需要清理的目录就多一处状态。

所以放在进程内，按会话隔离，带**全局字节预算**与 LRU 淘汰（与会话数彼此
独立）。演示结束进程一退，所有学生作业图随之消失——对一个处理未成年人
作业的系统来说，这个默认值比「存着以防万一」更合适。
"""
import os
import threading
import time
import uuid
from collections import OrderedDict


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


# 全局页图预算。实测这批真实作业单页 JPEG 0.3-0.5MB，256MB 约能存 600 页；
# 公开部署内存吃紧时调小，本机演示基本碰不到。
BUDGET_BYTES = _env_int("ZHIPI_PAGE_CACHE_MB", 256, 8, 4096) * 1024 * 1024
# 单会话页数上限：一次误传 20 页 PDF 不该把别人的页图全挤掉。
PER_SESSION_MAX = _env_int("ZHIPI_PAGE_PER_SESSION", 60, 1, 500)
# 单页存活时间。链路是「上传→识别→（教师改转写）→批改」，中间有人工环节，
# 给足 2 小时；过期的页在淘汰或访问时清掉。
TTL_SECONDS = _env_int("ZHIPI_PAGE_TTL", 2 * 3600, 300, 86400)

_pages: "OrderedDict[str, dict]" = OrderedDict()   # page_id -> 条目
_bytes = 0
_lock = threading.RLock()


def _drop_locked(page_id: str) -> None:
    global _bytes
    item = _pages.pop(page_id, None)
    if item:
        _bytes -= item["size"]


def _evict_locked() -> None:
    """清过期页并按 LRU 淘汰到预算之内（调用方须持锁）。"""
    now = time.time()
    for page_id in [k for k, v in _pages.items() if now - v["touched"] > TTL_SECONDS]:
        _drop_locked(page_id)
    while _pages and _bytes > BUDGET_BYTES:
        _drop_locked(next(iter(_pages)))


def _trim_session_locked(sid: str) -> None:
    """把某会话的页数压到上限内（调用方须持锁）。"""
    owned = [pid for pid, it in _pages.items() if it["sid"] == sid]
    for page_id in owned[:max(0, len(owned) - PER_SESSION_MAX)]:
        _drop_locked(page_id)


def put(sid: str, data: bytes, mime: str = "image/jpeg", meta: dict = None) -> str:
    """存一页原图，返回页 ID。sid 用于取图时的归属校验。"""
    global _bytes
    page_id = "PG-" + uuid.uuid4().hex[:16]
    with _lock:
        _pages[page_id] = {
            "sid": sid or "",
            "data": data,
            "mime": mime or "image/jpeg",
            "meta": dict(meta or {}),
            "marked": None,          # 画了批改痕迹的版本，批改完成后回填
            "marked_mime": "",
            "size": len(data),
            "created": time.time(),
            "touched": time.time(),
        }
        _bytes += len(data)
        _trim_session_locked(sid or "")
        _evict_locked()
    return page_id


def get(sid: str, page_id: str):
    """取一页。会话不匹配一律当作不存在——页图是别人的作业照片。"""
    with _lock:
        item = _pages.get(page_id)
        if item is None or item["sid"] != (sid or ""):
            return None
        if time.time() - item["touched"] > TTL_SECONDS:
            _drop_locked(page_id)
            return None
        item["touched"] = time.time()
        _pages.move_to_end(page_id)      # LRU：访问即续命
        return item


def set_marked(sid: str, page_id: str, data: bytes, mime: str = "image/jpeg") -> bool:
    """回填「画了批改痕迹」的版本。页不存在或不属于该会话时返回 False。"""
    global _bytes
    with _lock:
        item = _pages.get(page_id)
        if item is None or item["sid"] != (sid or ""):
            return False
        delta = len(data) - len(item["marked"] or b"")
        item["marked"] = data
        item["marked_mime"] = mime or "image/jpeg"
        item["size"] += delta
        item["touched"] = time.time()
        _bytes += delta
        _pages.move_to_end(page_id)
        _evict_locked()
        return True


def drop_session(sid: str) -> int:
    """清掉某会话的全部页图（演示重置用），返回清掉的页数。"""
    with _lock:
        owned = [pid for pid, it in _pages.items() if it["sid"] == (sid or "")]
        for page_id in owned:
            _drop_locked(page_id)
        return len(owned)


def stats() -> dict:
    """页图缓存概况（运维排查用，不含任何图像内容）。"""
    with _lock:
        return {
            "pages": len(_pages),
            "bytes": _bytes,
            "budget_bytes": BUDGET_BYTES,
            "per_session_max": PER_SESSION_MAX,
        }
