"""置信度评估与红黄绿分流模块。

严格按照详细设计方案 §9.7 的加权公式（百分制，各因子取值 0-100）：

    总置信度 = OCR 清晰度        × 0.25
             + 答案匹配度        × 0.25
             + Rubric 覆盖度     × 0.20
             + LLM 自检一致性    × 0.20
             + 历史教师通过率    × 0.10

分流阈值：
    置信度 ≥ 85        绿色，AI 自动通过（教师可抽查）
    60 ≤ 置信度 < 85   黄色，AI 给出建议，教师一键确认
    置信度 < 60        红色，AI 不直接判分，交教师人工批改
"""

# §9.7 五个因子及其权重（因子取值均为百分制 0-100）
WEIGHTS = {
    "ocr_clarity": 0.25,           # OCR 识别清晰度
    "answer_match": 0.25,          # 答案匹配程度
    "rubric_coverage": 0.20,       # 评分规则覆盖度
    "llm_self_consistency": 0.20,  # LLM 自检一致性
    "teacher_pass_rate": 0.10,     # 历史教师通过率
}

# 分流阈值
GREEN_THRESHOLD = 85   # ≥ 85 绿色
YELLOW_THRESHOLD = 60  # ≥ 60 黄色，否则红色

# 因子中文名，供前端解释展示
FACTOR_LABELS = {
    "ocr_clarity": "OCR 清晰度",
    "answer_match": "答案匹配度",
    "rubric_coverage": "Rubric 覆盖度",
    "llm_self_consistency": "LLM 自检一致性",
    "teacher_pass_rate": "历史教师通过率",
}

# 分流等级中文标签
STATUS_LABEL = {
    "green": "绿色 · 自动通过",
    "yellow": "黄色 · 教师确认",
    "red": "红色 · 人工批改",
}


def compute_confidence(factors: dict) -> float:
    """按 §9.7 公式加权求和，返回百分制置信度（保留 1 位小数）。"""
    total = 0.0
    for key, weight in WEIGHTS.items():
        total += float(factors.get(key, 0)) * weight
    return round(total, 1)


def route(confidence: float) -> str:
    """按阈值 85 / 60 将置信度分流为 green / yellow / red。"""
    if confidence >= GREEN_THRESHOLD:
        return "green"
    if confidence >= YELLOW_THRESHOLD:
        return "yellow"
    return "red"
