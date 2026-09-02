"""作业文件夹：把上传件与内置样例按夹归档。

设计约束（与 session_store 对齐）：
- 按浏览器会话隔离，重置演示时一并清空；
- 内置样例夹（语文 / 数学 / 英语 / 题库）是系统夹，不可删、不可改名；
- 自建夹只装本会话上传件；内置夹虚拟挂载 demopages 的清单；
- 不落盘——Demo 进程重启后自建夹消失，符合「体验态」边界。
"""
import re
import time
from collections import OrderedDict

from . import demopages

# 内置夹取自 demopages（那里同时定义了夹内该有哪些文件），这里只关心夹本身。
BUILTIN_FOLDERS = demopages.FOLDERS
BUILTIN_IDS = demopages.FOLDER_IDS
BUILTIN_NAMES = {f["name"] for f in BUILTIN_FOLDERS}

# 上传件的默认落点。选数学而不是题库夹：题库夹放的是教师答案页，
# 学生作业掉进去会让「这个夹里的东西该拿去建库还是拿去批改」变得含糊。
DEFAULT_FOLDER_ID = "bi-math"

# 单会话自建夹上限。与 session_store 的 SESSION_MAX / UPLOAD_MAX 同一套思路：
# 公开链接上任何「用户想建多少就建多少」的结构都是内存增长面——会话本身有
# 300 个上限和 TTL 兜着，但每个会话里的夹子不封顶，两者乘起来就没有边界了。
# 32 个夹对真实备课场景（按班级 / 按周次分夹）绰绰有余。
MAX_FOLDERS = 32

# 夹名：中文 / 英文 / 数字 / 空格 / 常见分隔符，1–24 字
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff \-_.·（）()]{1,24}$")


def ensure(session: dict) -> None:
    """保证会话里有 folders 结构，并种子化四个内置样例夹。"""
    if "folders" not in session:
        session["folders"] = OrderedDict()
        session["folder_seq"] = 0
        session["active_folder_id"] = DEFAULT_FOLDER_ID
    # 旧会话（升级前签发的 cookie）里可能还留着 "demo" 夹。直接丢掉会连带
    # 丢掉用户那时上传进去的作业，所以把它的上传件挪到默认夹再删。
    legacy = session["folders"].pop("demo", None)
    legacy_items = list((legacy or {}).get("item_ids") or [])
    # 内置夹排在最前，且顺序与 BUILTIN_FOLDERS 一致；move_to_end(last=False)
    # 是往队首插，所以要倒着遍历才能得到正序。
    for meta in reversed(BUILTIN_FOLDERS):
        fid = meta["folder_id"]
        if fid not in session["folders"]:
            session["folders"][fid] = {
                "folder_id": fid,
                "name": meta["name"],
                "kind": "builtin",
                "subject": meta.get("subject", ""),
                "created": time.time(),
                # 内置样例不放这里（list 时按 manifest 虚拟挂载），
                # 但用户可以把自己的上传件丢进内置夹，所以列表仍要有。
                "item_ids": [],
            }
        session["folders"].move_to_end(fid, last=False)
    if legacy_items:
        target = session["folders"][DEFAULT_FOLDER_ID].setdefault("item_ids", [])
        for uid in legacy_items:
            if uid not in target:
                target.append(uid)
    if session.get("active_folder_id") not in session["folders"]:
        session["active_folder_id"] = DEFAULT_FOLDER_ID


def is_builtin(folder: dict) -> bool:
    return folder.get("kind") == "builtin"


def _clean_name(raw: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", (raw or "")).strip()
    name = re.sub(r"\s+", " ", name)
    if not name or not _NAME_RE.match(name):
        raise ValueError("文件夹名称不合法（1–24 字，中英文/数字/空格/常见符号）")
    if name in BUILTIN_NAMES:
        raise ValueError("「%s」为系统保留名" % name)
    return name


def create(session: dict, name: str) -> dict:
    """自建文件夹，返回夹元数据。"""
    ensure(session)
    clean = _clean_name(name)
    # 上限只数自建夹：内置系统夹是种子化出来的，不该占用户的额度
    user_count = sum(1 for f in session["folders"].values()
                     if not is_builtin(f))
    if user_count >= MAX_FOLDERS:
        raise ValueError("文件夹数量已达上限（%d 个），请先删除不用的文件夹"
                         % MAX_FOLDERS)
    for f in session["folders"].values():
        if f["name"] == clean:
            raise ValueError("已存在同名文件夹：%s" % clean)
    session["folder_seq"] = int(session.get("folder_seq") or 0) + 1
    fid = "FD-%03d" % session["folder_seq"]
    meta = {
        "folder_id": fid,
        "name": clean,
        "kind": "user",
        "created": time.time(),
        "item_ids": [],
    }
    session["folders"][fid] = meta
    session["active_folder_id"] = fid
    return dict(meta)


def rename(session: dict, folder_id: str, name: str) -> dict:
    ensure(session)
    folder = session["folders"].get(folder_id)
    if not folder:
        raise KeyError("文件夹不存在：%s" % folder_id)
    if is_builtin(folder):
        raise ValueError("内置样例夹不可改名")
    clean = _clean_name(name)
    for fid, f in session["folders"].items():
        if fid != folder_id and f["name"] == clean:
            raise ValueError("已存在同名文件夹：%s" % clean)
    folder["name"] = clean
    return dict(folder)


def delete(session: dict, folder_id: str) -> None:
    """删除自建夹。夹内上传件仍留在 session.uploads，只是不再归属此夹。"""
    ensure(session)
    folder = session["folders"].get(folder_id)
    if not folder:
        raise KeyError("文件夹不存在：%s" % folder_id)
    if is_builtin(folder):
        raise ValueError("内置样例夹不可删除")
    # 清掉上传件上的 folder_id 归属，避免指向已删夹
    for uid in list(folder.get("item_ids") or []):
        up = session.get("uploads", {}).get(uid)
        if up and up.get("folder_id") == folder_id:
            up["folder_id"] = None
    del session["folders"][folder_id]
    if session.get("active_folder_id") == folder_id:
        session["active_folder_id"] = DEFAULT_FOLDER_ID


def set_active(session: dict, folder_id: str) -> str:
    ensure(session)
    if folder_id not in session["folders"]:
        raise KeyError("文件夹不存在：%s" % folder_id)
    session["active_folder_id"] = folder_id
    return folder_id


def add_item(session: dict, folder_id: str, item_id: str) -> None:
    """把一份上传件挂到指定夹（内置夹也可接收用户上传）。"""
    ensure(session)
    folder = session["folders"].get(folder_id)
    if not folder:
        raise KeyError("文件夹不存在：%s" % folder_id)
    ids = folder.setdefault("item_ids", [])
    if item_id not in ids:
        ids.append(item_id)


def resolve_target(session: dict, folder_id: str | None) -> str:
    """解析上传目标夹：显式指定 > 当前活动夹 > 默认夹。"""
    ensure(session)
    if folder_id:
        if folder_id not in session["folders"]:
            raise KeyError("文件夹不存在：%s" % folder_id)
        return folder_id
    active = session.get("active_folder_id") or DEFAULT_FOLDER_ID
    if active not in session["folders"]:
        return DEFAULT_FOLDER_ID
    return active


def get(session: dict, folder_id: str) -> dict:
    ensure(session)
    folder = session["folders"].get(folder_id)
    if not folder:
        raise KeyError("文件夹不存在：%s" % folder_id)
    return folder


def list_summaries(session: dict, builtin_counts: dict | None = None) -> list:
    """夹列表摘要，供前端堆叠卡片用。

    内置夹的件数 = 内置样例数（按 manifest）+ 用户丢进这个夹的上传数。
    """
    ensure(session)
    counts = builtin_counts or {}
    rows = []
    for fid, f in session["folders"].items():
        user_n = len(f.get("item_ids") or [])
        builtin_n = counts.get(fid, 0) if is_builtin(f) else 0
        count = builtin_n + user_n
        rows.append({
            "folder_id": fid,
            "name": f["name"],
            "kind": f.get("kind", "user"),
            "subject": f.get("subject", ""),
            "count": count,
            "stack": min(4, max(1 if is_builtin(f) else 0, count)),
            "created": f.get("created"),
            "is_active": fid == session.get("active_folder_id"),
        })
    return rows
