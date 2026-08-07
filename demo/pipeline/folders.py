"""作业文件夹：把上传件与 Demo 样例按夹归档，支持整夹一键批改。

设计约束（与 session_store 对齐）：
- 按浏览器会话隔离，重置演示时一并清空；
- Demo 样例夹是系统夹，不可删、不可改名；
- 自建夹只装本会话上传件；Demo 夹虚拟挂载内置样例图；
- 不落盘——Demo 进程重启后自建夹消失，符合「体验态」边界。
"""
import re
import time
from collections import OrderedDict

DEMO_FOLDER_ID = "demo"
DEMO_FOLDER_NAME = "Demo 样例"

# 夹名：中文 / 英文 / 数字 / 空格 / 常见分隔符，1–24 字
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff \-_.·（）()]{1,24}$")


def ensure(session: dict) -> None:
    """保证会话里有 folders 结构，并种子化 Demo 样例夹。"""
    if "folders" not in session:
        session["folders"] = OrderedDict()
        session["folder_seq"] = 0
        session["active_folder_id"] = DEMO_FOLDER_ID
    if DEMO_FOLDER_ID not in session["folders"]:
        session["folders"][DEMO_FOLDER_ID] = {
            "folder_id": DEMO_FOLDER_ID,
            "name": DEMO_FOLDER_NAME,
            "kind": "demo",
            "created": time.time(),
            "item_ids": [],   # Demo 夹不靠 item_ids；内置样例在 list 时虚拟挂载
        }
        # 保持 Demo 夹在最前
        session["folders"].move_to_end(DEMO_FOLDER_ID, last=False)
    if not session.get("active_folder_id"):
        session["active_folder_id"] = DEMO_FOLDER_ID


def _clean_name(raw: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", (raw or "")).strip()
    name = re.sub(r"\s+", " ", name)
    if not name or not _NAME_RE.match(name):
        raise ValueError("文件夹名称不合法（1–24 字，中英文/数字/空格/常见符号）")
    if name == DEMO_FOLDER_NAME:
        raise ValueError("「%s」为系统保留名" % DEMO_FOLDER_NAME)
    return name


def create(session: dict, name: str) -> dict:
    """自建文件夹，返回夹元数据。"""
    ensure(session)
    clean = _clean_name(name)
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
    if folder.get("kind") == "demo":
        raise ValueError("Demo 样例夹不可改名")
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
    if folder.get("kind") == "demo":
        raise ValueError("Demo 样例夹不可删除")
    # 清掉上传件上的 folder_id 归属，避免指向已删夹
    for uid in list(folder.get("item_ids") or []):
        up = session.get("uploads", {}).get(uid)
        if up and up.get("folder_id") == folder_id:
            up["folder_id"] = None
    del session["folders"][folder_id]
    if session.get("active_folder_id") == folder_id:
        session["active_folder_id"] = DEMO_FOLDER_ID


def set_active(session: dict, folder_id: str) -> str:
    ensure(session)
    if folder_id not in session["folders"]:
        raise KeyError("文件夹不存在：%s" % folder_id)
    session["active_folder_id"] = folder_id
    return folder_id


def add_item(session: dict, folder_id: str, item_id: str) -> None:
    """把一份上传件挂到指定夹（Demo 夹也可接收用户上传）。"""
    ensure(session)
    folder = session["folders"].get(folder_id)
    if not folder:
        raise KeyError("文件夹不存在：%s" % folder_id)
    ids = folder.setdefault("item_ids", [])
    if item_id not in ids:
        ids.append(item_id)


def resolve_target(session: dict, folder_id: str | None) -> str:
    """解析上传目标夹：显式指定 > 当前活动夹 > Demo。"""
    ensure(session)
    if folder_id:
        if folder_id not in session["folders"]:
            raise KeyError("文件夹不存在：%s" % folder_id)
        return folder_id
    active = session.get("active_folder_id") or DEMO_FOLDER_ID
    if active not in session["folders"]:
        return DEMO_FOLDER_ID
    return active


def get(session: dict, folder_id: str) -> dict:
    ensure(session)
    folder = session["folders"].get(folder_id)
    if not folder:
        raise KeyError("文件夹不存在：%s" % folder_id)
    return folder


def list_summaries(session: dict, sample_count: int = 0) -> list:
    """夹列表摘要，供前端堆叠卡片用。"""
    ensure(session)
    rows = []
    for fid, f in session["folders"].items():
        user_n = len(f.get("item_ids") or [])
        # Demo 夹：内置样例数 + 用户丢进 Demo 夹的上传数
        if f.get("kind") == "demo":
            count = sample_count + user_n
            stack = min(4, max(1, count))
        else:
            count = user_n
            stack = min(4, max(0, count))
        rows.append({
            "folder_id": fid,
            "name": f["name"],
            "kind": f.get("kind", "user"),
            "count": count,
            "stack": stack,
            "created": f.get("created"),
            "is_active": fid == session.get("active_folder_id"),
        })
    return rows
