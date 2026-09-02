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
import io
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import grader, analytics, feishu, folders, guard, session_store
from pipeline import bank, confidence as conf_mod
from pipeline import demopages
from pipeline import dimensions, marks, pagegrader
from pipeline import ocr as ocr_mod
from pipeline import pagestore, pdfpage

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
        else "不可用  ← 未配置 VLM 凭据，任何照片（含内置样例）都无法识别"), flush=True)
    if vlm_on:
        # 强模型这条必须打出来：它决定「界面上那个强模型重识别按钮存不存在」。
        # 没配的话按钮不显示，教师遇到潦草作业就只能干看着，而这在日志里
        # 是唯一能提前发现的地方。
        print("[智批π] 强模型重识别：%s" % (
            "可用（%s，教师可在识别结果处按需触发%s）"
            % (ocr_mod.strong_model_name(),
               "；ZHIPI_VLM_STRONG=1 已设为默认" if ocr_mod.strong_default() else "")
            if ocr_mod.strong_available()
            else "未配置  ← 填 ZHIPI_VLM_STRONG_* 或 ZHIPI_LLM_*_2 即可启用"),
            flush=True)
    if mode == "llm":
        print("[智批π] 单次批改超时 %d 秒（按「教师上传完可走开」定；"
              "现场演示要快速失败就调小 ZHIPI_LLM_TIMEOUT）"
              % grader._llm_timeout(), flush=True)
        # 这两条必须打出来。二次复批与交叉验证都按设计静默降级——配错了界面上
        # 完全看不出来，只是置信度因子悄悄退回模型自报值、或黄红件白等一场超时。
        # 启动横幅是唯一能在"跑之前"就发现配置没生效的地方。
        chain = _chain_snapshot()
        print("[智批π] 二次复批一致性：%s" % (
            "启用（每份多调一次，置信度因子取两次吻合度）" if chain["double_check"]
            else "关闭  ← llm_self_consistency 因子退回模型自报值"), flush=True)
        if not chain["cross_check"]:
            print("[智批π] 双模型交叉验证：未启用（第二模型三件套留空）"
                  "  ← 填上 ZHIPI_LLM_API_KEY_2 / BASE_URL_2 / MODEL_2 即自动启用",
                  flush=True)
        else:
            warn = ("  ← 预算偏小，实测第二模型多需 25-90 秒，可能每次超时"
                    if chain["cross_budget_tight"] else "")
            print("[智批π] 双模型交叉验证：%s，仅黄/红触发，预算 %d 秒；"
                  "分差超过满分 15%% 一票否决转人工（后置防线，不占置信度权重）%s" % (
                      chain["cross_model"], chain["cross_timeout"], warn), flush=True)
    yield


app = FastAPI(title="智批π · AI 智能作业批改系统 Demo", lifespan=_lifespan)


# 请求体字段 → 中文名。只列体验者真能撞到的，其余按字段名原样回显。
_FIELD_CN = {
    "student_name": "学生姓名",
    "ocr_text": "转写文本",
    "ocr_clarity": "卷面清晰度",
    "stem": "题面",
    "subject": "学科",
    "printed_max_score": "题面分值",
    "engine": "识别引擎",
    "folder_id": "文件夹",
    "paper_id": "试卷编号",
    "name": "名称",
    "final_score": "终判分",
    "final_comment": "终审评语",
}


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """把 Pydantic 的英文校验错误翻成一句能照着做的中文。

    默认 422 的 detail 长这样：`String should have at most 20 characters`——
    既不说是哪个字段，也不说该怎么办，前端原样弹出来就是「批改失败：
    String should have at most 20 characters」。体验者根本无从下手，
    实际原因可能只是文件名太长（微信临时名是 32 位十六进制）。
    """
    def cn(msg: str) -> str:
        m = re.match(r"String should have at most (\d+) characters", msg)
        if m:
            return "超长（最多 %s 个字符）" % m.group(1)
        m = re.match(r"String should have at least (\d+) characters", msg)
        if m:
            return "太短（至少 %s 个字符）" % m.group(1)
        m = re.match(r"Input should be less than or equal to (\S+)", msg)
        if m:
            return "超出上限（最大 %s）" % m.group(1)
        m = re.match(r"Input should be greater than or equal to (\S+)", msg)
        if m:
            return "低于下限（最小 %s）" % m.group(1)
        if msg.startswith("String should match pattern"):
            return "格式不合法"
        if msg == "Field required":
            return "缺失"
        return msg          # 没覆盖到的原样带出，至少字段名是中文的

    parts = []
    for err in exc.errors():
        loc = [str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")]
        field = loc[-1] if loc else "请求"
        parts.append("字段「%s」%s" % (_FIELD_CN.get(field, field), cn(err.get("msg", "取值不合法"))))
    return JSONResponse(
        status_code=422,
        content={"detail": "请求参数不合法：" + "；".join(parts[:3])})

class _CachedStatic(StaticFiles):
    """给静态资源补上 Cache-Control。

    StaticFiles 默认只发 ETag / Last-Modified，不发 Cache-Control，浏览器
    于是自行启发式决定缓存多久——可能压根不回来问。首页引用已带 ?v=<指纹>，
    内容一变 URL 就变，所以：
      带 ?v= 的  → 可以长缓存（一年、immutable），换版靠换 URL；
      不带 ?v= 的 → 只许协商缓存（no-cache），每次回来问一句。
    后者是给直接手敲 /static/... 的情形留的保险。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        qs = scope.get("query_string") or b""
        if b"v=" in qs:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


# 前端静态资源。目录内不含任何外部引用（图标为内联 SVG、字体走系统字体），
# 因此挂载后整站仍然断网可用。
app.mount("/static", _CachedStatic(directory=str(STATIC_DIR)), name="static")


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


def dimension_pass_rate(session: dict, subject: str) -> float | None:
    """按「学科 × 维度」聚合本会话的教师认可度，回灌成该学科的先验。

    题库外的作业没有 question_id，question_pass_rate 那条按题统计的路走不通。
    但维度是固定的：同学科任意两份作业的五个维度完全一致，所以可以按维度累计
    「教师改了没改这一维的分」，这比按题统计更快收敛——题是无穷的，维度只有五个。

    口径：教师确认、或虽走修改流程但该维度得分未变，都算认可这一维的判分。
    与 question_pass_rate 一致地跟先验做平滑（先验权重 4 次），避免样本极少时
    置信度大起大落。无任何同学科终审记录时返回 None（沿用冷启动默认值）。

    返回的是**该学科五个维度的平均认可度**：teacher_pass_rate 因子是单个数，
    而维度级明细另由 dimension_bias 给出，用于指导后续批改的松紧。
    """
    hit = miss = 0
    for sid, rec in session["reviews"].items():
        if rec.get("subject") != subject:
            continue
        per_dim = rec.get("dimension_deltas")
        if not per_dim:
            # 只确认未改分：视同五个维度全部认可
            if rec.get("teacher_action") == "confirmed" or \
                    rec.get("final_score") == rec.get("ai_score"):
                hit += len(dimensions.DIMENSION_KEYS)
            continue
        for _key, delta in per_dim.items():
            if delta == 0:
                hit += 1
            else:
                miss += 1
    total = hit + miss
    if total == 0:
        return None
    prior = 80.0        # 冷启动先验，与 derive_factors 的 teacher_pass_rate 默认值一致
    return round((prior * 4 + hit * 100.0) / (4 + total), 1)


# 一个维度累计到多少条教师修正，才算「系统性偏差」而不是「有人改过一次」。
# 回灌进 Prompt 的门槛用它；给教师看的诊断不设门槛（n=1 也是真实数据）。
# 取 3：低于此值时，单条修正的正负号完全由那一道题决定，把它写进 Prompt
# 等于让一次偶发分歧去左右后面所有同学科作业的判分松紧。
BIAS_MIN_SAMPLES = 3


def dimension_bias(session: dict, subject: str, min_samples: int = 1) -> dict:
    """该学科每个维度的「教师平均修正量」，用于提示后续批改的系统性偏差。

    正值 = 教师普遍往上改（模型在这一维偏严）；负值 = 普遍往下改（模型偏松）。
    这是**给教师看的诊断信息**，也写进 Prompt 提示模型收紧或放宽。

    min_samples：该维度至少累计多少条教师修正才纳入结果。
    展示用 1（如实给出全部已有数据），回灌进 Prompt 用 BIAS_MIN_SAMPLES——
    n=1 时 _bias_hint 就会因 |delta| ≥ 0.5 触发，一次偶发修正会变成
    「你在这一维历史上偏严」写进后续每一次批改的 Prompt。

    刻意不做成自动调分：把教师的历史修正量直接加到新的判分上，等于让模型
    的错误被一个统计量掩盖掉，而教师看到的分数不再是模型的真实判断——
    出问题时无从追溯。所以只提示、不改分，是否采纳仍由这一次的判分逻辑决定。
    """
    acc = {}
    for rec in session["reviews"].values():
        if rec.get("subject") != subject:
            continue
        for key, delta in (rec.get("dimension_deltas") or {}).items():
            slot = acc.setdefault(key, [0, 0])
            slot[0] += delta
            slot[1] += 1
    return {k: round(v[0] / v[1], 2)
            for k, v in acc.items() if v[1] >= max(1, min_samples)}


def dimension_bias_samples(session: dict, subject: str) -> dict:
    """每个维度累计了多少条教师修正。前端据此说明「样本还不够，尚未回灌」。"""
    acc = {}
    for rec in session["reviews"].values():
        if rec.get("subject") != subject:
            continue
        for key in (rec.get("dimension_deltas") or {}):
            acc[key] = acc.get(key, 0) + 1
    return acc


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
<meta name="color-scheme" content="light">
<title>智批π · 需要访问口令</title><style>
/* 与 static/css 同一套令牌，但刻意内联：口令页要在前端资源被拦住时也长得对。 */
:root{--paper:#F4F1EA;--paper-white:#FBF9F5;--paper-warm:#EFEADF;--ink:#16130E;
  --ink-mid:#4A4237;--ink-soft:#857B6C;--rule:#D6CFBE;--rule-soft:#E6E0D2;
  --riso-blue:#2B41C8;--err:#B42318;--err-lt:#FEF3F2;}
*,*::before,*::after{box-sizing:border-box;}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
  background:var(--paper);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Noto Sans SC",sans-serif;
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased;}
.gate{width:min(400px,100%);background:var(--paper-white);border:1px solid var(--rule);
  border-radius:12px;padding:28px 24px;
  box-shadow:2px 3px 0 #E4DDCD,6px 10px 28px -14px rgba(22,19,14,.42);}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.02em;}
.brand-mark{width:28px;height:28px;border-radius:8px;background:var(--riso-blue);color:#fff;
  display:grid;place-items:center;font-family:"Songti SC","Times New Roman",serif;font-size:16px;}
h1{margin:14px 0 6px;font-size:1.25rem;font-family:"Songti SC","STSong","Noto Serif SC",
  "Times New Roman",serif;font-weight:700;}
p{margin:0 0 18px;color:var(--ink-soft);font-size:13px;}
label{display:block;font-size:13px;font-weight:600;color:var(--ink-mid);margin-bottom:6px;}
input{width:100%;min-height:40px;padding:8px 12px;border:1px solid var(--rule);border-radius:4px;
  background:var(--paper-white);color:var(--ink);font:inherit;}
input:focus{outline:2px solid var(--riso-blue);outline-offset:1px;border-color:var(--riso-blue);}
button{width:100%;margin-top:16px;min-height:44px;padding:10px 16px;border:1px solid transparent;
  border-radius:8px;background:var(--ink);color:var(--paper-white);
  font:inherit;font-weight:600;cursor:pointer;}
button:hover{background:#2a241c;}
button:focus-visible{outline:2px solid var(--riso-blue);outline-offset:2px;}
.err{margin:12px 0 0;padding:8px 12px;border-radius:8px;background:var(--err-lt);
  border:1px solid #E8A9A5;color:var(--err);font-size:13px;}
</style></head><body><div class="gate">
<div class="brand"><span class="brand-mark" aria-hidden="true">&#960;</span>&#26234;&#25209;&#960;</div>
<h1>输入访问口令</h1>
<p>体验链接受口令保护，仅授权教师与评委进入批改工作台。</p>
<form onsubmit="location.search='?code='+encodeURIComponent(document.getElementById('c').value);return false;">
<label for="c">访问口令</label>
<input id="c" type="password" autofocus placeholder="请输入主办方 / 团队提供的口令" autocomplete="off">
<button type="submit">进入体验</button></form>
__ERROR__</div></body></html>"""


def _code_prompt(error: bool = False) -> HTMLResponse:
    """口令输入页。错误提示不回显任何口令内容。"""
    html = _CODE_PAGE.replace(
        "__ERROR__", '<p class="err">口令不正确，请重新输入。</p>' if error else "")
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


_index_cache = {"key": None, "html": ""}

# 需要打版本号的静态资源。顺序无关，按出现位置就地替换。
_ASSET_PATHS = (
    "/static/css/tokens.css",
    "/static/css/app.css",
    "/static/js/icons.js",
    "/static/js/motion.js",
    "/static/js/app.js",
)


def _asset_stamp(rel: str) -> str:
    """按「mtime + 体积」算一个短指纹。内容一变，URL 就变。

    不算文件内容哈希：首页是每请求都会走这个函数的热路径，读 5 个文件
    算摘要没必要。mtime 秒级 + 字节数已经足够——同一秒内改动且体积
    分毫不差才会撞，本地改代码不会这么巧。
    """
    p = STATIC_DIR / rel.replace("/static/", "", 1)
    try:
        st = p.stat()
    except OSError:
        return "0"
    return "%x%x" % (int(st.st_mtime), st.st_size)


def _index_html() -> str:
    """读取前端入口页，给静态资源 URL 打上版本号，按指纹缓存。

    为什么必须打版本号：/static 由 StaticFiles 提供，只发 ETag 与
    Last-Modified，**没有 Cache-Control**。浏览器于是按启发式规则自行
    决定缓存多久，可能压根不回来问。而首页 HTML 是按 mtime 每次读盘的，
    于是出现最坏的组合：**新 HTML + 旧 CSS/JS**。新骨架的类名撞上旧样式表，
    看起来像「界面变回旧版了」，其实比旧版更糟，是两版错配。
    手机上尤其明显——桌面硬刷过，手机没有。

    刻意不用 FileResponse：FastAPI 只把 Depends 里注入的 Response 头合并进
    「返回数据」的路由，直接返回 Response 对象时那些头会被丢掉——而首页
    正是下发演示会话 cookie 的地方，cookie 丢了会话隔离就失效。
    """
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="前端资源缺失：static/index.html")

    stamps = {rel: _asset_stamp(rel) for rel in _ASSET_PATHS}
    key = (path.stat().st_mtime, tuple(sorted(stamps.items())))
    if _index_cache["key"] != key:
        html = path.read_text(encoding="utf-8")
        for rel, stamp in stamps.items():
            # 只替换裸路径；已带 ?v= 的不重复加
            html = html.replace('"%s"' % rel, '"%s?v=%s"' % (rel, stamp))
        _index_cache["html"] = html
        _index_cache["key"] = key
    return _index_cache["html"]


@app.get("/", response_class=HTMLResponse)
def index(response: Response, session: dict = Depends(demo_session)):
    """返回单页前端（顺带下发演示会话 cookie）。

    首页必须不缓存：它承载着带指纹的资源 URL，缓存住首页就等于把旧
    指纹钉死，后面的版本号机制全部失效。
    """
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return _index_html()


def _chain_snapshot() -> dict:
    """批改链路构成：几个模型参与、各自是否启用。

    启动横幅与 /api/demo/config 共用这一份，避免"日志说启用、界面说未启用"
    这种两处各算一遍导致的口径分裂。

    cross_budget_tight 的阈值取 25 秒：实测各候选第二模型最快一次成功是
    25.1 秒（minimax-m3），预算低于此值意味着**每次必然超时**——那不是
    "偶尔拿不到结论"，而是功能等于没开，且完全静默。
    """
    creds2 = grader.llm_credentials_2()
    budget = grader._cross_timeout()
    return {
        "double_check": (str(os.environ.get("ZHIPI_DOUBLE_CHECK", "1")).strip()
                         != "0"),
        "cross_check": bool(creds2),
        "cross_model": creds2["model"] if creds2 else None,
        "cross_timeout": budget,
        "cross_budget_tight": bool(creds2) and budget < 25,
    }


@app.get("/api/demo/config")
def demo_config(session: dict = Depends(demo_session)):
    """前端启动参数：当前模式、识别引擎可用性、风控开关与本会话状态。"""
    folders.ensure(session)
    return {
        "mode": current_mode(),
        "vlm_configured": ocr_mod.vlm_configured(),
        # 强模型重识别：配了才在界面上给按钮。慢一个数量级，所以不做默认，
        # 由教师对着潦草的那一页按需触发。
        "strong_recognize": {
            "available": ocr_mod.strong_available(),
            "model": ocr_mod.strong_model_name(),
            "default_on": ocr_mod.strong_default(),
        },
        "adhoc_grading_available": bool(grader.llm_credentials()),
        "feishu_webhook_configured": bool(
            os.environ.get("ZHIPI_FEISHU_WEBHOOK", "").strip()),
        # 批改链路构成。前端据此如实说明「这次批改由几个模型参与」——
        # 两级复核都按设计静默降级，不透出来的话，界面在「配齐并生效」与
        # 「配了但每次超时」两种状态下长得一模一样。
        "chain": _chain_snapshot(),
        "guard": guard.config_snapshot(),
        "session": {
            "reviews": len(session["reviews"]),
            "uploads": len(session["uploads"]),
            "upload_max": session_store.UPLOAD_MAX,
            "active_folder_id": session.get("active_folder_id",
                                            folders.DEFAULT_FOLDER_ID),
            "folders": len(session.get("folders") or {}),
            "folder_max": folders.MAX_FOLDERS,
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
    dropped = pagestore.drop_session(session.get("_sid", ""))
    return {"status": "reset", "reviews": len(fresh["reviews"]),
            "uploads": len(fresh["uploads"]), "pages": dropped}



@app.get("/api/submissions")
def list_submissions():
    """列出内置学生作答（含转写预览）。

    保留的编程接口：前端不再有「按内置作答挑一份」的入口（改为内置样例夹），
    本接口供脚本 / 评测工具按 ID 枚举内置作答使用，②③④ 页面的基线数据也来自它。
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


@app.get("/api/demo-pages")
def list_demo_pages():
    """内置样例清单：三科真实作业（按学科分夹）+ 教师答案页（题库夹）。

    前端拿到 url 后取回文件字节，当成一份刚上传的作业送进 upload-pages，
    后面的拆页 / 识别 / 批改与体验者自己上传的作业**走同一条链路**。
    """
    groups = []
    for meta in demopages.FOLDERS:
        fid = meta["folder_id"]
        groups.append({
            "folder_id": fid,
            "name": meta["name"],
            "subject": meta.get("subject", ""),
            "role": meta.get("role", "student"),
            "items": [_demo_page_row(it) for it in demopages.by_folder(fid)],
        })
    return {
        "mode": current_mode(),
        "vlm_configured": ocr_mod.vlm_configured(),
        "groups": groups,
    }


def _demo_page_row(item: dict) -> dict:
    """一条内置样例的对外结构。刻意带上 pair_titles：学生页与答案页分处两个夹，
    不写清楚「这份对应题库里的哪一份」，体验者就得靠猜。"""
    return {
        "item_id": item["item_id"],
        "role": item["role"],
        "subject": item["subject"],
        "title": item["title"],
        "stage_name": item.get("stage_name") or item["title"],
        "mime": item["mime"],
        "page_count": item.get("page_count", 1),
        "bytes": item.get("bytes", 0),
        "is_pdf": item["mime"] == "application/pdf",
        "url": "/api/demo-pages/file/%s" % item["item_id"],
        "thumb": "/api/demo-pages/thumb/%s" % item["item_id"],
        "pairs_with": item.get("pairs_with") or [],
        "pair_titles": demopages.titles(item.get("pairs_with")),
    }


def _demo_page_or_404(item_id: str) -> dict:
    item = demopages.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="内置样例不存在：%s" % item_id)
    return item


@app.get("/api/demo-pages/file/{item_id}")
def get_demo_page_file(item_id: str):
    """取内置样例原件。文件名一律由 manifest 决定，不接受外部拼路径。"""
    item = _demo_page_or_404(item_id)
    return FileResponse(demopages.file_path(item), media_type=item["mime"])


@app.get("/api/demo-pages/thumb/{item_id}")
def get_demo_page_thumb(item_id: str):
    """夹内清单用的小图（长边 320）。PDF 取第 1 页渲染，打包时已生成。"""
    item = _demo_page_or_404(item_id)
    return FileResponse(demopages.thumb_path(item), media_type="image/jpeg")


def _open_question(stem: str, subject: str, printed_max: float | None = None) -> dict:
    """为题库外的作业构造一条题目记录（判别分维度体系）。

    与题库内题目的区别只有两处，其余字段同形，下游一律复用：
    - rubric 来自 dimensions.rubric_for(学科)，不是人工标注；
    - standard_answer 留空。刻意**不编**一个标准答案：编出来的答案会成为
      判分基准，错了却看不出来。改由模型在批改时自解并输出 reference_answer，
      展示给教师核对（见 grader.OPEN_PROMPT_TEMPLATE）。

    max_score 恒为 dimensions.TOTAL_SCORE（15）——这是**体系判别分**的满分，
    不是试卷上那道题的分值。试卷印的分值另存 printed_max_score，只展示、
    不参与计算，供教师换算成卷面分。
    """
    subject = (subject or "其他").strip() or "其他"
    return {
        "question_id": None,
        "open": True,                  # grade_adhoc 据此选维度体系 Prompt
        "subject": subject,
        "grade": "",
        "question_type": "开放题",
        "title": _adhoc_title(stem, subject),
        "question_text": (stem or "").strip(),
        "standard_answer": "",         # 刻意留空，理由见 docstring
        "rubric": dimensions.rubric_for(subject),
        "max_score": dimensions.TOTAL_SCORE,
        "printed_max_score": printed_max,
        # 题库外没有人工标注的知识点，也不能拿维度名充数（维度是批改的角度，
        # 不是课程知识点）。真实知识点由模型逐维给出，批改后写回结果。
        "knowledge_points": [],
    }


def _adhoc_title(stem: str, subject: str) -> str:
    """给题库外的题起一个短标题，用于教师台列表与看板分组。

    取题面首句、截断到 24 字。刻意**不**在标题里再写一遍学科——
    前端展示处已是「学科 · 标题」的格式，重复会出现「数学 · 数学 · 已知函数…」。
    """
    s = re.sub(r"\s+", " ", (stem or "")).strip()
    if not s:
        return (subject or "其他") + "作业"
    head = re.split(r"[。？？.!！\n]", s, 1)[0].strip() or s
    return head[:24] + ("…" if len(head) > 24 else "")


class RecognizeReq(BaseModel):
    # 二选一：page_id 指向 /api/upload-pages 已存下的页图（PDF 拆页后的走法），
    # image_base64 是直接把图片字节带上来（单张照片的老走法，保持兼容）。
    page_id: str | None = Field(None, max_length=40)
    image_base64: str | None = None
    mime: str = Field("image/png", max_length=64)
    # 教师在界面上点「强模型重识别」时为 true：这一页改用更强的多模态模型重读。
    # 慢一个数量级，所以由人按需触发，不做默认。
    strong: bool = False


def _decode_upload(image_base64: str) -> bytes:
    """把前端传来的 base64 还原为图片字节，并做体积 / 格式 / 像素三重校验。

    先按字符串长度粗筛再解码：base64 编码后约膨胀 4/3，先看长度可以在
    「解码出一个几十 MB 的 bytes」之前就拒掉，避免公网上被人用超大图刷内存。

    PDF 在这里被识别出来但**不解析**——它可能有多页，一页对应一份作业，
    展开成几份是调用方的事，这个函数只负责「还原并确认能用」。
    """
    limit = guard.MAX_IMAGE_BYTES
    if not image_base64 or len(image_base64) > (limit // 3 + 1) * 4 + 1024:
        raise HTTPException(status_code=413,
                            detail="文件为空或超过 %d MB 限制。" % (limit // 1024 // 1024))
    try:
        image_bytes = base64.b64decode(image_base64, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="文件 base64 解码失败")
    if not image_bytes or len(image_bytes) > limit:
        raise HTTPException(status_code=413,
                            detail="文件为空或超过 %d MB 限制。" % (limit // 1024 // 1024))
    if pdfpage.is_pdf(image_bytes):
        return image_bytes
    invalid = ocr_mod.validate_image(image_bytes)
    if invalid:
        raise HTTPException(status_code=400, detail=invalid)
    return image_bytes


def _as_page_images(data: bytes) -> list:
    """把上传内容摊成「一页一张图片字节」。图片是 1 页，PDF 是 N 页。

    返回 [(页码, image_bytes), ...]；页码从 1 起，图片恒为 [(1, data)]。
    """
    # EXIF 方向在**入口**统一处理掉：此后整条链路（尺寸、识别、bbox、痕迹渲染）
    # 只面对一种朝向。手机照片普遍是「横着存像素 + 一个 EXIF 说该转 90°」，
    # 浏览器认这个标记而 Pillow 不认，不在入口拉平就会出现「原图竖着显示、
    # 批改图横着显示、痕迹全部错位」——见 ocr.normalize_orientation 的说明。
    if not pdfpage.is_pdf(data):
        return [(1, ocr_mod.normalize_orientation(data))]
    try:
        pages, total = pdfpage.render_pages(data)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if total > len(pages):
        # 截断必须说出来。静默只批前 N 页，教师会以为整份都批过了。
        raise HTTPException(
            status_code=413,
            detail="这份 PDF 共 %d 页，单次最多处理 %d 页。请拆分后分次上传。"
                   % (total, pdfpage.MAX_PAGES))
    return pages


def _page_size(data: bytes) -> tuple:
    """读页图的像素尺寸；读不出返回 (0, 0)。批改痕迹按相对坐标画，前端要用它换算。

    按 EXIF 方向取**显示尺寸**而不是存储尺寸：横着存的竖向照片，img.size 报的是
    (3508, 2484)，而浏览器显示的是 (2484, 3508)。前端拿存储尺寸去换算相对坐标，
    宽高恰好对调，痕迹位置就整体错开。
    """
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as img:
            return ImageOps.exif_transpose(img).size
    except Exception:
        return (0, 0)


class UploadPagesReq(BaseModel):
    image_base64: str
    mime: str = Field("image/png", max_length=64)
    filename: str | None = Field(None, max_length=200)
    role: Literal["student", "teacher"] = "student"   # 学生页批改 / 教师页建题库


@app.post("/api/upload-pages")
def api_upload_pages(req: UploadPagesReq, request: Request,
                     session: dict = Depends(demo_session)):
    """把一份上传（照片或 PDF）拆成页，存进服务端页图暂存，返回每页的 ID。

    为什么要落到服务端而不是让前端自己拿着：批改痕迹要画回**学生本人那张
    作业图**，所以批改结束时服务端得还能拿到原图。让浏览器为了换一张画了
    勾叉的图再把整页传一遍，既慢又会撞上传体积上限。

    PDF 在这里被摊成 N 页，每页各自是一份待批作业——语文、英语的整份作业
    就是这样交上来的（扫描件，无文字层）。
    """
    _rate_guard(request, "recognize")
    data = _decode_upload(req.image_base64)
    sid = session.get("_sid", "")
    kind = "pdf" if pdfpage.is_pdf(data) else "image"
    pages = _as_page_images(data)
    mime = pdfpage.PAGE_MIME if kind == "pdf" else (req.mime or "image/png")

    out = []
    for page_no, blob in pages:
        width, height = _page_size(blob)
        page_id = pagestore.put(sid, blob, mime, {
            "page_no": page_no,
            "page_total": len(pages),
            "filename": req.filename or "",
            "role": req.role,
            "width": width,
            "height": height,
        })
        out.append({
            "page_id": page_id,
            "page_no": page_no,
            "width": width,
            "height": height,
            "mime": mime,
            "url": "/api/page/%s" % page_id,
        })
    return {"kind": kind, "page_total": len(out), "role": req.role, "pages": out}


@app.get("/api/page/{page_id}")
def api_page_image(page_id: str, marked: int = 0,
                   session: dict = Depends(demo_session)):
    """取回一页原图，marked=1 取带批改痕迹的版本。

    归属校验放在 pagestore.get 里：页图是别人的作业照片，拿到 ID 也不该
    跨会话取到。找不到与「不是你的」返回同一个 404，不泄露存在性。
    """
    item = pagestore.get(session.get("_sid", ""), page_id)
    if item is None:
        raise HTTPException(status_code=404, detail="页图不存在或已过期，请重新上传。")
    if marked:
        if not item.get("marked"):
            raise HTTPException(status_code=404, detail="这一页还没有批改痕迹。")
        body, mime = item["marked"], item["marked_mime"] or "image/jpeg"
    else:
        body, mime = item["data"], item["mime"]
    # 页图带会话归属，绝不能进共享缓存
    return Response(content=body, media_type=mime,
                    headers={"Cache-Control": "private, max-age=600"})


def _rate_guard(request: Request, bucket: str) -> None:
    """单 IP 限流；未启用时直接放行。"""
    ok, retry = guard.rate_limit(bucket, guard.client_ip(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="操作过于频繁，请 %d 秒后再试（公开体验限流）。" % retry,
            headers={"Retry-After": str(retry)})


def _resolve_page_input(req, session: dict) -> tuple:
    """把「page_id 或 image_base64」统一解析成 (图片字节, mime, 页元数据)。

    两条入口并存是有意的：整份 PDF 走 upload-pages 拆页后按 page_id 引用
    （服务端留着原图，批改痕迹才画得回去），单张照片的老链路仍可直接带
    base64 上来——内置样例图库、批量照片流水线都在用那一条。
    """
    if getattr(req, "page_id", None):
        item = pagestore.get(session.get("_sid", ""), req.page_id)
        if item is None:
            raise HTTPException(status_code=404,
                                detail="页图不存在或已过期，请重新上传这份作业。")
        return item["data"], item["mime"], item["meta"]
    if not getattr(req, "image_base64", None):
        raise HTTPException(status_code=400, detail="缺少 page_id 或 image_base64")
    # 直传 base64 这条路没经过 upload-pages，方向归一化要在这里补上，
    # 否则横置照片走单张链路时 bbox 与原图仍然错开一个 90°。
    return (ocr_mod.normalize_orientation(_decode_upload(req.image_base64)),
            req.mime, {})


@app.post("/api/recognize-image")
def api_recognize_image(req: RecognizeReq, request: Request,
                        session: dict = Depends(demo_session)):
    """作业照片手写识别：只有多模态大模型一条路，未配置凭据时如实报不可用。"""
    image_bytes, mime, page_meta = _resolve_page_input(req, session)
    _rate_guard(request, "recognize")

    # 真实多模态识别按日配额放行。额度用尽时如实说明「今天不能再识别了」——
    # 内置样例现在也是真实作业原件，没有离线兜底可以回落。
    allow_vlm, note = True, ""
    if ocr_mod.vlm_configured() and not guard.quota_take("vlm"):
        allow_vlm = False
        note = "今日真实识别额度已用尽（公开体验限额），请明天再试或自行部署配置密钥。"

    result = ocr_mod.recognize_image(image_bytes, mime,
                                     allow_vlm=allow_vlm, unavailable_note=note,
                                     strong=bool(req.strong))
    if note:
        result["quota_note"] = note
    if req.page_id:
        result["page_id"] = req.page_id
        result["page_no"] = page_meta.get("page_no", 1)
        result["page_total"] = page_meta.get("page_total", 1)
        result["page_url"] = "/api/page/%s" % req.page_id
    if result.get("engine") == "none":
        return result

    # 题库外的任意上传：学科与题面直接用识别结果，**不做题库匹配**。
    # 原先这里按 difflib 相似度硬套一道内置题，把一页导数题判成英语作文，
    # 再用英语评分标准批出 0 分。题库只有 3 道题，真实作业几乎必然不在
    # 里面，所以「匹配」本身就是错的问题——模型能读懂这张纸，让它直说。
    qlist = result.get("questions") or []
    if qlist:
        first = qlist[0]
        result.update({
            "question_id": None,          # 题库外，无 ID
            "subject": first["subject"],
            "question_title": _adhoc_title(first["stem"], first["subject"]),
            "question_text": first["stem"],
            "printed_max_score": first["printed_max_score"],
        })

    # 整份试卷 / 一图多题：把每道题都摊给前端。
    # 这里以前有一个「本次最多批前 8 道」的截断——那是「每道题一次模型调用」
    # 时代的产物。整页现在是一次调用批完，题数只影响一次请求的 token 数，
    # 截断的理由随之消失，全部题目一律可批。
    qlist = result.get("questions") or []
    for q in qlist:
        q["title"] = _adhoc_title(q["stem"], q["subject"])
        q["gradable"] = True
    result["question_count"] = len(qlist)
    result["gradable_count"] = len(qlist)
    return result


class GradeImageReq(BaseModel):
    question_id: str | None = None             # 题库内题目：题目 + 转写 + 清晰度
    ocr_text: str | None = Field(None, max_length=4000)   # 限长：转写文本要发给大模型，按 token 计费
    ocr_clarity: float = Field(75.0, ge=0, le=100)   # 防直调 API 构造超界置信度
    # 不设 max_length=20：姓名的限长由 _clean_name 截断承担。写在 Field 上会抢在
    # 清洗之前把整个请求打回 422——而这里最常见的"超长姓名"是微信 / 相机的
    # 机器文件名（如 32 位十六进制），用户完全没意识到自己填了名字，却只看到
    # 一句 "String should have at most 20 characters" 和一份没批成的作业。
    # 截断是无损的（姓名只用于展示），拒绝整次批改不是。
    student_name: str = Field("上传作业", max_length=200)
    engine: str | None = Field(None, max_length=32)   # 识别引擎（回显用）
    folder_id: str | None = Field(None, max_length=32)  # 上传归属文件夹
    # 题库外的真实作业：题面 + 学科由识别阶段给出，不再往题库里硬套。
    # 这三个字段是「判别分维度体系」的入口——有题面就能批，不需要 question_id。
    stem: str | None = Field(None, max_length=4000)     # 照片里的印刷题面（教师可修正）
    subject: str | None = Field(None, max_length=16)    # 识别出的学科
    printed_max_score: float | None = Field(None, ge=0, le=300)  # 题面印的分值，仅展示
    # 同一份作业拆出的多条结果共享 paper_id，前端与看板据此聚回一份。
    # 整页批改（/api/grade-page）不用它——那条路一次出一份结果，天然就是一份。
    # 这三个字段留给单题批改（/api/grade-image）被脚本按题逐条调用的场景。
    paper_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    paper_index: int | None = Field(None, ge=1, le=200)   # 本题在卷中的序号
    paper_total: int | None = Field(None, ge=1, le=200)   # 该卷共几道题


def _clean_name(raw: str) -> str:
    """清洗体验者填写的姓名：去控制字符、限长，空则回退默认值。

    前端渲染已统一转义，这里再收一道，避免脏数据进入讲评大纲 / 飞书台账。
    """
    name = re.sub(r"[\x00-\x1f\x7f]", "", (raw or "")).strip()[:20]
    return name or "上传作业"


@app.post("/api/grade-image")
def api_grade_image(req: GradeImageReq, request: Request,
                    session: dict = Depends(demo_session)):
    """图片批改：按识别出的转写文本走 LLM 临时批改。

    教师对识别结果的人工修正真实生效——批改吃的是前端回传的转写文本，
    不是识别时的原始输出（教师可控原则）。

    批改结果会存进**本会话**，并带一个 UP-xxx 临时 ID，这样它能出现在
    教师工作台与班级看板里，体验动线不至于批完就断。
    """
    if not (req.ocr_text and req.ocr_text.strip()):
        raise HTTPException(status_code=400, detail="缺少 ocr_text（学生作答转写）")

    if req.question_id:
        # 题库内题目：沿用人工标注的 Rubric 与标准答案
        question = QUESTIONS.get(req.question_id)
        if not question:
            raise HTTPException(status_code=404, detail="题目不存在：%s" % req.question_id)
    elif req.stem and req.stem.strip():
        # 题库外的真实作业：按学科下发维度 Rubric，模型自解后按维度判分。
        # 原先这里强求 question_id，而识别侧对题库外作业给的是 None，
        # 于是真实作业一律 400——「不再硬套题库」必须同时开出这条路，
        # 否则只是把「判错」换成了「批不了」。
        question = _open_question(req.stem, req.subject or "", req.printed_max_score)
    else:
        raise HTTPException(
            status_code=400,
            detail="缺少题面：题库外的作业需要印刷题面作为判分依据。"
                   "请在识别结果的「题面」框中补齐题目原文后重新提交。")

    _rate_guard(request, "grade")
    if not guard.quota_take("grade"):
        raise HTTPException(
            status_code=429,
            detail="今日真实批改额度已用尽（公开体验限额）。可点选内置样例作业照片，"
                   "离线链路完全不受影响，红黄绿分流与教师终审都能正常体验。")

    # 教师通过率回灌。题库内按题统计；题库外没有 question_id，改按
    # 「学科 × 维度」统计——题是无穷的，维度只有五个，收敛快得多。
    if req.question_id:
        rate = question_pass_rate(session, req.question_id)
    else:
        rate = dimension_pass_rate(session, question["subject"])
    overrides = {"teacher_pass_rate": rate} if rate is not None else None
    # 回灌进 Prompt 的偏差要过样本门槛：n=1 的一次修正不该左右后续判分松紧
    bias = dimension_bias(session, question["subject"],
                          min_samples=BIAS_MIN_SAMPLES) if question.get("open") else {}
    try:
        result = grader.grade_adhoc(question, req.ocr_text, req.ocr_clarity,
                                    factor_overrides=overrides,
                                    dimension_bias=bias)
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
                detail="批改超时（已等满 %d 秒）。大模型网关响应过慢或上游无响应，"
                       "可稍后重试、换用更快的模型，或调大 ZHIPI_LLM_TIMEOUT；"
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
    # 整份试卷分组信息。index / total 只有在 paper_id 存在时才有意义，
    # 单独给 index 而不给 id 是调用方拼错了，按无分组处理而不是记一个悬空序号。
    if req.paper_id:
        result["paper_id"] = req.paper_id
        result["paper_index"] = req.paper_index
        result["paper_total"] = req.paper_total
    # 存进本会话并就地写入 UP-xxx 临时 ID，让这份作业能进教师工作台与班级看板
    upload_id = session_store.add_upload(session, result)
    folders.add_item(session, target_folder, upload_id)
    return result


# ---------------------------------------------------------------------------
# 教师页 → 会话题库
# ---------------------------------------------------------------------------

class BankBuildReq(BaseModel):
    page_ids: list[str] = Field(..., max_length=pdfpage.MAX_PAGES)
    name: str | None = Field(None, max_length=40)


@app.post("/api/bank/build")
def api_bank_build(req: BankBuildReq, request: Request,
                   session: dict = Depends(demo_session)):
    """读教师答案页，建一套本会话的题库。

    题库是「答案匹配度」这一维的基准。没有它，该因子测不出、按权重重归一化
    剔除，五因子实际只有四项在工作——这不是缺陷，是如实表达「这一维没有
    依据」。教师传了答案页，它才真的开始计分。
    """
    if not ocr_mod.vlm_configured():
        raise HTTPException(
            status_code=400,
            detail="建题库需要读答案页上的手写标准答案，必须配置多模态识别密钥"
                   "（ZHIPI_VLM_API_KEY）。未配置时可直接批改学生作业——"
                   "由大模型自行判分，置信度的「答案匹配度」一维留空、权重重归一化。")
    if not req.page_ids:
        raise HTTPException(status_code=400, detail="请先上传教师答案页")

    _rate_guard(request, "recognize")
    sid = session.get("_sid", "")
    pages, subject = [], ""
    for page_id in req.page_ids:
        item = pagestore.get(sid, page_id)
        if item is None:
            raise HTTPException(status_code=404,
                                detail="答案页不存在或已过期，请重新上传。")
        if not guard.quota_take("vlm"):
            raise HTTPException(
                status_code=429,
                detail="今日真实识别额度已用尽（公开体验限额），暂时无法建题库。")
        try:
            recognized = ocr_mod.recognize_teacher_page(item["data"], item["mime"])
        except Exception as exc:
            guard.quota_refund("vlm")
            raise HTTPException(status_code=502, detail="答案页识别失败：%s" % exc)
        subject = subject or recognized.get("subject", "")
        pages.append({
            "page_id": page_id,
            "page_no": item["meta"].get("page_no", 1),
            "questions": recognized.get("questions") or [],
        })

    questions = bank.build_questions(pages)
    if not questions:
        raise HTTPException(
            status_code=422,
            detail="这几页答案页里没读出任何题目。请确认上传的是**教师答案页**"
                   "（印有题目、并写有标准答案），而不是空白练习页。")

    name = req.name or _bank_default_name(subject, questions)
    record = bank.create(session, name, subject, questions, req.page_ids)
    guessed = sum(1 for q in questions if q["score_source"] == "default")
    return {
        "bank_id": record["bank_id"],
        "name": record["name"],
        "subject": record["subject"],
        "question_count": len(questions),
        "total_score": round(sum(q["max_score"] for q in questions), 1),
        # 分值有几道是系统推的必须说出来。教师看到「总分 46」时要知道其中
        # 哪些是卷面印的、哪些是按题型默认推的，否则他会以为 46 是卷面数字。
        "guessed_score_count": guessed,
        "questions": questions,
    }


def _bank_default_name(subject: str, questions: list) -> str:
    """题库没起名时，用「学科 + 首题题干开头」凑一个可辨认的名字。"""
    head = ""
    for q in questions:
        head = _adhoc_title(q.get("stem", ""), subject)
        if head:
            break
    return ("%s · %s" % (subject or "作业", head or "答案页")).strip()[:40]


@app.get("/api/bank/list")
def api_bank_list(session: dict = Depends(demo_session)):
    """本会话的题库清单（供批改前选择）。"""
    return {"banks": bank.listing(session), "vlm_configured": ocr_mod.vlm_configured()}


@app.get("/api/bank/{bank_id}")
def api_bank_detail(bank_id: str, session: dict = Depends(demo_session)):
    """一套题库的完整题目（教师核对与改分值用）。"""
    record = bank.get(session, bank_id)
    if record is None:
        raise HTTPException(status_code=404, detail="题库不存在或已过期")
    return record


class BankScoreReq(BaseModel):
    qid: str = Field(..., max_length=16)
    max_score: float = Field(..., gt=0, le=150)


@app.post("/api/bank/{bank_id}/score")
def api_bank_set_score(bank_id: str, req: BankScoreReq,
                       session: dict = Depends(demo_session)):
    """教师改某题的分值。

    分值有两个来源：卷面印的、系统按题型推的。推定值必然有猜错的时候，
    所以必须能改——不能改的话，「系统推定」就成了一个教师无法反驳的判断。
    改过之后 score_source 记为 teacher，界面据此不再标「系统推定」。
    """
    record = bank.get(session, bank_id)
    if record is None:
        raise HTTPException(status_code=404, detail="题库不存在或已过期")
    for q in record["questions"]:
        if q["qid"] == req.qid:
            q["max_score"] = float(req.max_score)
            q["score_source"] = "teacher"
            q["rubric"] = bank.rubric_for_item(q["qtype"], q["max_score"])
            return {"qid": q["qid"], "max_score": q["max_score"],
                    "score_source": q["score_source"],
                    "total_score": round(sum(x["max_score"]
                                             for x in record["questions"]), 1)}
    raise HTTPException(status_code=404, detail="题库里没有这道题：%s" % req.qid)


@app.delete("/api/bank/{bank_id}")
def api_bank_delete(bank_id: str, session: dict = Depends(demo_session)):
    """删掉一套题库。"""
    if not bank.remove(session, bank_id):
        raise HTTPException(status_code=404, detail="题库不存在或已过期")
    return {"status": "deleted", "bank_id": bank_id}


# ---------------------------------------------------------------------------
# 学生页整页批改
# ---------------------------------------------------------------------------

class GradePageReq(BaseModel):
    page_id: str = Field(..., max_length=40)
    # 识别结果由前端回传：识别与批改分两步，中间教师可以修正转写。
    # 不在服务端重新识别一次——那会多烧一次多模态额度，还会让教师的修正失效。
    questions: list[dict] = Field(..., max_length=100)
    subject: str | None = Field(None, max_length=16)
    clarity: float = Field(75.0, ge=0, le=100)
    bank_id: str | None = Field(None, max_length=40)   # 选了题库就按题库判分
    student_name: str = Field("上传作业", max_length=200)
    engine: str | None = Field(None, max_length=32)
    folder_id: str | None = Field(None, max_length=32)


@app.post("/api/grade-page")
def api_grade_page(req: GradePageReq, request: Request,
                   session: dict = Depends(demo_session)):
    """整页批改：一次大模型调用批完这一页的全部题目，并在原图上留痕。

    有题库（req.bank_id）→ 按题干相似度把学生页的题对齐到题库，逐题带上
    教师给的标准答案与分值判分，答案匹配度真实计入置信度；
    无题库 → 同一条流程，大模型自行判分，答案匹配度留空、权重重归一化。
    """
    sid = session.get("_sid", "")
    page = pagestore.get(sid, req.page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="页图不存在或已过期，请重新上传。")

    student_questions = _clean_page_questions(req.questions)
    if not student_questions:
        raise HTTPException(
            status_code=400,
            detail="这一页没有可批改的题目。若识别结果为空，请确认上传的是学生作业页。")

    record = None
    if req.bank_id:
        record = bank.get(session, req.bank_id)
        if record is None:
            raise HTTPException(status_code=404, detail="题库不存在或已过期")

    subject = (req.subject or "").strip() or (record or {}).get("subject", "") \
        or (student_questions[0].get("subject") or "")
    items, matched_count, missing_qs = _align_to_bank(student_questions, record,
                                                      req.page_id)

    _rate_guard(request, "grade")
    if not guard.quota_take("grade"):
        raise HTTPException(
            status_code=429,
            detail="今日真实批改额度已用尽（公开体验限额）。可点选内置样例作业照片，"
                   "离线链路完全不受影响，红黄绿分流与教师终审都能正常体验。")

    rate = dimension_pass_rate(session, subject)
    overrides = {"teacher_pass_rate": rate} if rate is not None else None
    try:
        result = pagegrader.grade_page(subject, items, req.clarity,
                                       with_bank=bool(record),
                                       factor_overrides=overrides)
    except Exception as exc:
        guard.quota_refund("grade")
        text = "%s %s" % (type(exc).__name__, exc)
        if "timed out" in text.lower() or "timeout" in text.lower():
            raise HTTPException(
                status_code=504,
                detail="整页批改超时（已等满 %d 秒）。整页比单题的 prompt 长，"
                       "可稍后重试、换用更快的模型，或调大 ZHIPI_LLM_TIMEOUT。"
                       % grader._llm_timeout())
        raise HTTPException(status_code=502, detail="整页批改失败：%s" % exc)

    # 题库里有、这一页却没找到的题：可能是学生没做，也可能是识别漏读，系统分不出。
    # 所以既不判 0（做了的学生会被冤枉），也不静默略过（漏读就永远看不见），
    # 而是如实列出来；夹在本页题目区间之内的那些多半真是漏读，额外压住绿灯。
    if missing_qs:
        result["missing_questions"] = missing_qs
        inside = [m for m in missing_qs if m.get("inside_page_range")]
        result["missing_inside_count"] = len(inside)
        if inside:
            if result.get("status") == "green":
                result["status"] = "yellow"
            names = "、".join((m["no"] or m["stem"][:8]) for m in inside[:5])
            result["teacher_note"] = (result.get("teacher_note") or "") + \
                "｜题库里的第 %s 题夹在本页题目之间却没被识别到（共 %d 道），" \
                "多半是漏读而不是学生没作答；它们未计入总分，请人工确认" % (
                    names, len(inside))

    # 在学生自己那张作业图上画批改痕迹
    marked_url = None
    # located 数的是「记号有落点」的题：优先看学生作答框，其次题目框——
    # 与 marks._anchor 同一口径，不然界面会报出一个和图上对不上的数。
    located = sum(1 for q in result["questions"]
                  if q.get("answer_box") or q.get("bbox"))
    on_answer = sum(1 for q in result["questions"] if q.get("answer_box"))
    try:
        marked = marks.render(page["data"], result["questions"], header={
            "student": _clean_name(req.student_name),
            "total": result["total_score"],
            "max": result["max_score"],
            "status": result["status"],
        })
        if pagestore.set_marked(sid, req.page_id, marked, "image/jpeg"):
            marked_url = "/api/page/%s?marked=1" % req.page_id
    except Exception as exc:
        # 痕迹画不出来不该让整次批改失败——分数、证据链、分流都已经算好了。
        # 但也不能装作画了：把原因带回前端，界面显示「痕迹渲染失败」。
        result["mark_error"] = "批改痕迹渲染失败：%s" % exc
    # 有多少道题的记号真的落在题目上，如实报给界面。识别给的坐标不精确，
    # 报出来教师才知道「记号没贴着题」是坐标不准，不是系统认错了题。
    # on_answer 单独报：勾叉落在**学生作答**旁边才是想要的效果，只落在题目框上
    # 说明这题的作答没被定位到，记号是贴着题写的而不是贴着答案写的。
    result["mark_stats"] = {"located": located, "on_answer": on_answer,
                            "total": len(result["questions"])}

    try:
        target_folder = folders.resolve_target(session, req.folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    result.update({
        "source": "upload",
        "recognition_engine": req.engine or "vlm",
        "student_name": _clean_name(req.student_name),
        "question_id": None,
        "question_title": _page_title(page, subject, len(items)),
        "subject": subject,
        "question_text": "",
        "standard_answer": "",
        "ocr_text": "\n\n".join(
            "%s %s" % (q.get("no") or ("第%d题" % q["index"]), q.get("student_answer", ""))
            for q in items).strip(),
        "folder_id": target_folder,
        "page_id": req.page_id,
        "page_no": page["meta"].get("page_no", 1),
        "page_total": page["meta"].get("page_total", 1),
        "page_url": "/api/page/%s" % req.page_id,
        "marked_url": marked_url,
        "bank_id": req.bank_id,
        "bank_name": (record or {}).get("name", ""),
        "matched_count": matched_count,
        "question_count": len(items),
        # 满分里有多少来自「卷面真的印了分值」。整页总分是各题分值之和，
        # 而没印分值的题是按题型推的——不说清楚，教师会把这个总分当卷面分抄走。
        "printed_score_count": sum(
            1 for q in items
            if isinstance(q.get("printed_max_score"), (int, float))
            and q["printed_max_score"] > 0),
    })
    upload_id = session_store.add_upload(session, result)
    folders.add_item(session, target_folder, upload_id)
    return result


def _clean_page_questions(raw: list) -> list:
    """清洗前端回传的识别结果：只留下游真正要用的字段，并做长度与类型校验。

    直接把请求体里的 dict 往下传是不行的——这些字段会进 prompt、进结果、
    进教师工作台，任意长度的字符串在这三处都是问题。
    """
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or "").strip()[:2000]
        answer = str(item.get("answer") or item.get("student_answer") or "").strip()[:2000]
        if not stem and not answer:
            continue
        out.append({
            "index": len(out) + 1,
            "no": str(item.get("no") or "").strip()[:12],
            "subject": str(item.get("subject") or "").strip()[:16],
            # 题型从识别阶段透传下来。它决定「这道题该不该看解题步骤」——
            # 客观题（选择/填空/判断）只核对答案，没有过程分可扣。
            # 前端可能不回传（旧版本、手工构造的请求），留空由 _align_to_bank 按
            # 作答形态兜底推断，绝不能让它一路空到批改 prompt 里。
            "qtype": ocr_mod._normalize_qtype(
                item.get("qtype"), stem, answer) if item.get("qtype") else "",
            "stem": stem,
            "student_answer": answer,
            "printed_max_score": item.get("printed_max_score"),
            "bbox": ocr_mod._normalize_bbox(item.get("bbox")),
            # 学生作答所在的框。批改痕迹靠它把勾叉画到学生写的那几个字上，
            # 而不是画在题目框里——不传的话痕迹只能退回页边，跟着题走而不
            # 跟着答案走，一页批下来看不出「哪一处答案错了」。
            # 面积上限放到 1.0：一道解答题的作答本来就可能占半页，
            # 按题目框那套 0.35 的上限卡，长答案的框会被整个丢掉。
            "answer_box": ocr_mod._normalize_bbox(item.get("answer_box"), 1.0),
            # 识别阶段标出的越界作答与归属把握，透传给批改与界面：
            # 越界题的转写更可能有误，教师复核时该优先看它。
            "overflow": bool(item.get("overflow")),
            # 收敛成数值或 None：下游要拿它跟阈值比大小，字符串进来会直接抛。
            # bool 也挡掉——True 悄悄变成 1.0 会伪装成「归属把握满分」。
            "attribution_confidence": (
                float(item["attribution_confidence"])
                if isinstance(item.get("attribution_confidence"), (int, float))
                and not isinstance(item.get("attribution_confidence"), bool)
                else None),
            # 定向复识的留痕。refined=这道题的转写被系统改写过；legible_hint=False
            # 是模型自己都说看不清。两者都只用于「要不要请教师复核」，不参与判分，
            # 所以按回传值收下，但仍要收敛类型，别让任意 JSON 流进下游。
            "refined": bool(item.get("refined")),
            "refine_changed": bool(item.get("refine_changed")),
            "refine_reason": str(item.get("refine_reason") or "").strip()[:24],
            "legible_hint": False if item.get("legible_hint") is False else None,
        })
    return out


def _guess_max_score(q: dict, qtype: str = "") -> float:
    """给「没有题库依据」的题推一个分值。

    优先用卷面印的分值；卷面没印就按**题型**推（选择填空 2 分、解答 6 分……），
    题型优先用调用方已经推断好的值，传空则从作答形态再推一次。刻意不用一个
    统一的常数：一道选择题和一道解答题都按 10 分算，会让整页满分既不是卷面分
    也不是任何一种可解释的口径——实测一道 2 分的选择题因此被记成 10 分。
    """
    printed = q.get("printed_max_score")
    if isinstance(printed, (int, float)) and 0 < printed <= 150:
        return float(printed)
    if not qtype:
        qtype = ocr_mod._normalize_qtype("", q.get("stem", ""), q.get("student_answer", ""))
    return float(bank.default_score(qtype))


def _align_to_bank(student_questions: list, record, page_id: str) -> tuple:
    """把学生页的题对齐到题库，产出送批清单。返回 (items, 对上的题数, 缺题清单)。

    对齐按**题干相似度**，不按题号：教师页与学生页常常不是同一份版式
    （答案册 vs 练习册），题号各排各的；一份周周清里「1.」还会在多个板块
    重复出现。按题号对齐会稳定地对错行，且错得毫无征兆。

    没有题库依据的题（无题库、或有题库但这一题没对上）按卷面分值 / 题型
    推定分值，见 _guess_max_score。

    第三个返回值是**题库里有、这一页却没找到的题**。识别漏题是真实发生的
    （版面解析跳过页首页尾的续页题、把学生写工整的填空当成印刷题干），
    而漏掉的题以前是静默消失的：不判分、不提示、总分照出——教师看到的是一份
    「批完了」的作业，少掉的那道题没有任何痕迹。这里如实报出来。
    """
    if record is None:
        items = []
        for q in student_questions:
            # 题型推断：优先用识别阶段已经给出的值，没有再按作答形态猜。
            # 不能留空：空题型进 batch prompt 里，模型看不出这是填空题，
            # 就会对一道只要答案的题打出「步骤缺失」扣分。
            qtype = (q.get("qtype") or "").strip()
            if not qtype:
                qtype = ocr_mod._normalize_qtype(
                    "", q.get("stem", ""), q.get("student_answer", ""))
            items.append({
                **q,
                "qid": None,
                "qtype": qtype,
                "standard_answer": "",
                "max_score": _guess_max_score(q, qtype),
                "match_score": 0.0,
                "page_id": page_id,
            })
        return items, 0, []

    bank_map = {q["qid"]: q for q in record["questions"]}
    # 题库原顺序。归并时要按它给多条小问排序（否则 (3) 可能排在 (1) 前面，
    # 拼出来的标准答案顺序是乱的），后面判定「漏题在不在本页范围内」也用它。
    order_of = {q["qid"]: i for i, q in enumerate(record["questions"])}
    alignment = bank.align(student_questions, record["questions"])
    # 一对一之后再做一轮小问归并：教师页把 2(1)(2)(3)(4) 拆成四条、学生页
    # 第 2 题是一整块时，剩下的三条并不是「学生没做」，而是写在同一块里。
    # 不归并的话它们会被报成「题库里有、本页没识别到」，教师按提示去找一道
    # 根本不存在的漏题；反过来（教师页粗、学生页细）则报「题库中无此题」，
    # 分值退回按题型推定——两种都是同一个粒度错位的两面。
    absorbed = bank.absorb_leftovers(student_questions, record["questions"],
                                     alignment)
    items, matched = [], 0
    hit_qids = set()
    for si, (q, hit) in enumerate(zip(student_questions, alignment)):
        bq = bank_map.get(hit["qid"]) if hit["qid"] else None
        extra = absorbed.get(si) or []
        if bq or extra:
            matched += 1
            # 归并后这道题的题库依据 = 一对一命中的那条 + 被它包含的小问。
            # 标准答案按题库原顺序拼（否则 (3) 会排在 (1) 前面），但**代表这道题
            # 的仍是一对一命中的那条**：qid / 题型要跟着最可信的那个匹配走。
            # 早先这里取的是排序后的第一条，于是一道正确命中了本页题的作业，
            # 会因为顺带归并了一条排在更前面的题，qid 显示成那一条——教师看到
            # 的是「这题对到了另一道题上」，而实际匹配是对的。
            entries = ([(bq, hit["score"])] if bq else []) + extra
            entries.sort(key=lambda e: order_of.get(e[0]["qid"], 0))
            for ebq, _cov in entries:
                hit_qids.add(ebq["qid"])
            head = bq or entries[0][0]
            items.append({
                **q,
                "qid": head["qid"],
                "no": q.get("no") or head.get("no", ""),
                "qtype": head["qtype"],
                "standard_answer": bank.merge_standard_answers(entries),
                "max_score": round(sum(float(e[0]["max_score"]) for e in entries), 2),
                "match_score": hit["score"],
                # 归并留痕：教师看到的满分是几条小问加出来的，得说清是哪几条，
                # 否则「这题怎么 8 分」无从对账。
                "absorbed": [{"qid": e[0]["qid"], "no": e[0].get("no", ""),
                              "coverage": e[1]} for e in extra],
                "page_id": page_id,
            })
        else:
            # 没对上题库的题照批，只是它不进答案匹配度的统计（无基准可比）。
            qtype = (q.get("qtype") or "").strip()
            if not qtype:
                qtype = ocr_mod._normalize_qtype(
                    "", q.get("stem", ""), q.get("student_answer", ""))
            items.append({
                **q,
                "qid": None,
                "qtype": qtype,
                "standard_answer": "",
                "max_score": _guess_max_score(q, qtype),
                "match_score": hit["score"],
                "absorbed": [],
                "page_id": page_id,
            })

    # 题库里有、这一页没对上的题。分两类，因为一套题库常常是多页教师页合并的，
    # 而学生页是一页一批——「题库有而本页没有」本身很正常，别页的题不该报警：
    #   inside=True  夹在本页已对上的题之间 → 这一页确实该有它，多半是识别漏读
    #   inside=False 排在本页范围之外       → 很可能属于别的页，只列出不压分流
    hit_pos = sorted(order_of[qid] for qid in hit_qids if qid in order_of)
    lo, hi = (hit_pos[0], hit_pos[-1]) if hit_pos else (None, None)
    missing = []
    for i, bq in enumerate(record["questions"]):
        if bq["qid"] in hit_qids:
            continue
        missing.append({
            "qid": bq["qid"],
            "no": bq.get("no", ""),
            "stem": (bq.get("stem") or "")[:60],
            "qtype": bq.get("qtype", ""),
            "max_score": float(bq["max_score"]),
            "inside_page_range": lo is not None and lo < i < hi,
        })
    return items, matched, missing


def _page_title(page: dict, subject: str, count: int) -> str:
    """给整页批改结果起一个在教师工作台里可辨认的标题。"""
    name = (page["meta"].get("filename") or "").strip()
    total = page["meta"].get("page_total", 1)
    head = name or (subject or "作业")
    if total > 1:
        head += " 第 %d 页" % page["meta"].get("page_no", 1)
    return ("%s · %d 题" % (head, count))[:60]


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
            # 判别分口径与试卷原始分值：前端据此标注「体系判别分」而不是
            # 让 14/15 看起来像卷面分。题库内的题没有这两个字段。
            "score_basis": graded.get("score_basis", "question"),
            "printed_max_score": graded.get("printed_max_score"),
            "reference_answer": graded.get("reference_answer", ""),
            # 分组标记：单题批改按 paper_id 聚回一份作业；整页批改一次出一份，
            # 用页码标出它是多页作业里的第几页。教师台按行展示，两者都要能看出来源。
            "paper_id": graded.get("paper_id"),
            "paper_index": graded.get("paper_index"),
            "paper_total": graded.get("paper_total"),
            "page_no": graded.get("page_no"),
            "page_total": graded.get("page_total"),
            "marked_url": graded.get("marked_url"),
            "reviewed": bool(review),
            "final_score": review["final_score"] if review else None,
            "final_error_tags": review.get("final_error_tags") if review else None,
            "final_comment": review.get("final_comment") if review else None,
            "teacher_action": review["teacher_action"] if review else None,
            # 已按维度改过的分：重开弹层时要显示教师上次的值，而不是回到 AI 分
            "final_dimension_scores": review.get("final_dimension_scores") if review else None,
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
    # 维度级改分：{维度key: 教师给的分}。只对判别分维度体系的作业有意义。
    # 有它才知道教师改的是哪一维——只有总分的话，回灌只能得出「这题判错了」，
    # 得不出「运算执行这一维判得偏严」，而后者才是能指导下一份批改的信息。
    final_dimension_scores: dict[str, float] | None = None


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

    # 维度级改分 → 逐维修正量。这是回灌到后续批改的原料：
    # delta > 0 表示教师往上改（模型这一维偏严），< 0 表示往下改（偏松）。
    deltas, final_dims = _dimension_deltas(ai, req.final_dimension_scores)

    record = {
        "submission_id": sid,
        "teacher_action": req.teacher_action,
        "ai_score": ai["total_score"],
        "final_score": final_score,
        "max_score": ai["max_score"],
        "final_error_tags": final_tags,          # None = 沿用 AI 错因
        "final_comment": (req.final_comment or "")[:500],
        "reviewed": True,
        # 学科要存下来：dimension_pass_rate / dimension_bias 按学科聚合，
        # 而终审记录本身查不到学科（uploads 里才有）。
        "subject": ai.get("subject", ""),
        "dimension_deltas": deltas,             # {} = 未按维度改或非维度体系作业
        "final_dimension_scores": final_dims,
    }
    session["reviews"][sid] = record

    qid = _review_question_id(session, sid)
    subject = ai.get("subject", "")
    return {
        "status": req.teacher_action,
        "review": record,
        "question_pass_rate": question_pass_rate(session, qid) if qid else None,
        # 维度体系作业回传学科级认可度与逐维偏差，让前端能显示
        # 「教师终审已影响后续同类作业的判分」这条飞轮
        "dimension_pass_rate": dimension_pass_rate(session, subject) if subject else None,
        # 展示用不设样本门槛：n=1 也是真实数据，教师有权看见
        "dimension_bias": dimension_bias(session, subject) if subject else {},
        # 但要同时告知每一维攒了几条，以及攒到几条才会真正回灌进后续判分，
        # 否则「已影响后续批改」这句话在 n=1 时是不成立的
        "dimension_bias_samples": dimension_bias_samples(session, subject) if subject else {},
        "dimension_bias_min_samples": BIAS_MIN_SAMPLES,
    }


def _dimension_deltas(ai: dict, final_scores: dict | None):
    """算出教师逐维修正量，并返回校验后的终审维度分。

    校验：维度 key 必须在枚举内、分数必须在该维满分内。非法项静默丢弃而不报错——
    终审是教师的主流程，不该因为一个越界的维度分整单失败；被丢弃的项等同
    「这一维没改」，方向上是保守的。
    """
    steps = {s.get("dimension"): s for s in ai.get("step_analysis", [])
             if s.get("dimension")}
    if not steps or not final_scores:
        return {}, {}

    deltas, cleaned = {}, {}
    for key, raw in final_scores.items():
        step = steps.get(key)
        if step is None or key not in dimensions.DIMENSION_KEYS:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if not (0 <= val <= step["max_score"]):
            continue
        cleaned[key] = val
        deltas[key] = round(val - step["score"], 2)
    return deltas, cleaned


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
    # llm 模式下内置作答是惰性批改的：看板可能在还有若干份没批完时就被打开，
    # 此时平均得分率、薄弱知识点都是「按已批的那几份算出来的」。实测中途打开
    # 会看到 19.4% / 薄弱知识点 0 这种数，看起来却像最终结论。
    # 把进度一起给出去，让界面能说清这一点，而不是把半成品当成结果展示。
    data["graded_count"] = len(GRADED)
    data["graded_total"] = len(SUBMISSIONS)
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
        "active_folder_id": session.get("active_folder_id", folders.DEFAULT_FOLDER_ID),
        "folders": folders.list_summaries(session, builtin_counts=demopages.counts()),
    }


@app.post("/api/folders")
def api_folders_create(req: FolderCreateReq, request: Request,
                       session: dict = Depends(demo_session)):
    """自建文件夹。

    限流：夹子数量本身有 MAX_FOLDERS 兜底，但没有限流的话，脚本可以贴着上限
    反复建了删、删了建，每次都要重算摘要。改写类接口一律过 _rate_guard，
    与上传 / 批改口径一致。
    """
    _rate_guard(request, "folder")
    try:
        meta = folders.create(session, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "folder": meta,
        "folders": folders.list_summaries(session, builtin_counts=demopages.counts()),
        "active_folder_id": session.get("active_folder_id"),
    }


@app.patch("/api/folders/{folder_id}")
def api_folders_rename(folder_id: str, req: FolderRenameReq, request: Request,
                       session: dict = Depends(demo_session)):
    """重命名自建文件夹（Demo 样例夹拒绝）。"""
    _rate_guard(request, "folder")
    try:
        meta = folders.rename(session, folder_id, req.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "folder": meta,
        "folders": folders.list_summaries(session, builtin_counts=demopages.counts()),
    }


@app.delete("/api/folders/{folder_id}")
def api_folders_delete(folder_id: str, request: Request,
                       session: dict = Depends(demo_session)):
    """删除自建文件夹（Demo 样例夹拒绝）。"""
    _rate_guard(request, "folder")
    try:
        folders.delete(session, folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "status": "deleted",
        "folders": folders.list_summaries(session, builtin_counts=demopages.counts()),
        "active_folder_id": session.get("active_folder_id"),
    }


@app.post("/api/folders/active")
def api_folders_active(req: FolderActiveReq,
                       session: dict = Depends(demo_session)):
    """切换当前活动文件夹（上传默认落到此夹）。

    刻意**不限流**：切夹只改一个字符串指针，不分配任何东西，没有可增长面；
    而前端每点一次夹子就调一次本接口。默认限流是 30 次 / 600 秒，
    评委在演示里来回点十几个夹就能撞上，届时界面开始回 429——
    为一个零风险的操作换来"演示中途像坏了"，这笔交易不成立。
    建 / 改名 / 删这三个会分配或重算的操作才需要限流。
    """
    try:
        fid = folders.set_active(session, req.folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"active_folder_id": fid}


@app.get("/api/folders/{folder_id}")
def api_folders_detail(folder_id: str, session: dict = Depends(demo_session)):
    """文件夹详情：夹内条目清单（内置夹含内置样例 + 用户上传）。"""
    try:
        folder = folders.get(session, folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    items = []
    # 内置夹：先挂内置样例（真实作业原件，未批改——批改由体验者触发）
    if folders.is_builtin(folder):
        for it in demopages.by_folder(folder_id):
            row = _demo_page_row(it)
            row.update({"kind": "builtin", "graded": False, "status": None,
                        "score": None, "max_score": None,
                        "student_name": row["stage_name"]})
            items.append(row)

    # 用户上传（任意夹，含丢进内置夹的）
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

    # 题库夹装的是教师答案页，前端据此把按钮从「加入待批清单」换成「建题库」
    builtin_meta = next((m for m in demopages.FOLDERS
                         if m["folder_id"] == folder_id), None)
    return {
        "folder": {
            "folder_id": folder["folder_id"],
            "name": folder["name"],
            "kind": folder.get("kind", "user"),
            "role": (builtin_meta or {}).get("role", "student"),
            "count": len(items),
        },
        "items": items,
    }


@app.post("/api/folders/{folder_id}/grade")
def api_folders_grade(folder_id: str, request: Request,
                      session: dict = Depends(demo_session)):
    """整夹一键批改，完成后自动推送飞书审核提醒卡片。

    - 内置样例夹：不在这里批。内置样例是真实作业原件，每份都要走一次真实的
      识别 + 整页批改；一次点击烧掉五六次模型调用，代价与「点错一个按钮」
      不对等。改由前端把整夹加进待批清单，体验者确认后再批。
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

    for uid in list(folder.get("item_ids") or []):
        if uid in session["uploads"]:
            graded_ids.append(uid)
        elif not folders.is_builtin(folder):
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
