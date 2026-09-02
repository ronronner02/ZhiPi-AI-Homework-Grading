# -*- coding: utf-8 -*-
"""一次性诊断：三阶段 vs 单次识别，各自与题库的对齐结果对照。

排查「题库已上传、批改仍显示题库中无此题」。对齐**只看题干相似度**
（bank.align，阈值 0.55），不看题号，所以两条识别路径产出的 stem
写法一旦不同，匹配率就会变。这里把两条路的 stem 与题库 stem 摆在一起，
连相似度一起打出来，判断问题出在识别端还是阈值上。

    py -3 tools/probe_bank_align.py [教师页] [学生页]
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402  （import 副作用即加载 .env）
from pipeline import bank, ocr  # noqa: E402

BASE = Path(r"E:\希沃智教π\测评数据\测评数据\数学")
teacher_p = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "教师页" / "83.jpg"
student_p = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE / "学生页" / "83_student_2.jpg"

t_raw = ocr.normalize_orientation(teacher_p.read_bytes(), "image/jpeg")
s_raw = ocr.normalize_orientation(student_p.read_bytes(), "image/jpeg")
print("教师页: %s   学生页: %s" % (teacher_p.name, student_p.name))
print("对齐阈值 MATCH_THRESHOLD = %s" % bank.MATCH_THRESHOLD)
print("=" * 78)

# ---- 题库 ----
t0 = time.time()
tp = ocr.recognize_teacher_page(t_raw, "image/jpeg")
bank_qs = bank.build_questions([{"page_id": "P1", "page_no": 1,
                                 "questions": tp.get("questions") or []}])
print("\n[题库] %.1fs  学科=%s  题数=%d" % (time.time() - t0, tp.get("subject"), len(bank_qs)))
for q in bank_qs:
    print("  %-4s no=%-6s %s" % (q["qid"], q["no"], (q["stem"] or "")[:56]))


def report(tag, questions):
    print("\n[%s] 识别题数=%d" % (tag, len(questions)))
    alignment = bank.align(questions, bank_qs)
    hit = sum(1 for a in alignment if a["qid"])
    print("  对上题库: %d / %d" % (hit, len(questions)))
    for sq, a in zip(questions, alignment):
        stem = (sq.get("stem") or "").strip()
        # 没对上的题，把它与每道题库题的最高相似度也算出来——
        # 「最高才 0.31」和「最高 0.53 差一点过线」是两种完全不同的病。
        best, best_qid = 0.0, ""
        for bq in bank_qs:
            r = bank._similarity(stem, bq.get("stem", ""))
            if r > best:
                best, best_qid = r, bq["qid"]
        flag = ("命中 %s @%.3f" % (a["qid"], a["score"])) if a["qid"] \
            else ("未命中（最接近 %s @%.3f）" % (best_qid or "-", best))
        print("  no=%-6s %-52s %s" % (sq.get("no", ""), stem[:52], flag))


t0 = time.time()
try:
    staged = ocr.recognize_vlm_staged(s_raw, "image/jpeg")
    report("三阶段 %.1fs" % (time.time() - t0), staged.get("questions") or [])
except Exception as exc:
    print("\n[三阶段] 失败: %s: %s" % (type(exc).__name__, exc))

t0 = time.time()
try:
    single = ocr.recognize_vlm(s_raw, "image/jpeg")
    report("单次 %.1fs" % (time.time() - t0), single.get("questions") or [])
except Exception as exc:
    print("\n[单次] 失败: %s: %s" % (type(exc).__name__, exc))
