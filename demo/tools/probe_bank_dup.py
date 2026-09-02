# -*- coding: utf-8 -*-
"""分析合并题库内部的跨页相似题：判断「跨页错配」到底有没有害。

批量场景实测有 10 道题对到了别页的题库题上。要判断这是不是真问题，得看
那些被对上的题库题与本页正确那道**是不是同一道题**：
  · 两页本来就有重复的题（同一份练习册的复习页）→ 标准答案一样，错配无害；
  · 题不一样 → 拿别人的标准答案判分，而界面显示「已匹配」，教师看不出来。

只跑教师页识别（不跑学生页），比全量诊断省一半调用。

    py -3 tools/probe_bank_dup.py 英语
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402
from pipeline import bank, ocr  # noqa: E402

SUBJ = sys.argv[1] if len(sys.argv) > 1 else "英语"
BASE = Path(r"E:\希沃智教π\测评数据\测评数据") / SUBJ
t_pages = sorted(p for p in (BASE / "教师页").glob("*.jpg"))

pages = []
for i, p in enumerate(t_pages, 1):
    raw = ocr.normalize_orientation(p.read_bytes(), "image/jpeg")
    qs = ocr.recognize_teacher_page(raw, "image/jpeg").get("questions") or []
    pages.append({"page_id": p.stem, "page_no": i, "questions": qs})
    print("  %-12s → %d 题" % (p.stem, len(qs)))

bank_qs = bank.build_questions(pages)
print("\n合并题库 %d 题\n" % len(bank_qs))

# 跨页的高相似题对：这些就是贪心可能配错的地方
print("=== 跨页相似题对（相似度 ≥ %.2f，即可能互相配错的）===" % bank.MATCH_THRESHOLD)
hits = 0
for i, a in enumerate(bank_qs):
    for b in bank_qs[i + 1:]:
        if a.get("page_id") == b.get("page_id"):
            continue
        r = bank._similarity(a.get("stem", ""), b.get("stem", ""))
        if r >= bank.MATCH_THRESHOLD:
            hits += 1
            same = (a.get("standard_answer") or "").strip() == \
                   (b.get("standard_answer") or "").strip()
            print("  %.3f  %s(%s) ←→ %s(%s)　标准答案%s"
                  % (r, a["qid"], a.get("page_id"), b["qid"], b.get("page_id"),
                     "相同（错配无害）" if same else "**不同** ← 错配会拿错答案判分"))
            print("        A: %s" % (a.get("stem") or "")[:64])
            print("        B: %s" % (b.get("stem") or "")[:64])
if not hits:
    print("  （没有跨页高相似题对——那 10 道错配另有原因，需要看学生页题干）")
print("\n共 %d 对" % hits)
