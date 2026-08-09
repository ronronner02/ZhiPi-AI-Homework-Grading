"""过程级批改模块：Rubric 逐步判分。

两种模式：
- mock（默认）：规则引擎。读取每份作答「每步」的预标注（对 / 错、部分分比例、
  错因标签、判断依据、原文证据），按 Rubric 逐步计算得分，聚合总分、错因与
  知识点，再用规则模板生成个性化评语与教师备注。全程确定性，不使用任何随机数。
- llm（可选）：若配置了环境变量 ZHIPI_LLM_API_KEY，则按 Prompt 库 Prompt 1
  （完整版过程级批改，含证据链 evidence / legible 输出）让 OpenAI 兼容大模型
  按 Rubric 逐步批改并返回 JSON。未配置或调用 / 解析失败时，自动降级为 mock，
  并在结果中标注 mode = "mock"。

可靠性机制（均可通过环境变量控制，异常时静默跳过，保证 Demo 不中断）：
- 二次批改一致性（Prompt 6 思路）：同 Prompt 独立复批一次，程序化比对两次
  总分与错因标签得一致性分，作为 §9.7「LLM 自检一致性」因子；
- 双模型交叉验证（P2）：黄 / 红结果用第二模型复核，分差过大强制转红交人工。

对应总体流程（§7.1）中的「LLM 过程级批改 → 错因标签生成」环节。
"""
import difflib
import hashlib
import os
import json
import time

import requests

from . import ocr as ocr_mod
from . import confidence as conf
from . import dimensions

# 详细设计方案 §6.11 第二层「错因标签」枚举，全系统只允许使用这些标签
ERROR_TAGS = [
    "概念理解错误",
    "公式选择错误",
    "步骤缺失",
    "计算错误",
    "单位错误",
    "审题错误",
    "图形理解错误",
    "符号书写错误",
    "表达不完整",
    "答案正确但过程不规范",
]

# 错因标签 → 可执行改进建议（规则模板，用于拼装个性化评语）。
# 每个错因备 3 条措辞变体，由 seed（学生 / 作答标识）确定性选取，
# 避免同错因的学生拿到一字不差的模板评语。
ADVICE = {
    "概念理解错误": [
        "先回顾相关概念与定义，弄懂原理后再动笔。",
        "建议把这一知识点的概念重新梳理一遍，理解透了再做题。",
        "从课本定义出发，弄清概念的适用范围，再做两道同类题巩固。",
    ],
    "公式选择错误": [
        "解题前先确认应使用的公式，并注意公式的适用条件。",
        "动笔前先写下本题涉及的公式，核对每个量的含义再代入。",
        "建议整理一张公式卡片，弄清每个公式的使用前提，避免选错。",
    ],
    "步骤缺失": [
        "解题步骤要写完整，避免跳步导致失分。",
        "把每一步推理都写在卷面上，检查时逐步核对是否有遗漏。",
        "做完后按评分点自查一遍，确认关键步骤一个不落。",
    ],
    "计算错误": [
        "计算时放慢速度，算完把结果代回原式检验。",
        "关键运算建议分步演算或列竖式，减少口算失误。",
        "算完后用逆运算快速验算一次，能有效发现计算差错。",
    ],
    "单位错误": [
        "代入数值前先统一单位，最终结果记得写对单位。",
        "写答案时先检查单位是否与题目要求一致，再落笔。",
        "养成「数值+单位」一起写的习惯，代入前先做好单位换算。",
    ],
    "审题错误": [
        "读题时圈出已知量与所求量，避免看错条件。",
        "建议读两遍题：第一遍了解大意，第二遍标出关键条件再动笔。",
        "答题前先复述一遍题目要求，确认理解无误再列式。",
    ],
    "图形理解错误": [
        "结合图形逐一标注已知条件，再列式求解。",
        "先把图中的结构关系看清楚，标好各元素之间的联系再分析。",
        "建议边读题边在图上做记号，把图形信息转成文字条件列出来。",
    ],
    "符号书写错误": [
        "注意规范书写符号与拼写，避免因笔误失分。",
        "书写时放慢一点，写完检查正负号与拼写是否准确。",
        "把易混符号和易错拼写记进错题本，考前重点过一遍。",
    ],
    "表达不完整": [
        "把结论写完整，所有答案都要清晰表述出来。",
        "答题最后补上完整结论句，确认每一问都有明确回答。",
        "写完后对照题目所求检查一遍，别漏写任何一问的答案。",
    ],
    "答案正确但过程不规范": [
        "答案正确，但要注意书写过程的规范性。",
        "结果算对了很好，再把步骤按规范格式补全会更稳。",
        "保持正确率的同时，注意过程书写的完整与规范，别丢过程分。",
    ],
}

# 评语开头肯定句式池：同样由 seed 确定性选取，避免每条评语都以同一句式开头
OPENERS = [
    "你在{steps}方面是正确的，说明基本思路没有问题。",
    "{steps}这些部分你完成得很好，基础掌握得比较扎实。",
    "先肯定做得好的地方：{steps}都处理正确，解题方向是对的。",
    "这次作答中{steps}完成得不错，看得出你思考得很认真。",
    "{steps}处理得当，整体解题框架已经搭起来了。",
]

# Prompt 库 Prompt 1（完整版过程级批改）模板：逐步输出 evidence（引用学生
# 原文片段作为判断依据）与 legible（该步是否可辨认），配防幻觉与降级约束
PROMPT_TEMPLATE = """你是一名严谨的初中学科教师，请根据题目、标准答案、评分规则和学生作答进行批改。

【批改要求】
1. 必须按照评分规则（Rubric）逐项评分，不允许只根据最终答案判断；
2. **标准答案给的是一种写法，不是唯一写法。** 学生用等价形式写出同一结论时
   必须给分——通分与不通分（1 − 1/x² 与 (x²−1)/x²）、提公因式前后、
   y − y₀ = k(x − x₀) 与 y = kx + b、a ≥ e−2 与 a ∈ [e−2, +∞) 都是等价的。
   判「计算错误」前，先把两种写法代入同一个数值点各算一遍：数值相同即等价；
3. **判错要举证。** 扣分时 reason 必须指出具体错在哪一项（哪个符号、哪个系数、
   漏了哪一项），只写「应为 XXX」而给不出具体错项的，不得扣分；
4. step_analysis 逐项对应 Rubric，且每步 step 名称必须与 Rubric 步骤同名；
5. 必须指出具体错误步骤及错误位置；正确步骤也要写清给分理由，不得省略；
6. 错因标签必须从【可选错因标签】中选择，不得自造标签；正确步骤 error_tag 为 null；
7. 每一步都必须同时给出 reason 与 evidence：
   - reason：该步判定理由（正确写「为何给分」，错误写「错误位置与原因」）；
   - evidence：引用学生作答中的原文片段作为判断依据；
     学生未写出对应内容时 evidence 置为空字符串 ""。

【防幻觉与降级约束】
8. 只能依据学生实际写出的内容评判，不得臆造、补全学生未写出的步骤或结论；
9. 若某步内容无法辨认或存在歧义，将该步 legible 置为 false，如实说明
   「无法辨认 / 存在歧义」，不要猜测其含义，并降低整体 confidence；
10. 若证据不足以判定对错，**按正确给分**并降低 confidence 交人工——把正确
    作答判成错，会给出一条成形却指向错误的证据链，比漏判一处错误更难发现；
11. 只输出 JSON，不要输出多余文字。

【学科】{subject}    【学段】{grade}    【题型】{question_type}

【题目】
{question}

【标准答案】
{standard_answer}

【标准解题步骤】
{standard_solution_steps}

【评分规则 Rubric】（每步及其满分）
{rubric}

【学生作答（OCR 转写）】
{student_answer}

【可选错因标签】（只能从中选择）
{error_tags}

请严格输出以下 JSON：
{{
  "score": 总分,
  "max_score": 满分,
  "step_analysis": [
    {{"step": "与 Rubric 同名的步骤名称", "is_correct": true/false, "score": 该步得分,
      "error_tag": "错因标签或null", "reason": "该步判定理由（正确/错误均须填写）",
      "evidence": "引用学生作答原文片段，无则为空字符串", "legible": true/false}}
  ],
  "knowledge_points": [],
  "error_tags": [],
  "student_feedback": "面向学生的个性化评语",
  "teacher_note": "面向教师的备注",
  "confidence": 0到1之间的小数
}}"""


# 题库外作业的批改模板（判别分维度体系）。
# 题库外作业的批改 Prompt（维度体系版）。
#
# 与 PROMPT_TEMPLATE 的关键不同：没有人工标注的标准答案，只有固定的五个
# 评分维度（由 dimensions 模块按学科下发），模型在给定维度上直接打分举证。
#
# 这段 Prompt 被真实作答打脸过两次，两次都是**把正确的判成错的**：
# 1. 旧版让模型先自解、再以自己的解法为基准判分，等价变形（如
#    e^x/x-(x+1/x) 与 (e^x-1)/x-x）被判成计算错误；
# 2. 去掉「先自解」并加上「等价变形不扣分」之后仍然复发——模型自己把
#    g'(x) 少算了一项（给出 (e^x(x-1)+1)/x²，漏掉 -1），再拿这个错结果当
#    基准扣了学生 2 分。原因是「必须先验证是否等价」只是一句要求，
#    没有给出可执行的验证办法，模型于是继续凭印象比对。
#
# 现版把它变成一套可执行的程序：举证责任在模型（判错必须给出具体错项 +
# 数值反证）、等价性用数值代入检验、拿不准一律按对给分并降 confidence。
# 方向是刻意偏保守的——漏判的错误教师复核能捞回来，把对的判成错则会给出
# 一条完全成形却指向错误的证据链，那种错误最难被发现，也最伤信任。
OPEN_PROMPT_TEMPLATE = """你是一名严谨的 K12 学科教师，请直接批改学生作答，按给定的评分维度打分。

【核心判分原则：先假定学生是对的】
判断某一步对错的唯一标准，是**学生写下的数学命题本身是否成立**，
不是它和你心里的解法长得像不像。以下四条按顺序执行：

1. **不要自己重解一遍再拿去比对。** 你自己的推导同样会出错——一旦把它当成
   基准，学生正确的写法就会被判错，而扣分理由看上去还很有条理、证据链毫无
   异样，教师很难发现。请直接检查学生写下的这一步：它自身成立吗？
2. **举证责任在你。** 要判「计算错误」，reason 里必须指出**具体错在哪一项**
   （哪个符号、哪个系数、漏了哪一项），并给出可核对的反证：在定义域内取一个
   具体数值（如 x=2）代入，说明学生的式子在该点确实算不出正确结果。
   只写「应为 XXX」却给不出上述反证的，**一律不得扣分**。
3. **等价变形不是错误。** K12 里同一个式子的常见等价写法包括：
   - 通分与不通分：1 − 1/x² 与 (x²−1)/x²；e^x/x − (x + 1/x) 与 (e^x − x² − 1)/x；
   - 提公因式前后：e^x(x−1) − (x²−1) 与 (x−1)[e^x − (x+1)]；
   - 直线方程：y − y₀ = k(x − x₀) 与 y = kx + b；
   - 结论写法：a ≥ e−2 与 a ∈ [e−2, +∞)。
   判错之前，先把两种写法代入同一个数值点各算一遍；数值相同就是等价，必须给分。
4. **拿不准就按对给分。** 把正确作答判成错，学生拿到的是一条成形却指向错误的
   证据链，家长会直接质疑；而漏判一处错误，教师复核时还能捞回来。因此无法
   确证错误时：按正确给分 + 降低 confidence + 在 teacher_note 里点名这一维
   需要人工复核。

【评分规则】
1. step_analysis 必须**逐项对应下面的评分维度**，每项 step 名称与维度名完全一致，
   不得增删维度、不得改名、不得合并；
2. 每个维度的得分不得超过该维度满分；
3. 每个维度都要给出 reason 与 evidence：
   - reason：该维度的判定理由（得满分写「为何给满」，扣分按核心原则第 2 条举证）；
   - evidence：引用学生作答的原文片段作为依据；学生未写出对应内容时填 ""；
4. 错因标签只能从【可选错因标签】中选，不得自造；该维度无错则 error_tag 为 null；
5. 每个维度还要给出 knowledge_point：**该维度在本题实际考查的课程知识点**，
   用教材里的知识点名称（如「导数的四则运算」「函数单调性」「一元二次方程求根」
   「动词时态」），不超过 12 个字。
   注意：**不要把维度名（题意理解 / 方法选择 / 运算执行 / 步骤完整 / 结论正确
   或其学科别名）当成知识点填回来**——维度说的是批改的角度，知识点说的是
   这道题考什么，两者不是一回事。确实说不出对应知识点时填 ""，不要硬凑。

【防幻觉与降级约束】
6. 只能依据学生实际写出的内容评判，不得臆造、补全学生未写出的步骤；
7. 某维度内容无法辨认或存在歧义时，将该项 legible 置为 false，如实说明，
   不要猜测其含义，并降低整体 confidence；
8. 证据不足以判定对错时，按核心判分原则第 4 条处理——给分、降 confidence、
   交人工，不得强行给出扣分结论；
9. 只输出 JSON，不要输出多余文字。

【学科】{subject}

【题目（照片中的印刷题面）】
{question}

【评分维度】（每个维度及其满分，总分 {total_score} 分）
{dimensions}

【学生作答（识别转写）】
{student_answer}

【可选错因标签】（只能从中选择）
{error_tags}

请严格输出以下 JSON：
{{
  "reference_answer": "（可选）简要说明本题各步的判分要点，供教师核对判分依据",
  "score": 总分,
  "max_score": {total_score},
  "step_analysis": [
    {{"step": "与评分维度同名", "is_correct": true/false, "score": 该维度得分,
      "error_tag": "错因标签或null", "reason": "判定理由",
      "knowledge_point": "本题该维度考查的课程知识点，说不出填空字符串",
      "evidence": "引用学生作答原文，无则空字符串", "legible": true/false}}
  ],
  "error_tags": [],
  "student_feedback": "面向学生的个性化评语",
  "teacher_note": "面向教师的备注（尤其写明哪个维度最需要人工复核）",
  "confidence": 0到1之间的小数
}}"""


def _build_open_prompt(question: dict, student_text: str,
                       dimension_bias: dict = None) -> str:
    """拼装题库外作业的批改 Prompt（维度体系版）。

    dimension_bias 非空时追加一段「教师历史修正」提示：告诉模型它在哪个维度
    上历史性偏严或偏松。这是教师终审真正回灌到判分的通道——按维度累计，
    因为维度只有五个，比按题统计收敛快得多。
    """
    subject = question.get("subject", "") or "其他"
    prompt = OPEN_PROMPT_TEMPLATE.format(
        subject=subject,
        question=question.get("question_text") or "（题面未能识别，请仅凭作答内容保守判定）",
        dimensions=dimensions.prompt_block(subject),
        total_score=dimensions.TOTAL_SCORE,
        student_answer=student_text,
        error_tags="、".join(ERROR_TAGS),
    )
    hint = _bias_hint(subject, dimension_bias)
    if hint:
        # 插在【学生作答】之前：让模型读到作答时已经知道该在哪一维更谨慎
        marker = "【学生作答（识别转写）】"
        prompt = prompt.replace(marker, hint + "\n" + marker, 1)
    return prompt


def _bias_hint(subject: str, dimension_bias: dict) -> str:
    """把教师历史修正量写成一段自然语言提示。

    只提示明显偏差（|平均修正| ≥ 0.5 分）：小于半分的差异在 2-5 分制的维度上
    属于噪声，写进 Prompt 只会让模型对着噪声调整。
    """
    if not dimension_bias:
        return ""
    name_of = {item["dimension"]: item["step"]
               for item in dimensions.rubric_for(subject)}
    lines = []
    for key, delta in sorted(dimension_bias.items(), key=lambda kv: -abs(kv[1])):
        if abs(delta) < 0.5:
            continue
        name = name_of.get(key, key)
        if delta > 0:
            lines.append("- %s：教师历史上平均往上改 %.1f 分，说明此前判得偏严，"
                         "本次请确认是否存在「学生写法不同但同样正确」而被扣分的情况。"
                         % (name, delta))
        else:
            lines.append("- %s：教师历史上平均往下改 %.1f 分，说明此前判得偏松，"
                         "本次请更严格核对该维度的依据是否真的充分。"
                         % (name, abs(delta)))
    if not lines:
        return ""
    return ("【教师历史修正参考】（同学科既往终审的统计，供你校准松紧；"
            "不要据此直接加减分，仍按本次作答的实际内容判定）\n" + "\n".join(lines) + "\n")


def _step_score(ann: dict, max_score: int) -> int:
    """规则引擎的判分规则：由「对 / 错 / 部分对」推出该步得分。

    - is_correct 为真：给满分；
    - 否则若给出 partial_ratio（部分分比例）：按比例四舍五入取分；
    - 否则：0 分。
    """
    if ann.get("is_correct"):
        return max_score
    ratio = ann.get("partial_ratio")
    if ratio is not None:
        return int(round(max_score * float(ratio)))
    return 0


def _pick_variant(seed: str, salt: str, n: int) -> int:
    """用 md5(seed|salt) 确定性选取 0..n-1 下标。

    同一 seed 结果可复现（不引入随机数），不同 seed（不同学生 / 作答）
    落到不同变体，实现「同错因不同措辞」。salt 用于区分同一评语内的
    多个选取点（开头句式、各错因建议），避免彼此联动。
    """
    digest = hashlib.md5(("%s|%s" % (seed, salt)).encode("utf-8")).hexdigest()
    return int(digest, 16) % n


def _build_feedback(step_analysis: list, error_tags: list, seed: str = "") -> str:
    """规则模板生成个性化评语（§15.2 思路）：先肯定，再指出关键问题，最后给建议。

    seed 取 submission_id 或学生名：经 md5 确定性选取开头句式与建议变体，
    保证同错因不同学生的评语措辞不同，且同一份作答多次批改结果一致。
    """
    correct_steps = [s["step"] for s in step_analysis if s["is_correct"]]
    wrong_steps = [s for s in step_analysis if not s["is_correct"]]
    parts = []

    # 1. 先肯定已掌握的部分（开头句式从 OPENERS 池中确定性选取，避免千人一面）
    if correct_steps:
        opener = OPENERS[_pick_variant(seed, "opener", len(OPENERS))]
        parts.append(opener.format(steps="、".join(correct_steps)))
    else:
        parts.append("本题整体完成度偏低，我们一起把基础一步步补上来。")

    # 2. 指出最关键的问题（取扣分最多的错误步骤）
    if wrong_steps:
        key = max(wrong_steps, key=lambda s: s["max_score"] - s["score"])
        if key.get("error_tag"):
            parts.append("本次主要问题出在「%s」，属于%s。" % (key["step"], key["error_tag"]))
        else:
            parts.append("本次主要问题出在「%s」。" % key["step"])

    # 3. 针对每个错因给出可执行建议（每个错因 3 条变体中确定性选 1 条，最多两条）
    for tag in error_tags[:2]:
        variants = ADVICE.get(tag)
        if variants:
            parts.append(variants[_pick_variant(seed, "advice:" + tag, len(variants))])

    # 4. 全对时给予正向反馈
    if not error_tags:
        parts.append("整题解答完整规范，继续保持！")

    return "".join(parts)


def feedback_similarity(texts: list) -> float:
    """评语查重：返回任意两条评语间的最大 difflib 相似度（0-1）。

    供上层做「评语去模板化」效果展示：相似度越低说明措辞越分散。
    少于两条评语时返回 0.0。
    """
    best = 0.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            ratio = difflib.SequenceMatcher(None, texts[i] or "", texts[j] or "").ratio()
            if ratio > best:
                best = ratio
    return round(best, 3)


def _build_teacher_note(error_tags: list, status: str) -> str:
    """生成面向教师的备注：说明置信度水平与涉及错因。"""
    if status == "green":
        base = "AI 置信度高，可直接通过或抽查。"
    elif status == "yellow":
        base = "AI 置信度中等，请确认判分与错因是否准确。"
    else:
        base = "AI 置信度低，建议本题人工批改。"
    if error_tags:
        base += "涉及错因：" + "、".join(error_tags) + "。"
    return base


def _apply_overrides(factors: dict, factor_overrides) -> dict:
    """教师审核数据回灌：用外部传入的同名因子覆盖后再计算置信度。

    app.py 会把按题聚合的真实教师通过率作为 teacher_pass_rate 传入，
    形成「教师审核 → 置信度」的数据飞轮（§9.7）。只覆盖同名键，防止
    上层误传未知因子污染公式。
    """
    if factor_overrides:
        for key, value in factor_overrides.items():
            if key in factors:
                factors[key] = float(value)
    return factors


def grade_mock(question: dict, submission: dict, factor_overrides: dict = None) -> dict:
    """规则引擎批改：按 Rubric 逐步判分并聚合结果。"""
    rubric = question["rubric"]
    ann_map = {a["step_id"]: a for a in submission["step_annotations"]}

    step_analysis = []
    total = 0
    error_tags = []
    for step in rubric:
        ann = ann_map.get(step["step_id"], {})
        score = _step_score(ann, step["max_score"])
        total += score
        tag = ann.get("error_tag")
        # 只接受 §6.11 枚举内的错因标签
        if tag and tag not in ERROR_TAGS:
            tag = None
        step_analysis.append({
            "step": step["step"],
            "is_correct": bool(ann.get("is_correct")),
            "score": score,
            "max_score": step["max_score"],
            "error_tag": tag,
            "knowledge_point": step["knowledge_point"],
            "reason": ann.get("reason", ""),
            # 证据链：引用学生原文片段 + 可辨认标记（数据侧逐步补标，缺省容错）
            "evidence": ann.get("evidence", ""),
            "legible": bool(ann.get("legible", True)),
        })
        if tag and tag not in error_tags:
            error_tags.append(tag)

    # 知识点：本题 Rubric 覆盖的全部知识点（去重保序）
    knowledge_points = list(dict.fromkeys(s["knowledge_point"] for s in rubric))

    # 置信度：用 §9.7 公式对数据中预置的因子真实加权计算；
    # 复制一份再覆盖，避免污染内存中的原始 submissions 数据
    factors = _apply_overrides(dict(submission["confidence_factors"]), factor_overrides)
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)

    # 评语 seed：优先作答 ID，保证同错因不同学生措辞不同且可复现
    seed = submission.get("submission_id") or submission.get("student_name", "")

    return {
        "mode": "mock",
        "total_score": total,
        "max_score": question["max_score"],
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": step_analysis,
        "knowledge_points": knowledge_points,
        "error_tags": error_tags,
        "student_feedback": _build_feedback(step_analysis, error_tags, seed),
        "teacher_note": _build_teacher_note(error_tags, status),
    }


def _extract_json(content: str) -> dict:
    """从 LLM 返回文本中提取 JSON（容忍 ```json 代码块包裹）。"""
    text = content.strip()
    if text.startswith("```"):
        # 去掉首行 ```json 与尾部 ```
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def _build_prompt(question: dict, student_text: str) -> str:
    """按 Prompt 1 完整版拼装批改 Prompt。"""
    rubric_lines = "\n".join(
        "- %s（%d 分）" % (s["step"], s["max_score"]) for s in question["rubric"]
    )
    # 标准解题步骤为题库新增字段（字符串列表），旧题目可能缺失，容错为空
    solution_steps = question.get("standard_solution_steps", []) or []
    steps_text = "\n".join(
        "%d. %s" % (i + 1, s) for i, s in enumerate(solution_steps)
    ) or "（未提供，请参考标准答案）"
    return PROMPT_TEMPLATE.format(
        subject=question.get("subject", ""),
        grade=question.get("grade", ""),
        question_type=question.get("question_type", ""),
        question=question["question_text"],
        standard_answer=question["standard_answer"],
        standard_solution_steps=steps_text,
        rubric=rubric_lines,
        student_answer=student_text,
        error_tags="、".join(ERROR_TAGS),
    )


def llm_credentials():
    """文本批改所用的 OpenAI 兼容凭据。

    优先 ZHIPI_LLM_*；未配置时复用多模态凭据 ZHIPI_VLM_*（多模态大模型
    同样支持纯文本对话，保证只配一把 Key 也能跑通完整图片批改链路）。
    无任何密钥返回 None。
    """
    if os.environ.get("ZHIPI_LLM_API_KEY", "").strip():
        return {
            "api_key": os.environ["ZHIPI_LLM_API_KEY"],
            "base_url": os.environ.get("ZHIPI_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            "model": os.environ.get("ZHIPI_LLM_MODEL", "deepseek-chat"),
        }
    if os.environ.get("ZHIPI_VLM_API_KEY", "").strip():
        return {
            "api_key": os.environ["ZHIPI_VLM_API_KEY"],
            "base_url": os.environ.get(
                "ZHIPI_VLM_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
            "model": os.environ.get("ZHIPI_VLM_MODEL", "qwen-vl-max"),
        }
    return None


def llm_credentials_2():
    """双模型交叉验证的第二模型凭据（P2）。

    三个环境变量 ZHIPI_LLM_API_KEY_2 / ZHIPI_LLM_BASE_URL_2 /
    ZHIPI_LLM_MODEL_2 须配齐，否则视为未启用，返回 None。
    """
    api_key = os.environ.get("ZHIPI_LLM_API_KEY_2", "").strip()
    base_url = os.environ.get("ZHIPI_LLM_BASE_URL_2", "").strip()
    model = os.environ.get("ZHIPI_LLM_MODEL_2", "").strip()
    if not (api_key and base_url and model):
        return None
    return {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}


def _llm_timeout() -> int:
    """单次批改调用的超时秒数，可用 ZHIPI_LLM_TIMEOUT 覆盖（默认 30）。

    为什么必须可调：30 秒是按「课堂演示不能久等」定的，但实测中转网关
    与免费通道上，同一模型同一 prompt 的耗时会从 20 秒漂到 60 秒以上。
    批量评测时若仍用 30 秒，会把「网关慢」记成「批改失败」并静默降级
    mock —— 评测报告里的准确率就成了规则引擎的成绩，而不是大模型的。
    故：演示保持 30，跑评测时显式调到 120 以上。
    """
    try:
        return max(5, min(600, int(str(os.environ.get("ZHIPI_LLM_TIMEOUT", "")).strip())))
    except (TypeError, ValueError):
        return 30


def _cross_timeout() -> int:
    """双模型交叉验证的超时秒数，可用 ZHIPI_CROSS_TIMEOUT 覆盖（默认 90）。

    默认值为什么是 90 而不是更短：交叉验证只在配齐第二模型时启用，而
    「配齐了就该真的跑起来」。实测同一中转网关上的候选第二模型单次耗时
    minimax-m3 25-90 秒（中位 62）、glm-5.2 45-65、glm-4.5-flash 49-72、
    grok-4.5 77-91——预算低于最快一次成功（25 秒）时，这个功能不是「偶尔
    拿不到结论」，而是**每次必然 ReadTimeout**，且被 _maybe_cross_check 的
    except 静默吞掉：界面无任何异样，日志无任何报错，配置看起来完全正确。
    早先的缺省 12 秒就是这种状态，白等 12 秒再丢弃结果，比不开还差。
    宁可默认偏慢让人抱怨「黄/红件等得久」（看得见、可调小），也不要默认偏快
    让功能静默失效（看不见）。

    上限 300 而不是 120：调大是使用者对「我愿意等」的明确表达，被静默截断
    会重现同一类问题——设了 180 却仍在 120 秒超时，且无处得知。
    """
    try:
        return max(3, min(300, int(str(os.environ.get("ZHIPI_CROSS_TIMEOUT", "")).strip())))
    except (TypeError, ValueError):
        return 90


# 中转网关的「瞬时上游故障」特征。这些错误与请求本身无关，重试就能过。
#
# 最反直觉的一条是 400 + "API key not valid"：按 HTTP 语义 4xx 不该重试，
# 但实测中转网关会在多个上游 key 之间轮询，轮到失效的那个就把上游的 400
# 原样透出来。同一 payload 连打 10 次全成功、走完整链路 6 次里挂 2 次，
# 说明它取决于轮到哪个上游，而不是我们发了什么。
# 不重试的话，体验者会随机看到「批改失败」，还以为是自己的照片有问题。
_TRANSIENT_UPSTREAM = (
    "api key not valid",        # 网关轮到失效上游 key
    "upstream",                 # 网关自报上游故障
    "no available channel",     # 该模型当前无可用通道
    "rate limit", "too many requests",
    "bad gateway", "service unavailable", "gateway timeout",
)
_GATEWAY_RETRY_MAX = 3          # 首次 + 最多 2 次重试
_GATEWAY_RETRY_SLEEP = 1.2      # 退避基数（秒）：1.2、2.4


def _is_transient_gateway_error(status: int, body: str) -> bool:
    """判断是否为「重试一次就可能过」的网关瞬时故障。

    刻意收窄：只认带上述特征的正文，或 429/502/503/504 这类状态码。
    模型名写错（404 model_not_found）、鉴权彻底失败这类**请求本身有问题**
    的情况不在其中——那种重试只是重复烧钱和时间。
    """
    if status in (429, 502, 503, 504):
        return True
    low = (body or "").lower()
    return any(sig in low for sig in _TRANSIENT_UPSTREAM)


def _call_llm(creds: dict, prompt: str, temperature: float = 0,
              timeout: int = None) -> dict:
    """OpenAI 兼容 chat/completions 调用，返回解析后的 JSON 结果。

    - response_format 强制 JSON 输出（DeepSeek / Qwen 均支持），降低解析失败率；
    - timeout 统一 30 秒：单题批改足够，避免课堂演示场景长时间卡住。
    """
    payload = {
        "model": creds["model"],
        "messages": [
            {"role": "system", "content": "你是一名严谨的初中学科教师，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    tmo = _llm_timeout() if timeout is None else timeout
    last_err = None
    for attempt in range(1, _GATEWAY_RETRY_MAX + 1):
        try:
            resp = requests.post(
                creds["base_url"] + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + creds["api_key"],
                         "Content-Type": "application/json"},
                timeout=tmo,
            )
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError) as exc:
            # 连接层失败也要重试。原先 requests.post 裸在循环里，重试只覆盖了
            # 「拿到了 HTTP 错误响应」这一种，而连接根本没建起来时异常直接穿出去。
            # 实测这个网关在连续几发长请求（第二模型单次 60–90 秒）之后会拒连，
            # 报 SSL UNEXPECTED_EOF_WHILE_READING，0.3 秒就失败——典型的瞬时限连，
            # 隔几秒重试就能过。而交叉验证外层是 except: return None，
            # 不在这里重试的话，它会被无声记成「第二模型没结论」。
            last_err = exc
            if attempt >= _GATEWAY_RETRY_MAX:
                raise
            time.sleep(_GATEWAY_RETRY_SLEEP * (2 ** (attempt - 1)))
            continue
        if resp.status_code < 400:
            return _extract_json(resp.json()["choices"][0]["message"]["content"])

        # 网关的 4xx/5xx 正文里通常写着真正的原因（模型名错、参数不支持、
        # 上游 key 失效……），而 raise_for_status() 只抛一行「400 Bad Request」，
        # 不带正文就完全没法查。
        body = (resp.text or "").strip()
        last_err = requests.HTTPError(
            "%s %s ← %s" % (resp.status_code, resp.reason, body[:400]),
            response=resp)
        if (attempt >= _GATEWAY_RETRY_MAX
                or not _is_transient_gateway_error(resp.status_code, body)):
            raise last_err
        time.sleep(_GATEWAY_RETRY_SLEEP * (2 ** (attempt - 1)))
    raise last_err


def _align_llm_steps(rubric: list, steps_in: list) -> list:
    """将 LLM 返回的 step_analysis 对齐到 Rubric，容忍乱序与名称改写。

    LLM 偶尔会打乱步骤顺序或改写步骤名，按下标硬对齐会整体错位。
    三级策略：步骤名精确匹配 → difflib 模糊匹配（相似度 ≥ 0.6，取最高）
    → 按位置回退；每个返回步骤最多使用一次，仍找不到时给空 dict（判 0 分）。
    """
    names = [str(si.get("step", "") or "") for si in steps_in]
    used = set()
    aligned = [None] * len(rubric)

    # 1) 名称精确匹配
    for idx, step in enumerate(rubric):
        for j, name in enumerate(names):
            if j not in used and name == step["step"]:
                aligned[idx] = steps_in[j]
                used.add(j)
                break

    # 2) 模糊匹配：取相似度最高且 ≥ 0.6 的未用步骤
    for idx, step in enumerate(rubric):
        if aligned[idx] is not None:
            continue
        best_j, best_ratio = -1, 0.0
        for j, name in enumerate(names):
            if j in used or not name:
                continue
            ratio = difflib.SequenceMatcher(None, name, step["step"]).ratio()
            if ratio >= 0.6 and ratio > best_ratio:
                best_j, best_ratio = j, ratio
        if best_j >= 0:
            aligned[idx] = steps_in[best_j]
            used.add(best_j)

    # 3) 位置回退：名称完全对不上时按原下标对应
    for idx in range(len(rubric)):
        if aligned[idx] is None:
            if idx < len(steps_in) and idx not in used:
                aligned[idx] = steps_in[idx]
                used.add(idx)
            else:
                aligned[idx] = {}
    return aligned


def _step_kp(si: dict) -> str:
    """取模型为这一步返回的知识点，并挡掉「拿维度名充数」的回答。

    题库外的 Rubric 是五个评分维度，维度名（题意理解 / 方法选择 / 运算执行
    / 步骤完整 / 结论正确）描述的是**批改的角度**，不是课程知识点。模型
    偷懒时最容易把维度名原样抄回来，抄回来就会进「知识点错误率」——
    看板上出现一行「方法选择 0/1 · 0%」，一眼就是凑数的。
    """
    kp = str(si.get("knowledge_point") or "").strip()
    if not kp or kp in dimensions.ALL_NAMES:
        return ""
    return kp[:20]


def _parse_llm_steps(question: dict, data: dict):
    """把 LLM 返回结果对齐到 Rubric 并做边界校验，返回 (step_analysis, total, error_tags)。

    - 分数 clamp 到 [0, 该步满分]，错因标签只接受 §6.11 枚举；
    - 证据链字段 evidence / legible 缺省容错（空串 / true）；
    - legible=false 的步骤在 reason 前加「〔字迹难辨〕」，提示教师优先人工核对。
    """
    rubric = question["rubric"]
    steps_in = data.get("step_analysis", [])
    if not isinstance(steps_in, list):
        steps_in = []
    steps_in = [si for si in steps_in if isinstance(si, dict)]
    aligned = _align_llm_steps(rubric, steps_in)

    step_analysis = []
    total = 0
    error_tags = []
    for step, si in zip(rubric, aligned):
        score = max(0, min(int(round(float(si.get("score", 0)))), step["max_score"]))
        total += score
        tag = si.get("error_tag")
        if tag not in ERROR_TAGS:
            tag = None
        reason = str(si.get("reason", "") or "").strip()
        evidence = str(si.get("evidence") or "").strip()
        legible = bool(si.get("legible", True))
        # 全对时模型常只给 evidence、省略 reason（旧 Prompt 把 reason 写成
        # 「错误位置与原因」）。有原文证据却无理由时，补一条最小判定说明，
        # 避免 UI 空白，也避免 rubric_coverage 被误打成 0。
        if not reason and evidence:
            if score >= step["max_score"]:
                reason = "该步正确，依据作答原文判定"
            elif score > 0:
                reason = "该步部分正确，依据作答原文判定"
            else:
                reason = "该步未得分，依据作答原文判定"
        # 注意：这里不拼「字迹难辨」前缀——前端已用 s.legible 单独渲染
        # 复核提示（见 app.js step 渲染），前缀只会让无判定依据的步骤
        # 平白多出非空 reason，把 rubric_coverage 虚高。
        entry = {
            "step": step["step"],
            "is_correct": bool(si.get("is_correct", score >= step["max_score"])),
            "score": score,
            "max_score": step["max_score"],
            "error_tag": tag,
            # 题库内：知识点由人工 Rubric 给定，模型无权改。
            # 题库外：Rubric 是维度体系，本身没有知识点（维度名不是知识点），
            # 此时取模型返回的本步知识点；模型没给就留空，由看板跳过——
            # 宁可少一条聚合，也不能把「方法选择」当成一个知识点摆进
            # 「知识点错误率」，那是类目错误，教师一眼就看出来是凑的。
            "knowledge_point": step["knowledge_point"] or _step_kp(si),
            "reason": reason,
            "evidence": evidence,
            "legible": legible,
        }
        # 模型压根没返回这一评分点时，上面会记成 0 分且 reason / evidence 全空。
        # 「模型判了并给 0 分」与「模型没判」在界面上长得一模一样，但含义完全
        # 相反：前者是判分结论，后者是判分缺失。必须单独标出来，否则教师会把
        # 一个空白当成"这一维确实不得分"。
        #
        # 模型没返回任何有实质内容的判分信息时标为 missing。
        # 有两种情形需要覆盖：
        #   1. _align_llm_steps 找不到对应步骤，返回 {} —— `not si` 抓到的就是这个。
        #   2. 模型返回了步骤名但没给评分信息（如 {"step":"步骤完整"}），
        #      `_align_llm_steps` 位置回退会把它放进来，si 是 truthy，
        #      但 score/reason/evidence 全缺失，和完全没返回在效果上没区别。
        # 判据：没有 score/is_correct/reason/evidence/error_tag 任何一项，
        # 就视为判分缺失。注意 score=0 是合法的明确判分（模型说这步得0分），
        # 用 "score" in si 而不是 si.get("score") 来区分「明确给0」与「没给」。
        if not si or not ("score" in si or "is_correct" in si or
                          si.get("reason") or si.get("evidence") or si.get("error_tag")):
            entry["missing"] = True
        # 维度 key 只有维度体系下发的 Rubric 才有（题库内的人工 Rubric 没有）。
        # 教师改分后要按「学科 × 维度」回灌，靠这个字段定位改的是哪一维。
        if step.get("dimension"):
            entry["dimension"] = step["dimension"]
        step_analysis.append(entry)
        if tag and tag not in error_tags:
            error_tags.append(tag)

    # 补充 LLM 在顶层给出的、且在枚举内的错因标签
    for tag in data.get("error_tags", []) or []:
        if tag in ERROR_TAGS and tag not in error_tags:
            error_tags.append(tag)
    return step_analysis, total, error_tags


def consistency_check(question: dict, student_text: str, first_result: dict,
                      creds: dict) -> dict:
    """二次批改一致性检查（Prompt 库 Prompt 6 思路的工程化实现）。

    用相同 Prompt、temperature=0.3 再独立批改一次，程序化比对两次的
    总分与错因标签集合，得百分制一致性分：
        consistency = max(0, 100 - |分差| / 满分 × 200 - 标签对称差个数 × 10)
    该分作为 §9.7「LLM 自检一致性」因子来源，替代模型自报 confidence
    直接覆盖的做法——两次独立结论的吻合度比单次自我评估更可信。
    """
    data = _call_llm(creds, _build_prompt(question, student_text), temperature=0.3)
    _, second_score, second_tags = _parse_llm_steps(question, data)

    first_score = float(first_result.get("total_score", 0))
    max_score = float(first_result.get("max_score") or question["max_score"]) or 1.0
    tag_diff = len(set(first_result.get("error_tags", [])) ^ set(second_tags))
    consistency = max(0.0, 100.0 - abs(first_score - second_score) / max_score * 200.0
                      - tag_diff * 10.0)
    return {"second_score": second_score, "agreement": round(consistency, 1)}


def _maybe_consistency(question: dict, student_text: str, first_result: dict, creds):
    """按开关执行二次批改；关闭或异常时返回 None，走自报 confidence 回退。

    环境变量 ZHIPI_DOUBLE_CHECK 缺省启用，显式设为 "0" 时关闭（可省一半
    调用量）；二次调用任何异常都静默跳过，保证 Demo 演示不中断。
    """
    if os.environ.get("ZHIPI_DOUBLE_CHECK", "1").strip() == "0" or not creds:
        return None
    try:
        return consistency_check(question, student_text, first_result, creds)
    except Exception:
        return None


def _cross_agreement(gap: float, max_score: float) -> float:
    """把双模型的总分差换算成 0-100 的一致性分（§9.7 第四因子）。

    与二次批改一致性用同一条斜率（分差占满分每 1% 扣 2 分），两个「一致性」
    因子的刻度才可比：满分差 0 得 100，差满分的 15% 得 70（正是分歧阈值），
    差满分一半即归零。刻意不掺错因标签差异——跨厂商模型的标签用词本就不同，
    把措辞差异算进去会把分歧率抬成噪声。
    """
    ceiling = float(max_score) or 1.0
    return round(max(0.0, 100.0 - abs(float(gap)) / ceiling * 200.0), 1)


def cross_check(question: dict, student_text: str, first_total, creds2: dict = None,
                timeout: int = None):
    """双模型交叉验证：用第二模型独立批改一次并与首轮总分比对。

    产出两样东西：`agreement` 作为置信度第四因子并入加权（换一家模型仍判同
    一个分，是同模型两次自评给不出的独立佐证）；分差超过满分 15%
    （等价于 agreement < 70）时 `escalated` 置位，上层据此把分流强制转红交
    人工，避免单一模型的系统性误判被高一致性掩盖。

    第二模型凭据未配齐时返回 None，该因子留空按权重重归一化剔除。
    timeout 缺省走主批改超时；由 _maybe_cross_check 传入更短的值。
    """
    if creds2 is None:
        creds2 = llm_credentials_2()
    if not creds2:
        return None
    data = _call_llm(creds2, _build_prompt(question, student_text), timeout=timeout)
    _, second_total, _ = _parse_llm_steps(question, data)
    gap = abs(float(first_total) - second_total)
    return {
        "model2": creds2["model"],
        "model2_score": second_total,
        "gap": round(gap, 1),
        "agreement": _cross_agreement(gap, question["max_score"]),
        "escalated": gap > question["max_score"] * 0.15,
    }


def _maybe_cross_check(question: dict, student_text: str, first_total, status: str):
    """黄 / 红结果才触发交叉验证（绿区无需复核，省调用）；异常静默跳过。

    超时单独收紧（ZHIPI_CROSS_TIMEOUT，默认 12 秒）：交叉验证是**增强项**，
    拿不到结论只是少一份佐证，主批改结果照样可用。而它用的第二模型往往是
    另一家、稳定性未知——实测 grok-4.5 会静默挂 30 秒才失败，把一次
    5 秒的批改拖成 35 秒。让可选环节按主流程的超时等待，是把增强项的
    不确定性转嫁给了核心链路。
    """
    if status not in ("yellow", "red"):
        return None
    creds2 = llm_credentials_2()
    if not creds2:
        return None
    try:
        # 超时按参数传，不改环境变量：uvicorn 的同步端点跑在线程池里，
        # 改 os.environ 会被并发请求互相覆盖。
        return cross_check(question, student_text, first_total, creds2,
                           timeout=_cross_timeout())
    except Exception:
        return None


def _settle_cross(question: dict, student_text: str, total, factors: dict,
                  confidence: float, status: str):
    """跑双模型交叉验证并把一致性分并回置信度，返回五元组。

    返回 (factors, confidence, status, cross_res, prelim)；未触发时原样返回，
    cross_res 与 prelim 为 None。

    为什么必须分两段算：交叉验证只在黄 / 红件上跑，而「是不是黄 / 红」本身
    要由置信度决定——两者互为前提。解法是先用四项可测因子算一次**初评**，
    据此决定要不要调第二模型；拿到一致性分后把它写回交叉验证因子，整体**复评**
    一次。绿件永远走不到第二段，交叉验证因子留空按权重重归一化剔除，判定口径
    与旧的四因子完全一致（见 confidence.WEIGHTS 的取值说明）。

    复评可能把黄件抬成绿件——两个独立模型判出同一个分，本就是比单模型
    自评更硬的证据，这正是引入该因子的意义。但 escalated 一票否决：分差
    显著时无论复评多少分都强制转红，避免「一致性 0 分把总分拉低还不够转红」
    这种由加权决定的漏网。
    """
    cross_res = _maybe_cross_check(question, student_text, total, status)
    if cross_res is None:
        return factors, confidence, status, None, None

    prelim = {"confidence": confidence, "status": status}
    factors = dict(factors)
    factors["cross_model_agreement"] = cross_res["agreement"]
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)
    if cross_res.get("escalated"):
        status = "red"
    return factors, confidence, status, cross_res, prelim


def grade_llm(question: dict, submission: dict, student_text: str,
              factor_overrides: dict = None) -> dict:
    """真实 LLM 批改分支（OpenAI 兼容 chat/completions）。

    凭据经 llm_credentials() 解析（ZHIPI_LLM_* 优先，缺省复用 ZHIPI_VLM_*）。
    任何异常都向上抛出，由 grade() 捕获后降级为 mock。
    """
    creds = llm_credentials()
    if not creds:
        raise RuntimeError("未配置任何 LLM 凭据")

    data = _call_llm(creds, _build_prompt(question, student_text))
    step_analysis, total, error_tags = _parse_llm_steps(question, data)
    knowledge_points = list(dict.fromkeys(s["knowledge_point"] for s in question["rubric"]))

    # 置信度仍按 §9.7 公式计算；「LLM 自检一致性」因子优先取二次批改一致性分
    factors = dict(submission["confidence_factors"])
    first_result = {"total_score": total, "error_tags": error_tags,
                    "max_score": question["max_score"]}
    consistency_res = _maybe_consistency(question, student_text, first_result, creds)
    if consistency_res is not None:
        factors["llm_self_consistency"] = consistency_res["agreement"]
    else:
        # 回退：二次批改被禁用或失败时，才用模型自报 confidence
        llm_conf = data.get("confidence")
        if isinstance(llm_conf, (int, float)):
            factors["llm_self_consistency"] = float(llm_conf) * 100 if llm_conf <= 1 else float(llm_conf)
    factors = _apply_overrides(factors, factor_overrides)
    # 交叉验证因子先留空：要等初评分流决定跑不跑，未跑就按权重重归一化剔除
    factors.setdefault("cross_model_agreement", None)
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)

    # 双模型交叉验证：黄 / 红初评用第二模型复核，一致性分并入该因子后复评；
    # 分差显著时一票否决强制转红交人工
    factors, confidence, status, cross_res, prelim = _settle_cross(
        question, student_text, total, factors, confidence, status)

    seed = submission.get("submission_id") or submission.get("student_name", "")
    result = {
        "mode": "llm",
        "total_score": total,
        "max_score": question["max_score"],
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": step_analysis,
        "knowledge_points": knowledge_points,
        "error_tags": error_tags,
        "student_feedback": data.get("student_feedback") or _build_feedback(step_analysis, error_tags, seed),
        "teacher_note": data.get("teacher_note") or _build_teacher_note(error_tags, status),
    }
    if consistency_res is not None:
        result["consistency_check"] = consistency_res
    if cross_res is not None:
        result["cross_check"] = cross_res
        # 初评值透出去，教师才看得见「这份是被交叉验证改判的」
        result["confidence_preliminary"] = prelim["confidence"]
        result["status_preliminary"] = prelim["status"]
    return result


def grade(question: dict, submission: dict, factor_overrides: dict = None) -> dict:
    """批改入口：先转写，再按模式批改。

    默认走 mock 规则引擎；若配置了 ZHIPI_LLM_API_KEY 则优先尝试真实 LLM，
    失败时自动降级为 mock 并在结果 note 中说明原因。
    factor_overrides 用于回灌真实教师数据（如按题聚合的教师通过率）。
    """
    ocr_result = ocr_mod.mock_ocr(submission)

    result = None
    llm_error = None
    if llm_credentials():
        try:
            result = grade_llm(question, submission, ocr_result["text"], factor_overrides)
        except Exception as exc:  # 任何失败都降级，保证 Demo 始终可跑
            llm_error = str(exc)
            result = None

    if result is None:
        result = grade_mock(question, submission, factor_overrides)
        if llm_error:
            result["note"] = "LLM 调用失败，已自动降级为 mock：" + llm_error

    # 附加转写信息，供前端与学情模块使用
    result["ocr_text"] = ocr_result["text"]
    result["ocr_clarity"] = ocr_result["clarity"]
    return result


# ---------- 任意上传作业的临时批改（图片链路专用） ----------


def _step_has_judgment(step: dict) -> bool:
    """一步是否具备可解释的判定依据。

    Rubric 覆盖度度量的是「评分点有没有被真正判到」，不是「有没有写出错因」。
    全对步骤常被模型省略 reason（Prompt 旧口径把 reason 写成错误说明），
    但仍可能给出 evidence。因此 reason 或 evidence 任一非空即视为已覆盖；
    二者皆空才算该评分点没覆盖全。
    """
    reason = str(step.get("reason") or "").strip()
    evidence = str(step.get("evidence") or "").strip()
    return bool(reason or evidence)


def derive_factors(question: dict, student_text: str, clarity: float,
                   step_analysis: list, llm_conf) -> dict:
    """为无预置标注的上传作答推导五个置信度因子。

    - ocr_clarity        识别引擎给出的卷面清晰度；
    - rubric_coverage    具备判定依据（reason 或 evidence）的步骤占比；
    - llm_self_consistency  二次批改一致性（同一模型独立批两次的比较分）；
                            冷启动回退值 60，二次批改完成后被覆盖；
    - cross_model_agreement 双模型交叉验证一致性。这里恒为 None——它只在
                            初评落黄 / 红后才调第二模型，此刻还没算出分流，
                            由 _settle_cross 在复评阶段回填；始终为 None 时
                            按权重重归一化剔除，不当 0 分白扣；
    - teacher_pass_rate  冷启动默认 80（无历史数据）。

    「答案匹配度」已移除：该因子算法假设存在简短终答形式的标准答案，
    题库外作业没有，比对出来的是噪声。
    """
    covered = sum(1 for s in step_analysis if _step_has_judgment(s))
    coverage = round(covered / len(step_analysis) * 100, 1) if step_analysis else 0.0
    if isinstance(llm_conf, (int, float)):
        self_consistency = float(llm_conf) * 100 if llm_conf <= 1 else float(llm_conf)
    else:
        self_consistency = 60.0

    return {
        "ocr_clarity": round(float(clarity), 1),
        "rubric_coverage": coverage,
        "llm_self_consistency": round(self_consistency, 1),
        "cross_model_agreement": None,
        "teacher_pass_rate": 80.0,
    }


def grade_adhoc(question: dict, student_text: str, clarity: float,
                factor_overrides: dict = None,
                dimension_bias: dict = None) -> dict:
    """批改一份「任意上传」的作答文本（无预置步骤标注）。

    必须有 LLM 凭据（ZHIPI_LLM_* 或 ZHIPI_VLM_*）；置信度因子按
    derive_factors 冷启动推导，其余输出结构与 grade() 完全一致。

    dimension_bias：该学科各维度的教师平均修正量（正=模型偏严，负=偏松）。
    仅在题库外的维度体系批改时生效，作为 Prompt 里的一段提示写入。
    刻意**不**用它去自动加减分——那会让教师看到的分数不再是模型的真实判断，
    出错时无从追溯；只提示模型「你在这一维历史上偏严/偏松」，判分仍由本次逻辑决定。
    """
    creds = llm_credentials()
    if not creds:
        raise RuntimeError(
            "临时批改需要配置 ZHIPI_LLM_API_KEY 或 ZHIPI_VLM_API_KEY")

    # 题库外的题走维度体系 Prompt：没有人工标准答案，让模型先自解再按固定维度判分。
    # 判据是 question["open"]，由调用方在构造题目记录时置位——不靠「有没有
    # standard_answer」猜，那样一道恰好没填标准答案的题库内题会被误走这条路。
    is_open = bool(question.get("open"))
    prompt = _build_open_prompt(question, student_text, dimension_bias) if is_open \
        else _build_prompt(question, student_text)
    data = _call_llm(creds, prompt)
    step_analysis, total, error_tags = _parse_llm_steps(question, data)

    factors = derive_factors(question, student_text, clarity,
                             step_analysis, data.get("confidence"))
    # 「LLM 自检一致性」优先取二次批改一致性分；被禁用 / 失败时保留自报回退值
    first_result = {"total_score": total, "error_tags": error_tags,
                    "max_score": question["max_score"]}
    consistency_res = _maybe_consistency(question, student_text, first_result, creds)
    if consistency_res is not None:
        factors["llm_self_consistency"] = consistency_res["agreement"]
    factors = _apply_overrides(factors, factor_overrides)
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)

    # 黄 / 红初评触发双模型交叉验证，一致性分并入该因子后复评；分差显著强制转红
    factors, confidence, status, cross_res, prelim = _settle_cross(
        question, student_text, total, factors, confidence, status)

    # 临时批改无 submission_id，用作答文本本身作确定性评语 seed
    seed = student_text
    result = {
        "mode": "llm",
        "total_score": total,
        "max_score": question["max_score"],
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": step_analysis,
        "knowledge_points": list(dict.fromkeys(
            kp for kp in (str(s.get("knowledge_point") or "").strip()
                          for s in step_analysis) if kp)),
        "error_tags": error_tags,
        "student_feedback": data.get("student_feedback") or _build_feedback(step_analysis, error_tags, seed),
        "teacher_note": data.get("teacher_note") or _build_teacher_note(error_tags, status),
        "ocr_text": student_text,
        "ocr_clarity": round(float(clarity), 1),
    }
    if consistency_res is not None:
        result["consistency_check"] = consistency_res
    if cross_res is not None:
        result["cross_check"] = cross_res
        result["confidence_preliminary"] = prelim["confidence"]
        result["status_preliminary"] = prelim["status"]
    if is_open:
        # 判分基准要透出去。题库外的题没有人工标准答案，模型是拿自己的解法
        # 当基准的——不展示的话，教师无从判断「基准本身是不是错的」，
        # 而基准一错就会把正确作答判成错，且证据链看起来毫无异样。
        result["reference_answer"] = str(data.get("reference_answer") or "").strip()
        result["score_basis"] = "system"    # 分数口径：体系判别分，非试卷实际分值
        result["dimension_scores"] = [
            {"dimension": s.get("dimension"), "name": s["step"],
             "score": s["score"], "max_score": s["max_score"]}
            for s in step_analysis
        ]
    return result
