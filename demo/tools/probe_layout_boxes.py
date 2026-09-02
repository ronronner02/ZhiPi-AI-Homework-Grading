# -*- coding: utf-8 -*-
"""只跑版面解析，打印每个手写块的位置与形状。

复识能不能救回一道题，前提是版面阶段给出的块框对不对。这里只花一次调用把
框本身摊开看：宽高比、在页面的哪一侧、够不够大——「作答侧写在页边」这类
判断完全建立在这些数字上，判据调阈值时不该靠跑整条链路去猜。

    py -3 tools/probe_layout_boxes.py <用例名>
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402,F401  —— 只为触发 deploy/.env 的加载
from pipeline import ocr  # noqa: E402
from PIL import Image  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA = Path(r"E:\希沃智教π\测评数据\测评数据")
CASES = {
    "hardwork_117": "数学/学生页/hardwork_117.jpg",
    "hardwork_127": "数学/学生页/hardwork_127.jpg",
    "89_student_2": "数学/学生页/89_student_2.jpg",
    "83_student_2": "数学/学生页/83_student_2.jpg",
    "page_29": "英语/学生页/page_29.jpg",
}

name = sys.argv[1] if len(sys.argv) > 1 else "89_student_2"
raw = (DATA / CASES[name]).read_bytes()
if len(raw) > 1200 * 1024:                      # 复刻前端 compressImage
    with Image.open(io.BytesIO(raw)) as im:
        k = min(1, 1600 / max(im.size))
        o = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                     Image.LANCZOS)
        buf = io.BytesIO()
        o.save(buf, "JPEG", quality=88)
        raw = buf.getvalue()
work = ocr.normalize_orientation(raw, "image/jpeg")

data = ocr._call_vlm(work, "image/jpeg", ocr.LAYOUT_PROMPT)
questions = ocr._normalize_layout(data)
writings = ocr._normalize_writings(data)
print("%s  题 %d  手写块 %d" % (name, len(questions), len(writings)))

def fmt(b) -> str:
    return "x %.2f-%.2f  y %.2f-%.2f" % (b[0], b[2], b[1], b[3]) if b else "无框"


print("\n[题目框]")
for q in questions:
    print("  题%-4s %-6s %s" % (q.get("no") or "?", q.get("qtype") or "",
                                fmt(q.get("bbox"))))

print("\n[手写块]  宽高比 = 宽/高；框形状离谱（比 < %.2f 且高 >= %.2f）的不参与复识裁图"
      % (ocr._BOX_MIN_RATIO, ocr._BOX_MIN_H))
for w in writings:
    b = w.get("bbox")
    if not b:
        print("  #%-3d 无框  %s" % (w["id"], (w.get("brief") or "")[:40]))
        continue
    ww, hh = b[2] - b[0], b[3] - b[1]
    print("  #%-3d x %.2f-%.2f  y %.2f-%.2f  宽%.2f 高%.2f 比%.2f  %s  %s"
          % (w["id"], b[0], b[2], b[1], b[3], ww, hh, ww / hh if hh else 0,
             "可信" if ocr._box_trustworthy(b) else "形状离谱",
             (w.get("brief") or "")[:34]))
