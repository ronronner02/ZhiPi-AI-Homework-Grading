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
checks = 0


def ck(name, got, want):
    global checks
    checks += 1
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


# ---------------------------------------------------------------- 五因子加权与重归一化
from pipeline import confidence as conf

ck("权重和为 1", round(sum(conf.WEIGHTS.values()), 6), 1.0)
ck("因子集合就是原先五项", set(conf.WEIGHTS),
   {"ocr_clarity", "answer_match", "rubric_coverage",
    "llm_self_consistency", "teacher_pass_rate"})

FULL = {"ocr_clarity": 90, "answer_match": 80, "rubric_coverage": 70,
        "llm_self_consistency": 60, "teacher_pass_rate": 50}
ck("五项齐全 → 直接加权",
   conf.compute_confidence(FULL),
   round(90 * .25 + 80 * .25 + 70 * .20 + 60 * .20 + 50 * .10, 1))

# 无题库这条路是常态，不是异常：answer_match 留空要按重归一化剔除，
# 而不是当 0 分白扣 25 分。缺键与 None 必须同义。
NOBANK = dict(FULL, answer_match=None)
expect = round((90 * .25 + 70 * .20 + 60 * .20 + 50 * .10) / .75, 1)
ck("无题库 → answer_match=None 重归一化", conf.compute_confidence(NOBANK), expect)
miss = {k: v for k, v in FULL.items() if k != "answer_match"}
ck("无题库 → 缺键与 None 同义", conf.compute_confidence(miss), expect)
ck("全测不出 → 0 分而不是崩", conf.compute_confidence({k: None for k in conf.WEIGHTS}), 0.0)

ck("阈值分流 绿", conf.route(85.0), "green")
ck("阈值分流 黄", conf.route(84.9), "yellow")
ck("阈值分流 红", conf.route(59.9), "red")

# derive_factors：无题库不给 answer_match，有题库才写
d0 = grader.derive_factors(QUESTION, "x", 90.0, partial_parsed, 0.8)
ck("derive_factors 键集合正确", set(d0.keys()), set(conf.WEIGHTS))
ck("无题库 → answer_match 留空", d0["answer_match"], None)
d1 = grader.derive_factors(QUESTION, "x", 90.0, partial_parsed, 0.8, answer_match=88.25)
ck("有题库 → answer_match 写入并保留 1 位", d1["answer_match"], 88.2)


# ---------------------------------------------------------------- 交叉验证：后置防线，不占权重
_real_maybe = grader._maybe_cross_check
BASE_F = {"ocr_clarity": 80, "answer_match": 80, "rubric_coverage": 78,
          "llm_self_consistency": 72, "teacher_pass_rate": 80}
base_c = conf.compute_confidence(BASE_F)
try:
    grader._maybe_cross_check = lambda *a, **k: None
    s_, res = grader._settle_cross(QUESTION, "x", 5, BASE_F, base_c, "yellow")
    ck("未触发 → 分流不变", s_, "yellow")
    ck("未触发 → 无 cross_check", res, None)

    # 结论一致：不加分也不减分——它不占权重，只在分歧时说话
    grader._maybe_cross_check = lambda *a, **k: {
        "model2": "m2", "model2_score": 5, "gap": 0.0, "escalated": False}
    s_, res = grader._settle_cross(QUESTION, "x", 5, BASE_F, base_c, "yellow")
    ck("结论一致 → 分流仍按置信度，不被抬桶", s_, "yellow")
    ck("结论一致 → 仍回传复核详情", res["model2_score"], 5)
    ck("交叉验证不写进 factors", "cross_model_agreement" in BASE_F, False)

    # 分歧显著：一票否决，压过绿区置信度
    grader._maybe_cross_check = lambda *a, **k: {
        "model2": "m2", "model2_score": 0, "gap": 5.0, "escalated": True}
    s_, res = grader._settle_cross(QUESTION, "x", 5, BASE_F, 99.0, "green")
    ck("分歧 → 绿区也强制转红", s_, "red")
finally:
    grader._maybe_cross_check = _real_maybe


if fails:
    print("FAILED %d" % len(fails))
    for f in fails:
        print("-", f)
    sys.exit(1)

print("OK  %d checks" % checks)
