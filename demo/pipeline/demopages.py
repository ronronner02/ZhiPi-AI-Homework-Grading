# -*- coding: utf-8 -*-
"""内置样例作业页：三科真实作业 + 教师答案页的只读清单。

数据由 tools/pack_demo_pages.py 从真实测评数据压缩生成，落在
data/demo_pages/{manifest.json, files/, thumbs/}。本模块只做三件事：
读清单、按夹取条目、按 item_id 取文件路径。

为什么内置样例要走「文件」而不是像旧版那样走「预置转写」：
旧版内置的是程序合成的仿手写图 + 事先写好的转写文本，靠感知哈希命中后
直接返回，等于把识别与批改都跳过了——体验者看到的是一条**假链路**。
现在内置的是真实作业原件，取到的是文件字节，前端把它当成一份刚上传的
作业送进 upload-pages，后面的拆页、三阶段识别、整页批改、原图留痕
一步都不少，与体验者上传自己的作业**走的是同一条路**。

只读：进程内缓存 manifest，不接受任何写入；文件名一律由 manifest 白名单
决定，不接受外部拼路径（防目录穿越）。
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "demo_pages"
MANIFEST = DATA_DIR / "manifest.json"
FILES_DIR = DATA_DIR / "files"
THUMBS_DIR = DATA_DIR / "thumbs"

# 夹的展示顺序与名字。folder_id 与 manifest 里的 folder 字段一一对应。
FOLDERS = [
    {"folder_id": "bi-chinese", "name": "语文样例", "subject": "语文",
     "role": "student"},
    {"folder_id": "bi-math", "name": "数学样例", "subject": "数学",
     "role": "student"},
    {"folder_id": "bi-english", "name": "英语样例", "subject": "英语",
     "role": "student"},
    {"folder_id": "bi-bank", "name": "题库 · 教师答案页", "subject": "",
     "role": "teacher"},
]
FOLDER_IDS = [f["folder_id"] for f in FOLDERS]

_cache = None


def _load() -> list:
    """读 manifest（进程内缓存）。文件缺失时返回空清单，不抛异常——
    内置样例是体验入口，缺了应当让界面显示「暂无内置样例」，而不是整站 500。"""
    global _cache
    if _cache is not None:
        return _cache
    rows = []
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
        for it in data.get("items") or []:
            if it.get("folder") in FOLDER_IDS and (FILES_DIR / it["file"]).exists():
                rows.append(it)
    except (OSError, ValueError):
        rows = []
    _cache = rows
    return _cache


def available() -> bool:
    return bool(_load())


def listing() -> list:
    return list(_load())


def by_folder(folder_id: str) -> list:
    """夹内条目。题库夹按学科聚拢，其余按 item_id——都与 manifest 顺序一致，
    保证同一个夹每次打开的排列相同。"""
    rows = [it for it in _load() if it.get("folder") == folder_id]
    if folder_id == "bi-bank":
        order = {f["subject"]: i for i, f in enumerate(FOLDERS)}
        rows.sort(key=lambda it: (order.get(it.get("subject"), 99), it["item_id"]))
    else:
        rows.sort(key=lambda it: it["item_id"])
    return rows


def counts() -> dict:
    out = {fid: 0 for fid in FOLDER_IDS}
    for it in _load():
        out[it["folder"]] = out.get(it["folder"], 0) + 1
    return out


def get(item_id: str) -> dict | None:
    for it in _load():
        if it["item_id"] == item_id:
            return it
    return None


def file_path(item: dict) -> Path:
    return FILES_DIR / item["file"]


def thumb_path(item: dict) -> Path:
    return THUMBS_DIR / item["thumb"]


def titles(item_ids) -> list:
    """一组 item_id 的标题，用于「对应答案页：…」这类提示。"""
    index = {it["item_id"]: it["title"] for it in _load()}
    return [index[i] for i in (item_ids or []) if i in index]
