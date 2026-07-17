"""智批π · AI 智能作业批改系统 —— Demo 后端入口（FastAPI）。

提供接口：
    GET  /                      返回内嵌单页前端（原生 HTML/JS/CSS，完全离线）
    GET  /api/submissions       列出内置学生作答（含转写预览）
    POST /api/grade             对指定 submission_id 走批改流水线，返回批改 JSON
    GET  /api/teacher/results   教师工作台：全部作答的 AI 批改概览（叠加教师审核态）
    POST /api/teacher/review    教师确认 / 修改分数（内存态存储）
    GET  /api/analytics/class   班级学情聚合

运行：
    python -m uvicorn app:app --port 8010
默认 mock 模式无需任何 API Key；配置 ZHIPI_LLM_API_KEY 后自动尝试真实 LLM 批改。
"""
import os
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from pipeline import grader, analytics

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

app = FastAPI(title="智批π · AI 智能作业批改系统 Demo")


def current_mode() -> str:
    """当前批改模式：配置了密钥则为 llm，否则 mock。"""
    return "llm" if os.environ.get("ZHIPI_LLM_API_KEY") else "mock"


def grade_one(submission_id: str) -> dict:
    """批改单份作答并缓存，附加题目与学生信息。"""
    sub = SUB_MAP.get(submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="作答不存在：%s" % submission_id)
    question = QUESTIONS[sub["question_id"]]
    result = grader.grade(question, sub)
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
    """批改全部作答（缺失的才批改），返回结果列表。"""
    for sub in SUBMISSIONS:
        if sub["submission_id"] not in GRADED:
            grade_one(sub["submission_id"])
    return [GRADED[s["submission_id"]] for s in SUBMISSIONS]


# ---------- 接口 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    """返回内嵌单页前端。"""
    return HTML_PAGE


@app.get("/api/submissions")
def list_submissions():
    """列出内置学生作答，含转写预览，供学生提交页下拉选择。"""
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
            "reviewed": bool(review),
            "final_score": review["final_score"] if review else None,
            "teacher_action": review["teacher_action"] if review else None,
        })
    return {"mode": current_mode(), "results": rows}


class ReviewReq(BaseModel):
    submission_id: str
    teacher_action: str = "confirmed"      # confirmed（确认）/ modified（改分）
    final_score: float | None = None
    final_comment: str | None = None


@app.post("/api/teacher/review")
def api_review(req: ReviewReq):
    """教师确认或修改分数，结果存内存。"""
    if req.submission_id not in SUB_MAP:
        raise HTTPException(status_code=404, detail="作答不存在：%s" % req.submission_id)
    if req.submission_id not in GRADED:
        grade_one(req.submission_id)
    ai = GRADED[req.submission_id]
    final_score = req.final_score if req.final_score is not None else ai["total_score"]
    record = {
        "submission_id": req.submission_id,
        "teacher_action": req.teacher_action,
        "ai_score": ai["total_score"],
        "final_score": final_score,
        "max_score": ai["max_score"],
        "final_comment": req.final_comment or "",
        "reviewed": True,
    }
    REVIEWS[req.submission_id] = record
    return {"status": req.teacher_action, "review": record}


@app.get("/api/analytics/class")
def api_analytics(class_id: str = CLASS_ID):
    """班级学情聚合：知识点错误率、错因分布、红黄绿占比、讲评建议。"""
    results = grade_all()
    data = analytics.aggregate(class_id, results)
    data["mode"] = current_mode()
    data["class_name"] = _submissions_raw.get("class_name", class_id)
    return data


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
  <button data-tab="submit" class="active" onclick="switchTab('submit')">① 学生提交</button>
  <button data-tab="result" onclick="switchTab('result')">② 批改结果</button>
  <button data-tab="teacher" onclick="switchTab('teacher')">③ 教师工作台</button>
  <button data-tab="board" onclick="switchTab('board')">④ 班级看板</button>
</nav>
<main>
  <!-- Tab1 学生提交 -->
  <section id="tab-submit">
    <div class="card">
      <h2>学生提交作业</h2>
      <label>选择内置学生作答（模拟拍照上传）</label>
      <select id="sub-select" onchange="renderPreview()"></select>
      <p class="hint">Demo 使用内置手写作答样例与预置 OCR 转写，无需真实拍照上传。</p>
      <h3>作答转写预览（OCR）</h3>
      <div class="preview" id="preview">—</div>
      <div style="margin-top:14px;">
        <button class="btn" onclick="submitGrade()">提交批改</button>
        <span class="hint" id="submit-hint" style="margin-left:10px;"></span>
      </div>
    </div>
  </section>

  <!-- Tab2 批改结果 -->
  <section id="tab-result" style="display:none;">
    <div id="result-body">
      <div class="card"><div class="empty">请先在「① 学生提交」中选择作答并点击「提交批改」。</div></div>
    </div>
  </section>

  <!-- Tab3 教师工作台 -->
  <section id="tab-teacher" style="display:none;">
    <div class="card">
      <h2>教师工作台 · 红黄绿审核列表</h2>
      <p class="hint">绿色可抽查通过；黄色 / 红色可「确认」或「修改分数」，修改即时写入内存态。</p>
      <div id="teacher-body"><div class="empty">加载中…</div></div>
    </div>
  </section>

  <!-- Tab4 班级看板 -->
  <section id="tab-board" style="display:none;">
    <div id="board-body"><div class="card"><div class="empty">加载中…</div></div></div>
  </section>
</main>

<script>
const COLORS = {green:'#22c55e', yellow:'#eab308', red:'#ef4444'};
const STATUS_LABEL = {green:'绿色 · 自动通过', yellow:'黄色 · 教师确认', red:'红色 · 人工批改'};
let SUBMISSIONS = [];
let CURRENT = null;

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
  const data = await api('/api/submissions');
  SUBMISSIONS = data.submissions;
  document.getElementById('mode-badge').textContent =
    (data.mode==='llm') ? '真实 LLM 模式' : 'Mock 演示模式';
  const sel = document.getElementById('sub-select');
  sel.innerHTML = SUBMISSIONS.map(s =>
    `<option value="${s.submission_id}">${esc(s.student_name)} · ${esc(s.subject)} · ${esc(s.question_title)}</option>`
  ).join('');
  renderPreview();
}

function renderPreview(){
  const id = document.getElementById('sub-select').value;
  const s = SUBMISSIONS.find(x=>x.submission_id===id);
  if(!s){ return; }
  document.getElementById('preview').textContent =
    s.ocr_text + '\n\n（OCR 清晰度：' + s.ocr_clarity + ' / 100）';
}

async function submitGrade(){
  const id = document.getElementById('sub-select').value;
  const hint = document.getElementById('submit-hint');
  hint.textContent = '批改中…';
  try{
    CURRENT = await api('/api/grade', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({submission_id:id})
    });
    hint.textContent = '';
    renderResult();
    switchTab('result');
  }catch(e){ hint.textContent = '批改失败：'+e.message; }
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
    return `<div class="step">
      <div class="top">
        <span class="name"><span class="mark ${cls}">${mk}</span>${esc(s.step)}</span>
        <span class="muted">${s.score} / ${s.max_score} 分</span>
      </div>
      <div class="reason">${esc(s.reason)} ${tag}
        <span class="tag kp">知识点：${esc(s.knowledge_point)}</span></div>
    </div>`;
  }).join('');

  const factorsHtml = Object.entries(r.confidence_factors).map(([k,v])=>{
    const labels = {ocr_clarity:'OCR 清晰度',answer_match:'答案匹配度',rubric_coverage:'Rubric 覆盖度',
      llm_self_consistency:'LLM 自检一致性',teacher_pass_rate:'历史教师通过率'};
    return `<span class="factor">${labels[k]||k}：<b>${v}</b></span>`;
  }).join('');

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
        <div style="margin-left:auto;"><span class="factor">批改模式：<b>${r.mode==='llm'?'真实 LLM':'Mock 规则引擎'}</b></span></div>
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

async function loadTeacher(){
  const body = document.getElementById('teacher-body');
  try{
    const data = await api('/api/teacher/results');
    const rows = data.results.map(r=>{
      const color = COLORS[r.status];
      const conf = r.confidence.toFixed(1);
      let actions;
      if(r.reviewed){
        const label = r.teacher_action==='modified' ? '已修改' : '已确认';
        actions = `<span style="color:var(--green);font-weight:600;">✓ ${label}（${r.final_score} / ${r.max_score}）</span>`;
      }else if(r.status==='green'){
        actions = `<button class="btn small ghost" onclick="review('${r.submission_id}','confirmed')">抽查通过</button>`;
      }else{
        actions = `<button class="btn small ok" onclick="review('${r.submission_id}','confirmed')">确认</button>
          <button class="btn small ghost" onclick="review('${r.submission_id}','modified',${r.max_score})">修改分数</button>`;
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
  }catch(e){ body.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function review(id, action, maxScore){
  const body = {submission_id:id, teacher_action:action};
  if(action==='modified'){
    const v = prompt('请输入修改后的分数（0 - '+maxScore+'）：');
    if(v===null || v.trim()==='') return;
    const num = Number(v);
    if(isNaN(num) || num<0 || num>maxScore){ alert('请输入 0 到 '+maxScore+' 之间的数字'); return; }
    body.final_score = num;
  }
  await api('/api/teacher/review', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  loadTeacher();
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
      </div>`;
  }catch(e){ body.innerHTML = `<div class="card"><div class="empty">加载失败：${esc(e.message)}</div></div>`; }
}

init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010)
