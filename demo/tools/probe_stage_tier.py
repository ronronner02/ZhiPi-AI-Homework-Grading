# -*- coding: utf-8 -*-
"""档位实验：归属阶段（第二阶段）换强模型值不值。

现状是「版面/归属走轻量档，只有定向复识换强模型」。但实测下来错读多半发生在
归属阶段——复识只能重读已经归属好的块，归属本身读漏、读串它救不回来。
这个脚本按同一张图跑三种配置，比耗时与结果：

    A 全轻量档（现状）
    B 轻量档 LAYOUT + 强模型 ATTR
    C 全强模型（含 LAYOUT）

    py -3 tools/probe_stage_tier.py <用例名> [重复次数]

重复多次是刻意的：轻量档同一张图两次能给出不同答案，只跑一次分不清
「这次对了」和「这条路稳定」。
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo_app  # noqa: E402,F401  —— 触发 deploy/.env 加载
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

name = sys.argv[1] if len(sys.argv) > 1 else "hardwork_117"
rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 1
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
print("%s  工作图 %d KB  强模型=%s" % (name, len(work) // 1024,
                                       ocr.strong_model_name() or "未配置"))


def run(layout_strong: bool, attr_strong: bool) -> dict:
    t0 = time.time()
    layout = ocr._call_vlm(work, "image/jpeg", ocr.LAYOUT_PROMPT, strong=layout_strong)
    t1 = time.time()
    questions = ocr._normalize_layout(layout)
    writings = ocr._normalize_writings(layout)
    if not questions:
        raise ValueError("版面解析未识别到任何题目")
    attr = ocr._call_vlm(work, "image/jpeg",
                         ocr.build_attribution_prompt(questions, writings),
                         strong=attr_strong)
    t2 = time.time()
    ocr._apply_attribution(questions, attr, writings)
    return {"layout_s": t1 - t0, "attr_s": t2 - t1, "questions": questions,
            "writings": len(writings)}


PLANS = [("A 全轻量档", False, False),
         ("B 轻量LAYOUT+强ATTR", False, True),
         ("C 全强模型", True, True)]

for label, ls, as_ in PLANS:
    for r in range(rounds):
        try:
            out = run(ls, as_)
        except Exception as exc:
            print("\n%-22s 第%d次  失败：%s %s"
                  % (label, r + 1, type(exc).__name__, str(exc)[:120]))
            continue
        print("\n%-22s 第%d次  版面 %.1fs + 归属 %.1fs = %.1fs  题%d 手写块%d"
              % (label, r + 1, out["layout_s"], out["attr_s"],
                 out["layout_s"] + out["attr_s"], len(out["questions"]),
                 out["writings"]))
        for q in out["questions"]:
            ans = (q.get("answer") or "").replace("\n", " ⏎ ")
            print("   %-5s %-5s %s" % (q.get("no") or "?", q.get("qtype") or "",
                                       ans[:96]))
