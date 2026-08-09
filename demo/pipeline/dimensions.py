"""判别分维度体系（题库外作业的评分骨架）。

## 为什么需要这个模块

题库内的题有人工标注的 Rubric（每步名称、每步分值、对应知识点），批改时
逐步对齐即可。题库外的真实作业没有 Rubric——而真实作业几乎必然在题库外。

一种做法是让模型每次自己拟一套 Rubric。那样每份作业的评分骨架都不一样：
同一道题批两次可能拆成 3 步和 5 步，权重也不同，分数之间没有可比性，
更没法跟教师打分做一致性校验（对不上是"模型拆错了"还是"判错了"分不清）。

所以这里把骨架**固定下来**：五个维度、每个维度的分值由学科档位决定、
总分恒为 15。模型只负责在给定维度上打分与举证，不负责决定有哪些维度。
这样：
  - 同学科的任意两份作业，评分口径一致，分数可比；
  - 教师改分时能指出"是哪个维度判错了"，而不只是"总分不对"；
  - 那条修正可以按「学科 × 维度」回灌，下一份同类作业的该维度先验随之调整。

## 15 分是什么

15 是**体系判别分的满分**，不是试卷上那道题的实际分值。它的用途是排序与
分流（哪几份该老师先看），不是替代教师给分。试卷若印了分值，另存
printed_max_score 供教师换算，不参与计算。

选 15 而不是 100：五个维度各占 2-4 分，整数分配、无需小数，粒度够用又
不制造"87 分和 88 分有什么区别"这种无法辩护的精度。
"""

# ---------------------------------------------------------------------------
# 五个维度
# ---------------------------------------------------------------------------
# 顺序即解题的推进顺序：读懂题 → 选对路 → 算对 → 写完整 → 答对。
# 刻意不设"卷面整洁"之类与学业能力无关的维度——那属于卷面清晰度因子，
# 已在置信度里单独度量，混进判分会让分数掺进非学业信号。
DIMENSIONS = (
    {
        "key": "understanding",
        "name": "题意理解",
        "desc": "是否读懂题目要求：已知条件、所求目标、隐含约束（定义域、单位、题设范围）",
    },
    {
        "key": "method",
        "name": "方法选择",
        "desc": "解题路径是否正确可行：选用的定理/公式/结构是否适用于本题",
    },
    {
        "key": "execution",
        "name": "运算执行",
        "desc": "推导与计算是否准确：求导、化简、代入、变形等具体操作有无错误",
    },
    {
        "key": "reasoning",
        "name": "步骤完整",
        "desc": "关键步骤是否齐备且有依据：必要的讨论、检验、说明有无缺失或跳步",
    },
    {
        "key": "answer",
        "name": "结论正确",
        "desc": "最终答案是否正确且表述规范：数值、单位、区间写法、是否回答了所问",
    },
)

DIMENSION_KEYS = tuple(d["key"] for d in DIMENSIONS)
TOTAL_SCORE = 15
# ---------------------------------------------------------------------------
# 学科权重档位
# ---------------------------------------------------------------------------
# 同一套维度，不同学科的分值分配不同——这是学科差异的真实反映，不是调参：
#   理科（数学/物理/化学）运算执行占比最重，算错就是错；
#   语言类（语文/英语）没有"运算"，重心在表达完整与内容准确，
#     execution 在这里理解为"语言运用准确性"（时态、语法、用词、句式）；
#   文科（政治/历史/地理）重心在论述完整与结论准确，方法选择让位于要点覆盖。
# 每档五项之和必须等于 TOTAL_SCORE，模块加载时断言。
_WEIGHT_SCIENCE = {"understanding": 2, "method": 4, "execution": 4, "reasoning": 3, "answer": 2}
_WEIGHT_LANGUAGE = {"understanding": 3, "method": 2, "execution": 4, "reasoning": 4, "answer": 2}
_WEIGHT_HUMANITIES = {"understanding": 3, "method": 2, "execution": 2, "reasoning": 5, "answer": 3}

SUBJECT_WEIGHTS = {
    "数学": _WEIGHT_SCIENCE,
    "物理": _WEIGHT_SCIENCE,
    "化学": _WEIGHT_SCIENCE,
    "生物": _WEIGHT_SCIENCE,
    "科学": _WEIGHT_SCIENCE,
    "信息技术": _WEIGHT_SCIENCE,
    "语文": _WEIGHT_LANGUAGE,
    "英语": _WEIGHT_LANGUAGE,
    "政治": _WEIGHT_HUMANITIES,
    "历史": _WEIGHT_HUMANITIES,
    "地理": _WEIGHT_HUMANITIES,
}
# 学科认不出时用理科档：它的分配最均衡，且 execution 权重高会让
# 明显算错的作答拿不到高分——冷启动时宁可严一点。
DEFAULT_WEIGHTS = _WEIGHT_SCIENCE

# 维度键是稳定的，但呈现给教师与模型的名字要贴合学科，否则「运算执行」
# 出现在英语作文和历史论述题上会让人不知道该判什么。别名只改名称与说明，
# 分值分配与 key 不变——回灌统计仍按 key 走，跨学科可比。
_ALIAS_LANGUAGE = {
    "method":    ("行文思路", "结构与思路是否得当：体裁、逻辑顺序、要点组织"),
    "execution": ("语言运用", "语言运用是否准确：时态、语法、用词、句式、标点"),
    "reasoning": ("内容完整", "要点是否齐备：题目要求的信息点有无遗漏、展开是否充分"),
}
_ALIAS_HUMANITIES = {
    "method":    ("分析角度", "切入角度是否恰当：是否用对了本题该用的分析框架"),
    "execution": ("史实/概念准确", "引用的史实、概念、术语是否准确无误"),
    "reasoning": ("论述完整", "要点是否齐备、论据是否支撑结论、有无关键遗漏"),
}

# 全部维度名（含各学科别名）。给 grader 用来挡住「模型拿维度名当知识点交差」：
# 维度名说的是批改的角度，不是课程知识点，混进知识点聚合就是类目错误。
ALL_NAMES = frozenset(
    [d["name"] for d in DIMENSIONS]
    + [n for alias in (_ALIAS_LANGUAGE, _ALIAS_HUMANITIES) for n, _ in alias.values()]
)


def weights_for(subject: str) -> dict:
    """取该学科的维度分值分配。"""
    return SUBJECT_WEIGHTS.get((subject or "").strip(), DEFAULT_WEIGHTS)


def rubric_for(subject: str) -> list:
    """按学科生成 Rubric（结构与题库内题目的 rubric 字段完全一致）。

    返回结构与人工标注的 Rubric 同形，是为了让 _parse_llm_steps / derive_factors
    / 前端证据链渲染全部**原样复用**——题库内外只有 Rubric 来源不同，
    下游一行都不用改。
    """
    w = weights_for(subject)
    profile = SUBJECT_WEIGHTS.get((subject or "").strip())
    alias = {}
    if profile is _WEIGHT_LANGUAGE:
        alias = _ALIAS_LANGUAGE
    elif profile is _WEIGHT_HUMANITIES:
        alias = _ALIAS_HUMANITIES
    out = []
    for d in DIMENSIONS:
        name, desc = d["name"], d["desc"]
        if d["key"] in alias:
            name, desc = alias[d["key"]]
        out.append({
            "step": name,
            "max_score": w[d["key"]],
            # 刻意留空：维度名（题意理解 / 方法选择 / …）描述的是**批改的角度**，
            # 不是课程知识点。以前这里填的是 name，于是班级看板的「知识点错误率」
            # 里冒出五行「方法选择 0/1 · 0%」，是明显的类目错误。
            # 题库外的真实知识点改由模型在批改时逐步给出（grader._step_kp），
            # 模型给不出就留空，看板跳过这一步而不是拿维度名凑数。
            "knowledge_point": "",
            "dimension": d["key"],
            "desc": desc,
        })
    return out


def prompt_block(subject: str) -> str:
    """给批改 Prompt 用的维度说明块（含每维满分）。"""
    lines = []
    for item in rubric_for(subject):
        lines.append("- %s（%d 分）：%s" % (item["step"], item["max_score"], item["desc"]))
    return "\n".join(lines)


def dimension_of(step_name: str, subject: str = "") -> str | None:
    """由步骤名反查维度 key。教师改分后要知道改的是哪个维度，靠这个映射。"""
    for item in rubric_for(subject):
        if item["step"] == step_name:
            return item["dimension"]
    return None


# 分配之和必须等于满分，否则「总分 15」这个口径就是假的。
# 放在模块加载时断言：改权重改错会立刻炸，而不是等到线上分数对不上。
for _name, _w in list(SUBJECT_WEIGHTS.items()) + [("默认", DEFAULT_WEIGHTS)]:
    _s = sum(_w[k] for k in DIMENSION_KEYS)
    assert _s == TOTAL_SCORE, "%s 档维度分值之和为 %d，应为 %d" % (_name, _s, TOTAL_SCORE)
    assert set(_w) == set(DIMENSION_KEYS), "%s 档维度键与 DIMENSION_KEYS 不一致" % _name
