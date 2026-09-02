# -*- coding: utf-8 -*-
"""把三科真实作业打包成 Demo 内置样例（data/demo_pages/）。

为什么要有这个脚本，而不是把文件直接拷进仓库：

- 源图是手机 / 扫描原图，单张最大 12MB、总量 82.9MB，直接入库会让提交包翻几倍，
  而识别质量并不会因此变好——tools/probe_resolution.py 实测过 1800 / 2400 / 原图
  三档读数一致，转写失败的主因是模型档位与归属阶段，不是分辨率；
- 上传链路对单文件有 8MB 上限（guard.MAX_IMAGE_BYTES），12MB 的原图连传都传不上去；
- 学生页与教师答案页的配对关系没法从文件名可靠推出（数学的教师页是
  `harkwork_117.jpg`，学生页是 `hardwork_117.jpg`，差一个字母），必须显式落进 manifest。

用法：

    py -3 tools/pack_demo_pages.py                 # 用默认源目录
    py -3 tools/pack_demo_pages.py <源目录>         # 指定源目录
    py -3 tools/pack_demo_pages.py --check         # 只校验现有 manifest，不重打包

源目录结构（三科各自分学生页 / 教师页）：

    <源目录>/{数学,英语,语文}/{学生页,教师页}/*.{jpg,pdf}

产物：

    data/demo_pages/manifest.json     全部条目的元数据（含配对与 sha256）
    data/demo_pages/files/<id>.<ext>  压缩后的作业页（图片长边 2000 / JPEG q82；PDF 原样）
    data/demo_pages/thumbs/<id>.jpg   缩略图（长边 320，夹内清单用）

脚本可重复运行，同样的输入得到同样的输出（不写时间戳、不用随机数）。
"""
import hashlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEMO_DIR))

from pipeline import ocr as ocr_mod          # noqa: E402  复用 EXIF 方向归一化

DEFAULT_SRC = Path(r"E:\希沃智教π\测评数据\测评数据")
OUT_DIR = DEMO_DIR / "data" / "demo_pages"

# 图片长边上限。选 2000 而不是原图：前端 compressImage 对 >1200KB 的图会再压到
# 长边 1600，打包成 2000/q82 后多数落在 400–800KB，于是**原样上传**——服务端拿到
# 的就是这里的像素，链路上只有一次编码。
MAX_EDGE = 2000
JPEG_QUALITY = 82
THUMB_EDGE = 320
# 单份上限（与 guard.MAX_IMAGE_BYTES 同口径）。超了在这里就报出来，
# 而不是等体验者点了「加入待批清单」才在上传时 413。
MAX_ITEM_BYTES = 8 * 1024 * 1024

SUBJECTS = [
    ("数学", "math"),
    ("英语", "english"),
    ("语文", "chinese"),
]
STUDENT_DIR = "学生页"
TEACHER_DIR = "教师页"

FOLDER_BANK = "bi-bank"


def folder_of(subject_key: str, role: str) -> str:
    """学生页按学科分夹，教师答案页全部收进题库夹（夹内再按学科分节）。"""
    return FOLDER_BANK if role == "teacher" else "bi-" + subject_key


def norm_stem(stem: str) -> str:
    """把文件名归一到「同一份卷子」的键，用于学生页 ↔ 教师页配对。

    处理三处真实存在的不一致：
    - 学生页带 `_student_2` 之类的后缀，教师页没有；
    - 数学教师页把 hardwork 拼成了 harkwork；
    - page_9 / page9 两种写法混用。
    """
    s = stem.strip().lower()
    s = re.sub(r"_student(_\d+)?$", "", s)
    s = s.replace("harkwork", "hardwork")
    s = re.sub(r"[_\-\s]+", "", s)
    return s


def title_of(subject: str, stem: str, role: str, page_count: int) -> str:
    """人读的名字。学生页的标题会成为待批清单里的默认学生名，所以要短。"""
    s = stem.strip()
    base = None
    m = re.match(r"^page[_\-]?(\d+)$", s, re.I)
    if m:
        base = "第%s页" % m.group(1)
    if base is None:
        m = re.match(r"^(hardwork|harkwork)[_\-]?(\d+)$", s, re.I)
        if m:
            base = "作业%s" % m.group(2)
    if base is None:
        m = re.match(r"^courselearn[_\-]?(\d+)$", s, re.I)
        if m:
            base = "课练%s" % m.group(1)
    if base is None:
        m = re.match(r"^(\d+)(?:_student(?:_\d+)?)?$", s, re.I)
        if m:
            base = "%s号卷" % m.group(1)
    if base is None:
        base = re.sub(r"[_\-]+", " ", s)[:12]

    name = "%s·%s" % (subject, base)
    if role == "teacher":
        name += " 答案页"
    if page_count > 1:
        name += "（%d页）" % page_count
    return name[:20]      # 与前端 NAME_MAX / 后端 _clean_name 的截断长度一致


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def pdf_page_count(data: bytes) -> int:
    try:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return 1


def compress_image(src: Path) -> bytes:
    """EXIF 拉平 → 长边 MAX_EDGE → JPEG。返回压缩后的字节。"""
    from PIL import Image
    raw = ocr_mod.normalize_orientation(src.read_bytes())
    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY,
                 optimize=True, progressive=True)
    return out.getvalue()


def make_thumb(data: bytes, is_pdf_file: bool) -> bytes:
    """缩略图。PDF 取第 1 页渲染——夹内清单要能看出这是哪张卷子。"""
    from PIL import Image
    if is_pdf_file:
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            pix = doc[0].get_pixmap(dpi=60)
            data = pix.tobytes("png")
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80, optimize=True)
    return out.getvalue()


def collect(src_root: Path) -> list:
    """扫描源目录，产出条目骨架（尚未压缩）。"""
    rows = []
    for subject, key in SUBJECTS:
        for role, sub_dir in (("student", STUDENT_DIR), ("teacher", TEACHER_DIR)):
            d = src_root / subject / sub_dir
            if not d.is_dir():
                raise SystemExit("源目录缺少 %s" % d)
            files = sorted(p for p in d.iterdir()
                           if p.is_file() and not p.name.startswith(".")
                           and p.suffix.lower() in (".jpg", ".jpeg", ".png",
                                                    ".webp", ".pdf"))
            for idx, path in enumerate(files, 1):
                rows.append({
                    "subject": subject,
                    "subject_key": key,
                    "role": role,
                    "src": path,
                    "stem": path.stem,
                    "item_id": "%s-%s-%02d" % (key, role[0], idx),
                })
    return rows


def pair_up(rows: list) -> None:
    """写入 pairs_with：学生页 ↔ 对应教师答案页（多对多，故用列表）。"""
    teachers = {}
    for r in rows:
        if r["role"] == "teacher":
            teachers.setdefault(r["subject_key"], {}) \
                    .setdefault(norm_stem(r["stem"]), []).append(r["item_id"])

    for r in rows:
        if r["role"] != "teacher":
            continue
        r["pairs_with"] = []

    for r in rows:
        if r["role"] == "teacher":
            continue
        by_stem = teachers.get(r["subject_key"], {})
        hit = by_stem.get(norm_stem(r["stem"]), [])
        if not hit and r["subject_key"] == "chinese":
            # 语文的学生页是练习册、教师页是答案册（杨老师—1 / —2），版式不同、
            # 文件名也对不上，但两份答案册合起来正是这几页练习的答案。
            hit = [t for stem, ids in by_stem.items() for t in ids
                   if "老师" in stem or "laoshi" in stem]
        r["pairs_with"] = sorted(set(hit))
        for tid in r["pairs_with"]:
            for t in rows:
                if t["item_id"] == tid and r["item_id"] not in t["pairs_with"]:
                    t["pairs_with"].append(r["item_id"])


def build(src_root: Path) -> dict:
    rows = collect(src_root)
    pair_up(rows)

    files_dir = OUT_DIR / "files"
    thumbs_dir = OUT_DIR / "thumbs"
    for d in (files_dir, thumbs_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    items = []
    total = 0
    for r in rows:
        src = r["src"]
        pdf = is_pdf(src)
        if pdf:
            data = src.read_bytes()          # PDF 原样保留：正好演示自动拆页
            ext, mime = "pdf", "application/pdf"
            pages = pdf_page_count(data)
        else:
            data = compress_image(src)
            ext, mime = "jpg", "image/jpeg"
            pages = 1

        if len(data) > MAX_ITEM_BYTES:
            raise SystemExit(
                "%s 压缩后仍有 %.1fMB，超过上传上限 %dMB，请先降采样源文件"
                % (src.name, len(data) / 1048576, MAX_ITEM_BYTES // 1048576))

        name = "%s.%s" % (r["item_id"], ext)
        (files_dir / name).write_bytes(data)
        thumb = make_thumb(data, pdf)
        (thumbs_dir / ("%s.jpg" % r["item_id"])).write_bytes(thumb)
        total += len(data) + len(thumb)

        items.append({
            "item_id": r["item_id"],
            "folder": folder_of(r["subject_key"], r["role"]),
            "subject": r["subject"],
            "subject_key": r["subject_key"],
            "role": r["role"],
            "title": title_of(r["subject"], r["stem"], r["role"], pages),
            # 学生页进待批清单时的默认姓名：标题去掉页数后缀。页数是「这份怎么拆」
            # 的信息，不该出现在学生名里。
            "stage_name": re.sub(r"（\d+页）$", "",
                                 title_of(r["subject"], r["stem"], r["role"], pages)),
            "source_name": src.name,
            "file": name,
            "thumb": "%s.jpg" % r["item_id"],
            "mime": mime,
            "page_count": pages,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "pairs_with": r["pairs_with"],
        })

    manifest = {
        "version": 1,
        "note": "三科真实作业内置样例。学生页按学科分夹，教师答案页收在题库夹。"
                "由 tools/pack_demo_pages.py 生成，勿手改。",
        "max_edge": MAX_EDGE,
        "items": items,
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"manifest": manifest, "total": total}


def report(manifest: dict, total: int) -> None:
    items = manifest["items"]
    by_folder = {}
    for it in items:
        by_folder.setdefault(it["folder"], []).append(it)
    print("共 %d 份，合计 %.1fMB（含缩略图）" % (len(items), total / 1048576))
    for fid in ("bi-chinese", "bi-math", "bi-english", "bi-bank"):
        rows = by_folder.get(fid, [])
        size = sum(r["bytes"] for r in rows)
        print("  %-11s %2d 份  %5.1fMB" % (fid, len(rows), size / 1048576))
    orphan = [it["item_id"] for it in items
              if it["role"] == "student" and not it["pairs_with"]]
    if orphan:
        print("  ! 未配到答案页的学生页：%s" % ", ".join(orphan))
    big = [(it["item_id"], it["bytes"]) for it in items
           if it["bytes"] > 1200 * 1024]
    if big:
        print("  · 超过前端免压阈值（1200KB）、上传时会被再压到长边 1600 的：%d 份"
              % len(big))


def check() -> int:
    """只校验：manifest 与磁盘文件是否一致（sha256 + 存在性）。"""
    path = OUT_DIR / "manifest.json"
    if not path.exists():
        print("manifest 不存在，请先运行打包")
        return 1
    manifest = json.loads(path.read_text(encoding="utf-8"))
    bad = []
    for it in manifest["items"]:
        fp = OUT_DIR / "files" / it["file"]
        tp = OUT_DIR / "thumbs" / it["thumb"]
        if not fp.exists() or not tp.exists():
            bad.append("%s 文件缺失" % it["item_id"])
            continue
        if hashlib.sha256(fp.read_bytes()).hexdigest() != it["sha256"]:
            bad.append("%s sha256 不符" % it["item_id"])
    if bad:
        print("校验失败：\n  " + "\n  ".join(bad))
        return 1
    total = sum(it["bytes"] for it in manifest["items"])
    print("校验通过：%d 份，%.1fMB" % (len(manifest["items"]), total / 1048576))
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--check" in args:
        return check()
    src = Path(args[0]) if args else DEFAULT_SRC
    if not src.is_dir():
        print("源目录不存在：%s" % src)
        return 1
    print("源目录：%s" % src)
    out = build(src)
    report(out["manifest"], out["total"])
    print("产物：%s" % OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
