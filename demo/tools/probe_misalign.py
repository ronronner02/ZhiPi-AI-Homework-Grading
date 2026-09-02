# -*- coding: utf-8 -*-
"""查清跨页错配的成因：错配的那道题，本页到底有没有候选。

页面亲和加成没能纠正错配，有两种可能，处置完全不同：
  A. 本页有候选、只是分数略低  → 加成不够大，调参能解决；
  B. 本页压根没有 ≥ 阈值的候选 → 加成无从加起，得从别处修。
这里把错配题的学生题干、它对上的那道别页题、以及**本页最像的那道**
一起打出来，直接分辨是哪一种。

    py -3 tools/probe_misalign.py page_16
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402
from pipeline import bank, ocr  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "page_16"
BASE = Path(r"E:\希沃智教π\测评数据\测评数据\英语")

pages = []
for i, p in enumerate(sorted((BASE / "教师页").glob("*.jpg")), 1):
    raw = ocr.normalize_orientation(p.read_bytes(), "image/jpeg")
    qs = ocr.recognize_teacher_page(raw, "image/jpeg").get("questions") or []
    pages.append({"page_id": p.stem, "page_no": i, "questions": qs})
bank_qs = bank.build_questions(pages)
record = {"name": "合并题库", "subject": "英语", "questions": bank_qs}
by_qid = {q["qid"]: q for q in bank_qs}
print("合并题库 %d 题；本页(%s) %d 题"
      % (len(bank_qs), TARGET,
         sum(1 for q in bank_qs if q.get("page_id") == TARGET)))
print("page_id 是否落到题库里：%r" % (bank_qs[0].get("page_id"),))

s = ocr.normalize_orientation((BASE / "学生页" / (TARGET + ".jpg")).read_bytes(),
                              "image/jpeg")
sq = ocr.recognize_vlm_staged(s, "image/jpeg").get("questions") or []
items_in = _app._clean_page_questions(
    [{"no": q.get("no"), "stem": q.get("stem"), "answer": q.get("answer"),
      "qtype": q.get("qtype")} for q in sq])
items, matched, _m = _app._align_to_bank(items_in, record, TARGET)

# 主页判定复算一遍，确认亲和到底有没有启用
al = bank.align(items_in, bank_qs)
hits = {i: (a["qid"], a["score"]) for i, a in enumerate(al) if a["qid"]}
print("第一轮命中 %d 道，判定主页 = %r\n"
      % (len(hits), bank._dominant_page(hits, by_qid)))

for it in items:
    qid = it.get("qid")
    pid = (by_qid.get(qid) or {}).get("page_id", "")
    if not qid or pid == TARGET:
        continue
    stem = (it.get("stem") or "").strip().replace("\n", " ")
    print("错配 no=%s" % it.get("no", ""))
    print("  学生题干     : %s" % stem[:78])
    print("  对上(别页)   : %s(%s) @%.3f" % (qid, pid, it.get("match_score", 0)))
    print("                 %s" % (by_qid[qid].get("stem") or "")[:78])
    # 本页最像的那道
    best, best_q = 0.0, None
    for bq in bank_qs:
        if bq.get("page_id") != TARGET:
            continue
        r = bank._similarity(stem, bq.get("stem", ""))
        if r > best:
            best, best_q = r, bq
    if best_q:
        used = any(x.get("qid") == best_q["qid"] for x in items)
        print("  本页最像     : %s @%.3f %s" % (
            best_q["qid"], best,
            "（已被本页别的题占用）" if used else
            ("← 过阈值，加成本该救回" if best >= bank.MATCH_THRESHOLD
             else "← **低于阈值 0.55，本页没有候选**")))
        print("                 %s" % (best_q.get("stem") or "")[:78])
    print()
