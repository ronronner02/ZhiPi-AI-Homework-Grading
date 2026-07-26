"""智批π · AI 智能作业批改系统 —— Demo 后端入口（FastAPI）。

提供接口：
    GET  /                      返回内嵌单页前端（原生 HTML/JS/CSS，完全离线）
    GET  /api/submissions       列出内置学生作答（含转写预览）
    POST /api/grade             对指定 submission_id 走批改流水线，返回批改 JSON
    GET  /api/teacher/results   教师工作台：全部作答的 AI 批改概览（叠加教师审核态）
    POST /api/teacher/review    教师确认 / 修改分数（内存态存储）
    GET  /api/analytics/class   班级学情聚合
    POST /api/feishu/push       推送审核提醒互动卡片到飞书（§13.2 集成点二）
    POST /api/feishu/sync-base  同步学情台账到飞书多维表格（§13.2 集成点一）

运行：
    python -m uvicorn app:app --port 8010
默认 mock 模式无需任何 API Key；配置 ZHIPI_LLM_API_KEY 后自动尝试真实 LLM 批改。
飞书集成默认 demo 模式，无需任何凭据；配置飞书 Webhook / 多维表格凭据后自动真实推送 / 写表。
"""
import base64
import difflib
import os
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from pipeline import grader, analytics, feishu
from pipeline import confidence as conf_mod
from pipeline import ocr as ocr_mod

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"


def _load(name: str) -> dict:
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# 内置题库与作答（启动时加载）
_questions_raw = _load("questions.json")
_submissions_raw = _load("submissions.json")
QUESTIONS = {q["question_id"]: q for q in _questions_raw["questions"]}
SUBMISSIONS = _submissions_raw["submissions"]
SUB_MAP = {s["submission_id"]: s for s in SUBMISSIONS}
CLASS_ID = _submissions_raw.get("class_id", "C001")

# 内存态缓存
GRADED = {}   # submission_id -> 批改结果（缓存，避免重复批改）
REVIEWS = {}  # submission_id -> 教师审核记录


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    """启动自检：mock 模式下预批改全部作答并输出红黄绿统计（兼预热缓存）。"""
    if current_mode() == "mock":
        results = grade_all()
        dist = {"green": 0, "yellow": 0, "red": 0}
        for r in results:
            dist[r["status"]] = dist.get(r["status"], 0) + 1
        print("[智批π] 启动自检通过：预批改 %d 份作答 · 绿 %d / 黄 %d / 红 %d"
              % (len(results), dist["green"], dist["yellow"], dist["red"]))
    else:
        print("[智批π] 已配置 LLM 凭据，跳过预批改（现场按需真实批改）")
    yield


app = FastAPI(title="智批π · AI 智能作业批改系统 Demo", lifespan=_lifespan)


def current_mode() -> str:
    """当前批改模式：配置了任一 LLM 凭据则为 llm，否则 mock。"""
    return "llm" if grader.llm_credentials() else "mock"


def _load_history() -> dict:
    """读取模拟历史错因数据（学生长期画像时间线用）。"""
    path = DATA_DIR / "history.json"
    if not path.exists():
        return {"students": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _question_prior(question_id: str) -> float:
    """该题置信度先验：取同题各作答预置 teacher_pass_rate 的均值。

    用数据自带的先验替代全局常数，避免出现「教师确认后置信度反而
    低于预置值」的反直觉演示效果；无预置数据时退回 80。
    """
    rates = [
        s["confidence_factors"].get("teacher_pass_rate")
        for s in SUBMISSIONS
        if s["question_id"] == question_id
        and isinstance(s.get("confidence_factors", {}).get("teacher_pass_rate"), (int, float))
    ]
    return round(sum(rates) / len(rates), 1) if rates else 80.0


def question_pass_rate(question_id: str) -> float | None:
    """按题聚合教师审核通过率（§9.7 teacher_pass_rate 因子回灌）。

    口径：教师「确认」或虽走修改流程但未改动分数（仅修订评语 / 错因）
    均视同认可 AI 判分；与该题预置先验做贝叶斯式平滑（先验权重 4 次），
    避免样本极少时置信度大起大落。无任何审核记录时返回 None
    （沿用数据预置 / 冷启动默认值）。
    """
    total = passed = 0
    for sid, rec in REVIEWS.items():
        sub = SUB_MAP.get(sid)
        if not sub or sub["question_id"] != question_id:
            continue
        total += 1
        if rec.get("teacher_action") == "confirmed" or rec.get("final_score") == rec.get("ai_score"):
            passed += 1
    if total == 0:
        return None
    prior = _question_prior(question_id)
    return round((prior * 4 + passed * 100.0) / (4 + total), 1)


def grade_one(submission_id: str) -> dict:
    """批改单份作答并缓存，附加题目与学生信息。

    若该题已有教师审核记录，将真实通过率回灌为 teacher_pass_rate 因子，
    体现「教师审核数据反哺置信度」的飞轮（设计方案 §9.7）。
    """
    sub = SUB_MAP.get(submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="作答不存在：%s" % submission_id)
    question = QUESTIONS[sub["question_id"]]
    rate = question_pass_rate(sub["question_id"])
    overrides = {"teacher_pass_rate": rate} if rate is not None else None
    result = grader.grade(question, sub, factor_overrides=overrides)
    result.update({
        "submission_id": submission_id,
        "student_name": sub["student_name"],
        "question_id": question["question_id"],
        "question_title": question["title"],
        "subject": question["subject"],
        "question_text": question["question_text"],
        "standard_answer": question["standard_answer"],
    })
    GRADED[submission_id] = result
    return result


def grade_all() -> list:
    """批改全部作答（缺失的才批改），返回结果列表。

    llm 模式下并发批改（4 线程），避免现场演示串行等待过久；
    mock 模式毫秒级完成，无需并发。
    """
    pending = [s["submission_id"] for s in SUBMISSIONS if s["submission_id"] not in GRADED]
    if pending:
        if current_mode() == "llm" and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(grade_one, pending))
        else:
            for sid in pending:
                grade_one(sid)
    return [GRADED[s["submission_id"]] for s in SUBMISSIONS]


# ---------- 接口 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    """返回内嵌单页前端。"""
    return HTML_PAGE


@app.get("/api/submissions")
def list_submissions():
    """列出内置学生作答（含转写预览）。

    保留的编程接口：前端拍照提交页已改用 /api/sample-images 图库，
    本接口供脚本 / 评测工具按 ID 枚举内置作答使用。
    """
    items = []
    for sub in SUBMISSIONS:
        question = QUESTIONS[sub["question_id"]]
        items.append({
            "submission_id": sub["submission_id"],
            "student_name": sub["student_name"],
            "question_id": question["question_id"],
            "question_title": question["title"],
            "subject": question["subject"],
            "ocr_text": sub["ocr"]["text"],
            "ocr_clarity": sub["ocr"]["clarity"],
        })
    return {"mode": current_mode(), "class_id": CLASS_ID, "submissions": items}


class GradeReq(BaseModel):
    submission_id: str


@app.post("/api/grade")
def api_grade(req: GradeReq):
    """对指定作答走批改流水线，返回过程级批改结果。"""
    return grade_one(req.submission_id)


# ---------- 图片批改链路：作业照片 → 手写识别 → 过程级批改 ----------

SAMPLE_DIR = DATA_DIR / "sample_images"


def _sample_manifest() -> dict:
    """读取内置手写样例图片清单（tools/gen_sample_images.py 生成）。"""
    path = SAMPLE_DIR / "manifest.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/sample-images")
def list_sample_images():
    """内置手写作业照片图库，供前端「模拟拍照上传」选择。"""
    items = []
    for sid, meta in _sample_manifest().items():
        sub = SUB_MAP.get(sid)
        if not sub:
            continue
        question = QUESTIONS[sub["question_id"]]
        items.append({
            "submission_id": sid,
            "file": meta["file"],
            "url": "/api/sample-images/%s" % meta["file"],
            "student_name": sub["student_name"],
            "subject": question["subject"],
            "question_title": question["title"],
            "clarity": sub["ocr"]["clarity"],
        })
    items.sort(key=lambda x: x["submission_id"])
    return {
        "mode": current_mode(),
        "vlm_configured": ocr_mod.vlm_configured(),
        "images": items,
    }


@app.get("/api/sample-images/{name}")
def get_sample_image(name: str):
    """返回内置样例图片文件（只允许清单内的文件名，防路径穿越）。"""
    allowed = {meta["file"] for meta in _sample_manifest().values()}
    if name not in allowed:
        raise HTTPException(status_code=404, detail="样例图片不存在：%s" % name)
    return FileResponse(SAMPLE_DIR / name, media_type="image/png")


def _detect_question(text: str) -> str | None:
    """按转写文本与各题（题面 + 标准答案）的相似度自动判定所属题目。

    英语加权条件为「含 3 个以上英文单词」而非「前 40 字符出现字母」，
    防止 x=2 之类代数式被误判为英语作文；总分低于门槛返回 None
    （交前端提示教师手动选题），不做没有把握的猜测。
    """
    best_qid, best_score = None, -1.0
    for qid, q in QUESTIONS.items():
        ref = q["question_text"] + " " + q.get("standard_answer", "")
        score = difflib.SequenceMatcher(None, text, ref).ratio()
        # 关键特征加权：题面关键词出现在转写中
        for kw in q.get("knowledge_points", []):
            if kw and kw in text:
                score += 0.1
        if q["subject"] == "英语" and len(re.findall(r"[A-Za-z]{2,}", text)) >= 3:
            score += 0.15
        if score > best_score:
            best_qid, best_score = qid, score
    return best_qid if best_score >= 0.15 else None


class RecognizeReq(BaseModel):
    image_base64: str
    mime: str = "image/png"


@app.post("/api/recognize-image")
def api_recognize_image(req: RecognizeReq):
    """作业照片手写识别：多模态大模型（配置 Key）或离线样例匹配（默认）。"""
    try:
        image_bytes = base64.b64decode(req.image_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="图片 base64 解码失败")
    if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片为空或超过 8MB 限制")

    result = ocr_mod.recognize_image(image_bytes, req.mime, SUB_MAP)
    if result.get("engine") == "none":
        return result

    sid = result.get("matched_submission_id")
    if sid and sid in SUB_MAP:
        sub = SUB_MAP[sid]
        question = QUESTIONS[sub["question_id"]]
        result.update({
            "student_name": sub["student_name"],
            "question_id": question["question_id"],
            "question_title": question["title"],
            "subject": question["subject"],
        })
    else:
        qid = _detect_question(result.get("text", ""))
        if qid:
            question = QUESTIONS[qid]
            result.update({
                "question_id": qid,
                "question_title": question["title"],
                "subject": question["subject"],
            })
    return result


class GradeImageReq(BaseModel):
    matched_submission_id: str | None = None   # 命中内置样例：走既有流水线
    question_id: str | None = None             # 任意上传：题目 + 转写 + 清晰度
    ocr_text: str | None = None
    ocr_clarity: float = Field(75.0, ge=0, le=100)   # 防直调 API 构造超界置信度
    student_name: str = "上传作业"
    engine: str | None = None                  # 识别引擎（回显用）


@app.post("/api/grade-image")
def api_grade_image(req: GradeImageReq):
    """图片批改：命中样例复用既有流水线，任意上传走 LLM 临时批改。

    VLM 模式下即使命中内置样例，前端也走「转写文本」分支批改——
    保证教师对识别结果的人工修正真实生效（教师可控原则）。
    """
    if req.matched_submission_id:
        result = grade_one(req.matched_submission_id)
        result = dict(result)
        result["source"] = "sample"
        if req.engine:
            result["recognition_engine"] = req.engine
        return result

    if not (req.question_id and req.ocr_text):
        raise HTTPException(status_code=400, detail="缺少 question_id 或 ocr_text")
    question = QUESTIONS.get(req.question_id)
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在：%s" % req.question_id)
    # 临时批改同样回灌该题教师通过率，与内置链路口径一致
    rate = question_pass_rate(req.question_id)
    overrides = {"teacher_pass_rate": rate} if rate is not None else None
    try:
        result = grader.grade_adhoc(question, req.ocr_text, req.ocr_clarity,
                                    factor_overrides=overrides)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="临时批改失败：%s" % exc)
    result.update({
        "submission_id": None,
        "source": "upload",
        "recognition_engine": req.engine or "vlm",
        "student_name": req.student_name,
        "question_id": question["question_id"],
        "question_title": question["title"],
        "subject": question["subject"],
        "question_text": question["question_text"],
        "standard_answer": question["standard_answer"],
    })
    return result


@app.get("/api/teacher/results")
def teacher_results():
    """教师工作台：全部作答的 AI 批改概览，叠加教师审核状态。"""
    rows = []
    for graded in grade_all():
        sid = graded["submission_id"]
        review = REVIEWS.get(sid)
        rows.append({
            "submission_id": sid,
            "student_name": graded["student_name"],
            "question_title": graded["question_title"],
            "subject": graded["subject"],
            "ai_score": graded["total_score"],
            "max_score": graded["max_score"],
            "confidence": graded["confidence"],
            "status": graded["status"],
            "error_tags": graded["error_tags"],
            "ai_feedback": graded.get("student_feedback", ""),
            "reviewed": bool(review),
            "final_score": review["final_score"] if review else None,
            "final_error_tags": review.get("final_error_tags") if review else None,
            "final_comment": review.get("final_comment") if review else None,
            "teacher_action": review["teacher_action"] if review else None,
        })
    return {
        "mode": current_mode(),
        "results": rows,
        "error_tags_enum": grader.ERROR_TAGS,   # 前端错因改判多选框用
    }


class ReviewReq(BaseModel):
    submission_id: str
    teacher_action: Literal["confirmed", "modified"] = "confirmed"
    final_score: float | None = None
    final_error_tags: list[str] | None = None   # 教师改判错因（仅允许 §6.11 枚举）
    final_comment: str | None = None


@app.post("/api/teacher/review")
def api_review(req: ReviewReq):
    """教师终审：确认 / 改分 / 改判错因 / 修订评语（内存态）。

    审核落库后，同题其余未终审作答的批改缓存失效，下次读取时按
    最新教师通过率重新计算置信度 —— 前端可现场演示「教师审核 →
    该题置信度因子实时变化」的数据飞轮。
    """
    if req.submission_id not in SUB_MAP:
        raise HTTPException(status_code=404, detail="作答不存在：%s" % req.submission_id)
    if req.submission_id not in GRADED:
        grade_one(req.submission_id)
    ai = GRADED[req.submission_id]

    final_score = req.final_score if req.final_score is not None else ai["total_score"]
    if not (0 <= final_score <= ai["max_score"]):
        raise HTTPException(
            status_code=422,
            detail="final_score 须在 0 ~ %s 之间" % ai["max_score"])

    final_tags = None
    if req.final_error_tags is not None:
        illegal = [t for t in req.final_error_tags if t not in grader.ERROR_TAGS]
        if illegal:
            raise HTTPException(
                status_code=422,
                detail="错因标签不在 §6.11 枚举内：%s" % "、".join(illegal))
        final_tags = req.final_error_tags

    record = {
        "submission_id": req.submission_id,
        "teacher_action": req.teacher_action,
        "ai_score": ai["total_score"],
        "final_score": final_score,
        "max_score": ai["max_score"],
        "final_error_tags": final_tags,          # None = 沿用 AI 错因
        "final_comment": req.final_comment or "",
        "reviewed": True,
    }
    REVIEWS[req.submission_id] = record

    # 同题其余未终审作答：就地按新通过率重算置信度与分流（不重新批改，
    # llm 模式下避免整题重批的耗时与费用，判分结果保持不变）
    qid = SUB_MAP[req.submission_id]["question_id"]
    new_rate = question_pass_rate(qid)
    if new_rate is not None:
        for sub in SUBMISSIONS:
            sid = sub["submission_id"]
            if sub["question_id"] != qid or sid == req.submission_id or sid in REVIEWS:
                continue
            graded = GRADED.get(sid)
            if not graded:
                continue
            graded["confidence_factors"]["teacher_pass_rate"] = new_rate
            graded["confidence"] = conf_mod.compute_confidence(graded["confidence_factors"])
            new_status = conf_mod.route(graded["confidence"])
            # 双模型交叉验证判定的强制转红语义优先于阈值分流
            if graded.get("cross_check", {}).get("escalated"):
                new_status = "red"
            graded["status"] = new_status

    return {
        "status": req.teacher_action,
        "review": record,
        "question_pass_rate": question_pass_rate(qid),
    }


@app.get("/api/grade-progress")
def grade_progress():
    """批改进度（llm 模式下前端轮询展示「已批改 x / y」）。"""
    return {"graded": len(GRADED), "total": len(SUBMISSIONS), "mode": current_mode()}


@app.get("/api/analytics/class")
def api_analytics(class_id: str = CLASS_ID):
    """班级学情聚合：知识点错误率、错因分布、红黄绿占比、讲评建议。"""
    results = grade_all()
    data = analytics.aggregate(class_id, results)
    data["mode"] = current_mode()
    data["class_name"] = _submissions_raw.get("class_name", class_id)
    return data


def _class_analytics() -> dict:
    """聚合当前班级学情，并补充班级名（供飞书卡片与台账使用）。"""
    data = analytics.aggregate(CLASS_ID, grade_all())
    data["class_name"] = _submissions_raw.get("class_name", CLASS_ID)
    return data


@app.get("/api/students")
def list_students():
    """班级学生清单（学生画像选择器用）。"""
    seen = {}
    for sub in SUBMISSIONS:
        seen.setdefault(sub["student_id"], sub["student_name"])
    return {"students": [{"student_id": k, "student_name": v} for k, v in seen.items()]}


@app.get("/api/analytics/student/{student_id}")
def api_student_profile(student_id: str):
    """学生个人错因画像：跨题聚合 + 历史时间线（历史为模拟数据，界面已标注）。"""
    name = None
    for sub in SUBMISSIONS:
        if sub["student_id"] == student_id:
            name = sub["student_name"]
            break
    if not name:
        raise HTTPException(status_code=404, detail="学生不存在：%s" % student_id)
    data = analytics.aggregate_student(student_id, name, grade_all(), _load_history())
    data["mode"] = current_mode()
    return data


@app.get("/api/lecture-outline")
def api_lecture_outline():
    """生成下节课讲评课件大纲（Markdown，一键复制为课件底稿，可粘贴至希沃白板、飞书文档等备课环境）。"""
    results = grade_all()
    outline = analytics.build_lecture_outline(_class_analytics(), results)
    return {"mode": current_mode(), "outline_markdown": outline}


@app.post("/api/feishu/push")
def api_feishu_push():
    """推送审核提醒互动卡片到飞书（§13.2 集成点二·机器人互动卡片审核流转）。

    数据来自班级学情聚合；未配置 ZHIPI_FEISHU_WEBHOOK 时返回 demo 模式，
    展示将推送的卡片内容。
    """
    return feishu.push_review_card(_class_analytics())


@app.post("/api/feishu/sync-base")
def api_feishu_sync_base():
    """同步学情台账到飞书多维表格（§13.2 集成点一·多维表格 AI 学情台账）。

    将逐题批改结果（叠加教师终审）组装为多维表格记录；未配置多维表格凭据时
    返回 demo 模式，展示将写入的记录。
    """
    return feishu.sync_to_base(grade_all(), REVIEWS)


# ---------- 内嵌前端页面 ----------
# 原生 HTML + JS + CSS，无任何 CDN / 外部资源，完全离线可用。
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智批π · AI 智能作业批改系统</title>
<style>
  :root{
    --green:#22c55e; --yellow:#eab308; --red:#ef4444;
    --bg:#f5f7fa; --card:#ffffff; --line:#e5e9f0; --text:#1f2937;
    --muted:#6b7280; --brand:#2563eb; --brand-soft:#eef4ff;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    line-height:1.6;font-size:14px;}
  header{background:var(--card);border-bottom:1px solid var(--line);
    padding:14px 24px;display:flex;align-items:center;justify-content:space-between;
    position:sticky;top:0;z-index:10;}
  .brand{display:flex;align-items:baseline;gap:10px;}
  .brand h1{font-size:19px;margin:0;font-weight:700;}
  .brand .sub{color:var(--muted);font-size:13px;}
  .mode-badge{background:var(--brand-soft);color:var(--brand);border:1px solid #d5e3ff;
    padding:4px 12px;border-radius:999px;font-size:12px;font-weight:600;}
  nav{display:flex;gap:4px;padding:0 24px;background:var(--card);
    border-bottom:1px solid var(--line);position:sticky;top:57px;z-index:9;}
  nav button{background:none;border:none;padding:12px 16px;font-size:14px;cursor:pointer;
    color:var(--muted);border-bottom:2px solid transparent;font-family:inherit;}
  nav button.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:600;}
  main{max-width:1040px;margin:0 auto;padding:20px 24px 60px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:18px 20px;margin-bottom:16px;}
  .card h2{font-size:16px;margin:0 0 12px;}
  .card h3{font-size:14px;margin:18px 0 8px;color:var(--muted);font-weight:600;}
  label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px;}
  select,input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;
    font-size:14px;font-family:inherit;background:#fff;color:var(--text);}
  .btn{background:var(--brand);color:#fff;border:none;padding:10px 20px;border-radius:8px;
    font-size:14px;cursor:pointer;font-family:inherit;font-weight:600;}
  .btn:hover{opacity:.92;}
  .btn.small{padding:5px 12px;font-size:13px;}
  .btn.ghost{background:#fff;color:var(--brand);border:1px solid #cddcff;}
  .btn.ok{background:var(--green);}
  .preview{background:#f8fafc;border:1px dashed var(--line);border-radius:8px;padding:14px;
    white-space:pre-wrap;font-family:"Consolas","Courier New",monospace;font-size:13px;
    min-height:64px;color:#334155;}
  .row{display:flex;gap:16px;flex-wrap:wrap;}
  .muted{color:var(--muted);}
  .hint{color:var(--muted);font-size:12px;margin-top:6px;}
  /* 结果页 */
  .score-hero{display:flex;gap:22px;align-items:center;flex-wrap:wrap;}
  .score-big{font-size:40px;font-weight:800;line-height:1;}
  .score-big small{font-size:18px;color:var(--muted);font-weight:600;}
  .chip{display:inline-block;padding:4px 12px;border-radius:999px;font-size:13px;
    font-weight:700;color:#fff;}
  .conf-ring{font-size:26px;font-weight:800;}
  .step{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px;}
  .step .top{display:flex;align-items:center;gap:10px;justify-content:space-between;}
  .step .name{display:flex;align-items:center;gap:10px;font-weight:600;}
  .mark{width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;
    justify-content:center;color:#fff;font-size:14px;font-weight:700;flex:0 0 auto;}
  .mark.full{background:var(--green);} .mark.zero{background:var(--red);}
  .mark.partial{background:var(--yellow);}
  .step .reason{color:var(--muted);font-size:13px;margin-top:6px;}
  .step .evidence{background:#f8fafc;border-left:3px solid #93b4f8;border-radius:0 6px 6px 0;
    padding:6px 10px;margin-top:6px;font-size:12.5px;color:#475569;}
  .step .evidence q{font-family:"Consolas","Courier New",monospace;color:#1e3a8a;quotes:"『" "』";}
  .tag{display:inline-block;background:#fff1f0;color:#cf1322;border:1px solid #ffccc7;
    padding:2px 9px;border-radius:6px;font-size:12px;margin:2px 4px 2px 0;}
  .tag.kp{background:var(--brand-soft);color:var(--brand);border-color:#d5e3ff;}
  .feedback{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px 14px;}
  .factors{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}
  .factor{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:6px 10px;
    font-size:12px;color:var(--muted);}
  .factor b{color:var(--text);}
  /* 表格 */
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th,td{padding:10px 8px;text-align:left;border-bottom:1px solid var(--line);}
  th{color:var(--muted);font-weight:600;background:#fafbfc;}
  td .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;}
  /* 看板 */
  .kpi{display:flex;gap:14px;flex-wrap:wrap;}
  .kpi .box{flex:1;min-width:150px;background:#f8fafc;border:1px solid var(--line);
    border-radius:10px;padding:14px;}
  .kpi .box .n{font-size:26px;font-weight:800;}
  .bar-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;}
  .bar-row .lbl{width:130px;flex:0 0 auto;font-size:13px;}
  .bar-track{flex:1;background:#eef2f7;border-radius:6px;height:20px;overflow:hidden;}
  .bar-fill{height:100%;border-radius:6px;min-width:2px;
    display:flex;align-items:center;justify-content:flex-end;color:#fff;font-size:11px;
    padding-right:6px;transition:width .4s;}
  .bar-row .val{width:52px;flex:0 0 auto;text-align:right;font-size:13px;color:var(--muted);}
  .dist-bar{display:flex;height:26px;border-radius:8px;overflow:hidden;margin:6px 0 4px;}
  .dist-seg{display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;
    font-weight:600;}
  ul.sugg{margin:6px 0 0;padding-left:20px;} ul.sugg li{margin-bottom:6px;}
  .empty{color:var(--muted);padding:26px;text-align:center;}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px;}
  .legend span{display:inline-flex;align-items:center;gap:5px;}
  /* 作业照片图库 */
  .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;}
  .thumb{border:2px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer;
    background:#fff;transition:border-color .15s, box-shadow .15s;}
  .thumb:hover{border-color:#93b4f8;}
  .thumb.selected{border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-soft);}
  .thumb img{width:100%;height:118px;object-fit:cover;object-position:top;display:block;}
  .thumb .cap{padding:6px 8px;font-size:12px;color:var(--muted);line-height:1.4;}
  .thumb .cap b{color:var(--text);}
  textarea{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;
    font-size:13px;font-family:"Consolas","Courier New",monospace;background:#fff;
    color:var(--text);resize:vertical;line-height:1.6;}
  textarea[readonly]{background:#f8fafc;color:#334155;}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;
    font-weight:600;margin:0 6px 6px 0;}
  .badge.engine{background:var(--brand-soft);color:var(--brand);border:1px solid #d5e3ff;}
  .badge.ok{background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;}
  .badge.warn{background:#fffbe6;color:#ad6800;border:1px solid #ffe58f;}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid #d5e3ff;
    border-top-color:var(--brand);border-radius:50%;animation:spin .8s linear infinite;
    vertical-align:-2px;margin-right:6px;}
  @keyframes spin{to{transform:rotate(360deg);}}
  /* 教师终审弹窗 */
  .modal-mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:flex;
    align-items:center;justify-content:center;z-index:50;}
  .modal{background:#fff;border-radius:14px;max-width:540px;width:92%;max-height:88vh;
    overflow:auto;padding:20px 22px;box-shadow:0 10px 40px rgba(0,0,0,.25);}
  .modal h2{margin:0 0 4px;font-size:16px;}
  .tagpick{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 2px;}
  .tagpick label{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);
    border-radius:999px;padding:4px 11px;font-size:12.5px;cursor:pointer;margin:0;
    color:var(--text);user-select:none;}
  .tagpick label.on{background:#fff1f0;border-color:#ffccc7;color:#cf1322;font-weight:600;}
  .tagpick input{display:none;}
  /* 学生画像时间线 */
  .tl{margin:10px 0 0;padding-left:0;list-style:none;}
  .tl li{position:relative;padding:0 0 14px 22px;border-left:2px solid var(--line);margin-left:8px;}
  .tl li:last-child{border-left-color:transparent;padding-bottom:2px;}
  .tl .dot2{position:absolute;left:-7px;top:3px;width:12px;height:12px;border-radius:50%;
    background:var(--brand);border:2px solid #fff;box-shadow:0 0 0 1px var(--line);}
  .sim{background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0;border-radius:6px;
    padding:1px 7px;font-size:11px;margin-left:6px;}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>智批π</h1>
    <span class="sub">教师可控的多学科作业批改与错因诊断系统 · Demo</span>
  </div>
  <span class="mode-badge" id="mode-badge">加载中…</span>
</header>
<nav>
  <button data-tab="submit" class="active" onclick="switchTab('submit')">① 拍照提交</button>
  <button data-tab="result" onclick="switchTab('result')">② 批改结果</button>
  <button data-tab="teacher" onclick="switchTab('teacher')">③ 教师工作台</button>
  <button data-tab="board" onclick="switchTab('board')">④ 班级看板</button>
</nav>
<main>
  <!-- Tab1 拍照提交 -->
  <section id="tab-submit">
    <div class="card">
      <h2>作业照片提交（输入作业图片 → 手写识别 → 过程级批改）</h2>
      <p class="hint" id="engine-hint">加载中…</p>
      <div class="gallery" id="gallery"><div class="empty">加载样例图库…</div></div>
      <div style="margin-top:12px;">
        <label class="btn ghost" style="display:inline-block;cursor:pointer;">
          上传本地作业照片
          <input type="file" id="file-input" accept="image/png,image/jpeg,image/webp"
                 style="display:none;" onchange="onFileChosen(event)">
        </label>
        <span class="hint" style="margin-left:8px;">支持 PNG / JPG / WebP，≤ 8MB</span>
      </div>
    </div>

    <div class="card" id="recog-card" style="display:none;">
      <h2>手写识别</h2>
      <div class="row">
        <div style="flex:0 0 320px;max-width:100%;">
          <img id="chosen-img" alt="作业照片"
               style="width:100%;border-radius:10px;border:1px solid var(--line);display:block;">
        </div>
        <div style="flex:1;min-width:280px;">
          <div id="recog-status"></div>
          <div id="recog-result" style="display:none;">
            <div id="recog-badges"></div>
            <h3 id="ocr-title">转写文本</h3>
            <textarea id="ocr-text" rows="8"></textarea>
            <p class="hint" id="ocr-hint"></p>
            <div style="margin-top:12px;">
              <button class="btn" onclick="submitImageGrade()">提交批改</button>
              <span class="hint" id="grade-hint" style="margin-left:10px;"></span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- Tab2 批改结果 -->
  <section id="tab-result" style="display:none;">
    <div id="result-body">
      <div class="card"><div class="empty">请先在「① 拍照提交」中选择或上传作业照片，识别后点击「提交批改」。</div></div>
    </div>
  </section>

  <!-- Tab3 教师工作台 -->
  <section id="tab-teacher" style="display:none;">
    <div class="card">
      <h2>教师工作台 · 红黄绿审核列表</h2>
      <p class="hint">绿色可抽查通过；黄色 / 红色可「确认」或「终审修改」（改分 / 改判错因 / 修订评语）。
        教师终审会回灌该题的历史通过率因子，同题未终审作答的置信度实时更新。</p>
      <div id="teacher-note"></div>
      <div id="teacher-body"><div class="empty">加载中…</div></div>
    </div>
  </section>

  <!-- Tab4 班级看板 -->
  <section id="tab-board" style="display:none;">
    <div id="board-body"><div class="card"><div class="empty">加载中…</div></div></div>
  </section>
</main>
<div id="modal-root"></div>

<script>
const COLORS = {green:'#22c55e', yellow:'#eab308', red:'#ef4444'};
const STATUS_LABEL = {green:'绿色 · 自动通过', yellow:'黄色 · 教师确认', red:'红色 · 人工批改'};
const ENGINE_LABEL = {'vlm':'多模态大模型识别', 'sample-match':'离线演示识别（样例匹配）'};
let CURRENT = null;
let RECOG = null;   // 最近一次识别结果（含 engine / matched_submission_id / question_id）

async function api(path, opts){
  const r = await fetch(path, opts);
  if(!r.ok){ throw new Error(await r.text()); }
  return r.json();
}
function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function switchTab(name){
  ['submit','result','teacher','board'].forEach(t=>{
    document.getElementById('tab-'+t).style.display = (t===name)?'':'none';
  });
  document.querySelectorAll('nav button').forEach(b=>{
    b.classList.toggle('active', b.dataset.tab===name);
  });
  if(name==='teacher') loadTeacher();
  if(name==='board') loadBoard();
}

async function init(){
  try{
    const data = await api('/api/sample-images');
    document.getElementById('mode-badge').textContent =
      (data.mode==='llm') ? '真实 LLM 模式' : 'Mock 演示模式';
    document.getElementById('engine-hint').innerHTML = data.vlm_configured
      ? '识别引擎：<b>多模态大模型</b>（已配置 ZHIPI_VLM_API_KEY，可识别任意手写作业照片）'
      : '识别引擎：<b>离线演示识别</b>（未配置 ZHIPI_VLM_API_KEY，通过感知哈希匹配下方内置样例；'
        + '配置 Key 后可识别任意照片）';
    const g = document.getElementById('gallery');
    g.innerHTML = data.images.map(im=>`
      <div class="thumb" id="thumb-${im.submission_id}"
           onclick="selectSample('${im.submission_id}','${im.url}')">
        <img src="${im.url}" alt="${esc(im.student_name)}的作业" loading="lazy">
        <div class="cap"><b>${esc(im.student_name)}</b> · ${esc(im.subject)}<br>${esc(im.question_title)}</div>
      </div>`).join('');
  }catch(e){
    document.getElementById('gallery').innerHTML =
      `<div class="empty">图库加载失败：${esc(e.message)}</div>`;
  }
}

function markSelected(sid){
  document.querySelectorAll('.thumb').forEach(t=>t.classList.remove('selected'));
  if(sid){ const el = document.getElementById('thumb-'+sid); if(el) el.classList.add('selected'); }
}

async function selectSample(sid, url){
  markSelected(sid);
  const resp = await fetch(url);
  const blob = await resp.blob();
  const b64 = await blobToBase64(blob);
  document.getElementById('chosen-img').src = url;
  await recognize(b64, blob.type || 'image/png');
}

async function onFileChosen(ev){
  const file = ev.target.files[0];
  if(!file) return;
  if(file.size > 8*1024*1024){ alert('图片超过 8MB 限制'); return; }
  markSelected(null);
  const b64 = await blobToBase64(file);
  document.getElementById('chosen-img').src = URL.createObjectURL(file);
  await recognize(b64, file.type || 'image/png');
  ev.target.value = '';
}

function blobToBase64(blob){
  return new Promise((resolve, reject)=>{
    const fr = new FileReader();
    fr.onload = ()=>resolve(fr.result.split(',')[1]);
    fr.onerror = reject;
    fr.readAsDataURL(blob);
  });
}

async function recognize(b64, mime){
  RECOG = null;
  const card = document.getElementById('recog-card');
  const status = document.getElementById('recog-status');
  const resultBox = document.getElementById('recog-result');
  card.style.display = '';
  resultBox.style.display = 'none';
  status.innerHTML = '<p><span class="spinner"></span>正在识别手写内容…</p>';
  card.scrollIntoView({behavior:'smooth', block:'start'});
  try{
    const r = await api('/api/recognize-image', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({image_base64:b64, mime:mime})
    });
    if(r.engine === 'none'){
      status.innerHTML = `<div class="badge warn">识别失败</div><p class="hint">${esc(r.error)}</p>`;
      return;
    }
    RECOG = r;
    status.innerHTML = '';
    resultBox.style.display = '';
    const badges = [];
    badges.push(`<span class="badge engine">${ENGINE_LABEL[r.engine]||esc(r.engine)}</span>`);
    badges.push(`<span class="badge ${r.clarity>=85?'ok':(r.clarity>=60?'warn':'warn')}">卷面清晰度 ${r.clarity}</span>`);
    if(r.matched_submission_id){
      badges.push(`<span class="badge ok">命中内置样例：${esc(r.student_name||'')} · ${esc(r.subject||'')}</span>`);
    }else if(r.question_id){
      badges.push(`<span class="badge ok">自动判题：${esc(r.subject||'')} · ${esc(r.question_title||'')}</span>`);
    }
    document.getElementById('recog-badges').innerHTML = badges.join('');
    const ta = document.getElementById('ocr-text');
    ta.value = r.text || '';
    const editable = (r.engine === 'vlm');
    ta.readOnly = !editable;
    document.getElementById('ocr-title').textContent =
      editable ? '转写文本（可修正后再批改 · 教师可控）' : '转写文本（内置样例标准转写）';
    document.getElementById('ocr-hint').textContent = editable
      ? '识别结果可能有误？直接在上方修正文字，再提交批改。'
      : '离线演示模式：命中内置样例，按预置标准转写走批改流水线。';
    document.getElementById('grade-hint').textContent = '';
  }catch(e){
    status.innerHTML = `<div class="badge warn">识别异常</div><p class="hint">${esc(e.message)}</p>`;
  }
}

async function submitImageGrade(){
  if(!RECOG){ return; }
  const hint = document.getElementById('grade-hint');
  hint.innerHTML = '<span class="spinner"></span>批改中…';
  try{
    let body;
    if(RECOG.engine === 'vlm'){
      // VLM 模式：无论是否命中内置样例，都按「当前转写文本」批改，
      // 教师对识别结果的修正必须真实生效（教师可控原则）
      if(!RECOG.question_id){ hint.textContent = '未能判定题目，无法批改'; return; }
      body = {
        question_id: RECOG.question_id,
        ocr_text: document.getElementById('ocr-text').value,
        ocr_clarity: RECOG.clarity,
        engine: RECOG.engine,
      };
      if(RECOG.student_name){ body.student_name = RECOG.student_name; }
    }else if(RECOG.matched_submission_id){
      body = {matched_submission_id: RECOG.matched_submission_id, engine: RECOG.engine};
    }else{
      hint.textContent = '未能判定题目，无法批改'; return;
    }
    CURRENT = await api('/api/grade-image', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    hint.textContent = '';
    renderResult();
    switchTab('result');
  }catch(e){ hint.textContent = '批改失败：' + e.message; }
}

function stepMark(score, max){
  if(score>=max) return ['✓','full'];
  if(score<=0)   return ['✗','zero'];
  return ['△','partial'];
}

function renderResult(){
  const r = CURRENT;
  if(!r){ return; }
  const color = COLORS[r.status];
  const stepsHtml = r.step_analysis.map(s=>{
    const [mk, cls] = stepMark(s.score, s.max_score);
    const tag = s.error_tag ? `<span class="tag">${esc(s.error_tag)}</span>` : '';
    const evid = s.evidence
      ? `<div class="evidence">判断依据：<q>${esc(s.evidence)}</q></div>` : '';
    const illegible = (s.legible===false)
      ? `<div class="evidence" style="border-left-color:#f59e0b;color:#b45309;">⚠ 该步字迹难以辨认，建议教师人工复核</div>` : '';
    return `<div class="step">
      <div class="top">
        <span class="name"><span class="mark ${cls}">${mk}</span>${esc(s.step)}</span>
        <span class="muted">${s.score} / ${s.max_score} 分</span>
      </div>
      <div class="reason">${esc(s.reason)} ${tag}
        <span class="tag kp">知识点：${esc(s.knowledge_point)}</span></div>
      ${evid}${illegible}
    </div>`;
  }).join('');

  let factorsHtml = Object.entries(r.confidence_factors).map(([k,v])=>{
    const labels = {ocr_clarity:'OCR 清晰度',answer_match:'答案匹配度',rubric_coverage:'Rubric 覆盖度',
      llm_self_consistency:'LLM 自检一致性',teacher_pass_rate:'历史教师通过率'};
    return `<span class="factor">${labels[k]||k}：<b>${v}</b></span>`;
  }).join('');
  if(r.consistency_check){
    factorsHtml += `<span class="factor" style="border-color:#bbf7d0;background:#f0fdf4;">二次批改一致性：<b>${r.consistency_check.agreement}</b>（复核分 ${r.consistency_check.second_score}）</span>`;
  }
  if(r.cross_check){
    factorsHtml += `<span class="factor" style="border-color:${r.cross_check.escalated?'#ffccc7':'#bbf7d0'};background:${r.cross_check.escalated?'#fff1f0':'#f0fdf4'};">双模型交叉验证：<b>${r.cross_check.escalated?'结论分歧 · 已转人工':'结论一致'}</b>（${esc(r.cross_check.model2||'模型2')} 判 ${r.cross_check.model2_score} 分）</span>`;
  }

  const tagsHtml = r.error_tags.length
    ? r.error_tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')
    : '<span class="muted">无</span>';

  const noteHtml = r.note ? `<p class="hint" style="color:#b45309;">${esc(r.note)}</p>` : '';

  document.getElementById('result-body').innerHTML = `
    <div class="card">
      <h2>${esc(r.student_name)} · ${esc(r.subject)} · ${esc(r.question_title)}</h2>
      <div class="score-hero">
        <div><div class="muted">总分</div>
          <div class="score-big">${r.total_score}<small> / ${r.max_score}</small></div></div>
        <div><div class="muted">置信度</div>
          <div class="conf-ring" style="color:${color}">${r.confidence.toFixed(1)}</div></div>
        <div><div class="muted">分流</div>
          <div><span class="chip" style="background:${color}">${STATUS_LABEL[r.status]}</span></div></div>
        <div style="margin-left:auto;">
          ${r.recognition_engine?`<span class="factor">识别引擎：<b>${ENGINE_LABEL[r.recognition_engine]||esc(r.recognition_engine)}</b></span> `:''}
          <span class="factor">批改模式：<b>${r.mode==='llm'?'真实 LLM':'Mock 规则引擎'}</b></span>
        </div>
      </div>
      ${noteHtml}
      <div class="factors">${factorsHtml}</div>
    </div>

    <div class="card">
      <h3>题目</h3><div>${esc(r.question_text)}</div>
      <h3>标准答案</h3><div class="muted">${esc(r.standard_answer)}</div>
      <h3>学生作答（OCR 转写）</h3><div class="preview">${esc(r.ocr_text)}</div>
    </div>

    <div class="card">
      <h2>逐批改节点</h2>
      ${stepsHtml}
      <h3>知识点</h3>
      <div>${r.knowledge_points.map(k=>`<span class="tag kp">${esc(k)}</span>`).join('')}</div>
      <h3>错因标签</h3>
      <div>${tagsHtml}</div>
    </div>

    <div class="card">
      <h2>个性化评语</h2>
      <div class="feedback">${esc(r.student_feedback)}</div>
      <h3>教师备注</h3>
      <div class="muted">${esc(r.teacher_note)}</div>
    </div>`;
}

let TEACHER_ROWS = [];
let TAG_ENUM = [];

async function loadTeacher(){
  const body = document.getElementById('teacher-body');
  let poller = null;
  let done = false;   // 哨兵：防止迟到的进度响应覆盖已渲染的表格
  try{
    // llm 模式下真实批改较慢，轮询进度提示「已批改 x / y」
    poller = setInterval(async ()=>{
      if(done) return;
      try{
        const p = await api('/api/grade-progress');
        if(done) return;
        if(p.mode==='llm' && p.graded < p.total){
          body.innerHTML = `<div class="empty"><span class="spinner"></span>正在批改：已完成 ${p.graded} / ${p.total} 份…</div>`;
        }
      }catch(_e){}
    }, 700);
    const data = await api('/api/teacher/results');
    done = true;
    clearInterval(poller);
    TAG_ENUM = data.error_tags_enum || [];
    TEACHER_ROWS = data.results;
    const rows = data.results.map(r=>{
      const color = COLORS[r.status];
      const conf = r.confidence.toFixed(1);
      let actions;
      if(r.reviewed){
        const label = r.teacher_action==='modified' ? '已修改' : '已确认';
        const revTags = (r.final_error_tags&&r.final_error_tags.length)
          ? `<div class="hint">错因修订：${r.final_error_tags.map(esc).join('、')}</div>` : '';
        actions = `<span style="color:var(--green);font-weight:600;">✓ ${label}（${r.final_score} / ${r.max_score}）</span>${revTags}`;
      }else if(r.status==='green'){
        actions = `<button class="btn small ghost" onclick="review('${r.submission_id}','confirmed')">抽查通过</button>
          <button class="btn small ghost" onclick="openReview('${r.submission_id}')">终审修改</button>`;
      }else{
        actions = `<button class="btn small ok" onclick="review('${r.submission_id}','confirmed')">确认</button>
          <button class="btn small ghost" onclick="openReview('${r.submission_id}')">终审修改</button>`;
      }
      const tags = r.error_tags.length ? r.error_tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('') : '<span class="muted">—</span>';
      return `<tr>
        <td>${esc(r.student_name)}</td>
        <td>${esc(r.subject)} · ${esc(r.question_title)}</td>
        <td><b>${r.ai_score}</b> / ${r.max_score}</td>
        <td>${conf}</td>
        <td><span class="dot" style="background:${color}"></span>${STATUS_LABEL[r.status]}</td>
        <td>${tags}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('');
    body.innerHTML = `<table>
      <thead><tr><th>学生</th><th>题目</th><th>AI 分</th><th>置信度</th><th>状态</th><th>错因</th><th>操作</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }catch(e){
    done = true;
    if(poller) clearInterval(poller);
    body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function review(id, action){
  const resp = await api('/api/teacher/review', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({submission_id:id, teacher_action:action})
  });
  showTeacherNote(resp);
  loadTeacher();
}

function showTeacherNote(resp){
  const el = document.getElementById('teacher-note');
  if(!el) return;
  if(resp && resp.question_pass_rate != null){
    el.innerHTML = `<div style="background:#f0fdf4;border:1px solid #bbf7d0;color:#15803d;
      border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:13px;">
      ✓ 终审已记录。该题教师通过率因子回灌为 <b>${resp.question_pass_rate}</b>，
      同题未终审作答的置信度已按新因子重新计算。</div>`;
    setTimeout(()=>{ el.innerHTML=''; }, 6000);
  }
}

function openReview(id){
  const r = TEACHER_ROWS.find(x=>x.submission_id===id);
  if(!r) return;
  const cur = new Set(r.error_tags||[]);
  const tagsHtml = TAG_ENUM.map(t=>`
    <label class="${cur.has(t)?'on':''}" onclick="toggleTag(event,this)">
      <input type="checkbox" value="${esc(t)}" ${cur.has(t)?'checked':''}>${esc(t)}</label>`).join('');
  const root = document.getElementById('modal-root');
  root.innerHTML = `<div class="modal-mask" onclick="if(event.target===this) closeModal()">
    <div class="modal">
      <h2>教师终审 · ${esc(r.student_name)}</h2>
      <p class="hint">${esc(r.subject)} · ${esc(r.question_title)} ｜ AI 判分 ${r.ai_score} / ${r.max_score}，置信度 ${r.confidence.toFixed(1)}</p>
      <h3>终审分数</h3>
      <input type="number" id="rv-score" min="0" max="${r.max_score}" step="0.5" value="${r.ai_score}">
      <h3>错因标签改判（§6.11 十类枚举）</h3>
      <div class="tagpick" id="rv-tags">${tagsHtml}</div>
      <h3>评语修订（留空则沿用 AI 评语）</h3>
      <textarea id="rv-comment" rows="4" placeholder="${esc((r.ai_feedback||'').slice(0,60))}…"></textarea>
      <div style="margin-top:14px;display:flex;gap:10px;justify-content:flex-end;">
        <button class="btn ghost" onclick="closeModal()">取消</button>
        <button class="btn" onclick="submitReview('${id}', ${r.max_score})">提交终审</button>
      </div>
    </div>
  </div>`;
}

function closeModal(){ document.getElementById('modal-root').innerHTML = ''; }

function toggleTag(ev, el){
  ev.preventDefault();
  el.classList.toggle('on');
  const cb = el.querySelector('input');
  cb.checked = !cb.checked;
}

async function submitReview(id, maxScore){
  const score = Number(document.getElementById('rv-score').value);
  if(isNaN(score) || score<0 || score>maxScore){ alert('分数须在 0 ~ '+maxScore+' 之间'); return; }
  const tags = Array.from(document.querySelectorAll('#rv-tags input:checked')).map(cb=>cb.value);
  const comment = document.getElementById('rv-comment').value.trim();
  // 分数、错因、评语均未实际改动时按「确认」提交，避免无谓压低该题回灌通过率
  const row = TEACHER_ROWS.find(x=>x.submission_id===id);
  const tagsUnchanged = row && tags.length===row.error_tags.length
    && tags.every(t=>row.error_tags.includes(t));
  const untouched = row && score===row.ai_score && tagsUnchanged && !comment;
  const payload = untouched
    ? {submission_id:id, teacher_action:'confirmed'}
    : {submission_id:id, teacher_action:'modified', final_score:score,
       final_error_tags:tags, final_comment:comment||null};
  try{
    const resp = await api('/api/teacher/review', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    closeModal();
    showTeacherNote(resp);
    loadTeacher();
  }catch(e){ alert('提交失败：'+e.message); }
}

function severityColor(rate){
  if(rate>=60) return COLORS.red;
  if(rate>=40) return COLORS.yellow;
  return COLORS.green;
}

async function loadBoard(){
  const body = document.getElementById('board-body');
  try{
    const d = await api('/api/analytics/class');
    const dist = d.distribution;
    // 知识点错误率条形图
    const kpBars = d.weak_knowledge_points.map(w=>{
      const c = severityColor(w.error_rate);
      return `<div class="bar-row">
        <span class="lbl">${esc(w.name)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${w.error_rate}%;background:${c}">${w.error_rate>=12?w.error_rate+'%':''}</span></span>
        <span class="val">${w.wrong}/${w.total}</span>
      </div>`;
    }).join('');
    // 错因分布
    const maxCount = Math.max(1, ...d.error_tag_distribution.map(t=>t.count));
    const tagBars = d.error_tag_distribution.map(t=>{
      const pct = Math.round(t.count/maxCount*100);
      return `<div class="bar-row">
        <span class="lbl">${esc(t.name)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${pct}%;background:var(--brand)"></span></span>
        <span class="val">${t.count} 次</span>
      </div>`;
    }).join('');
    // 红黄绿占比
    const seg = (k,label)=> dist[k]>0
      ? `<span class="dist-seg" style="width:${dist[k+'_pct']}%;background:${COLORS[k]}">${label} ${dist[k]}</span>` : '';

    body.innerHTML = `
      <div class="card">
        <h2>班级学情看板 · ${esc(d.class_name)}</h2>
        <div class="kpi">
          <div class="box"><div class="muted">参与作答</div><div class="n">${d.student_count}</div></div>
          <div class="box"><div class="muted">平均得分率</div><div class="n">${d.average_score_pct}%</div></div>
          <div class="box"><div class="muted">薄弱知识点</div><div class="n">${d.weak_knowledge_points.length}</div></div>
          <div class="box"><div class="muted">批改模式</div><div class="n" style="font-size:18px;padding-top:6px;">${d.mode==='llm'?'真实 LLM':'Mock'}</div></div>
        </div>
      </div>

      <div class="card">
        <h2>红黄绿分流占比</h2>
        <div class="dist-bar">${seg('green','绿')}${seg('yellow','黄')}${seg('red','红')}</div>
        <div class="legend">
          <span><span class="dot" style="width:10px;height:10px;border-radius:50%;background:${COLORS.green}"></span>绿色 ${dist.green} 人（${dist.green_pct}%）</span>
          <span><span class="dot" style="width:10px;height:10px;border-radius:50%;background:${COLORS.yellow}"></span>黄色 ${dist.yellow} 人（${dist.yellow_pct}%）</span>
          <span><span class="dot" style="width:10px;height:10px;border-radius:50%;background:${COLORS.red}"></span>红色 ${dist.red} 人（${dist.red_pct}%）</span>
        </div>
      </div>

      <div class="card">
        <h2>知识点错误率</h2>
        ${kpBars || '<div class="empty">暂无数据</div>'}
        <div class="legend">
          <span><span class="dot" style="width:10px;height:10px;border-radius:50%;background:${COLORS.green}"></span>&lt;40%</span>
          <span><span class="dot" style="width:10px;height:10px;border-radius:50%;background:${COLORS.yellow}"></span>40%-60%</span>
          <span><span class="dot" style="width:10px;height:10px;border-radius:50%;background:${COLORS.red}"></span>≥60%</span>
        </div>
      </div>

      <div class="card">
        <h2>错因分布（§6.11 错因标签）</h2>
        ${tagBars || '<div class="empty">暂无数据</div>'}
      </div>

      <div class="card">
        <h2>下节课讲评建议</h2>
        <ul class="sugg">${d.teaching_suggestions.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>
      </div>

      <div class="card">
        <h2>讲评课件大纲</h2>
        <p class="hint">基于本次批改数据自动生成结构化大纲（共性错因 + 典型错例证据 + 分层任务 + 复测建议），
        可一键复制为讲评课件底稿，粘贴至希沃白板、飞书文档等备课环境（配合文档 AI 润色）。</p>
        <div class="row" style="margin-top:6px;">
          <button class="btn" onclick="genOutline()">生成讲评大纲</button>
          <button class="btn ghost" id="copy-outline-btn" style="display:none;" onclick="copyOutline()">复制 Markdown</button>
        </div>
        <div id="outline-body" style="margin-top:12px;"></div>
      </div>

      <div class="card">
        <h2>学生个人错因画像</h2>
        <div class="row" style="align-items:center;">
          <select id="stu-select" style="max-width:240px;" onchange="loadProfile()"></select>
          <span class="hint">跨题聚合本次批改 + 历史错因时间线（历史为<b>模拟数据</b>，用于演示画像形态）</span>
        </div>
        <div id="profile-body" style="margin-top:12px;"><div class="empty">选择学生查看画像</div></div>
      </div>

      <div class="card">
        <h2>飞书协同（设计方案 §13.2）</h2>
        <p class="hint">将本班学情推送到飞书第二现场：机器人互动卡片提醒教师审核（集成点二），
        多维表格沉淀学情台账并由 AI 字段捷径自动生成错因摘要与学习建议（集成点一·主用飞书 AI 能力）。</p>
        <div class="row" style="margin-top:6px;">
          <button class="btn" onclick="pushFeishuCard()">推送飞书审核提醒卡片</button>
          <button class="btn ghost" onclick="syncFeishuBase()">同步飞书多维表格学情台账</button>
        </div>
        <div id="feishu-result" style="margin-top:16px;"></div>
      </div>`;
    await initStudents();
  }catch(e){ body.innerHTML = `<div class="card"><div class="empty">加载失败：${esc(e.message)}</div></div>`; }
}

// ---------- 学生个人错因画像 ----------
async function initStudents(){
  const sel = document.getElementById('stu-select');
  if(!sel || sel.options.length) return;
  try{
    const d = await api('/api/students');
    sel.innerHTML = d.students.map(s=>
      `<option value="${esc(s.student_id)}">${esc(s.student_name)}</option>`).join('');
    if(d.students.length) loadProfile();
  }catch(e){ sel.innerHTML = '<option>加载失败</option>'; }
}

async function loadProfile(){
  const sel = document.getElementById('stu-select');
  const body = document.getElementById('profile-body');
  if(!sel || !sel.value) return;
  body.innerHTML = '<div class="empty"><span class="spinner"></span>聚合画像中…</div>';
  try{
    const p = await api('/api/analytics/student/' + encodeURIComponent(sel.value));
    const freqMax = Math.max(1, ...(p.error_tag_freq||[]).map(t=>t.count));
    const freqBars = (p.error_tag_freq||[]).map(t=>`
      <div class="bar-row">
        <span class="lbl">${esc(t.name)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${Math.round(t.count/freqMax*100)}%;background:var(--brand)"></span></span>
        <span class="val">${t.count} 次</span>
      </div>`).join('') || '<div class="muted">本次无错因记录</div>';
    const tl = (p.timeline||[]).map(item=>{
      const tags = (item.error_tags||[]).length
        ? item.error_tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')
        : '<span class="muted">无错因</span>';
      const sim = item.simulated ? '<span class="sim">模拟历史</span>' : '<span class="sim" style="background:#eef4ff;color:var(--brand);border-color:#d5e3ff;">本次批改</span>';
      const score = (item.score_pct!=null) ? ` · 得分率 ${item.score_pct}%` : '';
      return `<li><span class="dot2" style="${item.simulated?'background:#94a3b8;':''}"></span>
        <b>${esc(item.assignment)}</b>${item.subject?(' · '+esc(item.subject)):''}${score}${sim}
        <div style="margin-top:4px;">${tags}</div></li>`;
    }).join('');
    body.innerHTML = `
      <div class="kpi">
        <div class="box"><div class="muted">本次作答</div><div class="n">${p.submission_count}</div></div>
        <div class="box"><div class="muted">平均得分率</div><div class="n">${p.average_score_pct}%</div></div>
        <div class="box"><div class="muted">薄弱知识点</div><div class="n">${(p.weak_knowledge_points||[]).length}</div></div>
      </div>
      <h3>错因频次（本次作业）</h3>${freqBars}
      <h3>错因演变时间线</h3><ul class="tl">${tl}</ul>
      <h3>趋势判断</h3><div class="feedback">${esc(p.trend_summary||'')}</div>`;
  }catch(e){ body.innerHTML = `<div class="empty">画像加载失败：${esc(e.message)}</div>`; }
}

// ---------- 讲评课件大纲 ----------
let OUTLINE_MD = '';
async function genOutline(){
  const body = document.getElementById('outline-body');
  body.innerHTML = '<div class="empty"><span class="spinner"></span>生成中…</div>';
  try{
    const d = await api('/api/lecture-outline');
    OUTLINE_MD = d.outline_markdown || '';
    body.innerHTML = `<div class="preview" style="max-height:420px;overflow:auto;">${esc(OUTLINE_MD)}</div>`;
    document.getElementById('copy-outline-btn').style.display = '';
  }catch(e){ body.innerHTML = `<div class="empty">生成失败：${esc(e.message)}</div>`; }
}

async function copyOutline(){
  if(!OUTLINE_MD) return;
  try{
    await navigator.clipboard.writeText(OUTLINE_MD);
    document.getElementById('copy-outline-btn').textContent = '✓ 已复制';
    setTimeout(()=>{ document.getElementById('copy-outline-btn').textContent = '复制 Markdown'; }, 2000);
  }catch(e){ alert('复制失败，请手动选择文本复制'); }
}

// ---------- 飞书协同（§13.2）----------
function demoBanner(text){
  return `<div style="background:#fffbe6;border:1px solid #ffe58f;color:#ad6800;border-radius:8px;padding:10px 12px;margin-bottom:12px;font-weight:700;">⚠ ${esc(text)}</div>`;
}
function liveBanner(text){
  return `<div style="background:#f0fdf4;border:1px solid #bbf7d0;color:#15803d;border-radius:8px;padding:10px 12px;margin-bottom:12px;font-weight:700;">✓ ${esc(text)}</div>`;
}
// lark_md 极简渲染：转义后再还原换行与 **加粗**（内容均由后端生成，安全可控）
function larkMd(s){ return esc(s).replace(/\n/g,'<br>').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>'); }

async function pushFeishuCard(){
  const box = document.getElementById('feishu-result');
  box.innerHTML = '<div class="muted">推送中…</div>';
  try{ renderFeishuPush(await api('/api/feishu/push', {method:'POST'})); }
  catch(e){ box.innerHTML = `<div class="empty">推送失败：${esc(e.message)}</div>`; }
}

function renderFeishuPush(d){
  const box = document.getElementById('feishu-result');
  const banner = d.mode==='demo'
    ? demoBanner('演示模式：未配置飞书凭据，展示将推送的内容')
    : liveBanner('已真实推送到飞书自定义机器人');
  let cardHtml = '';
  const card = d.card && d.card.card;
  if(card){
    const header = (card.header && card.header.title) ? card.header.title.content : '飞书互动卡片';
    const parts = (card.elements||[]).map(el=>{
      if(el.tag==='div' && el.text) return `<div style="margin:6px 0;">${larkMd(el.text.content)}</div>`;
      if(el.tag==='hr') return '<hr style="border:none;border-top:1px dashed var(--line);margin:8px 0;">';
      if(el.tag==='action' && el.actions){
        const a = el.actions[0];
        return `<div style="margin-top:10px;"><span class="btn small" style="pointer-events:none;">${esc(a.text.content)} →</span>
          <div class="hint" style="margin-top:4px;">按钮链接（占位）：${esc(a.url)}</div></div>`;
      }
      if(el.tag==='note' && el.elements) return `<div class="hint" style="margin-top:10px;">${esc(el.elements[0].content)}</div>`;
      return '';
    }).join('');
    cardHtml = `<div style="border:1px solid var(--line);border-radius:12px;overflow:hidden;max-width:480px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
      <div style="background:var(--brand);color:#fff;padding:12px 16px;font-weight:700;">${esc(header)}</div>
      <div style="padding:14px 16px;">${parts}</div>
    </div>`;
  }
  box.innerHTML = banner + `<p class="hint">接口消息：${esc(d.message||'')}</p>`
    + `<h3>飞书互动卡片预览</h3>` + cardHtml
    + `<h3>接口返回 JSON（msg_type=interactive）</h3><div class="preview">${esc(JSON.stringify(d, null, 2))}</div>`;
}

async function syncFeishuBase(){
  const box = document.getElementById('feishu-result');
  box.innerHTML = '<div class="muted">同步中…</div>';
  try{ renderFeishuSync(await api('/api/feishu/sync-base', {method:'POST'})); }
  catch(e){ box.innerHTML = `<div class="empty">同步失败：${esc(e.message)}</div>`; }
}

function renderFeishuSync(d){
  const box = document.getElementById('feishu-result');
  const banner = d.mode==='demo'
    ? demoBanner('演示模式：未配置飞书凭据，展示将写入的记录')
    : liveBanner('已真实写入飞书多维表格《学情台账》');
  const records = d.records || [];
  let tableHtml = '';
  if(records.length){
    const cols = ['学生','题号','得分','满分','错因标签','置信度','分流状态','教师终审'];
    const head = cols.map(c=>`<th>${c}</th>`).join('');
    const rows = records.map(r=>{
      const f = r.fields||{};
      return `<tr>${cols.map(c=>`<td>${esc(f[c])}</td>`).join('')}</tr>`;
    }).join('');
    tableHtml = `<h3>多维表格《学情台账》记录预览（${records.length} 条）</h3>
      <div style="overflow-x:auto;"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>
      <p class="hint">写入后可对「错因标签」「得分/满分」等列配置飞书 <b>AI 字段捷径</b>，逐行自动生成
      「一句话错因摘要」与「个性化学习建议」（设计方案 §13.2 集成点一）。</p>`;
  }
  box.innerHTML = banner + `<p class="hint">接口消息：${esc(d.message||'')}</p>` + tableHtml
    + `<h3>接口返回 JSON</h3><div class="preview">${esc(JSON.stringify(d, null, 2))}</div>`;
}

init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010)
