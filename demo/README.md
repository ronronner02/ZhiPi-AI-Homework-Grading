# 智批π · AI 智能作业批改系统（Demo）

面向 K12 多学科作业的 **教师可控 AI 批改与错因诊断** 原型。一键跑通闭环：
**作业照片提交（内置样例图库 / 本地上传）→ 手写识别（双引擎）→ 过程级批改（证据链）→ 红黄绿分流 → 教师终审 → 班级看板（讲评大纲 + 学生画像）**。

## 一键启动

**方式一（推荐，Windows）**：双击 `run_demo.bat`，或在 PowerShell 中运行 `.\run_demo.ps1`。
脚本自动探测可用 Python（避开 Microsoft Store 占位命令）→ 安装依赖 → 启动服务 → 打开浏览器。

**方式二（手动）**：

```bash
cd E:\希沃智教π\zhipi-submission\demo
pip install -r requirements.txt
python -m uvicorn app:app --port 8010
```

浏览器打开 <http://127.0.0.1:8010/> 即可。

**运行模式怎么定**：启动时会自动读 `deploy/.env`（若存在），因此配了密钥就直接
进真实大模型链路，不需要手动 export。启动日志会明确打出当前模式：

```
[智批π] 配置来源：...\deploy\.env
[智批π] 批改模式：真实大模型（DeepSeek-V4-Flash）
[智批π] 手写识别：真实多模态（gemini-3.1-flash-lite-preview）
[智批π] 单次批改超时 30 秒（跑批量评测请调大 ZHIPI_LLM_TIMEOUT）
```

没有 `.env` 或密钥为空时退回 **mock 模式**，全程不联网、不消耗 API。
想在配了密钥的机器上临时验证离线表现（比赛断网预案）：

```bash
ZHIPI_SKIP_DOTENV=1 py -3 app.py     # 强制离线演示模式
```

> 命令行显式传入的环境变量**优先于** `.env`，Docker 部署由 compose 的
> `env_file` 注入，不走这条读取逻辑。

> **挂成公开链接**（发给评委/老师点开就能用）见 [`../deploy/README.md`](../deploy/README.md)：
> Docker 一键部署 + 访问口令 + 限流 + 日配额，五分钟上线。
>
> **前端交付自检**（禁表情符号、禁外部请求，比赛断网预案要求）：
> `py -3 tools/audit_static.py static`

## 架构

```text
              浏览器（单页前端，原生 HTML/CSS/JS，零外部请求，完全离线）
                 static/index.html · css/{tokens,app}.css
                 static/js/{icons,motion,app}.js
                                          │  HTTP / JSON
                                          ▼
┌──────────────────────────── app.py （FastAPI） ────────────────────────────┐
│  GET /（读 static/index.html）  ·  /static/* （StaticFiles 挂载）           │
│  GET /api/submissions  ·  POST /api/grade                                   │
│  GET /api/sample-images（/{name}）  ·  POST /api/recognize-image            │
│  POST /api/grade-image  ·  GET /api/grade-progress （图片批改链路）          │
│  GET /api/teacher/results  ·  POST /api/teacher/review                      │
│  GET /api/analytics/class  ·  GET /api/students                             │
│  GET /api/analytics/student/{id}  ·  GET /api/lecture-outline               │
│  POST /api/feishu/push  ·  POST /api/feishu/sync-base （§13.2 飞书集成）     │
│  POST /api/demo/reset  ·  GET /api/demo/config  ·  GET /healthz             │
└───────────────┬────────────────────────────────────────────┬──────────────┘
                │                                              │
                ▼  pipeline（批改流水线）                       ▼  状态分层
   ┌───────────────────────────────────────────┐   ┌────────────────────────┐
   │ ocr.py        手写识别双引擎：多模态大模型 │   │ GRADED  AI 基线（全局   │
   │               （VLM）/ 离线感知哈希样例匹配│   │         共享、只读）    │
   │ grader.py     Rubric 逐步判分 + 证据链    │   │ session_store.py       │
   │               （规则引擎 / LLM 分支），   │   │   教师终审 + 上传件     │
   │               二次批改一致性、双模型交叉验证│   │   按浏览器会话隔离      │
   │ confidence.py §9.7 置信度加权 + 红黄绿分流 │   │ guard.py 口令/限流/配额 │
   │ analytics.py  班级学情聚合 + 学生错因画像 │   └────────────────────────┘
   │               + 讲评课件大纲生成          │
   │ feishu.py     飞书互动卡片 / 多维表格台账 │
   └───────────────────────┬───────────────────┘
                           ▼  data（内置样例）
              questions.json    3 道题（数学 / 物理 / 英语）
              submissions.json  11 份作答（覆盖绿 / 黄 / 红全部分流场景）
              history.json      3 名学生的模拟历史错因（画像时间线用）
              sample_images/    11 张程序合成仿手写作业照片 + manifest.json
                                （tools/gen_sample_images.py 生成）
```

## mock / LLM 模式切换

**批改模式**：

| 模式 | 触发条件 | 说明 |
| ---- | -------- | ---- |
| **mock（默认）** | 不设任何环境变量 | 规则引擎按 11 份作答中每步预标注（对/错、部分分、错因、证据）逐 Rubric 判分，置信度按 §9.7 公式真实计算，全程确定性、不使用随机数。 |
| **LLM（可选）** | 设置 `ZHIPI_LLM_API_KEY`（或只设 `ZHIPI_VLM_API_KEY`，文本批改自动复用该凭据） | 按 Prompt 库 Prompt 1（含证据链 evidence / legible 输出）让 OpenAI 兼容大模型逐步批改并返回 JSON；调用或解析失败自动降级 mock 并标注 `"mode":"mock"`。 |

**手写识别引擎**（图片批改链路）：

| 引擎 | 触发条件 | 说明 |
| ---- | -------- | ---- |
| **sample-match（默认）** | 不设 `ZHIPI_VLM_API_KEY` | 对上传图片计算 SHA-256 + dHash 感知哈希，与 11 张内置样例图片比对，命中返回该样例的预置标准转写；完全离线可演示。 |
| **vlm（可选）** | 设置 `ZHIPI_VLM_API_KEY` | OpenAI 兼容多模态大模型（默认阿里云百炼 `qwen-vl-max`）转写手写文字/公式/英文并自评卷面清晰度，可识别**任意**上传的作业照片；调用失败且命中内置样例时自动降级 sample-match。 |

**可靠性机制**（llm 模式下生效，异常静默跳过，保证 Demo 不中断）：

- **二次批改一致性**：`ZHIPI_DOUBLE_CHECK` 缺省启用（设 `0` 关闭）。同 Prompt 独立复批一次，程序化比对两次总分与错因标签得一致性分，作为 §9.7「LLM 自检一致性」因子（比模型自报 confidence 更可信）；
- **双模型交叉验证（选配）**：`ZHIPI_LLM_API_KEY_2` / `ZHIPI_LLM_BASE_URL_2` / `ZHIPI_LLM_MODEL_2` 三项配齐后，黄/红结果用第二模型独立复核，分差超过满分 15% 视为分歧显著，强制转红交人工。

可选环境变量（均为 OpenAI 兼容 chat/completions）：

```bash
# Windows PowerShell —— 只配一把 VLM Key 即可跑通「拍照 → 识别 → 批改」全链路
$env:ZHIPI_VLM_API_KEY  = "sk-xxxx"
# $env:ZHIPI_VLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 可选，默认百炼
# $env:ZHIPI_VLM_MODEL    = "qwen-vl-max"                                        # 可选

# 文本批改单独指定（可选，如 DeepSeek / Qwen）
$env:ZHIPI_LLM_API_KEY = "sk-xxxx"
$env:ZHIPI_LLM_BASE_URL = "https://api.deepseek.com"   # 可选，默认 DeepSeek
$env:ZHIPI_LLM_MODEL    = "deepseek-chat"              # 可选
python -m uvicorn app:app --port 8010
```

结果 JSON 中的 `"mode"` 字段标明本次实际使用的是 `mock` 还是 `llm`，
`"recognition_engine"` 字段标明识别引擎是 `vlm` 还是 `sample-match`。

## 页面功能（4 个 Tab）

1. **① 拍照提交**：点选 11 张内置仿手写样例图库，或上传本地作业照片（PNG / JPG / WebP，≤ 8MB）→ 手写识别（展示识别引擎、卷面清晰度、命中样例 / 自动判题徽章）→ 转写文本预览（**VLM 模式下可直接修正转写再批改**，体现教师可控；样例匹配模式为预置标准转写，只读）→ 点「提交批改」。**多选或拖入 ≥2 张会自动进入批量流水线**：串行识别 + 批改，进度条与逐张状态，完成后一键去工作台按红黄绿审核。
2. **② 批改结果**：总分、置信度、红黄绿分流；逐批改节点展示对错（✓ 全对 / △ 部分 / ✗ 错）、每步得分、错因标签、知识点，以及**证据链**（引用学生作答原文片段作为判断依据，字迹难辨步骤显式提醒人工复核）；置信度四因子明细，llm 模式下附**二次批改一致性**与**双模型交叉验证**徽章；个性化评语与教师备注。
3. **③ 教师工作台**：全部作答红黄绿审核列表（学生 / 题目 / AI 分 / 置信度 / 状态 / 错因）；绿色可「抽查通过」，全部可「确认」或打开**终审弹窗**——改分（0 ~ 满分校验）、错因标签多选改判（§6.11 十类枚举）、评语修订；提交后显示「该题教师通过率因子回灌」提示，同题未终审作答的置信度实时重算；llm 模式下轮询展示「已批改 x / y」批改进度。
4. **④ 班级看板**：参与作答 / 平均得分率 / 薄弱点 KPI，红黄绿占比，知识点错误率与错因分布条形图（纯 div + CSS 宽度），下节课讲评建议；「讲评课件大纲」一键生成 Markdown（共性错因 + 匿名典型错例证据 + 分层任务 + 5 分钟复测建议）并可**一键复制为课件底稿**（粘贴至希沃白板、飞书文档等备课环境）；「学生个人错因画像」下拉选学生，展示跨题聚合、错因演变时间线（历史为**模拟数据**，界面已标注）与趋势判断；底部「飞书协同」区提供 **推送飞书审核提醒卡片** 与 **同步飞书多维表格学情台账** 两个按钮（详见下方「飞书集成」）。

## 数据与算法要点

- **错因标签** 只使用详细设计方案 §6.11 的 10 类枚举。
- **置信度** = OCR 清晰度 × 0.25 + Rubric 覆盖度 × 0.30 + 二次批改一致性 × 0.30 + 历史教师通过率 × 0.15（百分制）。「答案匹配度」已移除：它需要人工标注的标准答案，而真实作业几乎必然在题库外，比对结果是噪声。
- **分流阈值**：≥ 85 绿色，60 ~ 85 黄色，< 60 红色。
- **典型样例**：物理速度题「10 km/s」单位错误（公式、代入、计算均对，仅单位错，高置信度绿色）完整复现设计方案 §6.3。

## 接口一览

| 方法 | 路径 | 入参要点 | 说明 |
| ---- | ---- | -------- | ---- |
| GET  | `/` | — | 单页前端（读 `static/index.html`，静态资源挂在 `/static/*`） |
| GET  | `/api/submissions` | — | 列出 11 份内置作答（含转写预览与清晰度） |
| POST | `/api/grade` | `submission_id` | 对内置作答走批改流水线，返回过程级批改 JSON |
| GET  | `/api/sample-images` | — | 内置手写样例图库清单（含 `vlm_configured` 标记） |
| GET  | `/api/sample-images/{name}` | 路径参数 `name`（仅允许清单内文件名，防路径穿越） | 返回样例图片 PNG |
| POST | `/api/recognize-image` | `image_base64`、`mime`（图片 ≤ 8MB） | 手写识别：VLM 或离线样例匹配；返回 `engine` / `text` / `clarity`，命中样例附 `matched_submission_id`，未命中时按相似度自动判题附 `question_id` |
| POST | `/api/grade-image` | 命中样例：`matched_submission_id`；任意上传：`question_id` + `ocr_text` + `ocr_clarity`（需 LLM/VLM 凭据） | 图片批改：样例复用既有流水线；任意上传走临时批改（无预置标注，置信度因子冷启动推导） |
| GET  | `/api/teacher/results` | — | 教师工作台全部批改概览（叠加终审态，附 §6.11 错因枚举供多选） |
| POST | `/api/teacher/review` | `submission_id`、`teacher_action`（confirmed / modified）、`final_score`、`final_error_tags`、`final_comment` | 教师终审：确认 / 改分 / 改判错因 / 修订评语；校验分数区间与错因枚举，返回该题最新通过率 |
| GET  | `/api/grade-progress` | — | 批改进度 `graded` / `total`（llm 模式前端轮询用） |
| GET  | `/api/analytics/class` | `class_id`（可选） | 班级学情聚合：薄弱点、错因分布、红黄绿占比、讲评建议 |
| GET  | `/api/students` | — | 班级学生清单（9 名，学生画像选择器用） |
| GET  | `/api/analytics/student/{student_id}` | 路径参数 `student_id` | 学生个人错因画像：本次跨题聚合 + 历史时间线（历史为模拟数据）+ 趋势判断 |
| GET  | `/api/lecture-outline` | — | 生成讲评课件大纲（Markdown 课件底稿，可粘贴至希沃白板 / 飞书文档） |
| POST | `/api/feishu/push` | 无 | 推送审核提醒互动卡片到飞书（§13.2 集成点二） |
| POST | `/api/feishu/sync-base` | 无 | 同步学情台账到飞书多维表格（§13.2 集成点一） |

## 飞书集成

按比赛规则"至少运用 1 项飞书 AI 产品能力"要求，Demo 内置飞书协同模块（`pipeline/feishu.py`），
落地设计方案 §13.2《飞书 AI 产品能力集成设计》的两个可现场演示的集成点。在「班级看板」Tab 底部
点击两个按钮即可触发，返回结果在页面内展示（卡片预览 / 记录表格 + 原始 JSON）。

### 演示模式 vs live 模式

| 模式 | 触发条件 | 行为 |
| ---- | -------- | ---- |
| **演示模式（默认）** | 不配置任何飞书环境变量 | 接口返回 `"mode":"demo"`，做**内容预览**：展示将要推送的卡片内容与将要写入的多维表格记录，页面显著标注"演示模式：未配置飞书凭据，展示将推送的内容"。无网络、无凭据也能跑通。 |
| **live 模式（现场演示采用）** | 配置对应环境变量 | 接口返回 `"mode":"live"`：卡片经自定义机器人 Webhook **真实推送**；台账经开放平台鉴权后 `batch_create` **真实写入**多维表格。任何异常（网络 / 鉴权 / 频控 / 写表报错）自动降级演示模式，保证 Demo 始终可跑。 |

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

### 两个飞书接口

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

## 常见问题

| 问题 | 处理办法 |
| ---- | -------- |
| `python` 命令被解析到 Microsoft Store 占位程序（无输出或弹商店） | 改用 `py -3 -m uvicorn app:app --port 8010`，或直接用 `run_demo.bat` / `run_demo.ps1`（脚本自动探测可用解释器）。 |
| 8010 端口被占用，启动报错 | 换端口启动：`python -m uvicorn app:app --port 8011`，浏览器打开对应地址即可。 |
| 报 `ModuleNotFoundError: No module named 'PIL'`（pillow 未装） | 执行 `pip install -r requirements.txt`（requirements.txt 已包含 pillow，样例匹配识别依赖它）。 |
| 现场无网络环境 | 全功能离线可用：mock 规则引擎批改 + 感知哈希样例匹配识别 + 飞书演示模式预览，全程不发起任何外部请求。 |
