"""演示会话隔离：让公开 URL 上的多个体验者互不干扰。

**为什么需要**：Demo 早期按「本机单人演示」设计，教师终审记录是模块级全局
字典，且终审后会就地改写全局批改缓存里的置信度。一旦部署成公开链接，
A 点一次「确认」，B 刷新页面看到的置信度、红黄绿占比就跟着变——多人体验
时互相污染，且没有任何重置入口。

**隔离边界**：
    - 全局共享：AI 批改基线（同一份内置作答的批改结果对所有人一致，
      mock 模式下更是确定性的，重复批改纯属浪费；llm 模式下还能省调用费）；
    - 按会话隔离：教师终审记录、体验者自己上传的作业。

**内存上界**（公网部署必须有界，否则被刷会 OOM）：
    会话数 ≤ ZHIPI_SESSION_MAX（默认 300，超出按最久未访问淘汰），
    单会话上传件数 ≤ ZHIPI_UPLOAD_MAX（默认 20，超出淘汰该会话最旧一件），
    会话闲置 ZHIPI_SESSION_TTL 秒（默认 7200）后回收。
    被回收的会话再次访问时静默重建为空会话——等价于「演示已重置」，不报错。
"""
import os
import re
import threading
import time
import uuid
from collections import OrderedDict

COOKIE_NAME = "zhipi_sid"

# 会话 ID 形态固定为 32 位小写十六进制；不符合的一律视为无效并重新签发，
# 避免伪造的超长 / 含控制字符的 cookie 进入会话表当键。
_SID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """读取整数环境变量并夹到合理区间（配错值时退回默认值而不是崩溃）。"""
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


SESSION_MAX = _env_int("ZHIPI_SESSION_MAX", 300, 1, 100000)
SESSION_TTL = _env_int("ZHIPI_SESSION_TTL", 7200, 60, 86400 * 7)
UPLOAD_MAX = _env_int("ZHIPI_UPLOAD_MAX", 20, 1, 500)

# OrderedDict 充当 LRU：读写命中即 move_to_end，淘汰从头部取。
_sessions: "OrderedDict[str, dict]" = OrderedDict()
# uvicorn 的同步端点跑在线程池里，会真并发访问会话表，必须加锁。
_lock = threading.RLock()


def _new_session() -> dict:
    now = time.time()
    # folders / folder_seq / active_folder_id 由 pipeline.folders.ensure 种子化，
    # 这里只放空壳，避免 session_store 与 folders 互相 import。
    return {
        "reviews": {},            # submission_id -> 教师终审记录
        "uploads": OrderedDict(),  # upload_id -> 该会话上传作业的批改结果
        "upload_seq": 0,          # 上传编号自增，用于生成 UP-001 这样的可读 ID
        "folders": OrderedDict(),  # folder_id -> 夹元数据（含 item_ids）
        "folder_seq": 0,
        "active_folder_id": "demo",
        "created": now,
        "touched": now,
    }


def _purge_expired(now: float) -> None:
    """回收闲置超时的会话（调用方须持锁）。"""
    if SESSION_TTL <= 0:
        return
    stale = [sid for sid, s in _sessions.items() if now - s["touched"] > SESSION_TTL]
    for sid in stale:
        _sessions.pop(sid, None)


def get_or_create(raw_sid: str | None) -> tuple[str, dict, bool]:
    """按 cookie 取会话，无效 / 过期 / 被淘汰则新建。

    返回 (会话 ID, 会话数据, 是否为新建)。调用方拿到 is_new=True 时
    需要把会话 ID 写回 cookie。
    """
    now = time.time()
    with _lock:
        _purge_expired(now)

        sid = raw_sid if (raw_sid and _SID_RE.match(raw_sid)) else None
        if sid and sid in _sessions:
            session = _sessions[sid]
            session["touched"] = now
            _sessions.move_to_end(sid)
            return sid, session, False

        # 无效 cookie、过期会话、被 LRU 淘汰的会话，一律重新签发新 ID——
        # 沿用旧 ID 会让「已被淘汰」和「新会话」在日志上无法区分。
        sid = uuid.uuid4().hex
        session = _new_session()
        _sessions[sid] = session
        while len(_sessions) > SESSION_MAX:
            _sessions.popitem(last=False)   # 淘汰最久未访问的会话
        return sid, session, True


def reset(sid: str) -> dict:
    """清空该会话的终审记录与上传件，返回全新的空会话。"""
    with _lock:
        session = _new_session()
        if sid and _SID_RE.match(sid):
            _sessions[sid] = session
            _sessions.move_to_end(sid)
        return session


def add_upload(session: dict, result: dict) -> str:
    """把一份体验者上传作业的批改结果存进会话，返回分配的临时 ID。

    ID 在这里就地写回 result["submission_id"]，保证「字典的键」和「结果里
    自称的 ID」永远一致——两者不一致时，教师终审会找不到对应作业。

    超过单会话上限时淘汰最旧一件，保证内存有界；被淘汰件若已有终审记录
    一并清除，避免留下指向不存在作业的孤儿记录。
    """
    with _lock:
        session["upload_seq"] += 1
        upload_id = "UP-%03d" % session["upload_seq"]
        result["submission_id"] = upload_id
        session["uploads"][upload_id] = result
        while len(session["uploads"]) > UPLOAD_MAX:
            dropped, _ = session["uploads"].popitem(last=False)
            session["reviews"].pop(dropped, None)
        return upload_id


def stats() -> dict:
    """会话表概况（运维排查用，不含任何作答内容）。"""
    with _lock:
        return {
            "sessions": len(_sessions),
            "session_max": SESSION_MAX,
            "session_ttl": SESSION_TTL,
            "upload_max": UPLOAD_MAX,
        }
