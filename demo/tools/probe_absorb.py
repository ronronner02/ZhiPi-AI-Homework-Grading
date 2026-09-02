# -*- coding: utf-8 -*-
"""验证小问归并：跑真实的教师页 + 学生页，对比归并前后的对齐结果。

排查的现象是「教师页把 2(1)(2)(3)(4) 拆成四条、学生页第 2 题是一整块」，
于是三条小问被报成「题库里有、本页没识别到」。这里直接调 app._align_to_bank
（界面走的同一个函数），看 missing 是否归零、满分是否正确加总。

    py -3 tools/probe_absorb.py [教师页] [学生页]
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402
from pipeline import bank, ocr  # noqa: E402

BASE = Path(r"E:\希沃智教π\测评数据\测评数据\数学")
teacher_p = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "教师页" / "83.jpg"
student_p = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE / "学生页" / "83_student_2.jpg"

t_raw = ocr.normalize_orientation(teacher_p.read_bytes(), "image/jpeg")
s_raw = ocr.normalize_orientation(student_p.read_bytes(), "image/jpeg")

tp = ocr.recognize_teacher_page(t_raw, "image/jpeg")
bank_qs = bank.build_questions([{"page_id": "P1", "page_no": 1,
                                 "questions": tp.get("questions") or []}])
record = {"name": "验证题库", "subject": tp.get("subject", ""), "questions": bank_qs}
print("题库 %d 题，总分 %.1f" % (len(bank_qs), sum(q["max_score"] for q in bank_qs)))

sq = ocr.recognize_vlm_staged(s_raw, "image/jpeg").get("questions") or []
# 走服务端同一套清洗，字段名与真实请求一致（student_answer 而非 answer）
items_in = _app._clean_page_questions(
    [{"no": q.get("no"), "stem": q.get("stem"), "answer": q.get("answer"),
      "qtype": q.get("qtype")} for q in sq])
print("学生页识别 %d 题\n" % len(items_in))

items, matched, missing = _app._align_to_bank(items_in, record, "P1")

print("对上题库: %d / %d 题" % (matched, len(items)))
print("题库里没对上的（missing）: %d 道" % len(missing))
inside = [m for m in missing if m.get("inside_page_range")]
print("  其中夹在本页题目之间（会提示教师去找漏题）: %d 道" % len(inside))
for m in missing:
    print("    - %s %s" % (m["no"], m["stem"][:40]))

print("\n逐题：")
total = 0.0
for it in items:
    total += it["max_score"]
    ab = it.get("absorbed") or []
    tail = ("  ← 归并 " + "、".join("%s@%.2f" % (a["no"], a["coverage"]) for a in ab)) if ab else ""
    print("  no=%-5s qid=%-5s 满分 %-5.1f %s%s"
          % (it.get("no", ""), it.get("qid") or "—", it["max_score"],
             (it.get("stem") or "")[:34], tail))
print("\n学生页满分合计 %.1f  /  题库总分 %.1f" % (total, sum(q["max_score"] for q in bank_qs)))
