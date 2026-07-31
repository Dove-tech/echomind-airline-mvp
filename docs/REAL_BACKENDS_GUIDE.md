# EchoMind Airline MVP：Mock 与真实基础设施接入指南

## 1. 改造后的边界

本项目现在支持在不修改 LangGraph 业务图的前提下，逐项切换基础设施：

| 能力 | 默认模式 | 真实模式 | 是否访问外部服务 |
|---|---|---|---|
| 航班、PNR、客票、退款、支付 API | JSON Fixture | 本轮不实现 | 否 |
| 大模型 | `DeterministicModelGateway` | OpenAI-compatible Chat API | 是 |
| 应用数据库 | SQLite | PostgreSQL | PostgreSQL 模式是 |
| LangGraph Checkpoint | SQLite | PostgreSQL | PostgreSQL 模式是 |
| 向量数据库 | 本地持久化 Chroma | 本地持久化 Chroma | 否 |
| Embedding | Hash Embedding | OpenAI-compatible Embedding API | 是 |
| HTTP API | FastAPI | FastAPI | 仅监听本机 |

这里的 SQLite 不是“返回固定结果的假数据库”，而是真实的嵌入式关系数据库。
它被当作本地/Mock 运行档，是因为无需独立服务，且不具备 PostgreSQL 的连接池、
跨进程锁和部署形态。

航司 API 仍保持 Fixture，原因是航班和旅客业务系统通常不是个人项目能够合法
访问的基础设施。Fixture Store 已经放在 Tool Adapter 后面，将来替换航司 API
不需要修改 Graph。

## 2. 为什么推荐在 Windows 上使用 Docker PostgreSQL

对于这个面试 MVP，推荐顺序是：

1. 日常学习和跑测试：SQLite。
2. 展示真实数据库集成：Docker PostgreSQL。
3. 只有想学习 PostgreSQL 在 Windows 上的安装、服务、用户和目录管理时，
   才使用原生 Windows PostgreSQL。

### Docker 的优势

- 项目所需 PostgreSQL 版本、账号、端口写在 `compose.yaml` 中，可复现；
- 不会在 Windows 注册长期后台服务；
- 项目和数据库边界清晰，换电脑后更容易重建；
- 数据保存在命名卷中，停止容器不会丢失；
- 面试时一条 `docker compose up -d postgres` 就能解释环境。

代价是需要 Docker Desktop，内存占用通常高于原生服务。Docker Desktop 没有
运行时，数据库也不可用。

### 原生 Windows PostgreSQL 的优势

- 少一层容器网络；
- 长期运行资源占用通常更直接；
- 可以练习 Windows Service、`pg_hba.conf`、备份和数据库运维。

代价是安装、升级和卸载会影响整个 Windows 环境，端口和数据目录也更容易与
其他项目冲突。对于单人面试项目，这些运维工作通常没有直接业务价值。

结论：**本项目用 Docker PostgreSQL 更合适，SQLite 作为随时可用的保底模式。**

## 3. 第一次安装

在工程目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev,postgres]"
Copy-Item .env.example .env
```

如果暂时只使用 SQLite，不需要安装 `postgres` extra：

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

`.env` 已在 `.gitignore` 中；不要提交真实 API Key。

## 4. 配置真实大模型

编辑 `.env`：

```dotenv
AIRLINE_MVP_LLM_BACKEND=openai_compatible
AIRLINE_MVP_LLM_BASE_URL=
AIRLINE_MVP_LLM_API_KEY=
AIRLINE_MVP_LLM_MODEL=
AIRLINE_MVP_LLM_TEMPERATURE=0
AIRLINE_MVP_LLM_TIMEOUT_SECONDS=60
AIRLINE_MVP_LLM_MAX_RETRIES=2
```

把三个空值填成你自己的信息。`BASE_URL` 应包含服务商要求的 API 根路径，
常见形式以 `/v1` 结尾。不要把 Chat 网页地址填到这里。

真实模型会参与四个位置：

```mermaid
flowchart LR
    U["旅客消息"] --> A["真实 LLM：RequestUnderstanding"]
    A --> P["确定性 Planner：工具白名单"]
    P --> D["真实 LLM：DomainDecision"]
    D --> X["确定性 ToolExecutor"]
    X --> F["真实 LLM：DomainFinding"]
    F --> S["真实 LLM：ServiceResponse"]
    S --> Q["确定性 QualityGate"]
```

其中 Planner、ToolExecutor 和 QualityGate 仍然是代码规则：

- LLM 不能给自己增加工具；
- LLM 不能绕过旅客主体校验；
- 工具输入仍由 Pydantic Schema 校验；
- LLM 声称“已退款成功”会被 QualityGate 阻断；
- 人工接管记录仍由 Repository 创建。

启用真实模式后，复杂双领域 Case 会产生多次模型调用：一次理解，每个领域若干
次工具决策、一次领域汇总，最后一次旅客回复。面试演示前应关注模型费用和速率
限制；离线 Eval 默认应继续使用 `mock`，保证可重复且不产生费用。

### 模型不支持结构化输出怎么办

`StructuredLLMGateway` 使用 LangChain `with_structured_output`。部分所谓
OpenAI-compatible 服务只兼容文本对话，不兼容 Tool/JSON Schema。
这种服务可能在第一次请求时报错。此时需要：

- 换用支持结构化输出的模型/API；
- 或在 `StructuredLLMGateway._invoke_structured` 后新增该供应商专属 JSON
  解析 Adapter；
- 不应静默退回 Mock，因为那会让人误以为真实调用已经成功。

## 5. Docker 启动真实 PostgreSQL

项目已经提供 `compose.yaml`：

```powershell
docker compose up -d postgres
docker compose ps
docker compose logs postgres
```

当健康状态变为 `healthy` 后，填写 `.env`：

```dotenv
AIRLINE_MVP_DATABASE_BACKEND=postgres
AIRLINE_MVP_DATABASE_URL=postgresql://airline_mvp:airline_mvp_dev@127.0.0.1:5432/airline_mvp
AIRLINE_MVP_DATABASE_POOL_SIZE=5

AIRLINE_MVP_CHECKPOINT_BACKEND=postgres
AIRLINE_MVP_CHECKPOINT_DATABASE_URL=
```

Checkpoint URL 留空时复用业务数据库 URL。应用启动时会执行：

- 建立真实 psycopg 连接池；
- 等待数据库可连接；
- 只使用 `CREATE TABLE/INDEX IF NOT EXISTS` 创建应用表；
- 调用 `PostgresSaver.setup()` 创建 LangGraph Checkpoint 表；
- 不执行 DROP、TRUNCATE 或删除迁移。

停止服务但保留数据：

```powershell
docker compose stop postgres
```

`docker compose down -v` 会删除数据卷，本项目不建议执行。

## 6. 原生 Windows 启动 PostgreSQL

如果选择 Windows 安装版，确保 PostgreSQL Service 已启动，再通过 pgAdmin
或 `psql` 创建开发账号和数据库。示例 SQL：

```sql
CREATE ROLE airline_mvp LOGIN PASSWORD '请替换为你自己的密码';
CREATE DATABASE airline_mvp OWNER airline_mvp;
```

然后配置：

```dotenv
AIRLINE_MVP_DATABASE_BACKEND=postgres
AIRLINE_MVP_DATABASE_URL=postgresql://airline_mvp:你的密码@127.0.0.1:5432/airline_mvp
```

如果密码包含 `@`、`:`、`/` 等字符，需要先进行 URL 编码。原生服务和 Docker
默认都使用 5432；二者不要同时绑定同一端口。

## 7. 配置真实 Embedding

Chroma 本身已经是真实向量数据库，默认保存在：

```text
.runtime/chroma/
```

默认 Hash Embedding 只适合测试，它不具备生产级语义能力。启用真实向量：

```dotenv
AIRLINE_MVP_KNOWLEDGE_BACKEND=chroma
AIRLINE_MVP_EMBEDDING_BACKEND=openai_compatible
AIRLINE_MVP_EMBEDDING_BASE_URL=
AIRLINE_MVP_EMBEDDING_API_KEY=
AIRLINE_MVP_EMBEDDING_MODEL=
AIRLINE_MVP_EMBEDDING_DIMENSIONS=
```

填入自己的 Embedding API 信息。应用启动时会实际调用 Embedding API 为政策
文档建索引，查询时也会调用 API 生成 query vector。

不同模型可能输出不同维数，不能混入同一个 Chroma Collection。代码根据
`provider + model + dimensions` 创建隔离 Collection，切换模型不会覆盖旧索引。
API Key 不参与 Collection 名称，也不会写入 Trace。

如果只想验证真实 LLM，不想承担 Embedding 调用：

```dotenv
AIRLINE_MVP_KNOWLEDGE_BACKEND=chroma
AIRLINE_MVP_EMBEDDING_BACKEND=mock
```

这是最适合第一次联调的组合。

## 8. 推荐的渐进式联调顺序

不要一次打开所有真实后端，否则失败时难以定位。

### 第一步：全本地

```dotenv
AIRLINE_MVP_LLM_BACKEND=mock
AIRLINE_MVP_DATABASE_BACKEND=sqlite
AIRLINE_MVP_CHECKPOINT_BACKEND=sqlite
AIRLINE_MVP_KNOWLEDGE_BACKEND=chroma
AIRLINE_MVP_EMBEDDING_BACKEND=mock
```

运行：

```powershell
.\.venv\Scripts\airline-mvp-demo
.\.venv\Scripts\pytest
```

### 第二步：只打开真实 LLM

只修改 LLM 四个配置，完成一次 Demo，确认模型支持结构化输出。

### 第三步：打开 PostgreSQL

启动 Docker PostgreSQL，先切业务数据库，再把 Checkpoint 切到 PostgreSQL。

### 第四步：打开真实 Embedding

最后切 Embedding，确认政策检索结果和生效日期过滤仍正确。

## 9. 启动和验证

启动 API：

```powershell
.\.venv\Scripts\airline-mvp-api
```

查看实际后端：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

全真实基础设施、航司 API 保持 Fixture 时，应看到类似：

```json
{
  "status": "ok",
  "modelBackend": "openai_compatible",
  "databaseBackend": "postgres",
  "checkpointBackend": "postgres",
  "knowledgeBackend": "ChromaKnowledgeStore",
  "embeddingBackend": "openai_compatible",
  "airlineApiBackend": "fixture",
  "writeBusinessToolsEnabled": false
}
```

发起请求：

```powershell
$body = @{
  message = "CZ3101 航班 2026-07-29 取消，PNR AB12CD。退款 RF9001 为什么没到账？"
  verified_subject_id = "subject_demo"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/v1/chat `
  -ContentType "application/json" `
  -Body $body
```

随后使用返回的 `case_id` 查看 Trace：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/cases/你的case_id/trace
```

## 10. 代码入口

| 内容 | 文件 |
|---|---|
| 环境变量读取和校验 | `src/airline_mvp/config.py` |
| Mock/真实 LLM | `src/airline_mvp/model_gateway.py` |
| SQLite/PostgreSQL | `src/airline_mvp/persistence.py` |
| SQLite/PostgreSQL Checkpoint | `src/airline_mvp/checkpointing.py` |
| Mock/真实 Embedding 与 Chroma | `src/airline_mvp/knowledge.py` |
| 所有 Adapter 的统一装配 | `src/airline_mvp/service.py` |
| 后端状态检查 | `src/airline_mvp/api.py` |
| PostgreSQL 容器 | `compose.yaml` |

## 11. 当前仍有意保留的限制

- 航司业务 API 仍是 Fixture，不能查询真实旅客数据；
- 没有业务写工具，不能实际退票、退款或改签；
- 数据表当前由仅向前 `CREATE IF NOT EXISTS` 管理，尚未引入 Alembic；
- API 尚未加入真实 OAuth/OIDC，只用 `verified_subject_id` 模拟认证后的主体；
- 没有外部可观测平台，Trace 存在应用数据库中；
- 没有真实人工客服队列，Handoff 只持久化为结构化记录；
- 真实 LLM/Embedding 的费用、限流和服务可用性由用户选择的供应商决定。

这些限制不会伪装成已实现能力，`/health` 会明确展示当前真实后端。
