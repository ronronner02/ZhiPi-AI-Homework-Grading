"""置信度评估与红黄绿分流模块。

五因子加权公式（百分制，各因子取值 0-100）：

    总置信度 = OCR 清晰度        × 0.25
             + 答案匹配度        × 0.25   ← 无题库时标为"测不出"，见下文
             + Rubric 覆盖度     × 0.20
             + 二次批改一致性    × 0.20
             + 历史教师通过率    × 0.10

**答案匹配度的适用条件**：这一维比的是「学生作答 vs 标准答案」，因此必须先有
标准答案。系统有两条路：

- **有题库**（教师先传了答案页，或命中内置题库）：逐题比对教师页给出的标准
  答案，按各题分值加权成页级匹配度，该因子正常计分；
- **无题库**（只有学生页，让大模型自己判）：没有可比的基准，该因子置 null，
  按权重重归一化剔除——**不是给 0 分**。「测不出」与「测出来是 0 分」是两件
  事，把没有基准硬算成 0 分会凭空扣掉 25 分。

早期版本的答案匹配度算法是「取学生最后一行对比一句简短终答」，在整页作业上
毫无意义（一页十几道题），且只有题库内题目才有那句终答。现算法改为**逐题
比对教师页答案**，前提由「题库里恰好有这道题」变成「教师传了答案页」——
后者是教师本来就有的东西，这才让这一维在真实作业上可用。

双模型交叉验证是**后置防线**，不占权重：两模型分差超过满分 15% 时强制转红
交人工。它与加权是两种不同的机制——加权表达「有多少把握」，一票否决表达
「这份不能自动放行」，混在一起会让「分歧显著但加权仍及格」的作答漏网。

分流阈值：
    置信度 ≥ 85        绿色，AI 自动通过（教师可抽查）
    60 ≤ 置信度 < 85   黄色，AI 给出建议，教师一键确认
    置信度 < 60        红色，AI 不直接判分，交教师人工批改
"""

# 五个因子及其权重（因子取值均为百分制 0-100）
WEIGHTS = {
    "ocr_clarity": 0.25,              # OCR 识别清晰度
    "answer_match": 0.25,             # 答案匹配度（逐题对比题库标准答案）
    "rubric_coverage": 0.20,          # 评分规则覆盖度
    "llm_self_consistency": 0.20,     # 二次批改一致性（同一模型独立批两次）
    "teacher_pass_rate": 0.10,        # 历史教师通过率
}

# 分流阈值
GREEN_THRESHOLD = 85   # ≥ 85 绿色
YELLOW_THRESHOLD = 60  # ≥ 60 黄色，否则红色

# 交叉验证的分歧判定：两模型总分差超过满分这一比例即强制转红。
# 放在这里而不是 grader，是因为它与上面的阈值同属「分流口径」，
# 改一个通常要连带看另一个。
CROSS_DISAGREE_RATIO = 0.15

# 因子中文名，供前端解释展示
FACTOR_LABELS = {
    "ocr_clarity": "OCR 清晰度",
    "answer_match": "答案匹配度",
    "rubric_coverage": "Rubric 覆盖度",
    "llm_self_consistency": "二次批改一致性",
    "teacher_pass_rate": "历史教师通过率",
}

# 分流等级中文标签
STATUS_LABEL = {
    "green": "绿色 · 自动通过",
    "yellow": "黄色 · 教师确认",
    "red": "红色 · 人工批改",
}


def compute_confidence(factors: dict) -> float:
    """按五因子加权求和，返回百分制置信度（保留 1 位小数）。

    取值为 None **或整个键缺席**的因子表示「这一维本次测不了」，按权重重
    归一化剔除，而不是当成 0 分计入。两者必须同义：无题库批改时
    answer_match 这个键根本不会出现，若缺键按 0 分算，一个 0.25 权重的因子
    会凭空扣掉 25 分，且毫无报错迹象。

    全部因子都测不出时返回 0.0 —— 没有任何依据就不该给出置信度。
    """
    total = 0.0
    weight_sum = 0.0
    for key, weight in WEIGHTS.items():
        value = factors.get(key)
        if value is None:
            continue
        total += float(value) * weight
        weight_sum += weight
    if weight_sum <= 0:
        return 0.0
    return round(total / weight_sum, 1)


def route(confidence: float) -> str:
    """按阈值 85 / 60 将置信度分流为 green / yellow / red。"""
    if confidence >= GREEN_THRESHOLD:
        return "green"
    if confidence >= YELLOW_THRESHOLD:
        return "yellow"
    return "red"
