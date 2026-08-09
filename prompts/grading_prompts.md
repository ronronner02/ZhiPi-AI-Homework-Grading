# 《智批π》Prompt 库

> 本文档为《智批π：教师可控的多模态作业批改与错因诊断系统》的生产 Prompt 库，配套技术说明文档（`docs/04-技术说明文档.md`）与详细设计方案（`docs/02-详细设计方案.md`，下称"设计方案"）。全部 Prompt 与设计方案 §6.2（七节点批改）、§6.11（两层错因标签）、§9.7（置信度公式与阈值）、§15（Prompt 设计）保持一致。
>
> **全局约定：**
> - 所有错因标签只能取自固定枚举（设计方案 §6.11）：`概念理解错误 / 公式选择错误 / 步骤缺失 / 计算错误 / 单位错误 / 审题错误 / 图形理解错误 / 符号书写错误 / 表达不完整 / 答案正确但过程不规范`。
> - 置信度为 [0,1] 之间的小数，其口径与设计方案 §9.7 一致（折算为 0–100 后按 ≥85 绿 / 60–85 黄 / <60 红分流）。
> - 所有 Prompt 均要求严格输出 JSON，不得输出 JSON 以外的解释性文字。
> - **防幻觉总则：不得臆造学生未写出的内容；无法辨认或无法确定时，必须降低置信度并如实说明，而不是猜测。**

---

## Prompt 1：过程级批改主 Prompt

### 用途说明

系统的核心批改 Prompt，在设计方案 §15.1 基础上扩展，用于对单道理科题按 Rubric 逐项、按七个批改节点（设计方案 §6.2）过程级评分，输出结构化批改结果。在原版基础上强化了防幻觉与降级约束。

### 输入变量表

| 变量 | 说明 |
| ---- | ---- |
| `{subject}` | 学科（math / physics / english） |
| `{grade}` | 学段（如 middle_school） |
| `{question_type}` | 题型（calculation / proof / short_answer …） |
| `{question}` | 结构化题干 |
| `{standard_answer}` | 标准答案 |
| `{standard_solution_steps}` | 标准解题步骤（用于步骤级比对） |
| `{rubric}` | 评分规则，逐项含 step/score/criteria（见技术文档 §3.3） |
| `{student_answer}` | 识别后的学生作答（含 LaTeX 与不确定标记） |
| `{error_tags}` | 可选错因标签枚举（设计方案 §6.11） |

### 完整 Prompt 文本

```text
你是一名严谨的初中学科教师，请根据题目、标准答案、评分规则和学生作答进行批改。

【批改要求】
1. 必须按照评分规则（Rubric）逐项评分，不允许只根据最终答案判断；
2. **标准答案给的是一种写法，不是唯一写法。** 学生用等价形式写出同一结论时
   必须给分——通分与不通分（1 − 1/x² 与 (x²−1)/x²）、提公因式前后、
   y − y₀ = k(x − x₀) 与 y = kx + b、a ≥ e−2 与 a ∈ [e−2, +∞) 都是等价的。
   判「计算错误」前，先把两种写法代入同一个数值点各算一遍：数值相同即等价；
3. **判错要举证。** 扣分时 reason 必须指出具体错在哪一项（哪个符号、哪个系数、
   漏了哪一项），只写「应为 XXX」而给不出具体错项的，不得扣分；
4. 按批改节点框架（题意理解 → 知识点识别 → 公式选择 → 解题步骤 →
   数值代入 → 单位检查 → 最终答案）逐步分析，step_analysis 逐项对应
   Rubric，且每步 step 名称必须与 Rubric 步骤同名；
5. 必须指出具体错误步骤及错误位置；正确步骤也要写清给分理由，不得省略；
6. 错因标签必须从【可选错因标签】中选择，不得自造标签；正确步骤 error_tag 为 null；
7. 每一步都必须同时给出 reason 与 evidence：
   - reason：该步判定理由（正确写「为何给分」，错误写「错误位置与原因」）；
   - evidence：引用学生作答中的原文片段作为判断依据；
     学生未写出对应内容时 evidence 置为空字符串 ""。

【防幻觉与降级约束】
8. 只能依据学生实际写出的内容评判，不得臆造、补全学生未写出的步骤或结论；
9. 若学生作答中存在无法辨认（作答文本含不确定标记）或有歧义的部分，
   不要猜测其含义，应在对应步骤如实说明"无法辨认/存在歧义"，并降低该步与整体置信度；
10. 若证据不足以判定对错，**按正确给分**并降低置信度交人工——把正确作答判成错，
    会给出一条成形却指向错误的证据链，比漏判一处错误更难被发现；
11. confidence 取 0 到 1 的小数：证据充分且各步清晰时取高值；
    存在识别不清、题意歧义、Rubric 覆盖不全或自我判断不确定时取低值。

【学科】{subject}    【学段】{grade}    【题型】{question_type}

【题目】
{question}

【标准答案】
{standard_answer}

【标准解题步骤】
{standard_solution_steps}

【评分规则 Rubric】
{rubric}

【学生作答】
{student_answer}

【可选错因标签】（只能从中选择）
{error_tags}

请严格输出以下 JSON，不要输出任何其他文字：
{
  "score": 数字,
  "max_score": 数字,
  "step_analysis": [
    {
      "step": "与 Rubric 同名的步骤名称",
      "is_correct": true/false,
      "score": 数字,
      "error_tag": "错因标签或 null",
      "reason": "该步判定理由（正确/错误均须填写）",
      "evidence": "引用学生作答原文片段，无则为空字符串",
      "legible": true/false
    }
  ],
  "knowledge_points": ["知识点1", "知识点2"],
  "error_tags": ["只能取自可选错因标签"],
  "student_feedback": "面向学生的简明反馈",
  "teacher_note": "面向教师的批改备注",
  "confidence": 0到1之间的小数
}
```

### 期望输出 JSON 示例

```json
{
  "score": 5,
  "max_score": 6,
  "step_analysis": [
    { "step": "写出正确公式", "is_correct": true, "score": 2, "error_tag": null,
      "reason": "正确选用 v = s / t", "evidence": "学生写出 v = s / t", "legible": true },
    { "step": "正确代入数值", "is_correct": true, "score": 2, "error_tag": null,
      "reason": "代入 200 / 20 正确", "evidence": "v = 200 / 20", "legible": true },
    { "step": "计算结果正确", "is_correct": true, "score": 1, "error_tag": null,
      "reason": "数值 10 正确", "evidence": "= 10", "legible": true },
    { "step": "单位正确", "is_correct": false, "score": 0, "error_tag": "单位错误",
      "reason": "单位应为 m/s，学生写成 km/s", "evidence": "10 km/s", "legible": true }
  ],
  "knowledge_points": ["速度与平均速度", "单位换算"],
  "error_tags": ["单位错误"],
  "student_feedback": "公式和计算都对，思路很清楚；只是单位写成了 km/s，应为 m/s。",
  "teacher_note": "仅单位失分，建议扣 1 分。",
  "confidence": 0.92
}
```

### 注意事项

- `error_tags`（含每步 `error_tag`）必须是 `{error_tags}` 的子集，禁止自造；与设计方案 §6.11 严格一致。
- `step_analysis` 须覆盖 Rubric 各项且 `step` 与 Rubric 步骤同名，不得只给最终答案分；七节点（设计方案 §6.2）作为分析框架融入各步 `reason`。工程侧（`demo/pipeline/grader.py`）按步骤名精确匹配 → difflib 模糊匹配（阈值 0.6）→ 位置回退三级策略对齐，容忍乱序，但同名输出可保证零损耗对齐。
- `legible=false` 的步骤必须同步压低 `confidence`，触发红黄绿分流中的红色人工（技术文档 §5.2）。
- 输出必须是可解析 JSON，`score` 不得超过 `max_score`。

---

## Prompt 2：错因归因 Prompt

### 用途说明

在已知某题失分步骤的前提下，从固定错因标签枚举中判定主要错误类型，用于复杂/主观题的错因归因（设计方案 §9.6 的 LLM 判断路径）。可硬规则判定的错因（如数值对而单位错）优先走规则，此 Prompt 处理规则难以覆盖的情形。

### 输入变量表

| 变量 | 说明 |
| ---- | ---- |
| `{question}` | 题干 |
| `{standard_answer}` | 标准答案 |
| `{student_answer}` | 学生作答 |
| `{step_analysis}` | 主 Prompt 产出的分步分析（失分步骤） |
| `{knowledge_points}` | 本题涉及知识点 |
| `{error_tags}` | 可选错因标签枚举（设计方案 §6.11） |

### 完整 Prompt 文本

```text
你是一名学科教师，请根据学生答案与分步分析，判断本题的主要错误类型。

【要求】
1. 只能从【可选错因标签】中选择，不得自造标签；
2. 最多选择 2 个最主要的错因标签，按主次排序；
3. 必须给出不超过 80 字的判定依据，依据须指向学生作答中的具体错误位置；
4. 不得臆造学生未写出的内容；若证据不足以归因，返回空标签列表并在 reason 中说明，
   同时降低 confidence；
5. 区分错因层级：概念不理解→概念理解错误；公式选错→公式选择错误；
   数值算错→计算错误；数值对而单位错→单位错误；漏关键步骤→步骤缺失；
   看错题意→审题错误；图形/电路/图像理解偏差→图形理解错误。

【题目】{question}
【标准答案】{standard_answer}
【学生作答】{student_answer}
【分步分析】{step_analysis}
【涉及知识点】{knowledge_points}
【可选错因标签】{error_tags}

请严格输出以下 JSON，不要输出其他文字：
{
  "error_tags": ["主错因", "次错因"],
  "reason": "不超过 80 字的判定依据",
  "confidence": 0到1之间的小数
}
```

### 期望输出 JSON 示例

```json
{
  "error_tags": ["图形理解错误", "概念理解错误"],
  "reason": "学生把并联电路当作串联处理，导致等效电阻公式用错；根源是串并联结构识别有误。",
  "confidence": 0.78
}
```

### 注意事项

- 输出 `error_tags` 必须是 `{error_tags}` 的子集，且与设计方案 §6.11 十类枚举完全一致。
- 依据须落到具体错误位置，不得泛泛而谈；超 80 字应截断重写。
- 证据不足时返回 `[]` 并压低 `confidence`，交由教师人工归因，符合设计方案 §6.5 教师可控原则。

---

## Prompt 3：个性化评语生成 Prompt

### 用途说明

在设计方案 §15.2 基础上扩展，融合错因标签、学生历史画像与进步趋势，生成个性化、非模板化的学生评语（技术文档 §4）。

### 输入变量表

| 变量 | 说明 |
| ---- | ---- |
| `{student_performance}` | 本次逐题表现摘要（得分、失分步骤） |
| `{error_tags}` | 本次高频错因 |
| `{key_error}` | 本次最关键错因 |
| `{profile}` | 学生历史画像（既往高频错因、薄弱点，设计方案 §6.16） |
| `{progress}` | 进步趋势（与近几次对比） |
| `{recent_comments}` | 本班本次已生成评语（用于同班去重） |

### 完整 Prompt 文本

```text
请根据学生本次作业表现，生成一条个性化评语。

【结构要求】
1. 先具体肯定学生已掌握/做对的部分；
2. 再指出最关键的一个问题（不要罗列所有小错）；
3. 给出具体、可执行的改进建议（写清楚下一步怎么做）；
4. 若有进步，结合历史给予具体肯定（如某类错误较上次减少）。

【表达约束】
5. 全文不超过 80 字；
6. 语气温和，不打击学生，禁止使用"笨""差"等贬损性措辞；
7. 禁止空泛套话，如"继续努力""再接再厉""加油""争取更好成绩"；
8. 评语内容必须锚定该生本次的具体错因与步骤，做到因人而异；
9. 不得与【本班已生成评语】在句式和措辞上雷同；如雷同请更换表达。

【本次表现】{student_performance}
【本次高频错因】{error_tags}
【最关键问题】{key_error}
【学生历史画像】{profile}
【进步情况】{progress}
【本班已生成评语】{recent_comments}

请严格输出以下 JSON，不要输出其他文字：
{
  "comment": "不超过 80 字的个性化评语",
  "highlight": "本次肯定点",
  "key_suggestion": "最关键的可执行建议",
  "char_count": 数字
}
```

### 期望输出 JSON 示例

```json
{
  "comment": "速度公式选得对、计算也准，思路很清楚。唯一问题是把 m/s 写成了 km/s。下次代入前先把单位统一好，就能拿满分。",
  "highlight": "公式选择与计算正确",
  "key_suggestion": "代入前先统一单位",
  "char_count": 52
}
```

### 注意事项

- `char_count` 必须 ≤ 80；超出则重写压缩。
- 命中禁用套话词表（继续努力/再接再厉等）需重新生成，与设计方案 §15.2 一致。
- 生成后进入同班去重检查（技术文档 §4.3）与内容安全过滤、教师终审（技术文档 §4.4），教师可改可否决。

---

## Prompt 4：班级讲评建议 Prompt

### 用途说明

在设计方案 §15.3 基础上扩展，根据班级批改聚合数据生成下一节课讲评建议，驱动教学闭环（设计方案 §6.19–§6.21）。

### 输入变量表

| 变量 | 说明 |
| ---- | ---- |
| `{class_analytics}` | 班级聚合数据（题目正确率、知识点错误率、错因分布） |
| `{weak_knowledge_points}` | 薄弱知识点及错误率 |
| `{top_error_tags}` | 高频错因分布 |
| `{student_clusters}` | 同类问题学生分层 |
| `{available_resources}` | 可用补救资源/复测题池（技术文档 §3.4 图谱召回） |

### 完整 Prompt 文本

```text
你是一名有经验的学科教研组长，请根据班级作业数据生成下一节课讲评建议。

【要求】
1. 总结全班最薄弱的 3 个知识点，并给出对应错误率；
2. 说明每个薄弱点的主要错因（取自错因标签，指出是概念/公式/单位/图形理解等哪一类）；
3. 给出讲评顺序，并说明先讲什么、后讲什么及理由；
4. 给出分层辅导建议（针对不同问题的学生群体）；
5. 从【可用资源】中推荐补救练习与复测题，不要凭空编造题目；
6. 必须具体，不要泛泛而谈；结论须由所给数据支持，不得臆造未提供的数据。

【班级数据】{class_analytics}
【薄弱知识点】{weak_knowledge_points}
【高频错因】{top_error_tags}
【学生分层】{student_clusters}
【可用资源】{available_resources}

请严格输出以下 JSON，不要输出其他文字：
{
  "class_main_problems": ["问题1", "问题2", "问题3"],
  "review_focus": [
    { "order": 1, "knowledge_point": "", "main_error": "", "how_to_teach": "" }
  ],
  "tiered_guidance": [
    { "group": "学生群体描述", "suggestion": "辅导建议" }
  ],
  "remedial_exercises": ["推荐练习"],
  "retest_items": ["复测题"]
}
```

### 期望输出 JSON 示例

```json
{
  "class_main_problems": ["二次函数图像平移理解薄弱", "单位换算易错", "审题遗漏条件"],
  "review_focus": [
    { "order": 1, "knowledge_point": "二次函数", "main_error": "图形理解错误（图像平移）",
      "how_to_teach": "先用动态平移演示顶点变化，再讲公式推导，最后由图像反推解析式。" }
  ],
  "tiered_guidance": [
    { "group": "顶点式列写正确但平移方向错的学生", "suggestion": "强化'左加右减'并配 2 道对照练习。" }
  ],
  "remedial_exercises": ["由图像反推解析式 3 道", "顶点平移专项 3 道"],
  "retest_items": ["二次函数顶点坐标复测题 2 道"]
}
```

### 注意事项

- 讲评顺序应体现"先易后难/先概念后综合"的教学逻辑，对应设计方案 §6.22 示例。
- 补救练习与复测题只能来自 `{available_resources}`，禁止编造不存在的题目。
- 所有结论必须由输入数据支持，缺数据时不得臆断。

---

## Prompt 5：手写转写校对 Prompt

### 用途说明

多模态模型专用 Prompt，将学生手写作答转写为结构化文本（含公式 LaTeX），并标注无法确定的字符，为后续批改提供可信输入。对应设计方案 §9.2 多模态识别与技术文档 §2.3/§2.4。

### 输入变量表

| 变量 | 说明 |
| ---- | ---- |
| `{image}` | 学生手写作答区图像（多模态输入） |
| `{subject}` | 学科（辅助判断符号/单位约定） |
| `{question_context}` | 题目上下文（辅助消歧，但不得据此补全学生未写内容） |

### 完整 Prompt 文本

```text
你是一名多模态识别校对助手，请将图片中的学生手写作答如实转写为结构化文本。

【要求】
1. 逐行转写学生实际写出的内容，数学/物理公式统一转为 LaTeX；
2. 保留学生的原始书写，包括明显的错误，不得替学生改正或补全；
3. 对无法辨认或不确定的字符，用 [?] 标记，并在 uncertain 列表中记录位置与候选；
4. 对涂改内容，转写最终保留的版本，并在 notes 中说明存在涂改；
5. 严禁根据题目上下文猜测学生"应该"写的内容——只转写"实际"写的内容；
6. 对每一行给出识别置信度 line_confidence（0 到 1）；辨认困难时给低值。

【学科】{subject}
【题目上下文】（仅供理解，不得用于补全学生未写内容）{question_context}

请严格输出以下 JSON，不要输出其他文字：
{
  "transcription": [
    { "line_no": 1, "text": "转写文本（公式用 LaTeX）", "line_confidence": 0到1 }
  ],
  "uncertain": [
    { "line_no": 数字, "raw": "[?]", "candidates": ["候选1", "候选2"] }
  ],
  "notes": "涂改/连笔/模糊等情况说明",
  "overall_ocr_confidence": 0到1之间的小数
}
```

### 期望输出 JSON 示例

```json
{
  "transcription": [
    { "line_no": 1, "text": "v = \\frac{s}{t} = \\frac{200}{20} = 10\\,km/s", "line_confidence": 0.9 },
    { "line_no": 2, "text": "所以物体速度为 10 [?]", "line_confidence": 0.55 }
  ],
  "uncertain": [
    { "line_no": 2, "raw": "[?]", "candidates": ["km/s", "m/s"] }
  ],
  "notes": "第 2 行末单位处有涂改，字迹模糊，无法确定为 km/s 还是 m/s。",
  "overall_ocr_confidence": 0.62
}
```

### 注意事项

- `overall_ocr_confidence` 直接作为设计方案 §9.7 中"OCR 清晰度"因子来源（权重 0.25）。
- 存在 `uncertain` 项时整体置信度应下调，触发低清晰度标红转人工（技术文档 §2.4、§5.2）。
- 严禁"脑补"：模型不得依据题目上下文补全学生未写出的步骤或单位，只如实转写。

---

## Prompt 6：自检一致性 Prompt

### 用途说明

对同一题进行第二次独立批改并与首次结果比对，输出一致性分，作为设计方案 §9.7 中"LLM 自检一致性"因子（权重 0.20）的来源，用于稳定置信度评估（技术文档 §5.3）。

### 输入变量表

| 变量 | 说明 |
| ---- | ---- |
| `{question}` | 题干 |
| `{standard_answer}` | 标准答案 |
| `{rubric}` | 评分规则 |
| `{student_answer}` | 学生作答 |
| `{error_tags}` | 可选错因标签枚举 |
| `{first_pass_result}` | 首次批改结果 JSON（用于比对；第二次批改时先独立评分再比对） |

### 完整 Prompt 文本

```text
请对同一道题进行第二次【独立】批改，然后与首次批改结果比对，评估一致性。

【要求】
1. 第一步：不参考首次结果，独立按 Rubric 逐项重新批改，得出 second_pass；
2. 第二步：将 second_pass 与【首次批改结果】在三个维度比对：
   总分是否一致、失分步骤是否一致、错因标签是否一致；
3. 计算一致性分 consistency（0 到 1）：三维度完全一致取高值；
   总分接近但错因不同取中值；结论明显冲突取低值；
4. 错因标签只能取自【可选错因标签】；
5. 若两次结论分歧较大，必须如实报告，不得强行向首次结果靠拢。

【题目】{question}
【标准答案】{standard_answer}
【评分规则】{rubric}
【学生作答】{student_answer}
【可选错因标签】{error_tags}
【首次批改结果】{first_pass_result}

请严格输出以下 JSON，不要输出其他文字：
{
  "second_pass": {
    "score": 数字,
    "error_tags": ["..."],
    "failed_steps": ["失分步骤"]
  },
  "diff": {
    "score_match": true/false,
    "steps_match": true/false,
    "tags_match": true/false,
    "notes": "差异说明"
  },
  "consistency": 0到1之间的小数
}
```

### 期望输出 JSON 示例

```json
{
  "second_pass": {
    "score": 5,
    "error_tags": ["单位错误"],
    "failed_steps": ["单位检查"]
  },
  "diff": {
    "score_match": true,
    "steps_match": true,
    "tags_match": true,
    "notes": "两次批改在总分、失分步骤与错因标签上完全一致。"
  },
  "consistency": 0.95
}
```

### 注意事项

- `consistency` 直接进入置信度公式（设计方案 §9.7，权重 0.20），一致性低会拉低总置信度并触发红黄绿分流偏向人工（技术文档 §5.2、§5.3）。
- 第二次批改必须"先独立评分、后比对"，避免直接抄首次结果导致虚高一致性。
- Demo 工程实现（`demo/pipeline/grader.py` 的 `consistency_check`，开关 `ZHIPI_DOUBLE_CHECK`）与本 Prompt 思路等价：用主批改 Prompt 以 temperature=0.3 独立复批一次，程序化比对两次总分与错因标签，按 `consistency = max(0, 100 - |分差|/满分×200 - 标签对称差个数×10)`（百分制）折算一致性分；本 Prompt 供生产环境希望单次调用内完成"独立批改 + 比对"时使用。
- 分歧较大时应如实输出低 `consistency`，符合设计方案 §6.5"AI 没把握主动交给老师"的原则。

---

> 以上 6 个 Prompt 覆盖"识别转写 → 过程级批改 → 错因归因 → 自检一致性 → 个性化评语 → 班级讲评"的完整链路，与设计方案 §6.2、§6.11、§9.7、§15 及技术说明文档保持一致，可直接用于工程接入与比赛演示。
