"""智批π · AI 智能作业批改系统 —— Demo 后端入口（FastAPI）。

提供接口：
    GET  /                      返回单页前端（静态资源在 demo/static/，零外部请求）
    GET  /api/submissions       列出内置学生作答（含转写预览）
    POST /api/grade             对指定 submission_id 走批改流水线，返回批改 JSON
    GET  /api/teacher/results   教师工作台：全部作答的 AI 批改概览（叠加教师审核态）
    POST /api/teacher/review    教师确认 / 修改分数（会话内存态）
    GET  /api/analytics/class   班级学情聚合
    POST /api/feishu/push       推送审核提醒互动卡片到飞书（§13.2 集成点二）
    POST /api/feishu/sync-base  同步学情台账到飞书多维表格（§13.2 集成点一）
    POST /api/demo/reset        重置本次体验（清空本人终审记录与上传件）
    GET  /api/demo/config       前端启动参数（模式、风控开关、会话状态）
    GET  /healthz               存活探针（不受访问口令限制，供云平台健康检查）

运行：
    python -m uvicorn app:app --port 8010
默认 mock 模式无需任何 API Key；配置 ZHIPI_LLM_API_KEY 后自动尝试真实 LLM 批改。
飞书集成默认 demo 模式，无需任何凭据；配置飞书 Webhook / 多维表格凭据后自动真实推送 / 写表。

**公开部署**：本文件的状态分两层——AI 批改基线（GRADED）全局共享，教师终审与
体验者上传按浏览器会话隔离（pipeline/session_store.py），因此多人同时打开同一个
链接不会互相污染；访问口令、限流与 VLM 日配额见 pipeline/guard.py。这些机制在
不配置环境变量时全部不生效，本机演示行为与以前完全一致。部署见 deploy/README.md。
"""
import base64
import difflib
import os
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal


def _load_dotenv_once() -> str | None:
    """本地直启时把 deploy/.env 里的配置读进环境变量。

    为什么需要它：Docker 部署由 compose 的 env_file 注入环境变量，但
    `py -3 app.py` 直启不经过 compose——.env 里配好的密钥一个都不会生效，
    应用静默停在 mock 模式。界面上唯一的破绽是上传提示写着「只能识别内置
    样例」，很容易被当成正常状态，于是把规则引擎的表现误当成大模型的表现。

    刻意的两条约束：
      1. **不覆盖**已存在的环境变量——命令行显式传入的值优先级最高，
         也保证 Docker 里这个函数是无害的空操作；
      2. 解析失败不阻断启动——读不到配置就退回 mock，那是可用的降级，
         而不是崩掉。

    返回实际加载的文件路径（未加载时返回 None），供启动日志说明来源。

    注意：必须在 `from pipeline import ...` **之前**调用。guard.py 的
    MAX_BODY_BYTES / RATE_LIMIT / 日配额都是模块级常量，在 import 那一刻
    就读环境变量，晚一步就读不到了。
    """
    # 逃生开关：比赛有断网预案，需要能随时验证「离线演示模式长什么样」。
    # 没有它的话，.env 一旦存在就只能靠改名文件来测离线路径，
    # 而临场改名很容易忘了改回去。
    if str(os.environ.get("ZHIPI_SKIP_DOTENV", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        return None

    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "deploy" / ".env", here / ".env"):
        if not candidate.is_file():
            continue
        try:
            # utf-8-sig：Windows 记事本存的 .env 会带 BOM，
            # 不剥掉的话第一个键名会变成 "﻿ZHIPI_..." 而永远读不到
            with open(candidate, encoding="utf-8-sig") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key and value and key not in os.environ:
                        os.environ[key] = value
        except OSError:
            continue
        return str(candidate)
    return None


_DOTENV_SOURCE = _load_dotenv_once()

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import grader, analytics, feishu, folders, guard, session_store
from pipeline import confidence as conf_mod
from pipeline import ocr as ocr_mod

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"   # 前端：index.html + css/ + js/（全部本地，零外部请求）


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

# ---------- 状态分层（公开部署的关键） ----------
# GRADED：AI 批改基线缓存，全局共享且对教师终审**只读**。同一份内置作答的 AI
#   判分对所有体验者一致（mock 模式是确定性的，llm 模式还能省掉重复调用的钱）。
#   历史上教师终审会就地改写这里的置信度，导致一个人的审核改变所有人看到的
#   红黄绿——现在改为「基线只读 + 读取时按会话叠加通过率」。
# 会话态：教师终审记录与体验者自己上传的作业，按浏览器 cookie 隔离，
#   见 pipeline/session_store.py。
GRADED = {}   # submission_id -> AI 批改基线（不含任何教师通过率回灌）
GRADED_CACHE_FILE = DATA_DIR / "graded_cache.json"


def _load_graded_cache() -> None:
    """从磁盘恢复批改缓存，避免 llm 模式每重启重批内置 11 份。"""
    if not GRADED_CACHE_FILE.exists():
        return
    try:
        with open(GRADED_CACHE_FILE, encoding="utf-8") as f:
            GRADED.update(json.load(f))
    except Exception:
        pass


def _save_graded_cache() -> None:
    """每批完一份就落盘——首启中断也不全丢。"""
    try:
        with open(GRADED_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(GRADED, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# 体验者上传作业的临时 ID 前缀，用于和内置作答（SUBxxx）区分
UPLOAD_PREFIX = "UP-"


def demo_session(request: Request, response: Response) -> dict:
    """取当前浏览器的演示会话；cookie 缺失 / 非法 / 已过期时自动新建并下发。"""
    sid, session, is_new = session_store.get_or_create(
        request.cookies.get(session_store.COOKIE_NAME))
    # 旧会话可能没有 folders 字段（升级前签发的 cookie）；每次入口都 ensure 一次。
    folders.ensure(session)
    if is_new:
        response.set_cookie(
            session_store.COOKIE_NAME, sid,
            max_age=session_store.SESSION_TTL, httponly=True, samesite="lax")
    session["_sid"] = sid
    return session



from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    """启动自检：mock 模式下预批改全部作答并输出红黄绿统计（兼预热缓存）。"""
    _load_graded_cache()
    if current_mode() == "mock":
        results = grade_all()
        dist = {"green": 0, "yellow": 0, "red": 0}
        for r in results:
            dist[r["status"]] = dist.get(r["status"], 0) + 1
        print("[智批π] 启动自检通过：预批改 %d 份作答 · 绿 %d / 黄 %d / 红 %d"
              % (len(results), dist["green"], dist["yellow"], dist["red"]))
    else:
        pending = [s["submission_id"] for s in SUBMISSIONS if s["submission_id"] not in GRADED]
        if pending:
            print("[智批π] llm 启动预热：补批 %d 份缺失内置作答..." % len(pending), flush=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(grade_one, pending))
        results = [GRADED[s["submission_id"]] for s in SUBMISSIONS]
        dist = {"green": 0, "yellow": 0, "red": 0}
        for r in results:
            dist[r["status"]] = dist.get(r["status"], 0) + 1
        print("[智批π] 内置作答：绿 %d / 黄 %d / 红 %d%s"
              % (dist["green"], dist["yellow"], dist["red"],
                 "（磁盘缓存命中，秒过）" if not pending else ""))

    # 把「当前到底走不走真实模型」打在启动日志里。
    # 静默降级是这条链路最危险的失败模式：降级后界面照常出分、置信度、
    # 红黄绿分流，只有 mode 字段变了。启动时说清楚，省得把规则引擎的
    # 成绩误当成大模型的成绩。
    if _DOTENV_SOURCE:
        print("[智批π] 配置来源：%s" % _DOTENV_SOURCE, flush=True)
    mode = current_mode()
    vlm_on = ocr_mod.vlm_configured()
    creds = grader.llm_credentials() or {}
    # flush=True：stdout 被重定向到文件时是块缓冲的，不刷就看不到这几行，
    # 而这几行的全部意义就是「让人一眼看出走不走真实模型」。
    print("[智批π] 批改模式：%s%s" % (
        "真实大模型（%s）" % creds.get("model") if mode == "llm" else "mock 规则引擎",
        "" if mode == "llm" else "  ← 未配置 LLM 凭据，或凭据未被读到"), flush=True)
    print("[智批π] 手写识别：%s" % (
        "真实多模态（%s）" % os.environ.get("ZHIPI_VLM_MODEL", "?") if vlm_on
        else "离线样例匹配  ← 上传任意照片将无法识别"), flush=True)
    if mode == "llm":
        print("[智批π] 单次批改超时 %d 秒（跑批量评测请调大 ZHIPI_LLM_TIMEOUT）"
              % grader._llm_timeout(), flush=True)
    yield


app = FastAPI(title="智批π · AI 智能作业批改系统 Demo", lifespan=_lifespan)

# 前端静态资源。目录内不含任何外部引用（图标为内联 SVG、字体走系统字体），
# 因此挂载后整站仍然断网可用。
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


def _review_question_id(session: dict, submission_id: str) -> str | None:
    """取一条终审记录对应的题目 ID（内置作答或本会话上传件）。"""
    sub = SUB_MAP.get(submission_id)
    if sub:
        return sub["question_id"]
    uploaded = session["uploads"].get(submission_id)
    return uploaded.get("question_id") if uploaded else None


def question_pass_rate(session: dict, question_id: str) -> float | None:
    """按题聚合**本会话**教师审核通过率（§9.7 teacher_pass_rate 因子回灌）。

    口径：教师「确认」或虽走修改流程但未改动分数（仅修订评语 / 错因）
    均视同认可 AI 判分；与该题预置先验做贝叶斯式平滑（先验权重 4 次），
    避免样本极少时置信度大起大落。无任何审核记录时返回 None
    （沿用数据预置 / 冷启动默认值）。

    按会话统计而非全局：公开链接上每个体验者审自己的，看到的是自己那条
    飞轮曲线，不会被别人的点击带偏。
    """
    total = passed = 0
    for sid, rec in session["reviews"].items():
        if _review_question_id(session, sid) != question_id:
            continue
        total += 1
        if rec.get("teacher_action") == "confirmed" or rec.get("final_score") == rec.get("ai_score"):
            passed += 1
    if total == 0:
        return None
    prior = _question_prior(question_id)
    return round((prior * 4 + passed * 100.0) / (4 + total), 1)


def _apply_pass_rate(graded: dict, rate: float | None) -> dict:
    """按给定教师通过率重算置信度与红黄绿，返回**副本**。

    返回副本而不是就地改写，是为了让 GRADED 基线对所有会话保持只读；
    判分结果本身不变（llm 模式下避免整题重批的耗时与费用），只重算
    §9.7 的加权置信度与分流档位。
    """
    if rate is None:
        return graded
    factors = dict(graded["confidence_factors"])
    if factors.get("teacher_pass_rate") == rate:
        return graded
    factors["teacher_pass_rate"] = rate
    out = dict(graded)
    out["confidence_factors"] = factors
    out["confidence"] = conf_mod.compute_confidence(factors)
    status = conf_mod.route(out["confidence"])
    # 双模型交叉验证判定的强制转红语义优先于阈值分流
    if out.get("cross_check", {}).get("escalated"):
        status = "red"
    out["status"] = status
    return out



def grade_one(submission_id: str) -> dict:
    """批改单份内置作答并缓存 **AI 基线**，附加题目与学生信息。

    这里刻意不带任何教师通过率回灌——回灌是「读取时按会话叠加」的
    （见 session_result），这样同一份基线可以被所有体验者共享，
    而每个人只看到自己那条飞轮曲线。
    """
    cached = GRADED.get(submission_id)
    if cached is not None:
        return cached
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
    _save_graded_cache()
    return result


def grade_all() -> list:
    """批改全部内置作答（缺失的才批改），返回 AI 基线结果列表。

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


def session_result(session: dict, submission_id: str) -> dict:
    """该会话视角下的一份批改结果 = AI 基线 + 本会话教师通过率回灌。

    已被本人终审的那一份不再随通过率浮动——否则会出现「审完自己
    把自己的置信度也改了」的循环，演示时反而讲不清。
    """
    if submission_id.startswith(UPLOAD_PREFIX):
        uploaded = session["uploads"].get(submission_id)
        if uploaded is None:
            raise HTTPException(status_code=404, detail="上传作业不存在或已过期：%s" % submission_id)
        graded, qid = uploaded, uploaded.get("question_id")
    else:
        graded = grade_one(submission_id)
        qid = SUB_MAP[submission_id]["question_id"]
    if submission_id in session["reviews"]:
        return graded
    return _apply_pass_rate(graded, question_pass_rate(session, qid))


def session_results(session: dict, include_uploads: bool = True) -> list:
    """该会话视角下的全部批改结果：内置作答 +（可选）本人上传件。

    学生个人画像按姓名聚合，混入上传件会污染同名学生的画像，
    故那条链路传 include_uploads=False。
    """
    grade_all()
    rows = [session_result(session, s["submission_id"]) for s in SUBMISSIONS]
    if include_uploads:
        rows.extend(session_result(session, uid) for uid in list(session["uploads"]))
    return rows



# ---------- 公网访问闸门 ----------
# 未配置 ZHIPI_ACCESS_CODE 时整段旁路，本机演示行为与以前完全一致。

_PUBLIC_PATHS = {"/healthz"}

_CODE_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>智批π · 需要访问口令</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f5f7fa;color:#1f2937;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
.box{background:#fff;border:1px solid #e5e9f0;border-radius:14px;padding:28px 26px;width:min(92vw,360px);
  box-shadow:0 2px 12px rgba(0,0,0,.05);text-align:center;}
h1{font-size:18px;margin:0 0 6px;}p{color:#6b7280;font-size:13px;margin:0 0 18px;}
input{width:100%;padding:10px 12px;border:1px solid #e5e9f0;border-radius:9px;font-size:15px;}
button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:9px;background:#2563eb;
  color:#fff;font-size:15px;cursor:pointer;}
.err{color:#ef4444;font-size:13px;margin-top:10px;}
</style></head><body><div class="box">
<h1>智批π · AI 智能作业批改</h1>
<p>体验链接受口令保护，请输入主办方 / 团队提供的访问口令。</p>
<form onsubmit="location.search='?code='+encodeURIComponent(document.getElementById('c').value);return false;">
<input id="c" autofocus placeholder="访问口令" autocomplete="off">
<button type="submit">进入体验</button></form>
__ERROR__</div></body></html>"""


def _code_prompt(error: bool = False) -> HTMLResponse:
    """口令输入页。错误提示不回显任何口令内容。"""
    html = _CODE_PAGE.replace(
        "__ERROR__", '<div class="err">口令不正确，请重新输入。</div>' if error else "")
    return HTMLResponse(html, status_code=401 if error else 200)


@app.middleware("http")
async def _access_gate(request: Request, call_next):
    """请求体上限 + 访问口令。两项都未配置时直接放行。"""
    # 先看 Content-Length：超限直接拒，避免把超大 body 读进内存再判断。
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > guard.MAX_BODY_BYTES:
        return JSONResponse(
            {"detail": "请求体过大，上限 %d MB。" % (guard.MAX_BODY_BYTES // 1024 // 1024)},
            status_code=413)

    if request.url.path in _PUBLIC_PATHS or not guard.access_code():
        return await call_next(request)

    supplied = request.query_params.get("code")
    if supplied is not None:
        if not guard.check_code(supplied):
            return _code_prompt(error=True)
        if request.method == "GET":
            # 校验通过后 303 去掉 URL 上的明文口令：体验者转发链接、截图、
            # 浏览器历史里都不该留着口令原文。用相对 Location，免得反向代理
            # 下 http/https 或域名被写错。
            target = request.url.path
            rest = request.url.remove_query_params("code").query
            if rest:
                target += "?" + rest
            resp = Response(status_code=303, headers={"location": target})
        else:
            resp = await call_next(request)
        resp.set_cookie(guard.COOKIE_NAME, supplied, max_age=86400 * 7,
                        httponly=True, samesite="lax")
        return resp

    if guard.check_code(request.cookies.get(guard.COOKIE_NAME)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "需要访问口令，请回到首页重新进入。"}, status_code=401)
    return _code_prompt()


# ---------- 接口 ----------

@app.get("/healthz")
def healthz():
    """存活探针：不受访问口令限制，供云平台 / 反向代理健康检查。"""
    return {"status": "ok", "mode": current_mode()}


_index_cache = {"mtime": 0.0, "html": ""}


def _index_html() -> str:
    """读取前端入口页，按 mtime 缓存（改完前端刷新即可，不必重启）。

    刻意不用 FileResponse：FastAPI 只把 Depends 里注入的 Response 头合并进
    「返回数据」的路由，直接返回 Response 对象时那些头会被丢掉——而首页
    正是下发演示会话 cookie 的地方，cookie 丢了会话隔离就失效。
    """
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="前端资源缺失：static/index.html")
    mtime = path.stat().st_mtime
    if _index_cache["mtime"] != mtime:
        _index_cache["html"] = path.read_text(encoding="utf-8")
        _index_cache["mtime"] = mtime
    return _index_cache["html"]


@app.get("/", response_class=HTMLResponse)
def index(session: dict = Depends(demo_session)):
    """返回单页前端（顺带下发演示会话 cookie）。"""
    return _index_html()


@app.get("/api/demo/config")
def demo_config(session: dict = Depends(demo_session)):
    """前端启动参数：当前模式、识别引擎可用性、风控开关与本会话状态。"""
    folders.ensure(session)
    return {
        "mode": current_mode(),
        "vlm_configured": ocr_mod.vlm_configured(),
        "adhoc_grading_available": bool(grader.llm_credentials()),
        "feishu_webhook_configured": bool(
            os.environ.get("ZHIPI_FEISHU_WEBHOOK", "").strip()),
        "guard": guard.config_snapshot(),
        "session": {
            "reviews": len(session["reviews"]),
            "uploads": len(session["uploads"]),
            "upload_max": session_store.UPLOAD_MAX,
            "active_folder_id": session.get("active_folder_id",
                                            folders.DEMO_FOLDER_ID),
            "folders": len(session.get("folders") or {}),
        },
    }


@app.post("/api/demo/reset")
def demo_reset(request: Request, response: Response,
               session: dict = Depends(demo_session)):
    """重置本次体验：清空**本人**的终审记录与上传件。

    只影响当前浏览器会话——公开链接上一个人点重置，不会把别人正在
    演示的进度也清掉。AI 批改基线是共享只读的，无需重建。
    """
    fresh = session_store.reset(session.get("_sid", ""))
    return {"status": "reset", "reviews": len(fresh["reviews"]),
            "uploads": len(fresh["uploads"])}



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
def api_grade(req: GradeReq, session: dict = Depends(demo_session)):
    """对指定内置作答走批改流水线，返回过程级批改结果。

    返回的是「本会话视角」：AI 判分为共享基线，置信度叠加本人终审
    产生的教师通过率回灌。
    """
    if req.submission_id not in SUB_MAP:
        raise HTTPException(status_code=404, detail="作答不存在：%s" % req.submission_id)
    return session_result(session, req.submission_id)


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


def _decode_upload(image_base64: str) -> bytes:
    """把前端传来的 base64 还原为图片字节，并做体积 / 格式 / 像素三重校验。

    先按字符串长度粗筛再解码：base64 编码后约膨胀 4/3，先看长度可以在
    「解码出一个几十 MB 的 bytes」之前就拒掉，避免公网上被人用超大图刷内存。
    """
    limit = guard.MAX_IMAGE_BYTES
    if not image_base64 or len(image_base64) > (limit // 3 + 1) * 4 + 1024:
        raise HTTPException(status_code=413,
                            detail="图片为空或超过 %d MB 限制。" % (limit // 1024 // 1024))
    try:
        image_bytes = base64.b64decode(image_base64, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="图片 base64 解码失败")
    if not image_bytes or len(image_bytes) > limit:
        raise HTTPException(status_code=413,
                            detail="图片为空或超过 %d MB 限制。" % (limit // 1024 // 1024))
    invalid = ocr_mod.validate_image(image_bytes)
    if invalid:
        raise HTTPException(status_code=400, detail=invalid)
    return image_bytes


def _rate_guard(request: Request, bucket: str) -> None:
    """单 IP 限流；未启用时直接放行。"""
    ok, retry = guard.rate_limit(bucket, guard.client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="操作过于频繁，请 %d 秒后再试（公开体验限流）。" % retry,
            headers={"Retry-After": str(retry)})


@app.post("/api/recognize-image")
def api_recognize_image(req: RecognizeReq, request: Request,
                        session: dict = Depends(demo_session)):
    """作业照片手写识别：多模态大模型（配置 Key）或离线样例匹配（默认）。"""
    image_bytes = _decode_upload(req.image_base64)
    _rate_guard(request, "recognize")

    # 真实多模态识别按日配额放行；额度用尽时静默回落离线样例匹配，
    # 并在返回里说明原因，而不是把「没钱了」当成识别失败甩给体验者。
    allow_vlm, note = True, ""
    if ocr_mod.vlm_configured() and not guard.quota_take("vlm"):
        allow_vlm = False
        note = "今日真实识别额度已用尽（公开体验限额），已切换为离线样例识别。"

    result = ocr_mod.recognize_image(image_bytes, req.mime, SUB_MAP,
                                     allow_vlm=allow_vlm, unavailable_note=note)
    if note:
        result["quota_note"] = note
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
    ocr_text: str | None = Field(None, max_length=4000)   # 限长：转写文本要发给大模型，按 token 计费
    ocr_clarity: float = Field(75.0, ge=0, le=100)   # 防直调 API 构造超界置信度
    student_name: str = Field("上传作业", max_length=20)
    engine: str | None = Field(None, max_length=32)   # 识别引擎（回显用）
    folder_id: str | None = Field(None, max_length=32)  # 上传归属文件夹


def _clean_name(raw: str) -> str:
    """清洗体验者填写的姓名：去控制字符、限长，空则回退默认值。

    前端渲染已统一转义，这里再收一道，避免脏数据进入讲评大纲 / 飞书台账。
    """
    name = re.sub(r"[\x00-\x1f\x7f]", "", (raw or "")).strip()[:20]
    return name or "上传作业"


@app.post("/api/grade-image")
def api_grade_image(req: GradeImageReq, request: Request,
                    session: dict = Depends(demo_session)):
    """图片批改：命中样例复用既有流水线，任意上传走 LLM 临时批改。

    VLM 模式下即使命中内置样例，前端也走「转写文本」分支批改——
    保证教师对识别结果的人工修正真实生效（教师可控原则）。

    任意上传的批改结果会存进**本会话**，并带一个 UP-xxx 临时 ID，
    这样它能出现在教师工作台与班级看板里，体验动线不至于批完就断。
    """
    if req.matched_submission_id:
        if req.matched_submission_id not in SUB_MAP:
            raise HTTPException(status_code=404,
                                detail="作答不存在：%s" % req.matched_submission_id)
        result = dict(session_result(session, req.matched_submission_id))
        result["source"] = "sample"
        if req.engine:
            result["recognition_engine"] = req.engine
        return result

    if not (req.question_id and req.ocr_text and req.ocr_text.strip()):
        raise HTTPException(status_code=400, detail="缺少 question_id 或 ocr_text")
    question = QUESTIONS.get(req.question_id)
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在：%s" % req.question_id)

    _rate_guard(request, "grade")
    if not guard.quota_take("grade"):
        raise HTTPException(
            status_code=429,
            detail="今日真实批改额度已用尽（公开体验限额）。可点选内置样例作业照片，"
                   "离线链路完全不受影响，红黄绿分流与教师终审都能正常体验。")

    # 临时批改同样回灌该题教师通过率，与内置链路口径一致
    rate = question_pass_rate(session, req.question_id)
    overrides = {"teacher_pass_rate": rate} if rate is not None else None
    try:
        result = grader.grade_adhoc(question, req.ocr_text, req.ocr_clarity,
                                    factor_overrides=overrides)
    except Exception as exc:
        guard.quota_refund("grade")   # 没批成不占额度
        # 超时是这一步最常见的失败，而原始异常文本（HTTPSConnectionPool ...
        # Read timed out）对体验者毫无意义。实测中转网关的同一模型同一 prompt
        # 会从 20 秒漂到 180 秒以上，所以这里把「该怎么办」直接写出来：
        # 要么调大超时，要么换个更快的模型，而不是让人对着堆栈发愣。
        text = "%s %s" % (type(exc).__name__, exc)
        if "timed out" in text.lower() or "timeout" in text.lower():
            raise HTTPException(
                status_code=504,
                detail="批改超时（当前上限 %d 秒）。大模型网关响应过慢，"
                       "可调大 ZHIPI_LLM_TIMEOUT 或换用更快的模型；"
                       "也可以先点下方内置样例照片，离线链路不受影响。"
                       % grader._llm_timeout())
        raise HTTPException(status_code=502, detail="临时批改失败：%s" % exc)

    # 解析目标文件夹：显式指定 > 当前活动夹 > Demo
    try:
        target_folder = folders.resolve_target(session, req.folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    result.update({
        "source": "upload",
        "recognition_engine": req.engine or "vlm",
        "student_name": _clean_name(req.student_name),
        "question_id": question["question_id"],
        "question_title": question["title"],
        "subject": question["subject"],
        "question_text": question["question_text"],
        "standard_answer": question["standard_answer"],
        "ocr_text": req.ocr_text,
        "folder_id": target_folder,
    })
    # 存进本会话并就地写入 UP-xxx 临时 ID，让这份作业能进教师工作台与班级看板
    upload_id = session_store.add_upload(session, result)
    folders.add_item(session, target_folder, upload_id)
    return result


@app.get("/api/teacher/results")
def teacher_results(session: dict = Depends(demo_session)):
    """教师工作台：全部作答的 AI 批改概览，叠加**本会话**的教师审核状态。

    内置 11 份作答之外，还会带上体验者自己上传批改的作业（source=upload，
    前端标注为「我的上传」），让「拍照 → 批改 → 审核」的动线闭合。
    """
    rows = []
    for graded in session_results(session):
        sid = graded["submission_id"]
        review = session["reviews"].get(sid)
        rows.append({
            "submission_id": sid,
            "source": graded.get("source", "builtin"),
            "student_name": graded["student_name"],
            "question_title": graded["question_title"],
            "subject": graded["subject"],
            "ai_score": graded["total_score"],
            "max_score": graded["max_score"],
            "confidence": graded["confidence"],
            "status": graded["status"],
            "error_tags": graded["error_tags"],
            "ai_feedback": graded.get("student_feedback", ""),
            # 审卷面板同屏上下文（P0 块 A：终审不必跳结果页）
            "question_text": graded.get("question_text", ""),
            "standard_answer": graded.get("standard_answer", ""),
            "ocr_text": graded.get("ocr_text", ""),
            "step_analysis": graded.get("step_analysis", []),
            "confidence_factors": graded.get("confidence_factors", {}),
            "knowledge_points": graded.get("knowledge_points", []),
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
    submission_id: str = Field(..., max_length=64)
    teacher_action: Literal["confirmed", "modified"] = "confirmed"
    final_score: float | None = None
    # 教师改判错因（仅允许 §6.11 枚举，长度上限防构造超长数组）
    final_error_tags: list[str] | None = Field(None, max_length=20)
    final_comment: str | None = Field(None, max_length=500)


@app.post("/api/teacher/review")
def api_review(req: ReviewReq, session: dict = Depends(demo_session)):
    """教师终审：确认 / 改分 / 改判错因 / 修订评语（本会话内存态）。

    审核记录只落在**当前浏览器会话**里，同题其余未终审作答的置信度在
    下次读取时按最新教师通过率重算 —— 前端可现场演示「教师审核 →
    该题置信度因子实时变化」的数据飞轮，而公开链接上不同体验者之间
    互不干扰（历史上这里会就地改写全局缓存，多人访问时会互相污染）。
    """
    sid = req.submission_id
    if sid.startswith(UPLOAD_PREFIX):
        ai = session["uploads"].get(sid)
        if ai is None:
            raise HTTPException(status_code=404, detail="上传作业不存在或已过期：%s" % sid)
    elif sid in SUB_MAP:
        ai = grade_one(sid)
    else:
        raise HTTPException(status_code=404, detail="作答不存在：%s" % sid)

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
        "submission_id": sid,
        "teacher_action": req.teacher_action,
        "ai_score": ai["total_score"],
        "final_score": final_score,
        "max_score": ai["max_score"],
        "final_error_tags": final_tags,          # None = 沿用 AI 错因
        "final_comment": (req.final_comment or "")[:500],
        "reviewed": True,
    }
    session["reviews"][sid] = record

    qid = _review_question_id(session, sid)
    return {
        "status": req.teacher_action,
        "review": record,
        "question_pass_rate": question_pass_rate(session, qid) if qid else None,
    }


@app.get("/api/grade-progress")
def grade_progress():
    """批改进度（llm 模式下前端轮询展示「已批改 x / y」）。

    统计的是全局 AI 基线缓存——批改本身对所有体验者共享，进度也共享。
    """
    return {"graded": len(GRADED), "total": len(SUBMISSIONS), "mode": current_mode()}


@app.get("/api/analytics/class")
def api_analytics(class_id: str = CLASS_ID,
                  session: dict = Depends(demo_session)):
    """班级学情聚合：知识点错误率、错因分布、红黄绿占比、讲评建议。

    含本会话上传的作业（若有），并用 upload_count 告知前端「这里面有几份
    是你自己刚传的」，避免把内置班级数据和体验数据混为一谈。
    """
    results = session_results(session)
    data = analytics.aggregate(class_id, results)
    data["mode"] = current_mode()
    data["class_name"] = _submissions_raw.get("class_name", class_id)
    data["builtin_count"] = len(SUBMISSIONS)
    data["upload_count"] = len(session["uploads"])
    return data


def _class_analytics(session: dict) -> dict:
    """聚合当前班级学情，并补充班级名（供飞书卡片与台账使用）。"""
    data = analytics.aggregate(CLASS_ID, session_results(session))
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
def api_student_profile(student_id: str, session: dict = Depends(demo_session)):
    """学生个人错因画像：跨题聚合 + 历史时间线（历史为模拟数据，界面已标注）。

    只统计内置作答：画像按姓名聚合，把体验者的上传件混进来会污染同名
    学生的画像（体验者可以随便填姓名）。
    """
    name = None
    for sub in SUBMISSIONS:
        if sub["student_id"] == student_id:
            name = sub["student_name"]
            break
    if not name:
        raise HTTPException(status_code=404, detail="学生不存在：%s" % student_id)
    results = session_results(session, include_uploads=False)
    data = analytics.aggregate_student(student_id, name, results, _load_history())
    data["mode"] = current_mode()
    return data


@app.get("/api/lecture-outline")
def api_lecture_outline(session: dict = Depends(demo_session)):
    """生成下节课讲评课件大纲（Markdown，一键复制为课件底稿，可粘贴至希沃白板、飞书文档等备课环境）。"""
    results = session_results(session)
    outline = analytics.build_lecture_outline(_class_analytics(session), results)
    return {"mode": current_mode(), "outline_markdown": outline}


class FeishuPushReq(BaseModel):
    assignment_name: str | None = Field(None, max_length=80)
    folder_id: str | None = Field(None, max_length=32)


@app.post("/api/feishu/push")
def api_feishu_push(request: Request,
                    req: FeishuPushReq | None = None,
                    session: dict = Depends(demo_session)):
    """推送审核提醒互动卡片到飞书（§13.2 集成点二·机器人互动卡片审核流转）。

    数据来自班级学情聚合；未配置 ZHIPI_FEISHU_WEBHOOK 时返回 demo 模式，
    展示将推送的卡片内容。live 模式下会真的往群里发消息，故加限流——
    公开链接上不能让人拿它当群发按钮刷。

    可带 assignment_name / folder_id：整夹批改完成后自动推送时，用夹名作作业名。
    """
    _rate_guard(request, "feishu")
    body = req or FeishuPushReq()
    name = body.assignment_name
    if not name and body.folder_id:
        try:
            name = folders.get(session, body.folder_id)["name"] + " · 批改完成"
        except KeyError:
            name = None
    return feishu.push_review_card(_class_analytics(session), name)


@app.post("/api/feishu/sync-base")
def api_feishu_sync_base(request: Request, session: dict = Depends(demo_session)):
    """同步学情台账到飞书多维表格（§13.2 集成点一·多维表格 AI 学情台账）。

    将逐题批改结果（叠加教师终审）组装为多维表格记录；未配置多维表格凭据时
    返回 demo 模式，展示将写入的记录。
    """
    _rate_guard(request, "feishu")
    return feishu.sync_to_base(session_results(session), session["reviews"])


# ---------- 作业文件夹 ----------

def _sample_count() -> int:
    return len(_sample_manifest())


class FolderCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=24)


class FolderRenameReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=24)


class FolderActiveReq(BaseModel):
    folder_id: str = Field(..., max_length=32)


@app.get("/api/folders")
def api_folders_list(session: dict = Depends(demo_session)):
    """列出本会话全部文件夹（含 Demo 样例夹）。"""
    return {
        "active_folder_id": session.get("active_folder_id", folders.DEMO_FOLDER_ID),
        "folders": folders.list_summaries(session, sample_count=_sample_count()),
    }


@app.post("/api/folders")
def api_folders_create(req: FolderCreateReq,
                       session: dict = Depends(demo_session)):
    """自建文件夹。"""
    try:
        meta = folders.create(session, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "folder": meta,
        "folders": folders.list_summaries(session, sample_count=_sample_count()),
        "active_folder_id": session.get("active_folder_id"),
    }


@app.patch("/api/folders/{folder_id}")
def api_folders_rename(folder_id: str, req: FolderRenameReq,
                       session: dict = Depends(demo_session)):
    """重命名自建文件夹（Demo 样例夹拒绝）。"""
    try:
        meta = folders.rename(session, folder_id, req.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "folder": meta,
        "folders": folders.list_summaries(session, sample_count=_sample_count()),
    }


@app.delete("/api/folders/{folder_id}")
def api_folders_delete(folder_id: str, session: dict = Depends(demo_session)):
    """删除自建文件夹（Demo 样例夹拒绝）。"""
    try:
        folders.delete(session, folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "status": "deleted",
        "folders": folders.list_summaries(session, sample_count=_sample_count()),
        "active_folder_id": session.get("active_folder_id"),
    }


@app.post("/api/folders/active")
def api_folders_active(req: FolderActiveReq,
                       session: dict = Depends(demo_session)):
    """切换当前活动文件夹（上传默认落到此夹）。"""
    try:
        fid = folders.set_active(session, req.folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"active_folder_id": fid}


@app.get("/api/folders/{folder_id}")
def api_folders_detail(folder_id: str, session: dict = Depends(demo_session)):
    """文件夹详情：夹内条目清单（Demo 夹含内置样例 + 用户上传）。"""
    try:
        folder = folders.get(session, folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    items = []
    # Demo 夹：先挂内置样例
    if folder.get("kind") == "demo":
        for sid, meta in _sample_manifest().items():
            sub = SUB_MAP.get(sid)
            if not sub:
                continue
            q = QUESTIONS[sub["question_id"]]
            graded = GRADED.get(sid) or {}
            items.append({
                "item_id": sid,
                "kind": "sample",
                "student_name": sub["student_name"],
                "subject": q["subject"],
                "question_title": q["title"],
                "url": "/api/sample-images/%s" % meta["file"],
                "graded": sid in GRADED,
                "status": graded.get("status"),
                "score": graded.get("total_score"),
                "max_score": graded.get("max_score"),
            })

    # 用户上传（任意夹，含丢进 Demo 夹的）
    for uid in list(folder.get("item_ids") or []):
        up = session["uploads"].get(uid)
        if not up:
            continue
        items.append({
            "item_id": uid,
            "kind": "upload",
            "student_name": up.get("student_name", "上传作业"),
            "subject": up.get("subject", ""),
            "question_title": up.get("question_title", ""),
            "url": None,
            "graded": True,
            "status": up.get("status"),
            "score": up.get("total_score"),
            "max_score": up.get("max_score"),
            "submission_id": uid,
        })

    return {
        "folder": {
            "folder_id": folder["folder_id"],
            "name": folder["name"],
            "kind": folder.get("kind", "user"),
            "count": len(items),
        },
        "items": items,
    }


@app.post("/api/folders/{folder_id}/grade")
def api_folders_grade(folder_id: str, request: Request,
                      session: dict = Depends(demo_session)):
    """整夹一键批改，完成后自动推送飞书审核提醒卡片。

    - Demo 样例夹：批改全部内置作答（已缓存的跳过）；
    - 自建夹：夹内上传件在上传时已批改，这里只做汇总 + 飞书推送。
    返回批改摘要与飞书推送结果。
    """
    try:
        folder = folders.get(session, folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    _rate_guard(request, "grade")
    graded_ids = []
    errors = []

    if folder.get("kind") == "demo":
        # 内置样例：走全局基线缓存
        for sid in _sample_manifest():
            if sid not in SUB_MAP:
                continue
            try:
                grade_one(sid)
                graded_ids.append(sid)
            except Exception as exc:
                errors.append({"item_id": sid, "error": str(exc)})
        # Demo 夹里用户上传的也算进汇总（已在上传时批过）
        for uid in list(folder.get("item_ids") or []):
            if uid in session["uploads"]:
                graded_ids.append(uid)
    else:
        for uid in list(folder.get("item_ids") or []):
            if uid in session["uploads"]:
                graded_ids.append(uid)
            else:
                errors.append({"item_id": uid, "error": "上传件不存在或已过期"})

    # 聚合本夹结果做红黄绿统计（不全班，避免夹批改卡片被全班 11 份淹没）
    rows = []
    for iid in graded_ids:
        try:
            rows.append(session_result(session, iid))
        except HTTPException:
            continue

    dist = {"green": 0, "yellow": 0, "red": 0}
    for r in rows:
        st = r.get("status")
        if st in dist:
            dist[st] += 1

    assignment_name = "%s · 整夹批改" % folder["name"]
    # 用本夹聚合结果推卡片；没有结果时退回全班聚合，保证卡片仍有内容可预览
    if rows:
        folder_analytics = {
            "class_id": CLASS_ID,
            "class_name": _submissions_raw.get("class_name", CLASS_ID),
            "student_count": len(rows),
            "average_score_pct": round(
                sum((r.get("total_score") or 0) * 100.0 / max(r.get("max_score") or 1, 1)
                    for r in rows) / len(rows), 1),
            "distribution": dist,
            "error_tag_distribution": _class_analytics(session).get(
                "error_tag_distribution", []),
        }
        feishu_result = feishu.push_review_card(folder_analytics, assignment_name)
    else:
        feishu_result = feishu.push_review_card(
            _class_analytics(session), assignment_name)

    return {
        "folder_id": folder_id,
        "folder_name": folder["name"],
        "graded_count": len(graded_ids),
        "error_count": len(errors),
        "errors": errors,
        "distribution": dist,
        "feishu": feishu_result,
    }



if __name__ == "__main__":
    import uvicorn
    # 容器里必须监听 0.0.0.0，本机演示保持 127.0.0.1（不暴露到局域网）
    uvicorn.run(app,
                host=os.environ.get("ZHIPI_HOST", "127.0.0.1"),
                port=int(os.environ.get("ZHIPI_PORT", "8010")))
