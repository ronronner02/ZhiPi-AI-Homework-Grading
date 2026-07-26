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
import re

import requests

from . import ocr as ocr_mod
from . import confidence as conf

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
2. step_analysis 逐项对应 Rubric，且每步 step 名称必须与 Rubric 步骤同名；
3. 必须指出具体错误步骤及错误位置；
4. 错因标签必须从【可选错因标签】中选择，不得自造标签；
5. 每一步都必须给出 evidence：引用学生作答中的原文片段作为判断依据；
   学生未写出对应内容时 evidence 置为空字符串 ""。

【防幻觉与降级约束】
6. 只能依据学生实际写出的内容评判，不得臆造、补全学生未写出的步骤或结论；
7. 若某步内容无法辨认或存在歧义，将该步 legible 置为 false，如实说明
   「无法辨认 / 存在歧义」，不要猜测其含义，并降低整体 confidence；
8. 若证据不足以判定对错，宁可降低 confidence 交人工，也不得强行给出结论；
9. 只输出 JSON，不要输出多余文字。

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
      "error_tag": "错因标签或null", "reason": "错误位置与原因",
      "evidence": "引用学生作答原文片段，无则为空字符串", "legible": true/false}}
  ],
  "knowledge_points": [],
  "error_tags": [],
  "student_feedback": "面向学生的个性化评语",
  "teacher_note": "面向教师的备注",
  "confidence": 0到1之间的小数
}}"""


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


def _call_llm(creds: dict, prompt: str, temperature: float = 0) -> dict:
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
    resp = requests.post(
        creds["base_url"] + "/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer " + creds["api_key"],
                 "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return _extract_json(resp.json()["choices"][0]["message"]["content"])


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
        reason = si.get("reason", "") or ""
        legible = bool(si.get("legible", True))
        if not legible:
            reason = "〔字迹难辨〕" + reason
        step_analysis.append({
            "step": step["step"],
            "is_correct": bool(si.get("is_correct", score >= step["max_score"])),
            "score": score,
            "max_score": step["max_score"],
            "error_tag": tag,
            "knowledge_point": step["knowledge_point"],
            "reason": reason,
            "evidence": str(si.get("evidence") or ""),
            "legible": legible,
        })
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


def cross_check(question: dict, student_text: str, first_total, creds2: dict = None):
    """双模型交叉验证（P2）：用第二模型独立批改一次并与首轮总分比对。

    分差超过满分 15% 视为分歧显著（escalated），上层据此把分流强制转红
    交人工，避免单一模型的系统性误判。第二模型凭据未配齐时返回 None。
    """
    if creds2 is None:
        creds2 = llm_credentials_2()
    if not creds2:
        return None
    data = _call_llm(creds2, _build_prompt(question, student_text))
    _, second_total, _ = _parse_llm_steps(question, data)
    gap = abs(float(first_total) - second_total)
    return {
        "model2": creds2["model"],
        "model2_score": second_total,
        "gap": round(gap, 1),
        "escalated": gap > question["max_score"] * 0.15,
    }


def _maybe_cross_check(question: dict, student_text: str, first_total, status: str):
    """黄 / 红结果才触发交叉验证（绿区无需复核，省调用）；异常静默跳过。"""
    if status not in ("yellow", "red"):
        return None
    creds2 = llm_credentials_2()
    if not creds2:
        return None
    try:
        return cross_check(question, student_text, first_total, creds2)
    except Exception:
        return None


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
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)

    # 双模型交叉验证：黄 / 红结果用第二模型复核，分歧显著时强制转红交人工
    cross_res = _maybe_cross_check(question, student_text, total, status)
    if cross_res and cross_res.get("escalated"):
        status = "red"

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

# 数值与常见单位提取：单位按「长单位优先」排列，避免 m/s 被拆成 m 与 s；
# 结尾负向断言防止把单词前缀误认成单位（如 minutes 里的 min / m）
_NUM_UNIT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*"
    r"(km/h|km/s|m/s²|m/s2|m/s|cm/s|mm|cm|dm|km|kg|mg|mL|ml|"
    r"kPa|Pa|kN|kJ|kW|Hz|min|°C|℃|N|J|W|V|A|Ω|L|h|m|s|g)?"
    r"(?![A-Za-z])"
)


def _extract_nums_units(text: str):
    """从文本抽取（数值集合, 紧跟数值的单位集合），用于归一化比对。"""
    nums, units = set(), set()
    for num, unit in _NUM_UNIT_RE.findall(text or ""):
        nums.add(float(num))
        if unit:
            units.add(unit)
    return nums, units


def _answer_match_score(student_text: str, standard_answer: str) -> float:
    """粗粒度答案匹配度（0-100）：终答数值 + 单位归一比对，字符相似度兜底。

    口径（详见技术说明文档 §6.3）：本因子度量「学生终答与标准答案的
    可比对程度」，服务于置信度评估，并非判分本身。比对规则：
    1. 数值一致性 = 标准答案的最终数值出现在学生终答中，且学生终答的
       数值都在标准答案数值集合内（容忍学生只写最终结果、省略中间量）；
    2. 数值一致且单位一致 → 100；数值一致但单位缺失/不一致 → 70
       （单位分歧是明确可判信号，不允许高于正确终答的匹配度）；
    3. 数值不一致 → 序列相似度 × 100 且封顶 60（明确的数值分歧
       不允许伪装成高匹配度）；
    4. 双方均无数值（如作文题）→ 序列相似度 × 100（作文与评分标准
       描述天然相似度低，倾向交教师复核，符合冷启动保守原则）。
    """
    lines = [ln.strip() for ln in student_text.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    std = standard_answer or ""

    s_nums, s_units = _extract_nums_units(tail)
    a_matches = _NUM_UNIT_RE.findall(std)
    a_nums = {float(n) for n, _ in a_matches}
    a_units = {u for _, u in a_matches if u}
    final_num = float(a_matches[-1][0]) if a_matches else None

    ratio = difflib.SequenceMatcher(None, tail, std).ratio()

    if s_nums and a_nums:
        nums_consistent = final_num in s_nums and s_nums.issubset(a_nums)
        if nums_consistent:
            units_ok = s_units.issubset(a_units) if a_units else not s_units
            return 100.0 if (units_ok and (s_units or not a_units)) else 70.0
        return round(min(60.0, ratio * 100), 1)

    return round(ratio * 100, 1)


def derive_factors(question: dict, student_text: str, clarity: float,
                   step_analysis: list, llm_conf) -> dict:
    """为无预置标注的上传作答推导 §9.7 五个置信度因子。

    - ocr_clarity        识别引擎给出的卷面清晰度；
    - answer_match       最终答案与标准答案的数值 + 单位归一匹配度；
    - rubric_coverage    批改模型给出判定理由的步骤占比；
    - llm_self_consistency  批改模型自评置信度（冷启动回退值；启用二次
      批改时会被两次批改的一致性分覆盖）；
    - teacher_pass_rate  冷启动默认 80（无历史数据）。
    """
    covered = sum(1 for s in step_analysis if s.get("reason"))
    coverage = round(covered / len(step_analysis) * 100, 1) if step_analysis else 0.0
    if isinstance(llm_conf, (int, float)):
        self_consistency = float(llm_conf) * 100 if llm_conf <= 1 else float(llm_conf)
    else:
        self_consistency = 60.0
    return {
        "ocr_clarity": round(float(clarity), 1),
        "answer_match": _answer_match_score(student_text, question.get("standard_answer", "")),
        "rubric_coverage": coverage,
        "llm_self_consistency": round(self_consistency, 1),
        "teacher_pass_rate": 80.0,
    }


def grade_adhoc(question: dict, student_text: str, clarity: float,
                factor_overrides: dict = None) -> dict:
    """批改一份「任意上传」的作答文本（无预置步骤标注）。

    必须有 LLM 凭据（ZHIPI_LLM_* 或 ZHIPI_VLM_*）；置信度因子按
    derive_factors 冷启动推导，其余输出结构与 grade() 完全一致。
    """
    creds = llm_credentials()
    if not creds:
        raise RuntimeError(
            "临时批改需要配置 ZHIPI_LLM_API_KEY 或 ZHIPI_VLM_API_KEY")

    data = _call_llm(creds, _build_prompt(question, student_text))
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

    # 黄 / 红结果触发双模型交叉验证，分歧显著时强制转红交人工
    cross_res = _maybe_cross_check(question, student_text, total, status)
    if cross_res and cross_res.get("escalated"):
        status = "red"

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
        "knowledge_points": list(dict.fromkeys(s["knowledge_point"] for s in question["rubric"])),
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
    return result
