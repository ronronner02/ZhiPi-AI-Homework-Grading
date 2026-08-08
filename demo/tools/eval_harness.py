# -*- coding: utf-8 -*-
"""智批π · 实测评测脚本（eval harness）。

对「真实手写作答 + 教师人工判分」评测集，一键计算五组核心指标：

    1. OCR 字符错误率 CER      —— 机器转写 vs 人工校对转写（需图片 + VLM Key）
    2. 批改一致性              —— AI 分 vs 教师分：MAE / 总分一致率 / 皮尔逊相关
    3. 错因标签命中             —— AI 标签集合 vs 人工标签集合：Jaccard / 命中率
    4. 红黄绿分桶校准           —— 各置信度桶的「教师认可率」（分流是否校准的核心证据）
    5. Cohen's kappa           —— 分数离散为 优/良/需改进 三档后的 AI-人工一致性

指标口径与 `docs/07-实测评测方案与报告.md` §3 完全一致（该文档为预注册方案，
口径先于数据确定）。本脚本【不产生任何数据】，只对人工采集与标注的评测集计算指标。

评测集目录约定（<eval_dir>）::

    eval_set/
      manifest.json     # 评测条目清单，JSON 数组，每条：
                        #   {
                        #     "item_id":         "E001",              # 条目唯一 ID（必填）
                        #     "question_id":     "Q001",              # 题目 ID，须存在于题库（必填）
                        #     "image":           "images/E001.jpg",   # 作业照片相对路径（可选）
                        #     "ocr_text_human":  "x^2-5x+6=0\n...",   # 人工校对转写（必填，OCR 评测的参照）
                        #     "human_score":     8,                   # 教师终判分（必填，双评仲裁后的最终分）
                        #     "human_error_tags": ["计算错误"],        # 教师标注错因（必填，可为空数组）
                        #     "human_grader":    "T1",                # 判分教师标识（必填，追溯用）
                        #     "clarity":         85,                  # 卷面清晰度（可选，无图时批改用，默认 85）
                        #     "needs_review":    true                 # 可选：人工判定这份是否**必须**教师过目
                        #                                           #   （判分有争议 / 字迹无法辨认 / AI 结论有误）
                        #                                           #   标了才参与「表 F 分流安全性」漏拦率统计
                        #   }
      images/           # 真实作业照片（可选；配置 ZHIPI_VLM_API_KEY 时先跑机器识别）
      questions.json    # 题库补充（可选；格式同 demo/data/questions.json，
                        #   评测新题不必改动内置题库，放这里即可，按 question_id 覆盖合并）

    运行后在 <eval_dir> 下生成：
      report_data.json    全部指标（结构化，供报告与看板引用）
      report_tables.md    Markdown 指标表（可直接粘贴进 docs/07 §5 各空表）

运行方式::

    cd demo
    python tools/eval_harness.py <评测集目录>       # 正式评测（真实数据）
    python tools/eval_harness.py --selftest        # 自测模式：内置模拟数据跑通全流程，验证脚本本身

依赖说明：
    - 只用标准库 + 项目现有依赖（requests / 复用 pipeline 各模块），无任何新增第三方包；
    - OCR 评测需配置 ZHIPI_VLM_API_KEY（未配置时该项自动跳过并说明原因）；
    - 批改一致性评测需配置 ZHIPI_LLM_API_KEY 或 ZHIPI_VLM_API_KEY
      （未配置时提示需要 Key 并跳过，不会用 mock 结果冒充实测）。

诚实红线：--selftest 的全部数字均来自内置模拟数据，仅用于验证脚本无 bug，
运行时会显著标注「自测模式」，禁止将其作为实测结果对外引用。
"""
import argparse
import json
import math
import os
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # demo/
sys.path.insert(0, str(BASE_DIR))

from pipeline import grader                          # noqa: E402
from pipeline import ocr as ocr_mod                  # noqa: E402

# ---------- 指标口径常量（与 docs/07 §3 保持一致，先于数据确定） ----------

AGREE_RATIO = 0.10       # 总分一致 / 教师认可判定：|AI 分 - 教师分| ≤ 满分 × 10%
KAPPA_BINS = (           # kappa 三档离散口径：得分率 ratio = 分数 / 满分
    ("优", 0.90),        # ratio ≥ 0.90
    ("良", 0.70),        # 0.70 ≤ ratio < 0.90
    ("需改进", None),    # ratio < 0.70
)
BUCKET_ORDER = ["green", "yellow", "red"]
BUCKET_LABEL = {"green": "绿·自动通过", "yellow": "黄·教师确认", "red": "红·人工批改"}
EPS = 1e-9

SELFTEST_BANNER = (
    "⚠ 自测模式：使用内置模拟数据（demo/data/submissions.json 预置标注 + mock 批改），"
    "非真实实测，仅用于验证脚本流程，禁止作为实测结果引用！"
)


# ---------- 基础工具 ----------

def levenshtein(a: str, b: str) -> int:
    """经典编辑距离（插入 / 删除 / 替换各记 1），滚动数组实现，无第三方依赖。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,                      # 删除
                cur[j - 1] + 1,                   # 插入
                prev[j - 1] + (ca != cb),         # 替换
            ))
        prev = cur
    return prev[-1]


def _norm_ws(text: str) -> str:
    """宽松归一化：去掉全部空白字符（空格 / 换行 / 制表），只比内容字符。"""
    return "".join(str(text or "").split())


# 数学记号等价表：上下标的 Unicode 形式 ←→ ASCII 形式。
#
# 为什么必须有这一层：实测发现 VLM 输出 `x^2-5x+6=0`、`x1=2, x2=3`，
# 而人工校对转写写的是 `x²-5x+6=0`、`x₁=2, x₂=3`。两者**数学含义完全相同**，
# 但逐字符比会算出 20% 的 CER。若不归一化，报告里「转写字准率」这个
# 核心指标衡量的其实是「标注者和模型的记号习惯是否撞上」，
# 而不是「模型认没认对手写」——那会把结论引向完全错误的方向。
_SUP = {"⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4",
        "⁵": "^5", "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9"}
_SUB = {"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
        "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9"}
# 全角 / 印刷体符号 → ASCII。只收敛「同义不同形」，不碰任何会改变含义的字符。
_PUNCT = {"：": ":", "，": ",", "。": ".", "；": ";", "（": "(", "）": ")",
          "＝": "=", "－": "-", "×": "*", "÷": "/", "·": "*",
          "＋": "+", "＜": "<", "＞": ">", "≤": "<=", "≥": ">=",
          "'": "'", "'": "'", """: '"', """: '"'}
# 学生不会写、但模型爱加的引导词。去掉它们，避免「模型更啰嗦」被记成转写错误。
_LEAD = ("解:", "解答:", "答:", "因为", "所以")


def _norm_math(text: str) -> str:
    """数学记号归一化：上下标、全角符号、引导词一律收敛到同一种写法。

    刻意**不做**的事：不折叠大小写（英语作文里 i / I 是真实书写错误），
    不删除数字与运算符（那会掩盖真正的识别错误）。
    """
    s = str(text or "")
    for src, dst in _SUP.items():
        s = s.replace(src, dst)
    for src, dst in _SUB.items():
        s = s.replace(src, dst)
    for src, dst in _PUNCT.items():
        s = s.replace(src, dst)
    s = "".join(s.split())          # 先去空白，引导词才好整段匹配
    for lead in _LEAD:
        if s.startswith(lead):
            s = s[len(lead):]
            break
    return s


def compute_cer(machine_text: str, human_text: str) -> dict:
    """字符错误率 CER = 编辑距离(机器转写, 人工转写) / 人工转写长度。

    并列给出三种口径，全部写进报告，让读者自己判断该看哪一个：
        cer       主口径（预注册方案 §3 定义）：去除全部空白后计算
        cer_raw   严格口径：仅统一换行符，不做其它归一化
        cer_math  记号归一口径：在主口径之上再统一上下标 / 全角符号 / 引导词，
                  度量「数学含义是否被认对」，排除记号习惯差异的干扰

    三个并列而非替换：cer / cer_raw 是预注册口径，不能事后改动；
    cer_math 是新增的补充证据。三者差距本身就是有信息量的——
    差得越大，说明标注规范与模型输出习惯越不一致。
    """
    raw_h = str(human_text or "").replace("\r\n", "\n").strip()
    raw_m = str(machine_text or "").replace("\r\n", "\n").strip()
    norm_h, norm_m = _norm_ws(human_text), _norm_ws(machine_text)
    math_h, math_m = _norm_math(human_text), _norm_math(machine_text)
    dist = levenshtein(norm_m, norm_h)
    dist_raw = levenshtein(raw_m, raw_h)
    dist_math = levenshtein(math_m, math_h)
    return {
        "edit_distance": dist,
        "ref_len": len(norm_h),
        "cer": round(dist / len(norm_h), 4) if norm_h else None,
        "cer_raw": round(dist_raw / len(raw_h), 4) if raw_h else None,
        "cer_math": round(dist_math / len(math_h), 4) if math_h else None,
        "edit_distance_math": dist_math,
    }


def pearson(xs: list, ys: list):
    """皮尔逊相关系数；样本量 < 2 或任一侧方差为 0 时返回 None（报告中标注不可计算）。"""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < EPS or syy < EPS:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / math.sqrt(sxx * syy), 4)


def score_band(score: float, max_score: float) -> str:
    """把分数按得分率离散为 优 / 良 / 需改进 三档（口径见 KAPPA_BINS）。"""
    ratio = (float(score) / float(max_score)) if max_score else 0.0
    for label, floor in KAPPA_BINS:
        if floor is None or ratio >= floor - EPS:
            return label
    return KAPPA_BINS[-1][0]


def cohens_kappa(pairs: list) -> dict:
    """Cohen's kappa（自实现）：pairs 为 [(AI 档位, 人工档位), ...]。

    kappa = (Po - Pe) / (1 - Pe)；Po 为观测一致率，Pe 为边缘分布下的期望一致率。
    Pe → 1（双方几乎只出现同一档）时 kappa 无定义，按惯例：全一致记 1.0，否则 0.0。
    """
    labels = [b[0] for b in KAPPA_BINS]
    n = len(pairs)
    confusion = {a: {h: 0 for h in labels} for a in labels}
    for ai_band, human_band in pairs:
        confusion[ai_band][human_band] += 1
    if n == 0:
        return {"kappa": None, "po": None, "pe": None, "n": 0, "labels": labels,
                "confusion": confusion}
    po = sum(confusion[l][l] for l in labels) / n
    pe = sum(
        (sum(confusion[l][h] for h in labels) / n) *   # AI 判为 l 的边缘概率
        (sum(confusion[a][l] for a in labels) / n)     # 人工判为 l 的边缘概率
        for l in labels
    )
    if 1 - pe < EPS:
        kappa = 1.0 if po >= 1 - EPS else 0.0
    else:
        kappa = (po - pe) / (1 - pe)
    return {"kappa": round(kappa, 4), "po": round(po, 4), "pe": round(pe, 4),
            "n": n, "labels": labels, "confusion": confusion}


def tag_overlap(ai_tags: list, human_tags: list) -> dict:
    """错因标签重合度：Jaccard = |交|/|并|；命中率 = |交|/|人工|。

    口径：双方均为空 → Jaccard 记 1.0（都认为无错因，视为一致）；
         人工为空、AI 非空 → Jaccard 按公式为 0；命中率仅在人工标签非空时定义。
    """
    a, h = set(ai_tags or []), set(human_tags or [])
    inter, union = a & h, a | h
    jaccard = 1.0 if not union else len(inter) / len(union)
    hit = (len(inter) / len(h)) if h else None
    return {"jaccard": round(jaccard, 4),
            "hit_rate": round(hit, 4) if hit is not None else None}


# ---------- 控制台 / Markdown 表格渲染 ----------

def _disp_width(s: str) -> int:
    """按东亚宽字符占 2 列计算显示宽度，保证中文表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in str(s))


def _pad(s, width: int) -> str:
    s = str(s)
    return s + " " * max(0, width - _disp_width(s))


def render_table(headers: list, rows: list) -> str:
    """渲染等宽对齐的控制台表格。"""
    rows = [[("—" if c is None else c) for c in row] for row in rows]
    widths = [max(_disp_width(h), *([_disp_width(r[i]) for r in rows] or [0]))
              for i, h in enumerate(headers)]
    line = "  ".join(_pad(h, w) for h, w in zip(headers, widths))
    rule = "  ".join("-" * w for w in widths)
    body = "\n".join("  ".join(_pad(c, w) for c, w in zip(row, widths)) for row in rows)
    return "\n".join([line, rule, body]) if rows else "\n".join([line, rule, "（无数据）"])


def md_table(headers: list, rows: list) -> str:
    """渲染 Markdown 表格（空值以 — 显示；单元格内竖线转义防破表）。"""
    def cell(c):
        return ("—" if c is None else str(c)).replace("|", "\\|")
    lines = ["| " + " | ".join(cell(h) for h in headers) + " |",
             "| " + " | ".join("----" for _ in headers) + " |"]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _pct(x, digits: int = 1):
    """比率 → 百分数字符串；None 原样返回（渲染为 —）。"""
    return None if x is None else ("%.*f%%" % (digits, x * 100))


# ---------- 数据加载 ----------

def load_questions(eval_dir: Path = None) -> dict:
    """加载题库：内置 demo/data/questions.json，评测集自带 questions.json 按 ID 覆盖合并。"""
    with open(BASE_DIR / "data" / "questions.json", encoding="utf-8") as f:
        questions = {q["question_id"]: q for q in json.load(f)["questions"]}
    extra = (eval_dir / "questions.json") if eval_dir else None
    if extra and extra.exists():
        with open(extra, encoding="utf-8") as f:
            for q in json.load(f).get("questions", []):
                questions[q["question_id"]] = q
    return questions


def load_manifest(eval_dir: Path, questions: dict = None) -> list:
    """加载并校验 manifest.json：缺字段、类型错、分数越界一律在此拦下。

    为什么校验必须放在这里、而且要严：下游 make_record 会把 human_score 直接
    转成 float 参与 MAE。实测过一次——一条 `human_score: 9999` 的错标就把
    MAE 拉到 9991，而报告照样生成、看不出异常；`human_score: null` 更是直接
    让整轮评测崩在中途。标注是人手工填的，错填是常态而非例外，所以这里
    宁可啰嗦地逐条报错，也不能让脏数据无声地污染答辩材料。

    校验项：
        - 必填字段存在；
        - human_score 是数字（bool 不算）、且在 [0, 该题满分] 内；
        - human_error_tags 是数组；
        - needs_review（若给）必须是真正的布尔——字符串 "false" 会被
          bool() 判成 True，这类错标比缺字段更危险，因为它悄无声息；
        - clarity（若给）在 [0, 100] 内；
        - question_id 存在于题库（传入 questions 时才校验）。
    """
    path = eval_dir / "manifest.json"
    if not path.exists():
        raise SystemExit("评测集缺少 manifest.json：%s" % path)
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list) or not items:
        raise SystemExit("manifest.json 应为非空 JSON 数组")

    required = ["item_id", "question_id", "ocr_text_human", "human_score",
                "human_error_tags", "human_grader"]
    problems = []      # 致命：拒绝跑评测
    warnings = []      # 可疑：照跑，但必须让人看见

    for i, item in enumerate(items):
        where = "第 %d 条（%s）" % (i + 1, item.get("item_id", "?"))
        if not isinstance(item, dict):
            problems.append("%s 不是 JSON 对象" % where)
            continue

        missing = [k for k in required if k not in item]
        if missing:
            problems.append("%s 缺少必填字段：%s" % (where, "、".join(missing)))
            continue

        # human_score：bool 是 int 的子类，必须显式排除，否则 true 会被当成 1 分
        score = item["human_score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            problems.append("%s human_score 必须是数字，实际是 %s（%r）"
                            % (where, type(score).__name__, score))
        else:
            max_score = None
            if questions:
                q = questions.get(item["question_id"])
                if q is not None:
                    max_score = q.get("max_score")
            if score < 0:
                problems.append("%s human_score = %s，不能为负" % (where, score))
            elif max_score is not None and score > max_score:
                problems.append("%s human_score = %s 超过该题满分 %s"
                                % (where, score, max_score))

        if not isinstance(item["human_error_tags"], list):
            problems.append("%s human_error_tags 必须是数组，实际是 %s"
                            % (where, type(item["human_error_tags"]).__name__))
        else:
            # 枚举外的标签不算致命错误（错因枚举本身可能演进），但要提醒：
            # 它永远不会与 AI 标签相交，会把 Jaccard 拉低成 0 却看不出原因。
            unknown = [t for t in item["human_error_tags"]
                       if t not in grader.ERROR_TAGS]
            if unknown:
                warnings.append(
                    "%s 错因标签不在枚举内：%s。这些标签不会与 AI 输出相交，"
                    "会把 Jaccard 拉低；请确认是笔误还是需要扩充枚举。"
                    % (where, "、".join(map(str, unknown))))

        # null 视为「这条没标」，与整个字段缺失同义，合法放行（下游会跳过它）。
        # 但除此之外必须是真布尔：字符串 "false" 被 bool() 判成 True，
        # 会让「不必复核」算成「漏拦」，安全性指标反着走且毫无征兆。
        if (item.get("needs_review") is not None
                and not isinstance(item["needs_review"], bool)):
            problems.append(
                "%s needs_review 必须是布尔 true/false（或 null 表示未标注），"
                "实际是 %s（%r）——字符串 \"false\" 会被判成「需要复核」，指标会反着算"
                % (where, type(item["needs_review"]).__name__, item["needs_review"]))

        if "clarity" in item:
            c = item["clarity"]
            if isinstance(c, bool) or not isinstance(c, (int, float)):
                problems.append("%s clarity 必须是数字，实际是 %s"
                                % (where, type(c).__name__))
            elif not (0 <= c <= 100):
                problems.append("%s clarity = %s，应在 0-100 之间" % (where, c))

        if questions and item["question_id"] not in questions:
            problems.append("%s question_id = %r 不存在于题库"
                            % (where, item["question_id"]))

    if problems:
        raise SystemExit(
            "manifest 校验未通过，共 %d 处问题（请先修标注，再跑评测）：\n  - %s"
            % (len(problems), "\n  - ".join(problems)))

    # 重复标签会让 Jaccard 的分母失真，去重后再交给下游
    for item in items:
        tags = item.get("human_error_tags")
        if isinstance(tags, list) and len(set(tags)) != len(tags):
            item["human_error_tags"] = list(dict.fromkeys(tags))
            warnings.append("条目 %s 的错因标签有重复，已自动去重"
                            % item.get("item_id", "?"))

    if warnings:
        print("manifest 校验通过，但有 %d 处需要留意：" % len(warnings))
        for w in warnings:
            print("  ! " + w)
    return items


# ---------- a. OCR 评测 ----------

_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".bmp": "image/bmp"}


def eval_ocr(eval_dir: Path, items: list) -> dict:
    """OCR 字符错误率评测：机器转写（recognize_vlm）vs 人工校对转写。

    仅对「有 image 字段且图片存在」的条目执行；未配置 ZHIPI_VLM_API_KEY 时
    整体跳过并在结果中说明原因。返回逐条明细 + 平均 CER。
    机器转写文本会随明细返回，供后续批改环节复用（保持与线上链路同源）。
    """
    with_image = [it for it in items if it.get("image")]
    if not with_image:
        return {"evaluated": False, "reason": "评测集无 image 字段条目，跳过 OCR 评测",
                "items": [], "mean_cer": None, "mean_cer_raw": None, "n": 0}
    if not ocr_mod.vlm_configured():
        return {"evaluated": False,
                "reason": "未配置 ZHIPI_VLM_API_KEY，跳过 OCR 评测（%d 条含图片条目待评）"
                          % len(with_image),
                "items": [], "mean_cer": None, "mean_cer_raw": None, "n": 0}

    details = []
    for item in with_image:
        img_path = eval_dir / item["image"]
        row = {"item_id": item["item_id"], "image": item["image"]}
        if not img_path.exists():
            row["error"] = "图片不存在：%s" % img_path
            details.append(row)
            continue
        mime = _MIME_BY_EXT.get(img_path.suffix.lower(), "image/png")
        result, attempts, exc = _call_with_retry(
            lambda: ocr_mod.recognize_vlm(img_path.read_bytes(), mime),
            "识别 %s" % item["item_id"])
        if result is None:
            row["error"] = "识别调用失败（尝试 %d 次）：%s" % (attempts, exc)
        else:
            row["machine_text"] = result.get("text", "")
            row["clarity"] = result.get("clarity")
            row.update(compute_cer(row["machine_text"], item["ocr_text_human"]))
            if attempts > 1:
                row["retry_attempts"] = attempts
        details.append(row)

    def _mean(key):
        vals = [d[key] for d in details if d.get(key) is not None]
        return (round(sum(vals) / len(vals), 4) if vals else None), len(vals)

    mean_cer, n_cer = _mean("cer")
    mean_raw, _ = _mean("cer_raw")
    mean_math, _ = _mean("cer_math")
    return {"evaluated": True, "reason": None, "items": details,
            "mean_cer": mean_cer, "mean_cer_raw": mean_raw,
            "mean_cer_math": mean_math, "n": n_cer}


# ---------- b. 批改一致性评测（真实模式：grade_adhoc） ----------

# 可重试的瞬时故障特征。实测中转网关会出现 RemoteDisconnected、读超时、
# 502/503/504 这类与「作答内容」完全无关的失败——它们不是模型判断不了，
# 而是链路抖了一下。评测跑几十张图时几乎必然撞上若干次，
# 若不重试就会在报告里留下一堆「批改失败」，把样本量白白打掉。
#
# 反过来，4xx（除 429）不重试：那是请求本身有问题（模型名错、鉴权失败、
# 内容被拒），重试只是重复烧钱和时间。
_RETRIABLE = (
    "remotedisconnected", "connection aborted", "connection reset",
    "timed out", "timeout", "read timed out",
    "502", "503", "504", "429", "bad gateway", "service unavailable",
    "temporarily", "connectionerror", "chunkedencodingerror",
)

_RETRY_MAX = 3          # 首次 + 最多 2 次重试
_RETRY_BASE_SLEEP = 4   # 退避基数（秒）：4、8


def _is_retriable(exc: Exception) -> bool:
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    return any(sig in text for sig in _RETRIABLE)


def _call_with_retry(fn, label: str):
    """执行 fn，瞬时故障按指数退避重试。返回 (结果, 尝试次数, 最后异常)。

    刻意把重试次数一并返回：报告里要能看出「这批数据是一次过的，
    还是靠反复重试才凑齐的」——后者说明网关不稳，结论的可信度要打折。
    """
    last = None
    attempt = 0
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            return fn(), attempt, None
        except Exception as exc:
            last = exc
            if attempt >= _RETRY_MAX or not _is_retriable(exc):
                break
            wait = _RETRY_BASE_SLEEP * (2 ** (attempt - 1))
            print("  [%s] 第 %d 次失败（%s），%d 秒后重试…"
                  % (label, attempt, type(exc).__name__, wait))
            time.sleep(wait)
    # 返回**真实**尝试次数而非上限：鉴权错误只调了 1 次就该报 1 次，
    # 报成 3 次会让人误判成网关不稳，去查错方向。
    return None, attempt, last


def eval_grading(items: list, questions: dict, ocr_details: list) -> dict:
    """对每条评测条目跑真实 LLM 批改（grade_adhoc），产出逐条 AI 结果。

    学生作答文本优先级：机器转写（OCR 评测已跑出）→ 人工校对转写；
    卷面清晰度优先级：机器自评 clarity → manifest 的 clarity → 默认 85。
    未配置任何 LLM Key 时整体跳过（不用 mock 冒充实测）。
    批改结果字段一律 .get() 容错，兼容 grader 升级新增字段
    （factor_overrides / consistency_check / cross_check 等）。
    """
    if grader.llm_credentials() is None:
        return {"evaluated": False,
                "reason": ("未配置 ZHIPI_LLM_API_KEY / ZHIPI_VLM_API_KEY，"
                           "跳过批改一致性评测（真实评测必须走真实模型，不用 mock 冒充）"),
                "records": []}

    machine_by_id = {d["item_id"]: d for d in ocr_details}
    records, errors = [], []
    for item in items:
        qid = item["question_id"]
        question = questions.get(qid)
        if question is None:
            errors.append({"item_id": item["item_id"], "error": "题库中不存在题目 %s" % qid})
            continue
        ocr_row = machine_by_id.get(item["item_id"], {})
        student_text = ocr_row.get("machine_text") or item["ocr_text_human"]
        clarity = ocr_row.get("clarity")
        if clarity is None:
            clarity = float(item.get("clarity", 85))
        ai, attempts, exc = _call_with_retry(
            lambda: grader.grade_adhoc(question, student_text, clarity),
            "批改 %s" % item["item_id"])
        if ai is None:
            errors.append({"item_id": item["item_id"],
                           "error": "批改调用失败（尝试 %d 次）：%s" % (attempts, exc),
                           "attempts": attempts,
                           "retriable": _is_retriable(exc) if exc else None})
            continue
        rec = make_record(item, question, ai)
        if attempts > 1:
            rec["retry_attempts"] = attempts
        records.append(rec)
    retried = [r["item_id"] for r in records if r.get("retry_attempts")]
    return {"evaluated": True, "reason": None, "records": records,
            "errors": errors, "retried_items": retried}


def make_record(item: dict, question: dict, ai: dict) -> dict:
    """把「一条评测条目 + 一份 AI 批改结果」压成指标计算用的标准记录。

    对 AI 结果全部 .get() 容错：grader 正在升级（可能新增 consistency_check /
    cross_check / factor_overrides 等字段），缺失或新增均不影响本脚本。
    """
    max_score = float(ai.get("max_score") or question.get("max_score") or 0)
    ai_score = float(ai.get("total_score") or 0)
    # load_manifest 已经拦过脏标注，这里再兜一层：本函数也被自测与单测直接调用，
    # 不能假设调用方一定先过了校验。null 分数曾在这里抛 TypeError 中断整轮评测。
    try:
        human_score = float(item["human_score"])
    except (TypeError, ValueError):
        raise ValueError(
            "条目 %s 的 human_score 无法转成数字：%r。请先修正标注。"
            % (item.get("item_id", "?"), item.get("human_score")))
    delta = ai_score - human_score
    record = {
        "item_id": item["item_id"],
        "question_id": item["question_id"],
        "subject": question.get("subject", ""),
        "max_score": max_score,
        "ai_score": ai_score,
        "human_score": human_score,
        "human_grader": item.get("human_grader", ""),
        "delta": round(delta, 2),
        "agree": abs(delta) <= max_score * AGREE_RATIO + EPS,
        "confidence": ai.get("confidence"),
        "status": ai.get("status") or "red",       # 无分流字段按最保守的红桶计
        "mode": ai.get("mode"),
        "ai_tags": list(ai.get("error_tags") or []),
        "human_tags": list(item.get("human_error_tags") or []),
        "ai_band": score_band(ai_score, max_score),
        "human_band": score_band(human_score, max_score),
    }
    # needs_review 为可选标注字段：人工判定「这份**必须**教师过目」
    # （判分有争议 / 字迹无法辨认 / AI 结论有误）。标了才参与漏拦率统计。
    if item.get("needs_review") is not None:
        # 只认真正的布尔。不用 bool() 兜底是刻意的：bool("false") 为 True，
        # 一条 `"needs_review": "false"` 的错标会让「不必复核」被算成「漏拦」，
        # 指标反着走还看不出来。非布尔值宁可不纳入统计，也不猜它想表达什么。
        if isinstance(item["needs_review"], bool):
            record["needs_review"] = item["needs_review"]
            # 黄与红都会送到教师面前，只有绿是真正自动通过
            record["ai_flagged"] = record["status"] in ("yellow", "red")
    record.update(tag_overlap(record["ai_tags"], record["human_tags"]))
    # 升级中的 grader 可能附带的自检 / 交叉验证字段：有则透传，供报告引用
    for key in ("consistency_check", "cross_check", "factor_overrides"):
        if ai.get(key) is not None:
            record[key] = ai[key]
    return record


# ---------- c/d. 指标汇总：一致性、错因、分桶校准、kappa ----------

def compute_metrics(records: list) -> dict:
    """由标准记录列表计算全部汇总指标（口径见模块头与 docs/07 §3）。"""
    n = len(records)
    if n == 0:
        return {"n": 0}

    abs_deltas = [abs(r["delta"]) for r in records]
    agree_n = sum(1 for r in records if r["agree"])
    consistency = {
        "n": n,
        "mae": round(sum(abs_deltas) / n, 3),
        "agree_rate": round(agree_n / n, 4),
        "agree_n": agree_n,
        "pearson_r": pearson([r["ai_score"] for r in records],
                             [r["human_score"] for r in records]),
    }

    jaccards = [r["jaccard"] for r in records if r["jaccard"] is not None]
    hits = [r["hit_rate"] for r in records if r["hit_rate"] is not None]
    tags = {
        "mean_jaccard": round(sum(jaccards) / len(jaccards), 4) if jaccards else None,
        "mean_hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
        "n_jaccard": len(jaccards),
        "n_hit": len(hits),   # 命中率仅对人工标签非空的条目定义
    }

    buckets = []
    for status in BUCKET_ORDER:
        group = [r for r in records if r["status"] == status]
        approve_n = sum(1 for r in group if r["agree"])
        confs = [r["confidence"] for r in group if r["confidence"] is not None]
        buckets.append({
            "status": status,
            "label": BUCKET_LABEL[status],
            "n": len(group),
            "approve_n": approve_n,
            "approve_rate": round(approve_n / len(group), 4) if group else None,
            "mean_abs_delta": (round(sum(abs(r["delta"]) for r in group) / len(group), 3)
                               if group else None),
            "mean_confidence": round(sum(confs) / len(confs), 1) if confs else None,
        })

    kappa = cohens_kappa([(r["ai_band"], r["human_band"]) for r in records])
    return {"n": n, "consistency": consistency, "tags": tags,
            "calibration": buckets, "kappa": kappa,
            "safety": compute_safety(records)}


def compute_safety(records: list) -> dict:
    """分流安全性：漏拦率与召回。仅统计标了 needs_review 的条目。

    为什么必须单列这一项：分桶校准（指标 4）回答的是「AI 说绿的时候准不准」，
    而这里回答「教师认为必须看的，AI 放过了多少」。两者不能互相替代——
    一个系统可以每桶都很"准"，却恰好把少数高风险作答判进了绿桶。

    四象限：
        hit         该看的拦住了（人工要看 → AI 判黄/红）
        miss        **漏拦**：该看的被自动通过 —— 后果最严重
        false_alarm 误拦：不必看的被转人工 —— 只是浪费教师时间
        pass_ok     不必看的自动通过 —— 理想情况

    漏拦率的分母刻意用「人工认为该看的份数」，不用全体样本：
    用全体做分母会把这个数字稀释得很好看，是自欺欺人。
    """
    scoped = [r for r in records if r.get("needs_review") is not None]
    if not scoped:
        return {"evaluated": False,
                "reason": ("评测集未标注 needs_review 字段，跳过分流安全性统计。"
                           "该字段含义：人工判定这份是否**必须**教师过目。"),
                "n": 0}

    cnt = {"hit": 0, "miss": 0, "false_alarm": 0, "pass_ok": 0}
    missed_ids = []
    for r in scoped:
        if r["needs_review"]:
            if r["ai_flagged"]:
                cnt["hit"] += 1
            else:
                cnt["miss"] += 1
                missed_ids.append(r["item_id"])
        else:
            cnt["false_alarm" if r["ai_flagged"] else "pass_ok"] += 1

    need = cnt["hit"] + cnt["miss"]
    safe = cnt["false_alarm"] + cnt["pass_ok"]
    return {
        "evaluated": True, "reason": None, "n": len(scoped),
        "counts": cnt,
        "need_review": need,
        "miss_rate": round(cnt["miss"] / need, 4) if need else None,
        "recall": round(cnt["hit"] / need, 4) if need else None,
        "false_alarm_rate": round(cnt["false_alarm"] / safe, 4) if safe else None,
        "missed_items": missed_ids,
    }


# ---------- e. 输出：控制台 + report_data.json + report_tables.md ----------

def print_report(ocr_section: dict, grading_section: dict, metrics: dict,
                 selftest: bool) -> None:
    """控制台输出全部指标表。"""
    bar = "=" * 64
    print(bar)
    print("智批π 实测评测报告（eval_harness）  生成时间：%s"
          % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if selftest:
        print(SELFTEST_BANNER)
    print(bar)

    print("\n[1] OCR 字符错误率（CER）")
    if not ocr_section["evaluated"]:
        print("  跳过：%s" % ocr_section["reason"])
    else:
        rows = []
        for d in ocr_section["items"]:
            if d.get("error"):
                rows.append([d["item_id"], "失败", "—", "—", "—", d["error"]])
            else:
                rows.append([d["item_id"], _pct(d["cer"], 2), _pct(d["cer_raw"], 2),
                             _pct(d.get("cer_math"), 2), d["ref_len"], ""])
        print(render_table(["条目", "CER(去空白)", "CER(严格)", "CER(记号归一)",
                            "参照长度", "备注"], rows))
        print("  平均 CER（去空白口径）= %s（n=%d）；严格口径 = %s；记号归一口径 = %s"
              % (_pct(ocr_section["mean_cer"], 2), ocr_section["n"],
                 _pct(ocr_section["mean_cer_raw"], 2),
                 _pct(ocr_section.get("mean_cer_math"), 2)))
        print("  记号归一口径统一了上下标（x² ↔ x^2）、全角符号与「解:」类引导词，")
        print("  用于区分「真的认错字」与「记号习惯不同」。三者差距越大，")
        print("  说明人工标注规范与模型输出习惯越不一致，应优先统一标注规范。")

    print("\n[2] 批改一致性（AI 分 vs 教师分）")
    if not grading_section["evaluated"]:
        print("  跳过：%s" % grading_section["reason"])
        return
    for err in grading_section.get("errors", []):
        print("  [条目 %s 失败] %s" % (err["item_id"], err["error"]))
    # 靠重试才成功的条目要显式点出来：网关不稳时，结论可信度应打折
    retried = grading_section.get("retried_items") or []
    if retried:
        print("  注意：%d 条经重试后才成功（%s）。网关抖动频繁，"
              "建议重跑一遍确认指标稳定。" % (len(retried), "、".join(retried)))
    if metrics.get("n", 0) == 0:
        print("  无有效批改记录，指标不可计算。")
        return
    c = metrics["consistency"]
    rows = [[r["item_id"], r["question_id"],
             "%.1f" % r["ai_score"], "%.1f" % r["human_score"],
             "%+.1f" % r["delta"], "是" if r["agree"] else "否",
             r["confidence"], BUCKET_LABEL.get(r["status"], r["status"])]
            for r in grading_section["records"]]
    print(render_table(["条目", "题目", "AI分", "教师分", "Δ", "一致", "置信度", "分流"], rows))
    print("  MAE = %.3f    总分一致率(|Δ|≤满分%d%%) = %s（%d/%d）    Pearson r = %s"
          % (c["mae"], int(AGREE_RATIO * 100), _pct(c["agree_rate"]),
             c["agree_n"], c["n"],
             "不可计算（方差为 0 或样本不足）" if c["pearson_r"] is None else c["pearson_r"]))

    t = metrics["tags"]
    print("\n[3] 错因标签命中（AI 标签集合 vs 人工标签集合）")
    print("  平均 Jaccard = %s（n=%d）    平均命中率|交|/|人工| = %s（n=%d，仅统计人工标签非空条目）"
          % (_pct(t["mean_jaccard"]), t["n_jaccard"],
             _pct(t["mean_hit_rate"]), t["n_hit"]))

    print("\n[4] 红黄绿分桶校准（各桶教师认可率，认可 = |Δ|≤满分×%d%%）" % int(AGREE_RATIO * 100))
    rows = [[b["label"], b["n"], _pct(b["approve_rate"]),
             b["mean_abs_delta"], b["mean_confidence"]]
            for b in metrics["calibration"]]
    print(render_table(["分桶", "样本数", "教师认可率", "平均|Δ|", "平均置信度"], rows))

    k = metrics["kappa"]
    print("\n[5] Cohen's kappa（三档：优≥90% / 良70-90% / 需改进<70%）")
    print("  kappa = %s    Po = %s    Pe = %s    n = %d"
          % (k["kappa"], k["po"], k["pe"], k["n"]))
    labels = k["labels"]
    rows = [["AI:" + a] + [k["confusion"][a][h] for h in labels] for a in labels]
    print(render_table(["混淆矩阵"] + ["人工:" + h for h in labels], rows))

    sf = metrics.get("safety") or {}
    print("\n[6] 分流安全性（漏拦率：教师认为必须看的，AI 放过了多少）")
    if not sf.get("evaluated"):
        print("  跳过：%s" % sf.get("reason", "无 needs_review 标注"))
    else:
        c = sf["counts"]
        print(render_table(
            ["象限", "含义", "条数"],
            [["hit", "该看的拦住了（判黄/红）", c["hit"]],
             ["miss", "漏拦：该看的被自动通过", c["miss"]],
             ["false_alarm", "误拦：不必看的被转人工", c["false_alarm"]],
             ["pass_ok", "不必看的自动通过", c["pass_ok"]]]))
        print("  漏拦率 = %s（分母 = 教师认为该看的 %d 份，不是全体 %d 份）"
              % (_pct(sf["miss_rate"], 1), sf["need_review"], sf["n"]))
        print("  召回   = %s    误拦率 = %s"
              % (_pct(sf["recall"], 1), _pct(sf["false_alarm_rate"], 1)))
        if sf["missed_items"]:
            print("  ⚠ 漏拦条目：%s —— 这些是最该逐份复盘的样本，"
                  "误拦只浪费教师时间，漏拦会把错误批改直接发给学生。"
                  % "、".join(sf["missed_items"]))
        elif sf["need_review"]:
            print("  本轮无漏拦。")

    if selftest:
        print("\n" + SELFTEST_BANNER)


def build_report_tables_md(ocr_section: dict, grading_section: dict, metrics: dict,
                           selftest: bool) -> str:
    """生成可直接粘贴进 docs/07-实测评测方案与报告.md §5 的 Markdown 表格。"""
    lines = ["# 评测指标表（eval_harness.py 自动生成）",
             "",
             "- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "- 口径：一致 / 认可 = |AI 分 − 教师分| ≤ 满分 × %d%%；详见 docs/07 §3。"
             % int(AGREE_RATIO * 100)]
    if selftest:
        lines += ["", "> **⚠ 自测模式生成：全部数字来自内置模拟数据，非真实实测，禁止对外引用。**"]

    lines += ["", "## 表 A：OCR 字符错误率（CER）", ""]
    if not ocr_section["evaluated"]:
        lines.append("（未评测：%s）" % ocr_section["reason"])
    else:
        rows = [[d["item_id"], _pct(d.get("cer"), 2), _pct(d.get("cer_raw"), 2),
                 _pct(d.get("cer_math"), 2), d.get("ref_len"), d.get("error", "")]
                for d in ocr_section["items"]]
        lines.append(md_table(["条目", "CER（去空白）", "CER（严格）", "CER（记号归一）",
                               "参照长度", "备注"], rows))
        lines.append("")
        lines.append("平均 CER（去空白口径）= **%s**（n=%d）；严格口径 = %s；记号归一口径 = **%s**。"
                     % (_pct(ocr_section["mean_cer"], 2), ocr_section["n"],
                        _pct(ocr_section["mean_cer_raw"], 2),
                        _pct(ocr_section.get("mean_cer_math"), 2)))
        lines.append("")
        lines.append("> 前两个口径为 docs/07 §3 预注册定义，未作改动。"
                     "「记号归一」为新增补充口径：在去空白之上再统一上下标"
                     "（`x²` ↔ `x^2`）、全角符号与「解:」类引导词，"
                     "用于区分**真的认错字**与**记号习惯不同**。"
                     "实测中曾出现模型输出 `x^2`、人工标注 `x²` 而被记成 20% CER 的情况，"
                     "该口径即为此设。三者差距越大，说明标注规范与模型输出习惯越不一致。")

    lines += ["", "## 表 B：批改一致性", ""]
    if not grading_section["evaluated"] or metrics.get("n", 0) == 0:
        lines.append("（未评测：%s）" % (grading_section["reason"] or "无有效记录"))
        return "\n".join(lines) + "\n"
    c = metrics["consistency"]
    lines.append(md_table(
        ["指标", "值", "口径"],
        [["样本数 n", c["n"], "有效批改条目数"],
         ["MAE", "%.3f" % c["mae"], "平均绝对分差（分）"],
         ["总分一致率", _pct(c["agree_rate"]),
          "|Δ|≤满分×%d%% 的占比（%d/%d）" % (int(AGREE_RATIO * 100), c["agree_n"], c["n"])],
         ["Pearson r", c["pearson_r"] if c["pearson_r"] is not None else "不可计算",
          "AI 分与教师分的线性相关"]]))

    lines += ["", "### 表 B-1：逐条明细", ""]
    lines.append(md_table(
        ["条目", "题目", "AI 分", "教师分", "Δ", "一致", "置信度", "分流", "AI 错因", "人工错因"],
        [[r["item_id"], r["question_id"], "%.1f" % r["ai_score"],
          "%.1f" % r["human_score"], "%+.1f" % r["delta"], "是" if r["agree"] else "否",
          r["confidence"], BUCKET_LABEL.get(r["status"], r["status"]),
          "、".join(r["ai_tags"]) or "（无）", "、".join(r["human_tags"]) or "（无）"]
         for r in grading_section["records"]]))

    t = metrics["tags"]
    lines += ["", "## 表 C：错因标签命中", ""]
    lines.append(md_table(
        ["指标", "值", "口径"],
        [["平均 Jaccard", _pct(t["mean_jaccard"]),
          "|交|/|并|，双方均空记 1.0（n=%d）" % t["n_jaccard"]],
         ["平均命中率", _pct(t["mean_hit_rate"]),
          "|交|/|人工|，仅人工标签非空条目（n=%d）" % t["n_hit"]]]))

    lines += ["", "## 表 D：红黄绿分桶校准", ""]
    lines.append(md_table(
        ["分桶", "样本数", "教师认可率", "平均|Δ|", "平均置信度"],
        [[b["label"], b["n"], _pct(b["approve_rate"]),
          b["mean_abs_delta"], b["mean_confidence"]]
         for b in metrics["calibration"]]))

    k = metrics["kappa"]
    lines += ["", "## 表 E：Cohen's kappa（优≥90% / 良 70–90% / 需改进 <70%）", ""]
    lines.append(md_table(
        ["指标", "值"],
        [["kappa", k["kappa"]], ["观测一致率 Po", k["po"]],
         ["期望一致率 Pe", k["pe"]], ["样本数 n", k["n"]]]))
    lines += ["", "混淆矩阵（行 = AI 档位，列 = 人工档位）：", ""]
    lines.append(md_table(
        [""] + ["人工:" + h for h in k["labels"]],
        [["AI:" + a] + [k["confusion"][a][h] for h in k["labels"]] for a in k["labels"]]))

    sf = metrics.get("safety") or {}
    lines += ["", "## 表 F：分流安全性（漏拦率）", ""]
    if not sf.get("evaluated"):
        lines.append("（未评测：%s）" % sf.get("reason", "无 needs_review 标注"))
    else:
        c = sf["counts"]
        lines.append(md_table(
            ["象限", "含义", "条数"],
            [["hit", "该看的拦住了（判黄/红）", c["hit"]],
             ["miss", "**漏拦**：该看的被自动通过", c["miss"]],
             ["false_alarm", "误拦：不必看的被转人工", c["false_alarm"]],
             ["pass_ok", "不必看的自动通过", c["pass_ok"]]]))
        lines.append("")
        lines.append(md_table(
            ["指标", "值", "口径"],
            [["漏拦率", _pct(sf["miss_rate"], 1),
              "miss / (hit+miss)；分母为教师认为该看的 %d 份" % sf["need_review"]],
             ["召回", _pct(sf["recall"], 1), "hit / (hit+miss)"],
             ["误拦率", _pct(sf["false_alarm_rate"], 1),
              "false_alarm / (false_alarm+pass_ok)"]]))
        lines.append("")
        lines.append("> 分母刻意用「教师认为该看的份数」而非全体样本："
                     "用全体做分母会把漏拦率稀释得很好看。"
                     "误拦只浪费教师时间，漏拦会把错误批改直接发到学生手上，"
                     "两者严重程度不对等，故必须单列。")
        if sf["missed_items"]:
            lines.append("")
            lines.append("**漏拦条目**：%s" % "、".join(sf["missed_items"]))
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, ocr_section: dict, grading_section: dict,
                  metrics: dict, selftest: bool) -> None:
    """写出 report_data.json（结构化全量指标）与 report_tables.md（粘贴用表格）。"""
    report = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "selftest": selftest,
            "selftest_warning": SELFTEST_BANNER if selftest else None,
            "agree_ratio": AGREE_RATIO,
            "kappa_bins": {label: floor for label, floor in KAPPA_BINS},
            "llm_model": (grader.llm_credentials() or {}).get("model"),
            "vlm_model": os.environ.get("ZHIPI_VLM_MODEL", "qwen-vl-max")
                         if ocr_mod.vlm_configured() else None,
        },
        "ocr": ocr_section,
        "grading": grading_section,
        "metrics": metrics,
    }
    data_path = out_dir / "report_data.json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md_path = out_dir / "report_tables.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(build_report_tables_md(ocr_section, grading_section, metrics, selftest))
    print("\n已写出：%s" % data_path)
    print("已写出：%s（表格可直接粘贴进 docs/07 §5）" % md_path)


# ---------- 正式评测入口 ----------

def run_eval(eval_dir: Path) -> None:
    """正式评测：读取评测集目录 → OCR 评测 → 批改一致性评测 → 汇总输出。"""
    eval_dir = eval_dir.resolve()
    questions = load_questions(eval_dir)
    # 传入题库，让 human_score 能对着「该题满分」校验越界，
    # 也能提前发现 question_id 写错——两者都会在跑完几十次真实调用后
    # 才在指标里露出马脚，那时候额度已经烧掉了。
    items = load_manifest(eval_dir, questions)
    print("评测集：%s（共 %d 条）" % (eval_dir, len(items)))

    ocr_section = eval_ocr(eval_dir, items)
    grading_section = eval_grading(items, questions, ocr_section["items"])
    metrics = compute_metrics(grading_section.get("records", []))

    print_report(ocr_section, grading_section, metrics, selftest=False)
    write_outputs(eval_dir, ocr_section, grading_section, metrics, selftest=False)


# ---------- 自测模式（--selftest） ----------

def _simulated_machine_text(text: str, clarity: float) -> str:
    """自测用：按预置清晰度对人工转写做确定性字符扰动，模拟机器转写误差。

    清晰度越低替换的字符越多（每低 10 分多错 1 个字符），全程无随机数。
    """
    chars = list(text)
    k = max(0, int((95 - float(clarity)) // 10))
    if k == 0 or not chars:
        return text
    step = max(1, len(chars) // (k + 1))
    for i in range(1, k + 1):
        pos = min(i * step, len(chars) - 1)
        if not chars[pos].isspace():
            chars[pos] = "?"
    return "".join(chars)


def _simulated_human_label(idx: int, ai: dict) -> dict:
    """自测用：由 mock 批改结果确定性推演一份「模拟教师标注」。

    仅为验证指标计算路径的分支覆盖（有一致 / 不一致、有标签命中 / 漏标），
    不代表任何真实教师判分。规则（按条目序号取模，全程确定性）：
        idx % 4 == 1 → 教师分 = AI 分 - 1（模拟 AI 偏高）
        idx % 4 == 3 → 教师分 = AI 分 + 1（模拟 AI 偏低）
        其余         → 教师分 = AI 分（一致）
        idx % 3 == 2 → 教师多标 1 个 AI 未给的错因（模拟 AI 漏标）

    needs_review 的模拟规则：得分率低于 80% 即认为「教师必须过目」。
    注意：这份模拟数据上 mock 引擎恰好没有漏拦，所以 miss / false_alarm
    两个象限取不到样本。四象限的公式分支由 tools/test_eval_harness.py
    直接构造记录来覆盖，不靠这里的模拟数据碰运气。
    """
    max_score = float(ai.get("max_score") or 0)
    ai_score = float(ai.get("total_score") or 0)
    if idx % 4 == 1:
        human_score = max(0.0, ai_score - 1)
    elif idx % 4 == 3:
        human_score = min(max_score, ai_score + 1)
    else:
        human_score = ai_score
    human_tags = list(ai.get("error_tags") or [])
    if idx % 3 == 2:
        for tag in grader.ERROR_TAGS:
            if tag not in human_tags:
                human_tags.append(tag)
                break
    needs_review = bool(max_score) and (human_score / max_score) < 0.8
    return {"human_score": human_score, "human_error_tags": human_tags,
            "human_grader": "SIM", "needs_review": needs_review}


def run_selftest() -> None:
    """自测模式：用内置 submissions.json + mock 批改模拟评测集，跑通全流程。

    目的仅为验证脚本本身无 bug（指标公式、表格渲染、文件写出），
    输出写入系统临时目录，不污染仓库；所有数字均为模拟，非实测。
    """
    print(SELFTEST_BANNER)
    questions = load_questions()
    with open(BASE_DIR / "data" / "submissions.json", encoding="utf-8") as f:
        submissions = json.load(f)["submissions"]

    ocr_details, records = [], []
    for idx, sub in enumerate(submissions):
        question = questions[sub["question_id"]]
        ai = grader.grade_mock(question, sub)          # mock 批改充当「AI 结果」
        human = _simulated_human_label(idx, ai)        # 确定性模拟「教师标注」
        item = {
            "item_id": sub["submission_id"],
            "question_id": sub["question_id"],
            "ocr_text_human": sub["ocr"]["text"],
            **human,
        }
        records.append(make_record(item, question, ai))

        # 模拟「机器转写 vs 人工转写」跑 CER 计算路径（不调用任何外部接口）
        clarity = float(sub["ocr"]["clarity"])
        machine_text = _simulated_machine_text(sub["ocr"]["text"], clarity)
        row = {"item_id": sub["submission_id"], "image": "（模拟，无真实图片）",
               "machine_text": machine_text, "clarity": clarity}
        row.update(compute_cer(machine_text, sub["ocr"]["text"]))
        ocr_details.append(row)

    scored = [d for d in ocr_details if d.get("cer") is not None]

    def _sm(key):
        vals = [d[key] for d in ocr_details if d.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    ocr_section = {
        "evaluated": True, "reason": None, "items": ocr_details,
        "mean_cer": _sm("cer"),
        "mean_cer_raw": _sm("cer_raw"),
        "mean_cer_math": _sm("cer_math"),
        "n": len(scored),
    }
    grading_section = {"evaluated": True, "reason": None,
                       "records": records, "errors": []}
    metrics = compute_metrics(records)

    print_report(ocr_section, grading_section, metrics, selftest=True)
    out_dir = Path(tempfile.mkdtemp(prefix="zhipi_eval_selftest_"))
    write_outputs(out_dir, ocr_section, grading_section, metrics, selftest=True)
    print("\n自测完成：全流程（CER → 一致性 → 错因 → 分桶校准 → kappa → 报告写出）无异常。")


# ---------- CLI ----------

def main() -> None:
    # Windows 控制台重定向时默认 GBK，统一切 UTF-8 防中文 / 符号写出失败
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="智批π 实测评测脚本：CER / 一致性 / 错因命中 / 分桶校准 / kappa。"
                    "评测集目录格式见模块 docstring。")
    parser.add_argument("eval_dir", nargs="?", help="评测集目录（含 manifest.json）")
    parser.add_argument("--selftest", action="store_true",
                        help="自测模式：内置模拟数据跑通全流程，验证脚本本身（非实测）")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
    elif args.eval_dir:
        run_eval(Path(args.eval_dir))
    else:
        parser.error("请指定评测集目录，或使用 --selftest 自测模式")


if __name__ == "__main__":
    main()
