"""rubric_coverage / 步骤判定补全 的单元自检。

覆盖截图里暴露的真实缺陷：满分卷四步都有 evidence，但模型省略 reason
时，旧口径把 Rubric 覆盖度打成 0，把绿桶压成黄桶。

运行：py -3 tools/test_rubric_coverage.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import grader

fails = []


def ck(name, got, want):
    if got != want:
        fails.append("%s\n    期望 %r\n    实际 %r" % (name, want, got))


QUESTION = {
    "question_id": "Q002",
    "subject": "物理",
    "grade": "初二",
    "question_type": "计算题",
    "title": "速度与单位换算",
    "question_text": "求速度 v",
    "standard_answer": "v = s / t = 200 m ÷ 20 s = 10 m/s",
    "standard_solution_steps": ["写公式", "代入", "计算", "单位"],
    "max_score": 6,
    "rubric": [
        {"step_id": 1, "step": "写出正确公式", "max_score": 2, "knowledge_point": "速度公式"},
        {"step_id": 2, "step": "正确代入数值", "max_score": 2, "knowledge_point": "速度公式"},
        {"step_id": 3, "step": "计算结果正确", "max_score": 1, "knowledge_point": "速度公式"},
        {"step_id": 4, "step": "单位正确", "max_score": 1, "knowledge_point": "单位换算"},
    ],
}


# ---------------------------------------------------------------- 判定依据识别
ck("reason 非空 → 已覆盖", grader._step_has_judgment(
    {"reason": "公式正确", "evidence": ""}), True)
ck("仅 evidence → 已覆盖", grader._step_has_judgment(
    {"reason": "", "evidence": "v = s / t"}), True)
ck("空白 reason+evidence → 未覆盖", grader._step_has_judgment(
    {"reason": "  ", "evidence": ""}), False)
ck("全空 → 未覆盖", grader._step_has_judgment({}), False)


# ---------------------------------------------------------------- 截图同款：满分 + 只有 evidence
perfect_evidence_only = [
    {"step": "写出正确公式", "is_correct": True, "score": 2, "error_tag": None,
     "reason": "", "evidence": "v = s / t", "legible": True},
    {"step": "正确代入数值", "is_correct": True, "score": 2, "error_tag": None,
     "reason": "", "evidence": "200 / 20", "legible": True},
    {"step": "计算结果正确", "is_correct": True, "score": 1, "error_tag": None,
     "reason": "", "evidence": "10", "legible": True},
    {"step": "单位正确", "is_correct": True, "score": 1, "error_tag": None,
     "reason": "", "evidence": "m/s", "legible": True},
]

# 先走解析：应回填 reason，避免 UI 空白
steps, total, tags = grader._parse_llm_steps(QUESTION, {
    "step_analysis": perfect_evidence_only,
    "error_tags": [],
})
ck("满分总分", total, 6)
ck("无错因", tags, [])
ck("四步都回填了 reason", all(bool(s["reason"]) for s in steps), True)
ck("evidence 保留", [s["evidence"] for s in steps],
   ["v = s / t", "200 / 20", "10", "m/s"])

factors = grader.derive_factors(
    QUESTION,
    "v = s / t = 200 / 20 = 10 m/s",
    95.0,
    steps,
    1.0,
)
ck("截图同款覆盖度应为 100", factors["rubric_coverage"], 100.0)
ck("OCR 清晰度透传", factors["ocr_clarity"], 95.0)
# answer_match 已从四因子体系中移除，derive_factors 不再返回该键
ck("factor 键集合正确", set(factors.keys()), {"ocr_clarity", "rubric_coverage", "llm_self_consistency", "teacher_pass_rate"})


# ---------------------------------------------------------------- 旧口径回归：只认 reason 会把本例打成 0
legacy_covered = sum(1 for s in perfect_evidence_only if s.get("reason"))
ck("旧口径对截图样例会得到 0（对照）", legacy_covered, 0)


# ---------------------------------------------------------------- 真·未覆盖：reason/evidence 皆空
empty_steps = [
    {"step": "写出正确公式", "score": 2, "is_correct": True},
    {"step": "正确代入数值", "score": 2, "is_correct": True},
    {"step": "计算结果正确", "score": 1, "is_correct": True},
    {"step": "单位正确", "score": 1, "is_correct": True},
]
empty_parsed, _, _ = grader._parse_llm_steps(QUESTION, {"step_analysis": empty_steps})
empty_factors = grader.derive_factors(QUESTION, "10 m/s", 90.0, empty_parsed, 0.9)
ck("无任何依据 → 覆盖度 0", empty_factors["rubric_coverage"], 0.0)


# ---------------------------------------------------------------- 部分覆盖
partial = [
    {"step": "写出正确公式", "score": 2, "reason": "公式对", "evidence": "v=s/t"},
    {"step": "正确代入数值", "score": 2, "reason": "", "evidence": "200/20"},
    {"step": "计算结果正确", "score": 1, "reason": "", "evidence": ""},
    {"step": "单位正确", "score": 0, "reason": "单位写成 km/s", "evidence": "km/s"},
]
partial_parsed, _, _ = grader._parse_llm_steps(QUESTION, {"step_analysis": partial})
partial_factors = grader.derive_factors(QUESTION, "10 km/s", 88.0, partial_parsed, 0.8)
ck("4 步里 3 步有依据 → 75", partial_factors["rubric_coverage"], 75.0)


# ---------------------------------------------------------------- 空 step_analysis
zero = grader.derive_factors(QUESTION, "", 50.0, [], None)
ck("无步骤 → 覆盖度 0", zero["rubric_coverage"], 0.0)
ck("无自报 conf → 回退 60", zero["llm_self_consistency"], 60.0)


# ---------------------------------------------------------------- 字迹难辨不污染覆盖判定
illegible_no_evidence = [
    {"step": "写出正确公式", "score": 0, "is_correct": False, "legible": False,
     "reason": "", "evidence": ""},
    {"step": "正确代入数值", "score": 2, "is_correct": True, "legible": True,
     "reason": "", "evidence": "200/20"},
    {"step": "计算结果正确", "score": 1, "is_correct": True, "legible": True,
     "reason": "计算正确", "evidence": ""},
    {"step": "单位正确", "score": 0, "is_correct": False, "legible": False,
     "reason": "", "evidence": ""},
]
il_parsed, _, _ = grader._parse_llm_steps(QUESTION, {"step_analysis": illegible_no_evidence})
# 解析层不再给 legible=False 的步骤自动拼「字迹难辨」前缀——
# 否则无判定依据的步骤会被伪造出非空 reason，覆盖度虚高（review 发现）。
ck("legible=False 且无依据 → 不自动伪造 reason", [s["reason"] for s in il_parsed][0], "")
ck("legible=False 且无依据 → 该步不算覆盖",
   grader._step_has_judgment(il_parsed[0]), False)
il_factors = grader.derive_factors(QUESTION, "200/20", 88.0, il_parsed, 0.8)
ck("难辨步不算覆盖 → 覆盖度 50（4 步里 2 步有依据）",
   il_factors["rubric_coverage"], 50.0)


if fails:
    print("FAILED %d" % len(fails))
    for f in fails:
        print("-", f)
    sys.exit(1)

print("OK  %d checks" % 19)
