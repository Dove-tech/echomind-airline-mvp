# EchoMind Airline MVP：除航司 API 外的全真实运行指南

## 1. 最终运行边界

当前面试运行档只有航司业务 API 使用合成 Fixture，其余关键基础设施都是真实实现：

| 能力 | 运行实现 | 是否真实持久化/调用 |
|---|---|---|
| Chat LLM | OpenAI-compatible API | 是，真实网络调用 |
| 工具选择 | 原生 Function Calling | 是，真实模型 `tool_calls` |
| 业务数据库 | PostgreSQL 17 | 是 |
| Case/Trace/Evidence/Handoff | PostgreSQL Repository | 是 |
| LangGraph Checkpoint | `PostgresSaver` | 是 |
| 知识来源与原文 | PostgreSQL | 是 |
| 关键词检索 | PostgreSQL `tsvector` + GIN | 是 |
| 语义检索 | pgvector + HNSW | 是 |
| Embedding | FastEmbed `BAAI/bge-small-zh-v1.5` | 是，本地 ONNX 模型 |
| RAG 融合 | Vector + FTS + RRF | 是 |
| 航班/PNR/客票/退款/支付 | JSON Fixture Adapter | 否，这是唯一 Mock 边界 |

离线单元测试仍保留 SQLite、MemorySaver、LocalKnowledgeStore 和 Hash Embedding，
但测试会在代码中显式注入这些 Adapter，不会成为真实运行档的静默回退。

## 2. 为什么全部放在 PostgreSQL

这个 MVP 数据规模很小，不需要为了展示“向量数据库”单独维护专用向量服务。统一到
PostgreSQL 后，面试时可以清楚解释：

- Case、Trace、Evidence 与知识来源能够放在同一个事务和审计体系中；
- pgvector 负责语义向量距离，PostgreSQL FTS 负责精确关键词；
- RAG 结果可以直接回到 `knowledge_sources` 和 `knowledge_documents` 原文；
- Docker 只需维护一个有状态服务；
- LangGraph Checkpoint 与业务表逻辑隔离，但物理上复用同一个数据库实例；
- 对 32～几千个知识切块，PostgreSQL 的性能已经足够。

代价是三个逻辑模块分别持有连接池：业务 Repository、Checkpoint 和应用进程。
MVP 使用较小 Pool；正式生产可拆分数据库账号、Schema 和连接池预算。

## 3. 数据流

```mermaid
flowchart LR
    WEB["航空公司官网"] --> SYNC["白名单抓取与正文抽取"]
    SYNC --> SNAP["本地 UTF-8 快照 + SHA-256"]
    SNAP --> CHUNK["按正文块切分"]
    CHUNK --> EMB["FastEmbed 中文向量"]
    EMB --> PG[("PostgreSQL + pgvector")]
    PG --> V["HNSW 向量召回"]
    PG --> F["FTS/GIN 关键词召回"]
    V --> RRF["RRF 融合 + 权威微调"]
    F --> RRF
    RRF --> CANDIDATE["政策候选 Evidence"]
    CANDIDATE --> CLAUSE["get_policy_clause 原文下钻"]
    CLAUSE --> ANSWER["有来源支持的旅客回复"]
```

应用请求不会实时访问官网。官网抓取是显式、可审计的离线导入任务；运行时只访问
PostgreSQL 中已经版本化的快照切块。

## 4. 配置文件边界

项目根目录的 `.env` 含真实 Secret，不提交 Git。模板已经切换为 PostgreSQL：

```dotenv
AIRLINE_MVP_LLM_BACKEND=openai_compatible
AIRLINE_MVP_LLM_BASE_URL=
AIRLINE_MVP_LLM_API_KEY=
AIRLINE_MVP_LLM_MODEL=

AIRLINE_MVP_DATABASE_BACKEND=postgres
AIRLINE_MVP_DATABASE_URL=postgresql://airline_mvp:airline_mvp_dev@127.0.0.1:5432/airline_mvp
AIRLINE_MVP_CHECKPOINT_BACKEND=postgres
AIRLINE_MVP_CHECKPOINT_DATABASE_URL=

AIRLINE_MVP_KNOWLEDGE_BACKEND=postgres
AIRLINE_MVP_EMBEDDING_BACKEND=local_fastembed
AIRLINE_MVP_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
AIRLINE_MVP_EMBEDDING_DIMENSIONS=512
```

如果你已经有 `.env`，在 PowerShell 中显式运行：

```powershell
.\scripts\enable_postgres_stack.ps1
```

这个脚本只修改数据库、Checkpoint、RAG 和 Embedding 相关键，不打印也不重写
LLM URL、Key 和模型。若数据库账号或端口不是模板值，请在执行后手动调整 URL。

## 5. 推荐启动方式：Docker Compose

Docker Compose 包含三个服务：

| 服务 | 作用 | 生命周期 |
|---|---|---|
| `postgres` | PostgreSQL 17 + pgvector 0.8.2 | 长期运行 |
| `knowledge-sync` | 把本地官网快照向量化并增量写入数据库 | 一次性任务 |
| `api` | FastAPI + LangGraph + 真实 LLM | 长期运行 |

启动：

```powershell
docker compose up --build -d
docker compose ps
docker compose logs knowledge-sync
docker compose logs api
```

第一次启动 FastEmbed 会下载约 90MB 模型。模型缓存在
`echomind-airline-runtime-data` 命名卷中，容器重建后仍然复用。
容器将 `AIRLINE_MVP_DATA_DIR` 固定为 `/app/data`，并把 `HOME`、
`HF_HOME` 和 `XDG_CACHE_HOME` 放到同一个可写 Runtime 卷；这是因为 wheel
安装后的 Python 模块位于 `site-packages`，不能再用模块路径推导仓库数据目录。

端口安全边界：

- PostgreSQL 仅绑定 `127.0.0.1:5432`；
- API 默认仅发布到 `127.0.0.1:8000`；宿主端口被占用时可在 `.env` 设置
  `AIRLINE_MVP_API_HOST_PORT=18000`；
- 容器内 API 监听 `0.0.0.0`，但宿主机端口不会暴露到局域网。

## 6. 已有 PostgreSQL 命名卷如何升级

Docker 官方镜像只会在空数据卷第一次启动时执行初始化 SQL。已有命名卷不能通过
删除重建来“解决”迁移，因为其中可能已经有 Case 和 Trace。

本项目提供只增不删的迁移：

```powershell
docker compose exec -T postgres psql `
  -U airline_mvp -d airline_mvp `
  -f /docker-entrypoint-initdb.d/04_migrate_postgres_rag.sql

docker compose exec -T postgres psql `
  -U airline_mvp -d airline_mvp `
  -f /docker-entrypoint-initdb.d/02b_seed_extended.sql
```

迁移只执行 `CREATE EXTENSION IF NOT EXISTS`、`CREATE TABLE IF NOT EXISTS`、
`ALTER TABLE ADD COLUMN IF NOT EXISTS` 和 `CREATE INDEX IF NOT EXISTS`。
它不会 `DROP`、`TRUNCATE` 或删除已有记录。

## 7. 数据库内容

### 7.1 业务和审计表

- `conversations`
- `messages`
- `cases`
- `tool_calls`
- `evidence_items`
- `service_responses`
- `handoffs`
- `trace_events`

### 7.2 RAG 表

- `knowledge_sources`：官网 URL、承运人、快照路径、抓取时间、内容 Hash；
- `knowledge_documents`：版本化切块、有效期、承运人范围、FTS 和 Embedding；
- `knowledge_ingestion_runs`：每次同步的开始、结束、状态、来源数和切块数。

### 7.3 LangGraph 表

`PostgresSaver.setup()` 会创建 `checkpoints`、`checkpoint_blobs`、
`checkpoint_writes` 和迁移表。它们与 Case Repository 使用不同协议。

### 7.4 演示数据

初始化 SQL 幂等写入 5 类 Case：

1. Journey + Refund 跨域成功调查；
2. 写操作需要人工接管；
3. 缺少订单引用需要澄清；
4. 上游退款系统超时的降级回复；
5. 无需身份验证的公开航班查询。

Fixture 另外提供 EK302 / PNR `EK7D3M` / TKT3001 / TKT3002，专门用于与
Emirates 官网政策形成一致的端到端 Demo。

## 8. 官网资料与导入方式

当前白名单包含 6 个 Emirates 中文官网来源：

- [行李指南](https://www.emirates.com/cn/chinese/before-you-fly/baggage/)
- [客户服务计划](https://www.emirates.com/cn/chinese/information/our-customer-service-plan/)
- [取消或变更预订 FAQ](https://www.emirates.com/cn/chinese/help/faq-topics/cancelling-or-changing-a-booking/)
- [托运行李规定](https://www.emirates.com/cn/chinese/before-you-fly/baggage/checked-baggage/)
- [随身行李规定](https://www.emirates.com/cn/chinese/before-you-fly/baggage/cabin-baggage-rules/)
- [行李延误或损坏说明](https://www.emirates.com/cn/chinese/before-you-fly/baggage/delayed-damaged-baggage/)

这些页面只服务 EK 场景；代码通过 `carrier_codes` 隔离，不能把它们作为 CZ
或其他航司的政策回答。

来源清单：

```text
data/knowledge/airline_mvp/official_sources.json
```

本地正文快照：

```text
data/knowledge/airline_mvp/official_snapshots/*.txt
```

生成后的 24 个切块：

```text
data/knowledge/airline_mvp/official_policies.json
```

刷新官网快照但不连接数据库：

```powershell
.\.venv\Scripts\python.exe -B scripts/sync_official_knowledge.py --fetch-only
```

只把现有快照同步到 PostgreSQL：

```powershell
.\.venv\Scripts\python.exe -B scripts/sync_official_knowledge.py --database-only
```

同时刷新并同步：

```powershell
.\.venv\Scripts\python.exe -B scripts/sync_official_knowledge.py
```

安全措施：

- URL 来自固定 Manifest，不接受命令行任意 URL；
- 只允许 `https://www.emirates.com`；
- 重定向后再次检查 Host；
- 单页面限制 5MB；
- 只抽取 `<main>` 中的标题、段落和列表；
- 只保留配置关键词附近的正文块；
- 保存抓取时间和 SHA-256；
- 新版本把旧版本标记为 `superseded`，但不会删除旧快照记录。

## 9. 混合检索实现

`PostgreSQLKnowledgeStore.search()` 执行两路查询：

1. `embedding::vector(512) <=> query::vector(512)`，使用余弦 HNSW；
2. `search_vector @@ plainto_tsquery('simple', query)`，使用 GIN。

两路结果通过 Reciprocal Rank Fusion 合并：

```text
RRF score = Σ 1 / (60 + rank)
```

再对 `airline_official_web`、`official_policy` 和 `approved_faq` 做很小的权威
等级微调。这里不直接相加 vector distance 和 FTS rank，因为它们不在同一量纲。

检索还强制过滤：

- Domain；
- `active` 状态；
- `valid_from` / `valid_to`；
- 承运人代码；
- 当前 Embedding Provider 与维数。

通用内部规则使用承运人 `*`。Emirates 官网文档只允许 `EK`。查询 `CZ` 时不会
召回 Emirates 政策，避免跨航司污染。

## 10. 为什么仍需 `get_policy_clause`

`search_airline_knowledge` 只返回候选摘要。模型随后必须使用候选中的
`documentId + version + section` 调用 `get_policy_clause`，重新从 PostgreSQL
读取原文。

Evidence 中会保留：

- `sourceUrl`
- `sourcePath`
- `contentSha256`
- `version`
- `section`
- `retrievedAt`

因此摘要不会自动升级成最终事实，关键结论能够回到原始官网页面和本地快照。

## 11. 完整 Demo 请求

```powershell
$body = @{
    message = "EK302 航班 2026-08-15 已取消，PNR EK7D3M。请查询 TKT3001 的退款进度，并说明 TKT3002 可以如何退款或改签。"
    verified_subject_id = "subject_demo"
} | ConvertTo-Json

$result = Invoke-RestMethod `
    -Method Post `
    -Uri http://127.0.0.1:8000/v1/chat `
    -ContentType "application/json" `
    -Body $body

$result | ConvertTo-Json -Depth 20
Invoke-RestMethod "http://127.0.0.1:8000/v1/cases/$($result.case_id)/trace" |
    ConvertTo-Json -Depth 20
```

预期健康检查：

```json
{
  "status": "ok",
  "modelBackend": "openai_compatible",
  "databaseBackend": "postgres",
  "checkpointBackend": "postgres",
  "knowledgeBackend": "PostgreSQLKnowledgeStore",
  "embeddingBackend": "local_fastembed",
  "airlineApiBackend": "fixture",
  "writeBusinessToolsEnabled": false,
  "rag": {
    "backend": "PostgreSQLKnowledgeStore",
    "sources": 14,
    "activeDocuments": 31,
    "embeddedDocuments": 32,
    "vectorExtension": "0.8.2",
    "hnswIndexes": 1,
    "hybridSearch": true
  }
}
```

其中数量会随官网版本同步而增长；后端类型、vector 扩展、HNSW 索引和混合检索
标志才是健康检查中的稳定断言。

## 12. 数据库验证命令

```powershell
docker compose exec postgres psql -U airline_mvp -d airline_mvp -c `
  "SELECT COUNT(*) FROM knowledge_documents WHERE embedding IS NOT NULL;"

docker compose exec postgres psql -U airline_mvp -d airline_mvp -c `
  "SELECT source_id, carrier_code, source_url FROM knowledge_sources ORDER BY source_id;"

docker compose exec postgres psql -U airline_mvp -d airline_mvp -c `
  "SELECT indexname FROM pg_indexes WHERE tablename='knowledge_documents';"

docker compose exec postgres psql -U airline_mvp -d airline_mvp -c `
  "SELECT status, COUNT(*) FROM cases GROUP BY status ORDER BY status;"

docker compose exec postgres psql -U airline_mvp -d airline_mvp -c `
  "SELECT COUNT(*) FROM checkpoints;"
```

## 13. 测试

普通测试不会连接 PostgreSQL：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\airline-mvp-eval
```

显式运行真实 pgvector 集成测试：

```powershell
$env:AIRLINE_MVP_TEST_POSTGRES_URL = `
  "postgresql://airline_mvp:airline_mvp_dev@127.0.0.1:5432/airline_mvp"
.\.venv\Scripts\python.exe -m pytest tests/test_postgres_rag_integration.py
```

## 14. 当前限制

- 官网抓取目前只支持白名单中的 Emirates 中文网页，不处理 PDF 和 JavaScript
  动态 API；
- PostgreSQL FTS 的 `simple` 配置对中文分词有限，中文召回主要依赖真实
  Embedding，FTS 作为精确英文/编号补充；
- 官网内容适用于 Emirates，不能回答 CZ 等其他航司的专属政策；
- 航司业务 API 仍是 Fixture，因此航班、PNR、客票和退款状态不是实时生产数据；
- FastEmbed 第一次下载依赖互联网，之后可以离线使用缓存；
- 当前 MVP 不执行退票、改签或补偿写操作。

这些限制都是显式边界，不会通过 Mock 回退或自然语言进行掩盖。
