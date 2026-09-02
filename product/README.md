# 智批π Product

这是在不改动旧版 `zhipi-submission/` 的前提下完成的产品化重构。它保留“步骤证据链、红黄绿分流、教师终审、班级学情闭环”的核心价值，但把原先依赖单进程内存的 Demo 状态替换为可持久化、可并发、可测试的实现。

## 现在可以做什么

- 查看 11 份内置三学科作业及步骤级证据链；
- 上传 JPG / PNG / WebP 作业原图，图片会经过真实格式、完整性、像素、动图、长宽比校验，清除 EXIF 定位信息后持久化；
- 配置 OpenAI 兼容多模态模型后，从原图生成学生作答转写；教师核对转写后再进入判分；
- 配置 OpenAI 兼容文本模型后，按教师 Rubric 生成可审计的步骤分、证据、错因和置信度；
- 未配置模型时，内置样例仍可完整演示；新上传作业明确进入红色人工队列，不用关键词规则伪造 AI 分数；
- 教师确认或改分、改错因、改评语，结果写入 SQLite，并生成审计记录；
- 查看班级平均得分率、红黄绿分布、薄弱知识点、错因分布与讲评建议；
- 生成可复制的 Markdown 讲评提纲；
- 用访问口令保护工作台，支持签名 Cookie 或 Bearer Token；
- 使用多个 uvicorn worker：状态已外置到 SQLite 与文件系统，不再受“必须单 worker”限制。

## 快速开始

```powershell
cd E:\希沃智教π\zhipi-submission\product
python -m pip install -r requirements-dev.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8010
```

打开 `http://127.0.0.1:8010`。默认是开发环境和 mock 模式，无需 API Key。

运行测试：

```powershell
python -m pytest
```

Docker 构建使用 `requirements.lock.txt`；本地开发使用带兼容范围的 `requirements-dev.txt`。

## 配置真实模型

复制 `.env.example` 为 `.env`。程序只读取当前 `product/.env`，不会再意外加载旧版 `deploy/.env`；容器中由环境变量注入，不读取文件。

```dotenv
ZHIPI_ENV=development

# 图片转写（Base URL 应包含供应商的 /v1，或直接填写完整 /chat/completions）
ZHIPI_VLM_API_KEY=...
ZHIPI_VLM_BASE_URL=https://example.com/v1
ZHIPI_VLM_MODEL=your-vision-model

# 步骤级批改
ZHIPI_LLM_API_KEY=...
ZHIPI_LLM_BASE_URL=https://example.com/v1
ZHIPI_LLM_MODEL=your-text-model
```

生产环境必须同时配置：

- `ZHIPI_ENV=production`
- `ZHIPI_ACCESS_CODE`：教师工作台访问口令；
- `ZHIPI_SECRET_KEY`：至少 32 位随机密钥，只用于签名 Cookie，不会下发到浏览器；
- `ZHIPI_SECURE_COOKIE=1`：HTTPS 部署必须开启；
- 若启用 VLM/LLM，必须设置 `ZHIPI_VLM_DAILY_LIMIT` / `ZHIPI_LLM_DAILY_LIMIT`。额度计数持久化到 SQLite，多 worker 不会放大配额。

可用以下命令生成签名密钥：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Docker 部署

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少设置生产访问口令、随机签名密钥；启用模型时设置日配额
docker compose up -d --build
```

Compose 默认仅绑定 `127.0.0.1:8010`，建议由 Nginx / Caddy 负责 HTTPS。SQLite 和归一化后的作业图保存在命名卷 `zhipi-data`；容器其余文件系统只读，进程以非 root 用户运行。

## 代码结构

| 路径 | 职责 |
|---|---|
| `app.py` | uvicorn 兼容薄入口 |
| `zhipi/main.py` | FastAPI 工厂、路由、中间件、异常映射与安全响应头 |
| `zhipi/config.py` | `ZHIPI_` 环境配置、产品环境启动校验 |
| `zhipi/db.py` | SQLite schema、短连接仓储、事务、审计、持久化配额 |
| `zhipi/storage.py` | Base64 边界、图片校验、重编码、原子落盘与安全取图 |
| `zhipi/llm.py` | OpenAI 兼容 VLM 转写和 LLM 批改客户端 |
| `zhipi/grading.py` | 置信度、分流、内置样例和模型输出归一化 |
| `zhipi/service.py` | 上传→识别→批改→终审的业务编排 |
| `zhipi/analytics.py` | 班级与学生聚合 |
| `static/` | 零外部资源的教师工作台 |
| `tests/` | API、安全边界、持久化与并发回归测试 |
| `docs/REFACTOR_AUDIT.md` | 旧版问题与重构决策审阅记录 |

## 数据与并发语义

- 当前是“单部署、单教师工作区”的自托管产品 MVP。`ZHIPI_WORKSPACE_ID` 用于隔离部署内的数据命名空间；它不是完整的 SaaS 多租户账号系统。
- SQLite 开启 WAL、外键、10 秒 busy timeout；每次 API 操作使用短连接，教师终审、配额和上传元数据使用显式事务。
- 图片写入采用临时文件 + `fsync` + `os.replace`；数据库写入失败会删除已落盘文件，避免孤儿文件。
- 教师终审采用 upsert，保留创建时间、更新时间及审计事件。界面统计优先采用教师终审分数和错因。
- 文件 API 只接受数据库中的随机 `file_id`，不会把用户文件名拼入路径；响应为 `private, no-store`。

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` / `/readyz` | 存活与持久化就绪探针 |
| `POST` | `/api/auth/login` | 访问口令登录，签发 HttpOnly / SameSite=Strict Cookie |
| `GET` | `/api/questions` | 题库与 Rubric |
| `GET` | `/api/submissions` | 作业、批改与终审状态 |
| `POST` | `/api/submissions/upload` | Base64 图片安全上传 |
| `POST` | `/api/recognize` | 对已上传原图执行多模态转写 |
| `POST` | `/api/grade` | 按确认后的转写批改 |
| `POST` | `/api/teacher/review` | 教师终审（确认/改分/错因/评语） |
| `GET` | `/api/analytics/class` | 班级学情 |
| `GET` | `/api/analytics/student/{id}` | 学生画像 |
| `GET` | `/api/lecture-outline` | Markdown 讲评提纲 |

FastAPI 开发环境的 OpenAPI 文档位于 `/docs`；生产环境默认关闭。

## 明确的产品边界

本重构解决的是可运行、可持久化、可审核的自托管产品 MVP，不声称已经具备教育 SaaS 的全部组织能力。投入真实学校前仍需接入统一身份认证、教师/年级/班级 RBAC、法定监护与数据保留策略、数据库备份和恢复演练、模型供应商数据处理协议、内容安全策略及人工申诉流程。详见审阅报告的“剩余风险”。
