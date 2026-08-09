"""置信度评估与红黄绿分流模块。

五因子加权公式（百分制，各因子取值 0-100）：

    总置信度 = OCR 清晰度        × 0.20
             + Rubric 覆盖度     × 0.24
             + 二次批改一致性    × 0.24
             + 双模型交叉验证    × 0.20
             + 历史教师通过率    × 0.12

两个「一致性」因子测的不是同一件事，所以并列而不是二选一：

- 二次批改一致性：**同一个模型**对同一份作答独立批两次的吻合度。它抓的是
  随机性误差（采样波动、漏看一步）。同模型两次会共享同一套系统性偏见——
  模型认定某种解法错，两次都会一样地错，这一维照样给高分。
- 双模型交叉验证：**换一个厂商的模型**独立批一次的吻合度。它抓的正是上面
  测不到的系统性偏见：两家模型的训练数据与偏好不同，都判同一个分，
  这个分是错的可能性显著更低。

「答案匹配度」已移除：它的算法是「学生最后一行对比简短终答」，前提是
存在人工标注的标准答案，题库外作业没有，比对结果是噪声。

权重取值刻意让**未触发交叉验证时的重归一化结果等于旧的四因子口径**
（0.20/0.24/0.24/0.12 各除以 0.80 = 0.25/0.30/0.30/0.15）。也就是说，
绿件（不调第二模型）的判定口径一分未改，第五因子是纯增量。

分流阈值：
    置信度 ≥ 85        绿色，AI 自动通过（教师可抽查）
    60 ≤ 置信度 < 85   黄色，AI 给出建议，教师一键确认
    置信度 < 60        红色，AI 不直接判分，交教师人工批改
"""

# 五个因子及其权重（因子取值均为百分制 0-100）
WEIGHTS = {
    "ocr_clarity": 0.20,              # OCR 识别清晰度
    "rubric_coverage": 0.24,          # 评分规则覆盖度
    "llm_self_consistency": 0.24,     # 二次批改一致性（同一模型独立批两次）
    "cross_model_agreement": 0.20,    # 双模型交叉验证（换一家模型独立批一次）
    "teacher_pass_rate": 0.12,        # 历史教师通过率
}

# 分流阈值
GREEN_THRESHOLD = 85   # ≥ 85 绿色
YELLOW_THRESHOLD = 60  # ≥ 60 黄色，否则红色

# 交叉验证一致性低于此值即视为「两个模型结论分歧」，上层强制转人工。
# 与 grader.cross_check 的分差阈值是同一条线的两种写法：
# 一致性 = 100 - 分差/满分 × 200，分差 > 满分 15% ⟺ 一致性 < 70。
CROSS_DISAGREE_BELOW = 70

# 因子中文名，供前端解释展示
FACTOR_LABELS = {
    "ocr_clarity": "OCR 清晰度",
    "rubric_coverage": "Rubric 覆盖度",
    "llm_self_consistency": "二次批改一致性",
    "cross_model_agreement": "双模型交叉验证",
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
    归一化剔除，而不是当成 0 分计入。两者必须同义：双模型交叉验证只在黄 /
    红件上触发，绿件、未配第二模型、旧版缓存里这个键根本不存在——若缺键
    按 0 分算，一个 0.20 权重的因子会凭空扣掉 20 分，且毫无报错迹象。

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
