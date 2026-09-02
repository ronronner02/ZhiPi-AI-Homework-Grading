# -*- coding: utf-8 -*-
"""复识链路细查：某道题的答案是怎么来的、复识有没有跑、跑出了什么。

check_marked_orientation.py 只给最终转写，看不出「整页归属读到什么 → 复识改成
什么」。分式漏读这类问题的分歧点恰恰在这两步之间，所以单独打一份细账。

    py -3 tools/probe_refine_trace.py <用例名> [题号...]

顺带把指定题的裁图存下来，用来核对学生到底写了什么（ground truth）。
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402,F401  —— 只为触发 deploy/.env 的加载
from pipeline import ocr  # noqa: E402
from PIL import Image  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA = Path(r"E:\希沃智教π\测评数据\测评数据")
OUT = Path(__file__).resolve().parent / "ab_recognize_out"
CASE_PATHS = {
    "hardwork_117": "数学/学生页/hardwork_117.jpg",
    "hardwork_127": "数学/学生页/hardwork_127.jpg",
    "89_student_2": "数学/学生页/89_student_2.jpg",
    "83_student_2": "数学/学生页/83_student_2.jpg",
    "page_29": "英语/学生页/page_29.jpg",
    "page_9": "英语/学生页/page_9.jpg",
    "page_42": "语文/学生页/page_42.jpg",
    "page_57": "语文/学生页/page_57.jpg",
}

name = sys.argv[1] if len(sys.argv) > 1 else "hardwork_117"
want = set(sys.argv[2:])
src = DATA / CASE_PATHS[name]
raw = src.read_bytes()

# 复刻前端 compressImage：>1200KB 才压，长边 1600
if len(raw) > 1200 * 1024:
    with Image.open(io.BytesIO(raw)) as im:
        k = min(1, 1600 / max(im.size))
        o = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                     Image.LANCZOS)
        buf = io.BytesIO()
        o.save(buf, "JPEG", quality=88)
        raw = buf.getvalue()
work = ocr.normalize_orientation(raw, "image/jpeg")
with Image.open(io.BytesIO(work)) as im:
    W, H = im.size
print("工作图 %dx%d  %d KB" % (W, H, len(work) // 1024))

calls = []
_orig = ocr._call_vlm


def traced(image_bytes, mime, prompt, strong=False):
    tag = ("LAYOUT" if prompt is ocr.LAYOUT_PROMPT
           else "REFINE" if "复识" in prompt or "REFINE" in prompt[:40]
           else "ATTR" if "归属" in prompt[:200] else "OTHER")
    out = _orig(image_bytes, mime, prompt, strong=strong)
    calls.append((tag, strong, len(image_bytes)))
    return out


ocr._call_vlm = traced
try:
    res = ocr.recognize_vlm_staged(work, "image/jpeg")
finally:
    ocr._call_vlm = _orig

print("\n模型调用 %d 次：%s" % (len(calls),
      "  ".join("%s%s" % (t, "(强)" if s else "") for t, s, _ in calls)))
print("\n" + "=" * 78)
for q in res.get("questions", []):
    no = str(q.get("no") or "")
    if want and no not in want:
        continue
    print("题 %-4s [%s]" % (no, q.get("qtype") or "?"))
    print("  stem            : %s" % (q.get("stem") or "")[:90])
    print("  answer          : %r" % (q.get("answer") or ""))
    for k in ("refined", "refine_reason", "refine_changed", "refine_prev",
              "attribution_confidence", "overflow", "legible_hint", "legible"):
        if k in q:
            print("  %-16s: %r" % (k, q[k]))
    bb = q.get("bbox")
    print("  bbox            : %r" % (bb,))
    if bb and want:
        x0, y0, x1, y1 = bb
        pad = 0.02
        box = (max(0, int((x0 - pad) * W)), max(0, int((y0 - pad) * H)),
               min(W, int((x1 + pad) * W)), min(H, int((y1 + pad) * H)))
        OUT.mkdir(exist_ok=True)
        p = OUT / ("crop_%s_q%s.png" % (name, no.replace("/", "_")))
        with Image.open(io.BytesIO(work)) as im:
            im.crop(box).resize(((box[2] - box[0]) * 2, (box[3] - box[1]) * 2),
                                Image.LANCZOS).save(p)
        print("  裁图            : %s" % p)
    print("-" * 78)

meta = {k: v for k, v in res.items() if k not in ("questions", "text")}
print("整页元信息：%s" % json.dumps(meta, ensure_ascii=False)[:400])
