"""过程级批改模块：Rubric 逐步判分。

两种模式：
- mock（默认）：规则引擎。读取每份作答「每步」的预标注（对 / 错、部分分比例、
  错因标签、判断依据），按 Rubric 逐步计算得分，聚合总分、错因与知识点，
  再用规则模板生成个性化评语与教师备注。全程确定性，不使用任何随机数。
- llm（可选）：若配置了环境变量 ZHIPI_LLM_API_KEY，则按详细设计方案 §15.1
  的批改 Prompt，让 OpenAI 兼容大模型按 Rubric 逐步批改并返回 JSON。
  未配置或调用 / 解析失败时，自动降级为 mock，并在结果中标注 mode = "mock"。

对应总体流程（§7.1）中的「LLM 过程级批改 → 错因标签生成」环节。
"""
import os
import json

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

# 错因标签 → 可执行改进建议（规则模板，用于拼装个性化评语）
ADVICE = {
    "概念理解错误": "先回顾相关概念与定义，弄懂原理后再动笔。",
    "公式选择错误": "解题前先确认应使用的公式，并注意公式的适用条件。",
    "步骤缺失": "解题步骤要写完整，避免跳步导致失分。",
    "计算错误": "计算时放慢速度，算完把结果代回原式检验。",
    "单位错误": "代入数值前先统一单位，最终结果记得写对单位。",
    "审题错误": "读题时圈出已知量与所求量，避免看错条件。",
    "图形理解错误": "结合图形逐一标注已知条件，再列式求解。",
    "符号书写错误": "注意规范书写符号与拼写，避免因笔误失分。",
    "表达不完整": "把结论写完整，所有答案都要清晰表述出来。",
    "答案正确但过程不规范": "答案正确，但要注意书写过程的规范性。",
}

# §15.1 批改 Prompt 模板（真实 LLM 模式使用）
PROMPT_TEMPLATE = """你是一名严谨的初中学科教师，请根据题目、标准答案、评分规则和学生作答进行批改。

要求：
1. 必须按照评分规则逐步评分；
2. 不允许只根据最终答案判断；
3. 必须指出错误步骤；
4. 必须从给定错因标签中选择；
5. 如果无法确定，请降低置信度；
6. 只输出 JSON，不要输出多余文字。

题目：
{question}

标准答案：
{standard_answer}

评分规则（每步及其满分）：
{rubric}

学生作答（OCR 转写）：
{student_answer}

可选错因标签（只能从中选择）：
{error_tags}

请输出如下 JSON：
{{
  "score": 总分,
  "max_score": 满分,
  "step_analysis": [
    {{"step": "步骤名称", "is_correct": true/false, "score": 该步得分, "error_tag": "错因标签或null", "reason": "原因"}}
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


def _build_feedback(step_analysis: list, error_tags: list) -> str:
    """规则模板生成个性化评语（§15.2 思路）：先肯定，再指出关键问题，最后给建议。"""
    correct_steps = [s["step"] for s in step_analysis if s["is_correct"]]
    wrong_steps = [s for s in step_analysis if not s["is_correct"]]
    parts = []

    # 1. 先肯定已掌握的部分
    if correct_steps:
        parts.append("你在" + "、".join(correct_steps) + "方面是正确的，说明基本思路没有问题。")
    else:
        parts.append("本题整体完成度偏低，我们一起把基础一步步补上来。")

    # 2. 指出最关键的问题（取扣分最多的错误步骤）
    if wrong_steps:
        key = max(wrong_steps, key=lambda s: s["max_score"] - s["score"])
        if key.get("error_tag"):
            parts.append("本次主要问题出在「%s」，属于%s。" % (key["step"], key["error_tag"]))
        else:
            parts.append("本次主要问题出在「%s」。" % key["step"])

    # 3. 针对每个错因给出可执行建议（去重，最多两条）
    for tag in error_tags[:2]:
        if tag in ADVICE:
            parts.append(ADVICE[tag])

    # 4. 全对时给予正向反馈
    if not error_tags:
        parts.append("整题解答完整规范，继续保持！")

    return "".join(parts)


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


def grade_mock(question: dict, submission: dict) -> dict:
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
        })
        if tag and tag not in error_tags:
            error_tags.append(tag)

    # 知识点：本题 Rubric 覆盖的全部知识点（去重保序）
    knowledge_points = list(dict.fromkeys(s["knowledge_point"] for s in rubric))

    # 置信度：用 §9.7 公式对数据中预置的因子真实加权计算
    factors = submission["confidence_factors"]
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)

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
        "student_feedback": _build_feedback(step_analysis, error_tags),
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
    """按 §15.1 拼装批改 Prompt。"""
    rubric_lines = "\n".join(
        "- %s（%d 分）" % (s["step"], s["max_score"]) for s in question["rubric"]
    )
    return PROMPT_TEMPLATE.format(
        question=question["question_text"],
        standard_answer=question["standard_answer"],
        rubric=rubric_lines,
        student_answer=student_text,
        error_tags="、".join(ERROR_TAGS),
    )


def grade_llm(question: dict, submission: dict, student_text: str) -> dict:
    """真实 LLM 批改分支（OpenAI 兼容 chat/completions）。

    读取环境变量：
        ZHIPI_LLM_API_KEY   接口密钥（必填，未配置则不会走本分支）
        ZHIPI_LLM_BASE_URL  接口地址，默认 https://api.deepseek.com
        ZHIPI_LLM_MODEL     模型名，默认 deepseek-chat
    任何异常都向上抛出，由 grade() 捕获后降级为 mock。
    """
    api_key = os.environ["ZHIPI_LLM_API_KEY"]
    base_url = os.environ.get("ZHIPI_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("ZHIPI_LLM_MODEL", "deepseek-chat")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一名严谨的初中学科教师，只输出 JSON。"},
            {"role": "user", "content": _build_prompt(question, student_text)},
        ],
        "temperature": 0,
        "stream": False,
    }
    resp = requests.post(
        base_url + "/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)

    # 将 LLM 返回逐步对齐到本题 Rubric，并做边界校验
    rubric = question["rubric"]
    steps_in = data.get("step_analysis", [])
    step_analysis = []
    total = 0
    error_tags = []
    for i, step in enumerate(rubric):
        si = steps_in[i] if i < len(steps_in) else {}
        score = max(0, min(int(round(float(si.get("score", 0)))), step["max_score"]))
        total += score
        tag = si.get("error_tag")
        if tag not in ERROR_TAGS:
            tag = None
        step_analysis.append({
            "step": step["step"],
            "is_correct": bool(si.get("is_correct", score >= step["max_score"])),
            "score": score,
            "max_score": step["max_score"],
            "error_tag": tag,
            "knowledge_point": step["knowledge_point"],
            "reason": si.get("reason", ""),
        })
        if tag and tag not in error_tags:
            error_tags.append(tag)

    # 补充 LLM 在顶层给出的、且在枚举内的错因标签
    for tag in data.get("error_tags", []):
        if tag in ERROR_TAGS and tag not in error_tags:
            error_tags.append(tag)

    knowledge_points = list(dict.fromkeys(s["knowledge_point"] for s in rubric))

    # 置信度仍按 §9.7 公式计算；用 LLM 自检置信度覆盖「LLM 自检一致性」因子
    factors = dict(submission["confidence_factors"])
    llm_conf = data.get("confidence")
    if isinstance(llm_conf, (int, float)):
        factors["llm_self_consistency"] = float(llm_conf) * 100 if llm_conf <= 1 else float(llm_conf)
    confidence = conf.compute_confidence(factors)
    status = conf.route(confidence)

    return {
        "mode": "llm",
        "total_score": total,
        "max_score": question["max_score"],
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": step_analysis,
        "knowledge_points": knowledge_points,
        "error_tags": error_tags,
        "student_feedback": data.get("student_feedback") or _build_feedback(step_analysis, error_tags),
        "teacher_note": data.get("teacher_note") or _build_teacher_note(error_tags, status),
    }


def grade(question: dict, submission: dict) -> dict:
    """批改入口：先转写，再按模式批改。

    默认走 mock 规则引擎；若配置了 ZHIPI_LLM_API_KEY 则优先尝试真实 LLM，
    失败时自动降级为 mock 并在结果 note 中说明原因。
    """
    ocr_result = ocr_mod.mock_ocr(submission)

    result = None
    llm_error = None
    if os.environ.get("ZHIPI_LLM_API_KEY"):
        try:
            result = grade_llm(question, submission, ocr_result["text"])
        except Exception as exc:  # 任何失败都降级，保证 Demo 始终可跑
            llm_error = str(exc)
            result = None

    if result is None:
        result = grade_mock(question, submission)
        if llm_error:
            result["note"] = "LLM 调用失败，已自动降级为 mock：" + llm_error

    # 附加转写信息，供前端与学情模块使用
    result["ocr_text"] = ocr_result["text"]
    result["ocr_clarity"] = ocr_result["clarity"]
    return result
