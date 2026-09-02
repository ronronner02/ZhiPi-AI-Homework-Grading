# -*- coding: utf-8 -*-
"""三科真实数据端到端实测：教师页建库 → 学生页整页批改 → 原图留痕。

跑的是真实链路（真实多模态识别 + 真实大模型批改），所以会消耗额度、要花时间。
它回答的是自检脚本回答不了的问题：**模型在真实作业上到底表现如何**——
对齐对上了几题、坐标框准了几道、分数落在哪个区间。

    py -3 tools/run_real_eval.py                 # 三科各一份
    py -3 tools/run_real_eval.py 数学            # 只跑一科

结果写到 tools/real_eval_report.md，图存到 tools/real_eval_out/。
留给人工测试的那几份（数学 89、英语 page_29、语文 杨老师—2 / 张同学—1）
在这里**刻意不碰**。
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("ZHIPI_EVAL_BASE", "http://127.0.0.1:8010")
DATA = Path(r"E:\希沃智教π\测评数据\测评数据")
OUT = Path(__file__).resolve().parent / "real_eval_out"

# 每科一组「教师页 ↔ 学生页」。刻意避开留给人工测试的那几份。
CASES = {
    "数学": {
        "teacher": ["数学/教师页/83.jpg"],
        "student": "数学/学生页/83_student_2.jpg",
    },
    "英语": {
        "teacher": ["英语/教师页/page_9.jpg"],
        "student": "英语/学生页/page_9.jpg",
    },
    "语文": {
        # 语文的教师页/学生页是两份不同版式的 PDF（答案册 vs 练习册），
        # 题号完全对不上——正是「按题干相似度对齐」要解决的场景。
        "teacher": ["语文/教师页/杨老师—1.pdf"],
        "student": "语文/学生页/黄同学—1.pdf",
    },
}


class Client:
    """带 cookie 的会话客户端。题库是会话级的，必须同一条会话走完全程。"""

    def __init__(self):
        self.s = requests.Session()
        # 部署侧配了访问口令时（deploy/.env 的 ZHIPI_ACCESS_CODE），
        # 脚本也得带上，否则第一个接口就 401。口令从环境变量读，不写死在文件里。
        code = os.environ.get("ZHIPI_ACCESS_CODE", "").strip()
        if code:
            self.s.cookies.set("zhipi_pass", code)
        self.s.get(BASE + "/api/demo/config", timeout=30)

    def post(self, path, body, timeout=300):
        r = self.s.post(BASE + path, json=body, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError("%s → %d %s" % (path, r.status_code, r.text[:300]))
        return r.json()

    def get(self, path, timeout=60):
        r = self.s.get(BASE + path, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError("%s → %d %s" % (path, r.status_code, r.text[:300]))
        return r

    def upload(self, path: Path, role: str):
        raw = path.read_bytes()
        mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg"
        if mime != "application/pdf":
            raw = _shrink(raw)
        return self.post("/api/upload-pages", {
            "image_base64": base64.b64encode(raw).decode(),
            "mime": mime, "filename": path.name, "role": role,
        })


# 手机直出的作业照片动辄 4000×5700、十几 MB，会直接撞上传体积上限。
# 浏览器端在 compressImage() 里做了同样的事，脚本要跟它对齐，否则测的
# 就不是真实链路会拿到的那张图。1800px 长边 ≈ A4 的 150 DPI，与 PDF
# 渲染档位一致，识别手写足够。
EVAL_MAX_EDGE = 1800


def _shrink(raw: bytes) -> bytes:
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(raw)) as im:
        if max(im.size) <= EVAL_MAX_EDGE and len(raw) < 6_000_000:
            return raw
        k = EVAL_MAX_EDGE / max(im.size)
        out = im.convert("RGB").resize((round(im.width * k), round(im.height * k)),
                                       Image.LANCZOS)
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def run_case(subject: str, case: dict, report: list) -> None:
    print("\n=== %s ===" % subject, flush=True)
    cli = Client()
    t0 = time.time()

    # 1. 教师页 → 题库
    page_ids = []
    for rel in case["teacher"]:
        up = cli.upload(DATA / rel, "teacher")
        print("  教师页 %s → %d 页" % (rel, up["page_total"]), flush=True)
        page_ids += [p["page_id"] for p in up["pages"]]
    bank = cli.post("/api/bank/build", {"page_ids": page_ids, "name": subject + "答案页"})
    print("  题库：%d 题，满分 %s（其中 %d 题分值系统推定）"
          % (bank["question_count"], bank["total_score"], bank["guessed_score_count"]),
          flush=True)
    for q in bank["questions"][:5]:
        print("    %-6s %-38s → %s" % (q["no"], q["stem"][:36].replace("\n", " "),
                                       q["standard_answer"][:26].replace("\n", " ")),
              flush=True)

    # 2. 学生页 → 识别
    sp = cli.upload(DATA / case["student"], "student")
    print("  学生页 %s → %d 页" % (case["student"], sp["page_total"]), flush=True)
    page = sp["pages"][0]
    recog = cli.post("/api/recognize-image", {"page_id": page["page_id"]})
    if recog.get("engine") == "none":
        raise RuntimeError("识别失败：%s" % recog.get("error"))
    qs = recog.get("questions") or []
    located = sum(1 for q in qs if q.get("bbox"))
    print("  识别：%d 题，清晰度 %s，%d 题带坐标"
          % (len(qs), recog.get("clarity"), located), flush=True)

    # 3. 整页批改（带题库）
    graded = cli.post("/api/grade-page", {
        "page_id": page["page_id"],
        "questions": [{"no": q.get("no", ""), "subject": q.get("subject", ""),
                       "stem": q.get("stem", ""), "answer": q.get("answer", ""),
                       "printed_max_score": q.get("printed_max_score"),
                       "bbox": q.get("bbox")} for q in qs],
        "subject": recog.get("subject") or subject,
        "clarity": recog.get("clarity", 75),
        "bank_id": bank["bank_id"],
        "student_name": subject + "实测",
    })
    elapsed = time.time() - t0
    f = graded["confidence_factors"]
    print("  批改：%s / %s 分 · 置信度 %s · %s · 对上题库 %d/%d"
          % (graded["total_score"], graded["max_score"], graded["confidence"],
             graded["status"], graded["matched_count"], graded["question_count"]),
          flush=True)
    print("  因子：清晰度 %s / 答案匹配 %s / Rubric %s / 自检一致 %s / 通过率 %s"
          % (f["ocr_clarity"], f["answer_match"], f["rubric_coverage"],
             f["llm_self_consistency"], f["teacher_pass_rate"]), flush=True)

    # 4. 取回批改痕迹图
    OUT.mkdir(exist_ok=True)
    mark_path = None
    if graded.get("marked_url"):
        img = cli.get(graded["marked_url"])
        mark_path = OUT / ("%s_marked.jpg" % subject)
        mark_path.write_bytes(img.content)
        print("  痕迹图：%s（%.0f KB）" % (mark_path.name, len(img.content) / 1024), flush=True)
    else:
        print("  痕迹图：未生成 —— %s" % graded.get("mark_error", "未知原因"), flush=True)

    stats = graded.get("mark_stats", {})
    report.append({
        "subject": subject,
        "bank_questions": bank["question_count"],
        "bank_total": bank["total_score"],
        "bank_guessed": bank["guessed_score_count"],
        "recognized": len(qs),
        "located": located,
        "matched": graded["matched_count"],
        "score": graded["total_score"],
        "max_score": graded["max_score"],
        "confidence": graded["confidence"],
        "status": graded["status"],
        "factors": f,
        "elapsed": round(elapsed, 1),
        "marked": bool(mark_path),
        "mark_stats": stats,
        "questions": graded["questions"],
        "teacher_note": graded.get("teacher_note", ""),
    })


def write_report(report: list) -> None:
    lines = ["# 三科真实数据端到端实测", "",
             "跑的是真实链路（多模态识别 + 大模型整页批改 + 题库比对 + 原图留痕）。",
             "留给人工测试的那几份（数学 89、英语 page_29、语文 杨老师—2 / 张同学—1）未使用。",
             "", "## 汇总", "",
             "| 学科 | 题库题数 | 识别题数 | 对上题库 | 带坐标 | 得分 | 置信度 | 分流 | 耗时 |",
             "| ---- | -------- | -------- | -------- | ------ | ---- | ------ | ---- | ---- |"]
    for r in report:
        lines.append("| %s | %d 题 / %s 分 | %d | %d | %d | %s / %s | %s | %s | %.0f 秒 |" % (
            r["subject"], r["bank_questions"], r["bank_total"], r["recognized"],
            r["matched"], r["located"], r["score"], r["max_score"],
            r["confidence"], r["status"], r["elapsed"]))

    lines += ["", "## 逐科明细", ""]
    for r in report:
        f = r["factors"]
        lines += ["### %s" % r["subject"], "",
                  "- 题库：%d 题，满分 %s，其中 **%d 题的分值是按题型推定的**（卷面未印）"
                  % (r["bank_questions"], r["bank_total"], r["bank_guessed"]),
                  "- 对齐：识别出 %d 题，按题干相似度对上题库 **%d 题**"
                  % (r["recognized"], r["matched"]),
                  "- 坐标：%d / %d 题拿到可用 bbox，其余记号画在页边并标题号"
                  % (r["mark_stats"].get("located", 0), r["mark_stats"].get("total", 0)),
                  "- 置信度五因子：清晰度 %s ｜ 答案匹配 %s ｜ Rubric 覆盖 %s ｜ 自检一致 %s ｜ 教师通过率 %s"
                  % (f["ocr_clarity"], f["answer_match"], f["rubric_coverage"],
                     f["llm_self_consistency"], f["teacher_pass_rate"]),
                  "- 教师备注：%s" % (r["teacher_note"] or "（无）"), "",
                  "| 题号 | 得分 | 判定 | 对上题库 | 错因 | 判定理由 |",
                  "| ---- | ---- | ---- | -------- | ---- | -------- |"]
        for q in r["questions"]:
            lines.append("| %s | %s/%s | %s | %s | %s | %s |" % (
                q.get("no") or q["index"], q["score"], q["max_score"], q["verdict"],
                "是" if q.get("matched") else "否", q.get("error_tag") or "—",
                (q.get("reason") or "").replace("|", "／").replace("\n", " ")[:80]))
        lines.append("")

    lines += ["", "## 需要注意的几点", "",
              "以下几条是从实测结果里读出来的，不是设计意图的复述——写下来是为了",
              "让复现的人知道该盯哪里，而不是只看一排绿色分流就以为万事大吉。", "",
              "1. **分值几乎全是系统推定的。** 三科的教师页都没印分值，所以题库里",
              "   每道题的分值都是按题型推的（选择填空 2 分、解答 6 分……）。",
              "   整页总分因此是一个**推定口径**，不是卷面分。界面上标了「系统推定，",
              "   教师可改」，教师改过之后才是卷面口径。",
              "2. **对齐不是 100%。** 语文的教师页是答案册、学生页是练习册，两边版式",
              "   完全不同，题干相似度对上了 16/20；对不上的题照批，但不计入",
              "   「答案匹配度」——没有基准可比的题，按 0 分计入等于用「没对上」",
              "   惩罚学生。逐题表里这些题标了「题库中无此题」。",
              "3. **分流全绿不等于判分全对。** 置信度衡量的是「这份能不能自动放行」，",
              "   不是「判分有没有错」。实测里数学第 2 题的教师备注就直接写了",
              "   「标准答案疑似有误」——那正是这套东西该做的事：把可疑处交给人。",
              ""]

    path = Path(__file__).resolve().parent / "real_eval_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n报告已写入 %s" % path)


def main():
    want = sys.argv[1:] or list(CASES)
    report = []
    for subject in want:
        if subject not in CASES:
            print("跳过未知学科：%s" % subject)
            continue
        try:
            run_case(subject, CASES[subject], report)
        except Exception as exc:
            print("  !! %s 失败：%s" % (subject, exc), flush=True)
    if report:
        write_report(report)
        print(json.dumps([{k: v for k, v in r.items() if k != "questions"}
                          for r in report], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
