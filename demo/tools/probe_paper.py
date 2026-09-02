# -*- coding: utf-8 -*-
"""一次性诊断：一份卷子的题干转写、题库对齐、逐题作答，一次全打出来。

排查两件事：
  1. 学生页题干里的印刷公式有没有被吞成 ___（吞了就与题库对不上 →「题库中无此题」）
  2. 被划掉的作答有没有被当成最终答案

    py -3 tools/probe_paper.py [教师页] [学生页]
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402
from pipeline import bank, ocr  # noqa: E402

BASE = Path(r"E:\希沃智教π\测评数据\测评数据\数学")
teacher_p = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "教师页" / "harkwork_127.jpg"
student_p = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE / "学生页" / "hardwork_127.jpg"

t_raw = ocr.normalize_orientation(teacher_p.read_bytes(), "image/jpeg")
s_raw = ocr.normalize_orientation(student_p.read_bytes(), "image/jpeg")

tp = ocr.recognize_teacher_page(t_raw, "image/jpeg")
bank_qs = bank.build_questions([{"page_id": "P1", "page_no": 1,
                                 "questions": tp.get("questions") or []}])
record = {"name": "验证题库", "subject": tp.get("subject", ""), "questions": bank_qs}
print("题库 %d 题" % len(bank_qs))
for q in bank_qs:
    print("  %-4s no=%-6s %s" % (q["qid"], q["no"], (q["stem"] or "")[:60]))

st = ocr.recognize_vlm_staged(s_raw, "image/jpeg")
sq = st.get("questions") or []
print("\n学生页识别 %d 题　（复识改写 %d 处）"
      % (len(sq), len(st.get("refined") or [])))

# 题干里还剩多少 ___ ：印刷公式被吞掉的直接指标
holes = sum((q.get("stem") or "").count("___") for q in sq)
print("题干中的 ___ 占位共 %d 处（公式被吞会让这个数很大）\n" % holes)

for q in sq:
    print("  no=%-5s 题干：%s" % (q.get("no", ""), (q.get("stem") or "")[:70]))
    print("  %-9s 作答：%s" % ("", (q.get("answer") or "（空）")[:70]))

items_in = _app._clean_page_questions(
    [{"no": q.get("no"), "stem": q.get("stem"), "answer": q.get("answer"),
      "qtype": q.get("qtype")} for q in sq])
items, matched, missing = _app._align_to_bank(items_in, record, "P1")
print("\n对上题库: %d / %d 题　　missing: %d 道（夹在本页之间的 %d 道）"
      % (matched, len(items), len(missing),
         len([m for m in missing if m.get("inside_page_range")])))
for m in missing:
    print("    未对上 - %s %s" % (m["no"], m["stem"][:44]))
for it in items:
    ab = it.get("absorbed") or []
    tail = ("  ← 归并 " + "、".join("%s@%.2f" % (a["no"], a["coverage"]) for a in ab)) if ab else ""
    print("  no=%-5s qid=%-5s 满分 %-5.1f%s" % (it.get("no", ""),
                                               it.get("qid") or "—",
                                               it["max_score"], tail))
print("\n学生页满分合计 %.1f / 题库总分 %.1f"
      % (sum(i["max_score"] for i in items), sum(q["max_score"] for q in bank_qs)))
