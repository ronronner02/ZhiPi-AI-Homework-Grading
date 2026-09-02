# -*- coding: utf-8 -*-
"""复现批量场景：多张教师页合并成一个题库，再逐张学生页对齐。

单页建库时英语是 14/14 全中，而批量上传时题库是多页合并的——英语选择题的
题干高度相似（"1. A. I'm B. You're C. He's D. She's"），跨页之间会互相干扰，
而 bank.align 是贪心一对一：一道题库题被别页的学生题占走，本页这道就只能
落到「题库中无此题」。这里把每道没对上的题的最高相似度和它最像谁一起打出来，
用来区分「差一点过线」和「被别页抢走了」。

    py -3 tools/probe_batch_bank.py 英语
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import app as _app  # noqa: E402
from pipeline import bank, ocr  # noqa: E402

SUBJ = sys.argv[1] if len(sys.argv) > 1 else "英语"
BASE = Path(r"E:\希沃智教π\测评数据\测评数据") / SUBJ
# 只用 jpg，PDF 要拆页另说；控制这一轮的调用量
t_pages = sorted(p for p in (BASE / "教师页").glob("*.jpg"))
s_pages = sorted(p for p in (BASE / "学生页").glob("*.jpg"))
print("教师页 %d 张：%s" % (len(t_pages), "、".join(p.stem for p in t_pages)))
print("学生页 %d 张：%s" % (len(s_pages), "、".join(p.stem for p in s_pages)))

# ---- 建一个合并题库（模拟批量上传所有答案页）----
pages, subject = [], ""
for i, p in enumerate(t_pages, 1):
    raw = ocr.normalize_orientation(p.read_bytes(), "image/jpeg")
    tp = ocr.recognize_teacher_page(raw, "image/jpeg")
    subject = subject or tp.get("subject", "")
    qs = tp.get("questions") or []
    pages.append({"page_id": p.stem, "page_no": i, "questions": qs})
    print("  建库 %-12s → %d 题" % (p.stem, len(qs)))

bank_qs = bank.build_questions(pages)
record = {"name": "%s合并题库" % SUBJ, "subject": subject, "questions": bank_qs}
print("\n合并题库共 %d 题\n" % len(bank_qs))
qid_page = {q["qid"]: q.get("page_id", "") for q in bank_qs}

# ---- 逐张学生页对齐 ----
for p in s_pages:
    raw = ocr.normalize_orientation(p.read_bytes(), "image/jpeg")
    try:
        sq = ocr.recognize_vlm_staged(raw, "image/jpeg").get("questions") or []
    except Exception as exc:
        print("=== %s 识别失败：%s" % (p.stem, exc))
        continue
    items_in = _app._clean_page_questions(
        [{"no": q.get("no"), "stem": q.get("stem"), "answer": q.get("answer"),
          "qtype": q.get("qtype")} for q in sq])
    items, matched, _missing = _app._align_to_bank(items_in, record, p.stem)

    unmatched = [it for it in items if not it.get("qid")]
    print("=== %-12s 识别 %2d 题　对上 %2d 题　未对上 %d 题"
          % (p.stem, len(items), matched, len(unmatched)))
    # 对上的题落到哪一页去了：错配的话会指向别的页
    wrong_page = [it for it in items
                  if it.get("qid") and qid_page.get(it["qid"]) != p.stem]
    if wrong_page:
        print("    !! %d 道对到了**别页**的题库题上：%s" % (
            len(wrong_page),
            "、".join("%s→%s" % (it.get("no", "?"), qid_page.get(it["qid"], "?"))
                     for it in wrong_page[:6])))
    for it in unmatched:
        stem = (it.get("stem") or "").strip()
        best, best_qid = 0.0, ""
        for bq in bank_qs:
            r = bank._similarity(stem, bq.get("stem", ""))
            if r > best:
                best, best_qid = r, bq["qid"]
        taken = any(x.get("qid") == best_qid for x in items)
        print("    未对上 no=%-5s %-38s 最像 %s(%s)@%.3f%s"
              % (it.get("no", ""), stem[:38], best_qid,
                 qid_page.get(best_qid, "?"), best,
                 "　← 该题库题已被本页别的题占走" if taken else ""))
    print()
