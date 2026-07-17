# 智批π · AI 智能作业批改系统（Demo）

面向 K12 多学科作业的 **教师可控 AI 批改与错因诊断** 原型。一键跑通闭环：
**学生提交 → 过程级批改 → 红黄绿分流 → 教师审核 → 班级看板**。

## 三步跑起来

```bash
cd E:\希沃智教π\zhipi-submission\demo
pip install -r requirements.txt
python -m uvicorn app:app --port 8010
```

浏览器打开 <http://127.0.0.1:8010/> 即可。默认 **mock 模式**，无需任何 API Key。

## 架构

```text
                         浏览器（内嵌单页前端，原生 HTML/JS/CSS，完全离线）
                                          │  HTTP / JSON
                                          ▼
┌──────────────────────────── app.py （FastAPI） ────────────────────────────┐
│  GET /  ·  GET /api/submissions  ·  POST /api/grade                         │
│  GET /api/teacher/results  ·  POST /api/teacher/review  ·  GET /api/analytics/class │
└───────────────┬────────────────────────────────────────────┬──────────────┘
                │                                              │
                ▼  pipeline（批改流水线）                       ▼  内存态
   ┌───────────────────────────────────────────┐   ┌────────────────────────┐
   │ ocr.py        转写（mock + 预留真实 OCR） │   │ GRADED  批改结果缓存    │
   │ grader.py     Rubric 逐步判分（规则引擎 / │   │ REVIEWS 教师审核记录    │
   │               可选 LLM 分支）             │   └────────────────────────┘
   │ confidence.py §9.7 置信度加权 + 红黄绿分流 │
   │ analytics.py  班级学情聚合 + 讲评建议     │
   └───────────────────────┬───────────────────┘
                           ▼  data（内置样例）
              questions.json   3 道题（数学 / 物理 / 英语）
              submissions.json 9 份作答（每题 3 份，覆盖绿 / 黄 / 红）
```

## mock / LLM 模式切换

| 模式 | 触发条件 | 说明 |
| ---- | -------- | ---- |
| **mock（默认）** | 不设任何环境变量 | 规则引擎按数据中每步预标注（对/错、部分分、错因）逐 Rubric 判分，置信度按 §9.7 公式真实计算，全程确定性、不使用随机数。 |
| **LLM（可选）** | 设置 `ZHIPI_LLM_API_KEY` | 按 §15.1 Prompt 让 OpenAI 兼容大模型逐步批改并返回 JSON；调用或解析失败自动降级 mock 并标注 `"mode":"mock"`。 |

可选环境变量（OpenAI 兼容 chat/completions，如 DeepSeek / Qwen）：

```bash
# Windows PowerShell
$env:ZHIPI_LLM_API_KEY = "sk-xxxx"
$env:ZHIPI_LLM_BASE_URL = "https://api.deepseek.com"   # 可选，默认 DeepSeek
$env:ZHIPI_LLM_MODEL    = "deepseek-chat"              # 可选
python -m uvicorn app:app --port 8010
```

结果 JSON 中的 `"mode"` 字段标明本次实际使用的是 `mock` 还是 `llm`。

## 页面功能（4 个 Tab）

1. **学生提交**：下拉选择内置学生作答（模拟拍照上传），预览 OCR 转写文本与清晰度，点「提交批改」。
2. **批改结果**：逐批改节点展示对错（✓ 全对 / △ 部分 / ✗ 错）、每步得分、错因标签徽章、知识点、总分、置信度与红黄绿分流、个性化评语与教师备注。
3. **教师工作台**：全部批改结果表格（学生 / 题目 / AI 分 / 置信度 / 红黄绿状态 / 错因）；绿色可「抽查通过」，黄色 / 红色可「确认」或「修改分数」，即时写入内存态。
4. **班级看板**：知识点错误率横向条形图（纯 div + CSS 宽度）、错因分布、红黄绿占比、规则生成的下节课讲评建议。

## 数据与算法要点

- **错因标签** 只使用详细设计方案 §6.11 的 10 类枚举。
- **置信度** = OCR 清晰度 × 0.25 + 答案匹配度 × 0.25 + Rubric 覆盖度 × 0.20 + LLM 自检一致性 × 0.20 + 历史教师通过率 × 0.10（百分制）。
- **分流阈值**：≥ 85 绿色，60 ~ 85 黄色，< 60 红色。
- **典型样例**：物理速度题「10 km/s」单位错误（公式、代入、计算均对，仅单位错，高置信度绿色）完整复现设计方案 §6.3。

## 接口一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/` | 内嵌单页前端 |
| GET  | `/api/submissions` | 列出内置作答（含转写预览） |
| POST | `/api/grade` | 入参 `submission_id`，返回过程级批改 JSON |
| GET  | `/api/teacher/results` | 教师工作台全部批改概览 |
| POST | `/api/teacher/review` | 教师确认 / 改分（内存态） |
| GET  | `/api/analytics/class` | 班级学情聚合 |
