# -*- coding: utf-8 -*-
"""验证「无判分基准」这道防线在真实批改里生效。

构造一页三道题：两道有标准答案且学生答对，一道无基准且学生只列了算式没算出
结果。踩过的坑是模型对无基准题写「由于标准答案缺失，按正确处理」直接给满分，
于是整页绿灯自动通过。这里看三件事：那道题给没给满分、置信度有没有被折减、
分流有没有被压到人工。

只调 2 次大模型（主批改 + 二次复批），不跑识别，省额度。

    py -3 tools/probe_unbased.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402
from pipeline import pagegrader  # noqa: E402

items = [
    {"index": 1, "no": "1", "stem": "计算：(-16x^2y^3)/(20xy^4) 约分的结果。",
     "student_answer": "-4x/(5y)", "max_score": 6.0, "qid": "Q1",
     "qtype": "解答题", "standard_answer": "-4x/(5y)", "match_score": 0.95},
    {"index": 2, "no": "2", "stem": "若 a/4 = b/5 = c/6 ≠ 0，求 (a+b)/c 的值。",
     "student_answer": "设a=4k,b=5k,c=6k，(4k+5k)/(6k)=3/2", "max_score": 6.0,
     "qid": "Q2", "qtype": "解答题", "standard_answer": "3/2", "match_score": 0.93},
    # 无基准：题库里没有这一题。学生只抄了原式、写了已知条件，没算出结果（正确答案 18）
    {"index": 3, "no": "7", "stem": "已知 1/x + 1/y = 3，求 (5x+3xy+5y)/(x-2xy+y) 的值。",
     "student_answer": "算式为：(5x+3xy+5y)/(x-2xy+y)\n1/x+1/y=3",
     "max_score": 6.0, "qid": None, "qtype": "解答题",
     "standard_answer": "", "match_score": 0.0},
]

res = pagegrader.grade_page("数学", items, clarity=92.0, with_bank=True)

print("总分 %.1f / %.1f" % (res["total_score"], res["max_score"]))
print("置信度 %.1f　分流 %s" % (res["confidence"], res["status"]))
print("\n逐题：")
for q in res["questions"]:
    print("  第%s题 %s %.1f/%.1f  matched=%s  %s"
          % (q["no"], q["verdict"], q["score"], q["max_score"], q["matched"],
             (q.get("reason") or "")[:52]))
print("\n教师备注：\n  %s" % res["teacher_note"])

print("\n判定：")
q3 = [q for q in res["questions"] if q["no"] == "7"][0]
print("  无基准题没拿满分       : %s (%.1f/%.1f)"
      % ("是" if q3["score"] < q3["max_score"] else "否 ← 仍被判满分",
         q3["score"], q3["max_score"]))
print("  分流没有自动放行       : %s (%s)"
      % ("是" if res["status"] != "green" else "否 ← 仍是绿灯", res["status"]))
print("  教师备注点名了这道题   : %s"
      % ("是" if "无判分基准" in res["teacher_note"] else "否"))
