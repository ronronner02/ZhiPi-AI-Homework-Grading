from __future__ import annotations

from typing import Iterable


ERROR_TAGS = (
    "概念理解错误",
    "公式选择错误",
    "计算错误",
    "单位错误",
    "审题错误",
    "步骤缺失",
    "符号书写错误",
    "表达不完整",
    "图形理解错误",
    "知识点遗漏",
)

WEIGHTS = {
    "ocr_clarity": 0.25,
    "answer_match": 0.25,
    "rubric_coverage": 0.20,
    "llm_self_consistency": 0.20,
    "teacher_pass_rate": 0.10,
}


def compute_confidence(factors: dict) -> float:
    weighted = 0.0
    used = 0.0
    for key, weight in WEIGHTS.items():
        value = factors.get(key)
        if value is None:
            continue
        value = max(0.0, min(100.0, float(value)))
        weighted += value * weight
        used += weight
    return round(weighted / used, 1) if used else 0.0


def route(confidence: float) -> str:
    if confidence >= 85:
        return "green"
    if confidence >= 60:
        return "yellow"
    return "red"


def _step_score(annotation: dict, max_score: float) -> float:
    if annotation.get("is_correct"):
        return float(max_score)
    try:
        ratio = float(annotation.get("partial_ratio", 0))
    except (TypeError, ValueError):
        ratio = 0
    return round(max_score * max(0.0, min(1.0, ratio)), 1)


def grade_seed(question: dict, submission: dict) -> dict:
    annotation_map = {
        int(item.get("step_id", -1)): item for item in submission.get("step_annotations", [])
    }
    steps = []
    tags = []
    total = 0.0
    for rubric in question.get("rubric", []):
        annotation = annotation_map.get(int(rubric.get("step_id", -1)), {})
        score = _step_score(annotation, float(rubric.get("max_score", 0)))
        tag = annotation.get("error_tag")
        if tag not in ERROR_TAGS:
            tag = None
        if tag and tag not in tags:
            tags.append(tag)
        total += score
        steps.append(
            {
                "step": rubric.get("step", "评分步骤"),
                "is_correct": bool(annotation.get("is_correct")),
                "score": score,
                "max_score": float(rubric.get("max_score", 0)),
                "error_tag": tag,
                "knowledge_point": rubric.get("knowledge_point", ""),
                "reason": annotation.get("reason", ""),
                "evidence": annotation.get("evidence", ""),
                "legible": bool(annotation.get("legible", True)),
            }
        )
    factors = dict(submission.get("confidence_factors") or {})
    confidence = compute_confidence(factors)
    status = route(confidence)
    max_score = float(question.get("max_score", 0))
    if tags:
        feedback = "已识别主要问题：%s。请结合证据逐步订正。" % "、".join(tags[:2])
    else:
        feedback = "解答过程完整，关键步骤与最终结论均正确。"
    return {
        "mode": "mock",
        "total_score": round(total, 1),
        "max_score": max_score,
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": steps,
        "knowledge_points": list(dict.fromkeys(question.get("knowledge_points") or [])),
        "error_tags": tags,
        "student_feedback": feedback,
        "teacher_note": {
            "green": "AI 置信度高，可抽查后通过。",
            "yellow": "AI 置信度中等，请教师确认判分与错因。",
            "red": "AI 置信度低，请教师人工终审。",
        }[status],
    }


def manual_result(question: dict | None, transcript: str, clarity: float = 0) -> dict:
    """未配置模型时不伪造分数，明确转入教师终审。"""
    rubric = (question or {}).get("rubric") or [
        {"step": "教师人工判定", "max_score": 0, "knowledge_point": ""}
    ]
    steps = [
        {
            "step": item.get("step", "教师人工判定"),
            "is_correct": False,
            "score": 0.0,
            "max_score": float(item.get("max_score", 0)),
            "error_tag": None,
            "knowledge_point": item.get("knowledge_point", ""),
            "reason": "未配置真实批改模型，系统未自动判分。",
            "evidence": transcript[:240],
            "legible": clarity >= 60,
        }
        for item in rubric
    ]
    return {
        "mode": "manual",
        "total_score": 0.0,
        "max_score": float((question or {}).get("max_score", 0)),
        "confidence": 0.0,
        "status": "red",
        "confidence_factors": {"ocr_clarity": clarity, "answer_match": None},
        "step_analysis": steps,
        "knowledge_points": list((question or {}).get("knowledge_points") or []),
        "error_tags": [],
        "student_feedback": "本次作业等待教师终审。",
        "teacher_note": "未配置真实批改模型，系统没有生成虚构分数，请人工终审。",
    }


def normalize_llm_result(raw: dict, question: dict, transcript: str, clarity: float) -> dict:
    rubric = question.get("rubric") or []
    raw_steps = raw.get("step_analysis") if isinstance(raw.get("step_analysis"), list) else []
    steps = []
    total = 0.0
    tags = []
    for index, rubric_item in enumerate(rubric):
        candidate = raw_steps[index] if index < len(raw_steps) and isinstance(raw_steps[index], dict) else {}
        max_score = float(rubric_item.get("max_score", 0))
        try:
            score = float(candidate.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        score = round(max(0.0, min(max_score, score)), 1)
        tag = candidate.get("error_tag")
        if tag not in ERROR_TAGS:
            tag = None
        if tag and tag not in tags:
            tags.append(tag)
        total += score
        steps.append(
            {
                "step": rubric_item.get("step", "评分步骤"),
                "is_correct": score >= max_score and max_score > 0,
                "score": score,
                "max_score": max_score,
                "error_tag": tag,
                "knowledge_point": rubric_item.get("knowledge_point", ""),
                "reason": str(candidate.get("reason", ""))[:500],
                "evidence": str(candidate.get("evidence", ""))[:240],
                "legible": bool(candidate.get("legible", clarity >= 60)),
            }
        )
    coverage = round(100 * len(raw_steps) / len(rubric), 1) if rubric else 0
    factors = {
        "ocr_clarity": clarity,
        "answer_match": raw.get("answer_match"),
        "rubric_coverage": min(100.0, coverage),
        "llm_self_consistency": raw.get("llm_self_consistency", 70),
        "teacher_pass_rate": None,
    }
    confidence = compute_confidence(factors)
    status = route(confidence)
    return {
        "mode": "llm",
        "total_score": round(total, 1),
        "max_score": float(question.get("max_score", 0)),
        "confidence": confidence,
        "status": status,
        "confidence_factors": factors,
        "step_analysis": steps,
        "knowledge_points": list(dict.fromkeys(question.get("knowledge_points") or [])),
        "error_tags": tags,
        "student_feedback": str(raw.get("student_feedback", "请根据步骤证据完成订正。"))[:500],
        "teacher_note": str(raw.get("teacher_note", "请教师核对 AI 判分。"))[:500],
    }


def final_error_tags(result: dict, review: dict | None) -> Iterable[str]:
    if review and review.get("final_error_tags") is not None:
        return review["final_error_tags"]
    return result.get("error_tags") or []

