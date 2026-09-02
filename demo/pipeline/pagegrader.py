# -*- coding: utf-8 -*-
"""整页批改：一次调用批完一整页作业。

## 为什么整页一起批，而不是逐题批

逐题批曾经是这条链路的做法：一页认出 N 道题，就发 N 次批改请求。它带来
三个真实问题：

1. **成本与等待随题数线性膨胀。** 真实作业一页十几道题很常见，逐题批就是
   十几次模型调用（还要各自带上二次批改与交叉验证），演示时要对着转圈等
   十几分钟。当年那个「本次最多批前 8 道」的上限就是被这一点逼出来的——
   而它的代价是教师得手工拆卷。
2. **模型看不见上下文。** 一份作业里的题往往互相牵连（第 (2) 问用第 (1) 问的
   结论；英语的同一段短文下挂着五个小题）。逐题批时模型只看得到孤零零的
   一题，判不了「他第 (1) 问算错了，第 (2) 问在错误结论上推得其实是对的」。
3. **总分口径割裂。** 每题各自出一个「体系判别分 15」，教师拿不到「这一页
   一共多少分」，而那恰恰是他唯一关心的数字。

整页一次调用把三个问题一起解掉：一次请求、完整上下文、一个卷面总分。

## 与题库的关系

有题库（教师传过答案页）时，每道题都带上标准答案与该题分值，判分基准是
**教师给的答案**，答案匹配度这一维真实可算；没题库时同一套流程照跑，只是
基准换成模型自解，答案匹配度留空、按权重重归一化剔除。
两条路走的是同一段代码，区别只在 prompt 里有没有那段标准答案。
"""
import json
import os
import re

from . import confidence as conf
from . import grader


# 判分口径：有题库时按卷面分（题库里每题的分值之和）；无题库时每题的分值
# 由调用方按卷面印的数字 / 题型默认值推定（见 app._guess_max_score），
# 页级满分是它们之和。刻意不用一个统一常数——一道选择题和一道解答题都按
# 同一个数算，得出的总分既不是卷面分也不是任何一种可解释的口径。


PAGE_PROMPT_HEAD = """你是一名严谨的 K12 学科教师，正在批改**一整页**学生作业。
下面按题号列出了这一页的每道题、学生的手写作答{with_answer}。
请逐题判分，并给出这一页的总评。

【核心判分原则：先假定学生是对的】
判断某一步对错的唯一标准，是**学生写下的命题本身是否成立**，
不是它和你心里的解法长得像不像。以下四条按顺序执行：

1. **不要自己重解一遍再拿去比对。** 你自己的推导同样会出错——一旦把它当成
   基准，学生正确的写法就会被判错，而扣分理由看上去还很有条理，教师很难发现。
   请直接检查学生写下的这一步：它自身成立吗？
2. **举证责任在你。** 要判「计算错误」，reason 里必须指出**具体错在哪一项**
   （哪个符号、哪个系数、漏了哪一项），并给出可核对的反证：在定义域内取一个
   具体数值（如 x=2）代入，说明学生的式子在该点确实算不出正确结果。
   只写「应为 XXX」却给不出上述反证的，**一律不得扣分**。
3. **等价变形不是错误。** K12 里同一个式子的常见等价写法包括：
   - 通分与不通分：1 − 1/x² 与 (x²−1)/x²；e^x/x − (x + 1/x) 与 (e^x − x² − 1)/x；
   - 提公因式前后：e^x(x−1) − (x²−1) 与 (x−1)[e^x − (x+1)]；
   - 直线方程：y − y₀ = k(x − x₀) 与 y = kx + b；
   - 结论写法：a ≥ e−2 与 a ∈ [e−2, +∞)；
   - 英语：缩写与全写（don't / do not）、同义表达（at the same time / meanwhile）。
   判错之前，先确认两种写法是否真的不等价；等价就必须给分。
4. **拿不准就按对给分。** 把正确作答判成错，学生拿到的是一条成形却指向错误的
   证据链，家长会直接质疑；而漏判一处错误，教师复核时还能捞回来。因此无法
   确证错误时：按正确给分 + 降低 confidence + 在 teacher_note 里点名该题需人工复核。

【题型特别说明：客观题不看步骤】
本页包含不同题型，**判分规则按题型有所不同**：
- **选择题 / 填空题 / 判断题**（客观题）：学生只需给出答案，**不要求写解题过程**。
  评判标准仅为「答案对不对」，不得因为没有解题步骤而扣分，
  error_tag **不得**填写「步骤缺失」或「答案正确但过程不规范」。
  选择题字迹潦草时，在 teacher_note 里说明，由教师人工复核，
  **不要凭猜测判错**（见核心原则第 4 条）。
- **计算题 / 解答题 / 简答题**（主观题）：需要过程分，按步骤完整度给分。
- **翻译题 / 作文题**：按内容与语言双维度评分，不拆解步骤。

【逐题判分规则】
1. **必须逐题返回，一题一条，不得合并、不得漏题、不得增题**：
   返回的 questions 数组长度必须与下面列出的题目数量一致，且 index 一一对应；
2. 每题的 score 不得超过该题的 max_score，也不得为负；
3. verdict 从 correct（全对）/ partial（部分对）/ wrong（错）/ blank（未作答）中选：
   学生这一题什么都没写时必须是 blank 且 score 为 0，不要判成 wrong 之外的东西；
4. 每题都要给出 reason（判定理由）与 evidence（引用学生作答的原文片段；
   未作答填 ""）；扣分的题按核心原则第 2 条举证；
5. 每题给出 error_tag：从【可选错因标签】里选一个，该题无错则填 null；
   客观题**不得**使用「步骤缺失」或「答案正确但过程不规范」；
6. 每题给出 knowledge_point：**该题实际考查的课程知识点**，用教材里的知识点
   名称（如「特殊角的三角函数值」「动词时态」「说明方法及作用」），不超过 12 个字。
   不要把题型（选择题 / 填空题）或批改角度（运算执行）当成知识点；说不出填 ""；
7. 学生字迹无法辨认的题，legible 填 false 并在 reason 里说明，不要猜测其含义。

【整页总评】
8. student_feedback：面向学生的一段评语，点出这一页的共性问题与下一步该练什么；
9. teacher_note：面向教师的备注，**写明哪几题最需要人工复核**；
10. confidence：0 到 1 之间的小数，表示你对这一页判分整体的把握。

11. 只输出 JSON，不要输出多余文字。

【学科】{subject}
{bank_note}
【本页题目】
{question_block}

【可选错因标签】（只能从中选择）
{error_tags}

请严格输出以下 JSON：
{{
  "questions": [
    {{"index": 1, "score": 该题得分, "verdict": "correct/partial/wrong/blank",
      "error_tag": "错因标签或null", "reason": "判定理由",
      "knowledge_point": "本题考查的课程知识点，说不出填空字符串",
      "evidence": "引用学生作答原文，无则空字符串", "legible": true/false}}
  ],
  "student_feedback": "面向学生的整页评语",
  "teacher_note": "面向教师的备注（写明哪几题最需人工复核）",
  "confidence": 0到1之间的小数
}}"""

_BANK_NOTE_ON = """
【判分基准：教师提供的标准答案】
下面每道题都附了**教师答案页上的标准答案**与该题分值。判分以它为准：
学生作答与标准答案实质一致即给满分（允许等价表达，见核心原则第 3 条），
不一致再按错在哪里扣分。

**标注了「无基准」的题不得因此判对**。没有标准答案时只能看学生作答本身：
过程完整、结论明确、算到底了才给分；只抄了题目、只写了个开头、列出算式却
没得出结果的，一律不给满分。「我看不出它对不对」的正确处置是把这道题交给
教师，不是给满分——教师之所以传了答案页，就是要求按基准判，而不是要一份
「凡是对不上的都算对」的成绩。这类题一律写进 teacher_note 点名复核。
"""

_BANK_NOTE_OFF = """
【判分基准：无标准答案】
这一页没有教师答案页，你需要自己先解出每道题再判分。**正因如此，核心判分
原则第 1、4 条尤其重要**：你的解法可能是错的，不要拿它当唯一基准去否定学生。
"""


def _question_block(items: list, with_bank: bool) -> str:
    """把待批题目排成 prompt 里的题目清单。"""
    lines = []
    for it in items:
        head = "第 %d 题" % it["index"]
        if it.get("no"):
            head += "（卷面题号 %s）" % it["no"]
        head += "　满分 %g 分" % it["max_score"]
        if it.get("qtype"):
            head += "　题型：%s" % it["qtype"]
        lines.append(head)
        stem = (it.get("stem") or "").strip()
        lines.append("  题面：%s" % (stem or "（未识别到印刷题面，请仅凭作答内容保守判定）"))
        if with_bank:
            std = (it.get("standard_answer") or "").strip()
            lines.append("  标准答案：%s" % (
                std or "（**无基准**：题库里没有这一题，或教师答案页此题为空。"
                       "按上面「无基准」那一条保守判分，不得因缺基准而判对）"))
        ans = (it.get("student_answer") or "").strip()
        lines.append("  学生作答：%s" % (ans or "（学生未作答）"))
        lines.append("")
    return "\n".join(lines).rstrip()


def build_prompt(subject: str, items: list, with_bank: bool) -> str:
    return PAGE_PROMPT_HEAD.format(
        with_answer="、以及教师答案页上的标准答案" if with_bank else "",
        subject=subject or "其他",
        bank_note=_BANK_NOTE_ON if with_bank else _BANK_NOTE_OFF,
        question_block=_question_block(items, with_bank),
        error_tags="、".join(grader.ERROR_TAGS),
    )


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_VERDICTS = ("correct", "partial", "wrong", "blank")
_OBJECTIVE_QTYPES = {"选择题", "填空题", "判断题"}
_STEP_FORBIDDEN_TAGS = {"步骤缺失", "答案正确但过程不规范"}

# ---------------------------------------------------------------------------
# 待复核判定（后置防线）
# ---------------------------------------------------------------------------
#
# 为什么不做成置信度里的一个因子：加权表达的是「这份整体有多少把握」，而这里
# 表达的是「某一道题系统自己知道没读准」。掺进加权，一道读不出作答的题会被其它
# 高分因子稀释掉，照样绿灯放行。所以按后置防线处理，和双模型分歧同一机制：
# 有题待复核，这一页就不许自动通过。
#
# **这道防线抓不到什么，必须写在这里，别让后人误以为它兜住了识别错误**：
# 实测 hardwork_117 第 1 题（分式 x/(x+y) 的 x、y 同时扩大 2 倍，值不变，答案
# D），学生潦草写的是 D，整页识别读成 B；裁图放大后独立复识 5 次，5 次全部仍是
# B，每次都 legible=true、confidence=1.0。模型读错时和读对时一样自信，下面五个
# 信号一个都不会亮——学生答对却判错、置信度满分、教师看不到任何警告，这是最坏
# 的一种失败。它只能靠教师看转写发现：把转写显眼地摆在他面前、让他一键改，
# 比在这里继续加检测规则有用。

_LOW_ATTRIBUTION = 0.7          # 归属把握低于此值即请人复核
_CHOICE_LETTERS = set("ABCDEFGH")


def _review_flags(items: list) -> list:
    """挑出「系统自己知道没读准」的题，返回 [{index, no, reason}]。"""
    # 本页答了几道。用来区分「整页空白」与「只有这道空着」：后者可疑得多——
    # 学生一路做下来偏偏跳过一道，和系统把这道漏读了，在数据上长得一模一样。
    answered = sum(1 for it in items
                   if (it.get("student_answer") or it.get("answer") or "").strip())
    flags = []
    for it in items:
        answer = (it.get("student_answer") or it.get("answer") or "").strip()
        no = str(it.get("no") or "").strip() or str(it.get("index", ""))
        reason = ""
        if it.get("legible_hint") is False:
            reason = "字迹辨认不出"
        elif "〔?〕" in answer:
            reason = "转写里有认不出的字符"
        elif not answer and (it.get("refine_reason") == "blank_with_stray"
                             or 0 < answered < len(items)):
            # 纸上有没归属上的笔迹，或者这一页别的题都答了只有它空着——两种
            # 情况都比「学生真没做」更像是系统没读到，不该按 0 分自动放行。
            # 系统在这一层分不出二者，而两边代价差得远：学生答了却判 0 分是
            # 最伤人的一类错，多让教师看一眼不过是多一次点击。
            # 实测 89_student_2：模型某一跑没报出第 4 题那两块写在页边的字迹，
            # 该题判 0 分，全页 16/24 高于低分线，于是绿灯自动通过。
            reason = "疑似漏读作答"
        elif it.get("refine_changed"):
            # 整页读一次、裁图放大再读一次，两次给出了不同的转写。改写后的值
            # 未必更对（实测有 x-1 → x 这种越改越错的），能确定的只是这块字迹
            # 读不稳——这正是「识别错了却判得很有把握」的高发地带。
            reason = "两次读取结果不一致"
        else:
            conf = it.get("attribution_confidence")
            if isinstance(conf, (int, float)) and not isinstance(conf, bool) \
                    and conf < _LOW_ATTRIBUTION:
                reason = "作答归属把握低"
            elif (it.get("qtype") or "").strip() == "选择题" and answer:
                # 选择题的转写应该落在选项字母上；落不上多半是把旁边的
                # 演算过程当成了答案，此时按它判对错没有意义。
                letters = re.sub(r"[^A-Za-z]", "", answer).upper()
                if not letters or not set(letters) <= _CHOICE_LETTERS:
                    reason = "选择题转写不是选项"
        if reason:
            flags.append({"index": it.get("index"), "no": no, "reason": reason})
    return flags


# 每道可疑题从 OCR 清晰度里扣多少分，以及扣分上限。
_CLARITY_PENALTY = 12
_CLARITY_PENALTY_MAX = 40


# 得分率低于此值的页不自动通过。
#
# 这和置信度是两件事：置信度衡量的是「系统对自己的判断有多少把握」，它不会
# 因为学生错得多而下降。一份判成 14/24 的作业有两种可能——学生真的不会，
# 或者转写把答对的题读错了（实测第 89 课时那份，最终答案写在中缝，没被归属
# 进来，判 2/6；答案归属回来之后同一题是 6/6）。后一种从置信度上完全看不出，
# 而两种情况教师都该看一眼，所以一律压住绿灯。
_LOW_SCORE_RATIO = 0.6


# 无判分基准的题：教师传了答案页，但这一题在题库里没有对应题（题干没对上、
# 或答案页上这题本来就空着）。prompt 里已经要求这类题保守判分，但**光靠
# prompt 约束不住**——实测就是这条路把一份没做完的卷子判成了满分：第 7 题
# 学生只写到「1/x+1/y=3」没算出结果，判分理由写着「由于标准答案缺失，
# 按正确处理」，照样 6/6，整页 38/38。
#
# 所以按已有的 _review_flags 那套做双落点：既折减置信度（进加权），
# 又压住绿灯（后置防线）。只做其中一个都不够——只折减置信度，一页里一两道
# 无基准题扣不动分流；只压分流，这份在看板上仍是高置信度样本。
#
# 折减而不是当成第六个因子：五因子衡量的是「在能判的范围内有多可信」，
# 无基准占比衡量的是「能判的范围有多大」，两者是乘法关系而不是并列加权。
# 按分值占比而不是题数占比，理由同 answer_match：一道 15 分的大题没基准，
# 比一道 2 分的选择题没基准严重得多。
_UNBASED_DISCOUNT = 0.6


def _unbased_flags(items: list, with_bank: bool) -> list:
    """挑出「有题库、但这道题没有标准答案可比」的题，返回 [{index, no, max_score}]。

    只在有题库时有意义：整页无题库是另一回事，那时 answer_match 整个不参与
    加权（按权重重归一化剔除），教师也知道自己没传答案页。而「传了答案页、
    偏偏这几道对不上」是教师看不见的——他以为整页都按基准判过了。
    """
    if not with_bank:
        return []
    return [{
        "index": it["index"],
        "no": str(it.get("no") or "").strip() or str(it.get("index", "")),
        "max_score": float(it.get("max_score") or 0),
    } for it in items if not (it.get("standard_answer") or "").strip()]


def _discount_unbased(confidence: float, unbased: list, max_total: float) -> float:
    """按无基准题的**分值占比**折减整页置信度。"""
    if not unbased or max_total <= 0:
        return confidence
    ratio = min(1.0, sum(u["max_score"] for u in unbased) / max_total)
    return round(confidence * (1 - _UNBASED_DISCOUNT * ratio), 1)


def _adjust_clarity(clarity: float, review: list) -> float:
    """按可疑题数量下调这一页的 OCR 清晰度。

    模型自报的 clarity 回答的是「这张照片拍得清不清楚」，与「我有没有读对」
    是两件事——一页里三道题读不准，clarity 照样可以是 90。而 clarity 会经由
    置信度进入分流、教师工作台的排序与看板统计：不下调的话，一份读错了几道题
    的作业会以高置信度自动通过，还在看板上算作优等样本。

    扣分设上限，是因为再往下已经改变不了结论——该转人工的在阈值处就转了，
    继续扣只是把分数做得更难看，不产生新的处置。
    """
    if not review or clarity <= 0:
        return float(clarity)
    return max(0.0, float(clarity) - min(_CLARITY_PENALTY_MAX,
                                         _CLARITY_PENALTY * len(review)))


def _parse_questions(items: list, data: dict) -> list:
    """把模型返回的逐题判分对齐回题目清单。

    按 index 对齐而不是按数组下标：模型偶尔会漏一题或多返回一条，按下标
    硬对会让后面所有题整体错位——错位后分数照出、证据链照给，只是每一条
    都挂在了别的题上，是最难被发现的一类错误。
    """
    by_index = {}
    for row in (data.get("questions") or []):
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        by_index.setdefault(idx, row)

    out = []
    for it in items:
        row = by_index.get(it["index"]) or {}
        max_score = float(it["max_score"])
        try:
            score = float(row.get("score"))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(max_score, round(score * 2) / 2))

        verdict = str(row.get("verdict") or "").strip().lower()
        answered = bool((it.get("student_answer") or "").strip())
        if verdict not in _VERDICTS:
            # 模型没给或给了别的说法时按分数反推，别把「没说」当成「错」
            verdict = ("correct" if score >= max_score else
                       "partial" if score > 0 else
                       ("wrong" if answered else "blank"))
        if not answered:
            verdict, score = "blank", 0.0

        tag = str(row.get("error_tag") or "").strip()
        if tag not in grader.ERROR_TAGS:
            tag = ""
        # 客观题的判分规则只看答案对错，没有过程分；模型被 prompt 明确告知了
        # 这一点，但偶尔仍会给出步骤相关的 tag（prompt 与安全规则的优先级
        # 竞争时，规则有时赢）。在这里做一道硬拦截：客观题拿到步骤类 tag 就清掉，
        # 不让它进入证据链、不让它影响 teacher_note 里的复核建议。
        is_objective = (it.get("qtype") or "") in _OBJECTIVE_QTYPES
        if is_objective and tag in _STEP_FORBIDDEN_TAGS:
            tag = ""
        # 客观题的 score：模型可能因为"没写步骤"给了小于满分的值。
        # 一旦确认是客观题且学生作答非空，score 只能是 max_score 或 0，不该有
        # 中间值——填空题填了答案要么对要么错，2.5/5 这样的"过程分"没有意义。
        # 用 verdict 来决定而不是反过来：verdict 来自模型对答案对错的判断，
        # 它比分数更准确（模型可能说 correct 但分数填了 3/5）。
        if is_objective and answered:
            if verdict == "correct":
                score = max_score
            elif verdict in ("wrong", "blank"):
                score = 0.0
            # partial 在客观题上是无效判定，按分数决定向上还是向下取整：
            # score > max_score/2 → correct，否则 → wrong
            elif score >= max_score / 2:
                score, verdict = max_score, "correct"
            else:
                score, verdict = 0.0, "wrong"
        out.append({
            "index": it["index"],
            "no": it.get("no", ""),
            "qid": it.get("qid"),
            "qtype": it.get("qtype", ""),
            "stem": it.get("stem", ""),
            "student_answer": it.get("student_answer", ""),
            "standard_answer": it.get("standard_answer", ""),
            "score": score,
            "max_score": max_score,
            "verdict": verdict,
            "error_tag": tag,
            "reason": str(row.get("reason") or "").strip(),
            "evidence": str(row.get("evidence") or "").strip(),
            "knowledge_point": grader._step_kp(row),
            "legible": row.get("legible") is not False,
            "bbox": it.get("bbox"),
            "answer_box": it.get("answer_box"),
            "page_id": it.get("page_id"),
            "matched": bool(it.get("qid")),
            "match_score": it.get("match_score", 0.0),
            # 归并进这道题的题库小问（教师页拆条、学生页整块时会有）。
            # 满分是几条加出来的，不带出去教师无从对账「这题怎么 8 分」。
            "absorbed": it.get("absorbed") or [],
            "answered": answered,
        })
    return out


def _as_step_analysis(questions: list) -> list:
    """把逐题判分摊成 step_analysis（每题一行）。

    下游——教师工作台、班级看板、飞书台账、结果页证据链——全部按
    step_analysis 消费。整页批改把「一步」重新定义为「一题」，这些地方
    就一行都不用改：它们要的本来就是「一条条带证据的判分记录」。
    """
    steps = []
    for q in questions:
        name = "第 %s 题" % (q["no"] or q["index"])
        steps.append({
            "step": name,
            "max_score": q["max_score"],
            "score": q["score"],
            "is_correct": q["verdict"] == "correct",
            "partial": q["verdict"] == "partial",
            "error_tag": q["error_tag"] or None,
            "reason": q["reason"],
            "evidence": q["evidence"],
            "knowledge_point": q["knowledge_point"],
            "legible": q["legible"],
        })
    return steps


# ---------------------------------------------------------------------------
# 答案匹配度
# ---------------------------------------------------------------------------

def answer_match(questions: list) -> float | None:
    """按题分加权的答案匹配度（0-100）；没有任何题对上题库时返回 None。

    刻意用「得分率」而不是「判对的题数占比」：一道 15 分的作文和一道 2 分的
    选择题，对答案匹配度这一维的贡献不该一样重。

    只统计**对上了题库**的题。没对上的题（教师页里没有这一题、或题干相似度
    不够）是无基准可比的，把它们按 0 分计入等于用「没对上」去惩罚学生。
    """
    matched = [q for q in questions if q.get("matched")]
    if not matched:
        return None
    total = sum(q["max_score"] for q in matched)
    if total <= 0:
        return None
    got = sum(q["score"] for q in matched)
    return round(got / total * 100, 1)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _double_check(subject: str, items: list, with_bank: bool, creds,
                  first_total: float) -> dict | None:
    """同模型独立再批一次，比较两次总分得「LLM 自检一致性」。

    比模型自报的 confidence 可信：自报值是它对自己的印象，两次独立批改
    的分差是可观测的行为。整页只多一次调用，不随题数放大。
    """
    if os.environ.get("ZHIPI_DOUBLE_CHECK", "1").strip() == "0" or not creds:
        return None
    try:
        data = _call(creds, subject, items, with_bank, temperature=0.3)
        second = _parse_questions(items, data)
        second_total = sum(q["score"] for q in second)
        max_total = sum(q["max_score"] for q in items) or 1.0
        gap = abs(first_total - second_total)
        return {
            "second_score": round(second_total, 1),
            "agreement": round(max(0.0, 100.0 - gap / max_total * 100), 1),
        }
    except Exception:
        return None


def _cross_check(subject: str, items: list, with_bank: bool,
                 first_total: float) -> dict | None:
    """第二家模型独立复核整页，分差超过满分 15% 时一票否决转人工。

    与逐题批改时一样，这是**后置防线，不占置信度权重**：加权表达「有多少
    把握」，一票否决表达「这份不能自动放行」。
    """
    creds2 = grader.llm_credentials_2()
    if not creds2:
        return None
    try:
        data = _call(creds2, subject, items, with_bank,
                     timeout=grader._cross_timeout())
        second = _parse_questions(items, data)
        second_total = sum(q["score"] for q in second)
        max_total = sum(q["max_score"] for q in items) or 1.0
        gap = abs(first_total - second_total)
        return {
            "model2": creds2["model"],
            "model2_score": round(second_total, 1),
            "gap": round(gap, 1),
            "escalated": gap > max_total * conf.CROSS_DISAGREE_RATIO,
        }
    except Exception:
        return None


def _call(creds, subject, items, with_bank, temperature: float = 0,
          timeout: int = None) -> dict:
    return grader._call_llm(creds, build_prompt(subject, items, with_bank),
                            temperature=temperature, timeout=timeout)


def grade_page(subject: str, items: list, clarity: float,
               with_bank: bool = False,
               factor_overrides: dict = None) -> dict:
    """一次调用批完一整页。

    items: 已对齐好的题目清单，每项至少要有
        {index, stem, student_answer, max_score}；有题库时还带
        {qid, no, qtype, standard_answer}，有 bbox 时一并带上（画批改痕迹用）。

    返回结构与 grader.grade_adhoc 同形（total_score / status /
    confidence_factors / step_analysis / error_tags / …），再附一个
    questions 字段给整页视图与痕迹渲染用。同形是刻意的：教师工作台、
    班级看板、飞书台账全部原样复用。
    """
    creds = grader.llm_credentials()
    if not creds:
        raise RuntimeError("整页批改需要配置 ZHIPI_LLM_API_KEY 或 ZHIPI_VLM_API_KEY")
    if not items:
        raise ValueError("这一页没有可批改的题目")

    data = _call(creds, subject, items, with_bank)
    questions = _parse_questions(items, data)
    total = round(sum(q["score"] for q in questions), 1)
    max_total = round(sum(q["max_score"] for q in questions), 1)

    step_analysis = _as_step_analysis(questions)
    error_tags = list(dict.fromkeys(q["error_tag"] for q in questions if q["error_tag"]))

    # 识别层自己报出来的可疑题。它有两个落点：先下调这一页的 OCR 清晰度（进加权），
    # 再在分流上压住绿灯（后置防线）。两者缺一不可——只下调清晰度，一页里一道
    # 可疑题只值 3 分置信度，照样绿灯；只压分流，看板上这份仍是高清晰度样本。
    review = _review_flags(items)
    clarity_used = _adjust_clarity(clarity, review)

    factors = grader.derive_factors(
        {"max_score": max_total}, "", clarity_used, step_analysis,
        data.get("confidence"),
        answer_match=answer_match(questions) if with_bank else None)

    consistency = _double_check(subject, items, with_bank, creds, total)
    if consistency is not None:
        factors["llm_self_consistency"] = consistency["agreement"]
    factors = grader._apply_overrides(factors, factor_overrides)
    confidence = conf.compute_confidence(factors)
    # 无判分基准的题要先折减置信度，再分流——顺序不能反，否则折减出来的
    # 分数与红黄绿对不上，界面上会出现「置信度 62 却是绿灯」。
    unbased = _unbased_flags(items, with_bank)
    confidence = _discount_unbased(confidence, unbased, max_total)
    status = conf.route(confidence)

    cross = _cross_check(subject, items, with_bank, total) \
        if status in ("yellow", "red") else None
    if cross and cross["escalated"]:
        status = "red"

    if review and status == "green":
        status = "yellow"
    # 无判分基准的题同样压住绿灯：这几道的分是模型自行判断的，而教师传了
    # 答案页就意味着他要的是按基准判。放它自动通过，等于把「没对上」
    # 悄悄变成了「对了」。
    if unbased and status == "green":
        status = "yellow"
    # 大面积失分同样压住绿灯，理由见 _LOW_SCORE_RATIO
    low_score = bool(max_total) and (total / max_total) < _LOW_SCORE_RATIO
    if low_score and status == "green":
        status = "yellow"

    seed = json.dumps([q["index"] for q in questions], ensure_ascii=False)
    teacher_note = (str(data.get("teacher_note") or "").strip()
                    or grader._build_teacher_note(error_tags, status))
    if review:
        # 点名到题：教师拿到的是「先看这几道」，而不是「这一页请复核」——
        # 后者等于让他从头核一遍，那这道防线就白设了。
        teacher_note += "｜请优先复核第 %s 题（%s）" % (
            "、".join(f["no"] for f in review[:5]),
            "；".join(dict.fromkeys(f["reason"] for f in review)))
    if low_score:
        teacher_note += "｜本页得分率 %d%%，已转人工确认：大面积失分也可能是转写" \
                        "把答对的题读错了" % round(100 * total / max_total)
    if unbased:
        # 点名到题，理由同 review：教师要的是「先看这几道」。
        # 同时说清这几道的分是怎么来的——他传了答案页，有权知道哪几道
        # 其实没按他的答案判。
        teacher_note += "｜第 %s 题在题库里没有对应题，无判分基准（分数由模型" \
                        "自行判断，占本页 %d%% 分值），已转人工确认" % (
                            "、".join(u["no"] for u in unbased[:5]),
                            round(100 * sum(u["max_score"] for u in unbased)
                                  / max_total) if max_total else 0)
    result = {
        "mode": "llm",
        "grade_scope": "page",          # 区别于逐题批改的单题结果
        "total_score": total,
        "max_score": max_total,
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": step_analysis,
        "questions": questions,
        "knowledge_points": list(dict.fromkeys(
            q["knowledge_point"] for q in questions if q["knowledge_point"])),
        "error_tags": error_tags,
        "student_feedback": (str(data.get("student_feedback") or "").strip()
                             or grader._build_feedback(step_analysis, error_tags, seed)),
        "teacher_note": teacher_note,
        "ocr_clarity": round(float(clarity_used), 1),
        "score_basis": "paper" if with_bank else "system",
        "bank_used": bool(with_bank),
        # 识别层报出的可疑题。needs_review 是给界面用的开关，review_flags 带着
        # 逐题原因——只给一个布尔，教师还是不知道该看哪道。
        "needs_review": bool(review),
        "review_flags": review,
        "low_score_hold": low_score,
    }
    if consistency is not None:
        result["consistency_check"] = consistency
    if cross is not None:
        result["cross_check"] = cross
    return result
