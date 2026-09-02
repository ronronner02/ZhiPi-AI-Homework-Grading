# -*- coding: utf-8 -*-
"""横置作业的批改痕迹诊断：marked 图到底是竖的还是横的，痕迹落在哪。

courselearn_21.jpg 是「横着存像素 + EXIF Orientation=8」的照片。链路上每一处
都声称做了朝向归一，但界面上看到的批改件仍然是横的、痕迹挤在最右边。这个脚本
把整条链路的尺寸逐段打出来，看是哪一段没转：

    上传前原图 → _as_page_images 归一 → pagestore 存的 → 识别看到的
    → 每题 bbox → marked 图

前端 compressImage 只压 >1200KB 的图，courselearn_21 是 948KB，所以真实链路是
**原样上传**——这里也原样传，不做 shrink，否则测的不是同一条路。

    py -3 tools/check_marked_orientation.py [用例名]

产物：tools/ab_recognize_out/marked_<用例>.jpg
"""
import base64
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DATA = Path(r"E:\希沃智教π\测评数据\测评数据\数学\学生页")
NAME = sys.argv[1] if len(sys.argv) > 1 else "courselearn_21"
IMG = _DATA / ("%s.jpg" % NAME)
OUT = Path(__file__).resolve().parent / "ab_recognize_out"


def dim(raw, label):
    with Image.open(io.BytesIO(raw)) as im:
        ori = im.getexif().get(274)
        print("  %-22s %4dx%-5d  EXIF方向=%-4s  %s"
              % (label, im.width, im.height, ori,
                 "竖" if im.height > im.width else "横"))
        return im.size


def front_end_bytes(raw):
    """复刻前端 compressImage：>1200KB 才压，长边压到 1600。

    参数必须跟 static/js/app.js 里的 COMPRESS_SKIP / MAX_EDGE 一致，否则测的
    是另一条链路——真实上传的像素比测试用的少，识别质量就不是一回事。
    """
    if len(raw) <= 1200 * 1024:
        return raw, "不压缩，原样上传"
    with Image.open(io.BytesIO(raw)) as im:
        k = min(1, 1600 / max(im.size))
        out = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                       Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "压到长边 1600"


def main():
    raw = IMG.read_bytes()
    print("\n[链路各段尺寸]  %s  %d KB" % (IMG.name, len(raw) // 1024))
    dim(raw, "① 磁盘原图")
    sent, how = front_end_bytes(raw)
    print("  前端 compressImage：%s（阈值 1200KB）" % how)
    if sent is not raw:
        dim(sent, "①b 前端实际上传的")

    from pipeline import ocr as ocr_mod
    dim(ocr_mod.normalize_orientation(sent), "② normalize 后")

    client = TestClient(demo_app.app)
    code = os.environ.get("ZHIPI_ACCESS_CODE", "").strip()
    if code:
        client.cookies.set("zhipi_pass", code)
    client.get("/api/demo/config")

    up = client.post("/api/upload-pages", json={
        "image_base64": base64.b64encode(sent).decode(),
        "mime": "image/jpeg", "filename": IMG.name, "role": "student"}).json()
    page = up["pages"][0]
    page_id = page["page_id"]
    print("  ③ upload 返回的 width/height：%sx%s" % (page["width"], page["height"]))

    got = client.get("/api/page/%s" % page_id)
    stored = dim(got.content, "④ pagestore 存的原图")

    rec = client.post("/api/recognize-image", json={"page_id": page_id}).json()
    qs = rec.get("questions") or []
    print("  ⑤ 识别：%d 题，%d 题带 bbox" % (len(qs), sum(1 for q in qs if q.get("bbox"))))

    payload = [{"no": q.get("no"), "subject": q.get("subject"), "stem": q.get("stem"),
                "answer": q.get("answer") or "", "qtype": q.get("qtype"),
                "printed_max_score": q.get("printed_max_score"), "bbox": q.get("bbox"),
                "overflow": q.get("overflow"),
                "attribution_confidence": q.get("attribution_confidence"),
                "refined": q.get("refined"), "refine_changed": q.get("refine_changed"),
                "refine_reason": q.get("refine_reason"),
                "legible_hint": q.get("legible_hint")} for q in qs]
    gr = client.post("/api/grade-page", json={
        "page_id": page_id, "questions": payload,
        "subject": rec.get("subject") or "数学", "clarity": rec.get("clarity", 75)})
    if gr.status_code != 200:
        print("  批改失败：%s" % gr.text[:300])
        return 1
    g = gr.json()
    print("  ⑥ 批改：%s/%s 分，分流 %s，%d 题带 bbox"
          % (g.get("total_score"), g.get("max_score"), g.get("status"),
             sum(1 for q in g.get("questions") or [] if q.get("bbox"))))
    print("\n[逐题]")
    for q in g.get("questions") or []:
        print("  %-4s %-4s %4s/%-4s %-8s %s"
              % (q.get("no") or q.get("index"), q.get("qtype") or "",
                 q.get("score"), q.get("max_score"), q.get("verdict") or "",
                 (q.get("student_answer") or q.get("answer") or "(空)")
                 .replace("\n", " ⏎ ")[:56]))
    for f in g.get("review_flags") or []:
        print("  待复核：第 %s 题 · %s" % (f.get("no"), f.get("reason")))

    mk = client.get("/api/page/%s?marked=1" % page_id)
    if mk.status_code != 200:
        print("  取 marked 图失败：%d" % mk.status_code)
        return 1
    marked = dim(mk.content, "⑦ marked 批改件")
    OUT.mkdir(exist_ok=True)
    dst = OUT / ("marked_%s.jpg" % NAME)
    dst.write_bytes(mk.content)
    print("\n  批改件已存：%s" % dst)

    if marked != stored:
        print("  ** marked 图与 pagestore 原图尺寸不一致：%s vs %s" % (marked, stored))
    if marked[0] > marked[1]:
        print("  ** marked 图是横的——界面上就会看到横躺的批改件")

    # bbox 的分布：全都挤在同一侧，说明落的是 fallback 位置而不是题目位置
    boxes = [q["bbox"] for q in (g.get("questions") or []) if q.get("bbox")]
    if boxes:
        xs = [b[0] for b in boxes]
        print("  bbox 左边界范围 %.2f ~ %.2f（%d 个）" % (min(xs), max(xs), len(boxes)))
    print("  无 bbox 的题：%d 道（这些会落到右侧 fallback 位置堆叠）"
          % sum(1 for q in (g.get("questions") or []) if not q.get("bbox")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
