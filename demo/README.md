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
│  POST /api/feishu/push  ·  POST /api/feishu/sync-base （§13.2 飞书集成）     │
└───────────────┬────────────────────────────────────────────┬──────────────┘
                │                                              │
                ▼  pipeline（批改流水线）                       ▼  内存态
   ┌───────────────────────────────────────────┐   ┌────────────────────────┐
   │ ocr.py        转写（mock + 预留真实 OCR） │   │ GRADED  批改结果缓存    │
   │ grader.py     Rubric 逐步判分（规则引擎 / │   │ REVIEWS 教师审核记录    │
   │               可选 LLM 分支）             │   └────────────────────────┘
   │ confidence.py §9.7 置信度加权 + 红黄绿分流 │
   │ analytics.py  班级学情聚合 + 讲评建议     │
   │ feishu.py     飞书互动卡片 / 多维表格台账 │
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
4. **班级看板**：知识点错误率横向条形图（纯 div + CSS 宽度）、错因分布、红黄绿占比、规则生成的下节课讲评建议；底部「飞书协同」区提供 **推送飞书审核提醒卡片** 与 **同步飞书多维表格学情台账** 两个按钮（详见下方「飞书集成」）。

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
| POST | `/api/feishu/push` | 推送审核提醒互动卡片到飞书（§13.2 集成点二） |
| POST | `/api/feishu/sync-base` | 同步学情台账到飞书多维表格（§13.2 集成点一） |

## 飞书集成

按比赛规则"至少运用 1 项飞书 AI 产品能力"要求，Demo 内置飞书协同模块（`pipeline/feishu.py`），
落地设计方案 §13.2《飞书 AI 产品能力集成设计》的两个可现场演示的集成点。在「班级看板」Tab 底部
点击两个按钮即可触发，返回结果在页面内展示（卡片预览 / 记录表格 + 原始 JSON）。

### 演示模式 vs 真实模式

| 模式 | 触发条件 | 行为 |
| ---- | -------- | ---- |
| **演示模式（默认）** | 不配置任何飞书环境变量 | 接口返回 `"mode":"demo"`，展示**将要推送的卡片内容**与**将要写入的多维表格记录**，页面显著标注"演示模式：未配置飞书凭据，展示将推送的内容"。无网络、无凭据也能现场演示。 |
| **真实模式** | 配置对应环境变量 | 卡片经自定义机器人 Webhook 真实推送；台账经开放平台鉴权后 `batch_create` 真实写入多维表格。任何异常（网络 / 鉴权 / 频控 / 写表报错）自动降级演示模式，保证 Demo 始终可跑。 |

### 环境变量

| 环境变量 | 作用 | 用于 |
| -------- | ---- | ---- |
| `ZHIPI_FEISHU_WEBHOOK` | 飞书自定义机器人 Webhook 地址，配置后卡片真实推送到群 | `/api/feishu/push` |
| `ZHIPI_FEISHU_APP_ID` | 飞书自建应用 App ID，用于开放平台鉴权 | `/api/feishu/sync-base` |
| `ZHIPI_FEISHU_APP_SECRET` | 飞书自建应用 App Secret，用于换取 `tenant_access_token` | `/api/feishu/sync-base` |
| `ZHIPI_FEISHU_BASE_APP_TOKEN` | 多维表格（Base）的 App Token，定位目标多维表格 | `/api/feishu/sync-base` |
| `ZHIPI_FEISHU_TABLE_ID` | 多维表格中目标数据表的 Table ID，定位《学情台账》表 | `/api/feishu/sync-base` |

> 写表需上表后 4 个变量同时配置，缺一即降级演示模式。另有可选变量 `ZHIPI_REVIEW_CONSOLE_URL`
> 用于覆盖卡片"前往审核工作台"按钮的跳转地址（缺省为占位链接）。本模块不硬编码任何真实凭据。

```bash
# Windows PowerShell（真实模式示例，占位值请替换为自有凭据）
$env:ZHIPI_FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
$env:ZHIPI_FEISHU_APP_ID = "cli_xxxx"
$env:ZHIPI_FEISHU_APP_SECRET = "xxxx"
$env:ZHIPI_FEISHU_BASE_APP_TOKEN = "bascnxxxx"
$env:ZHIPI_FEISHU_TABLE_ID = "tblxxxx"
python -m uvicorn app:app --port 8010
```

### 两个新接口

| 方法 | 路径 | 入参 | 说明 |
| ---- | ---- | ---- | ---- |
| POST | `/api/feishu/push` | 无 | 聚合当前班级学情，构造 `msg_type=interactive` 互动卡片（作业名、红黄绿分流统计、待教师处理数量、"前往审核工作台"按钮），推送或返回预览。 |
| POST | `/api/feishu/sync-base` | 无 | 将逐题批改结果（叠加教师终审）组装为多维表格记录（学生 / 题号 / 得分 / 满分 / 错因标签 / 置信度 / 分流状态 / 教师终审），写入或返回预览。 |

### 与设计方案 §13.2 的对应关系

| Demo 能力 | 设计方案 §13.2 集成点 | 说明 |
| --------- | --------------------- | ---- |
| `/api/feishu/sync-base`（多维表格记录组装与写入） | **集成点一：多维表格 AI 学情台账（主用飞书 AI 能力）** | 逐题结果结构化写入《学情台账》，可对「错因标签」「得分/满分」等字段配置 **AI 字段捷径**，逐行自动生成一句话错因摘要与个性化学习建议，并以仪表盘呈现。 |
| `/api/feishu/push`（互动卡片推送） | **集成点二：机器人互动卡片审核流转** | 批改完成后机器人向教师推送红黄绿分流与待审数量卡片，一键跳转审核工作台，闭合"批改—提醒—审核"流转。 |

> 集成点三（知识问答 / Aily 讲评助手）与集成点四（飞书文档 AI 讲评提纲）属入围后随企业教练搭建的能力，
> 详见 `docs/04-技术说明文档.md`「飞书 AI 产品能力集成实现」章节。
