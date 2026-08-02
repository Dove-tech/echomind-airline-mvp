# EchoMind Airline Care — Interview MVP

一个面向普通旅客的航司智能客服 MVP。它不是“多个 Prompt 串起来”的演示，
而是一个可运行、可追踪、可评测的 LangGraph 多智能体控制闭环：

- `ServiceCoordinatorAgent`：理解、计划、路由和面向旅客的最终回复，不持有业务工具；
- `JourneyServiceAgent`：只读查询航班、PNR、客票、航变和相关知识；
- `RefundServiceAgent`：只读查询退款、支付链路和相关知识；
- 统一 Tool Registry：Schema、域权限、身份边界、超时重试和标准状态；
- RAG：真实本地 Chroma 或内存检索；Embedding 可选 Mock Hash/真实 API；
- 数据库：默认 SQLite，可切换本机或 Docker 中的真实 PostgreSQL；
- 模型：默认确定性 Mock，可切换任意 OpenAI-compatible Chat API；
- Quality Gate：硬规则阻止无证据引用和“已退款/已改签”等越权宣称；
- Offline Eval：路由、轨迹、证据和安全指标分别评分。

## 快速开始

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\airline-mvp-demo
.\.venv\Scripts\airline-mvp-eval
.\.venv\Scripts\pytest
```

启动 API（仅在你明确需要服务时运行）：

```powershell
.\.venv\Scripts\airline-mvp-api
```

然后可调用：

```text
POST /v1/chat
GET  /v1/cases/{case_id}
GET  /v1/cases/{case_id}/trace
GET  /health
```

示例请求：

```json
{
  "message": "CZ3101 航班 2026-07-29 取消，PNR AB12CD。一张票退款未到账，另一张请帮我退票。",
  "verified_subject_id": "subject_demo"
}
```

项目默认使用确定性 Model Gateway，因此没有模型 Key 也能跑完整闭环。它只替代
LLM 的意图/工具决策，业务事实仍必须由工具与 RAG 返回。真实模型接入点是
`StructuredLLMGateway`；真实模式会参与理解、工具决策、调查结论和最终旅客
回复，但不会绕过 ToolExecutor、Evidence 和 QualityGate。

## 切换真实基础设施

```powershell
Copy-Item .env.example .env
```

然后在 `.env` 中填写你自己的 URL、Key 和模型名。PostgreSQL 支持原生
Windows 服务和 Docker；本项目更推荐 Docker：

```powershell
docker compose up -d postgres
.\.venv\Scripts\python -m pip install -e ".[dev,postgres]"
```

容器使用 PostgreSQL 17 + pgvector。空数据卷首次启动时会自动创建应用表、
知识文档表，并写入 2 个演示 Case、8 条政策和对应 Trace。初始化 SQL 位于
`scripts/postgres/`；当前 RAG 运行时仍默认使用 Chroma，PostgreSQL 知识表是
后续 pgvector Adapter 的持久化落点。

完整配置、原理、验证方式和 Windows/Docker 取舍见：
[真实基础设施接入指南](docs/REAL_BACKENDS_GUIDE.md)。

## 阅读顺序

1. [完整设计](docs/DESIGN.md)
2. [代码与设计章节映射](docs/CODE_DESIGN_MAP.md)
3. `src/airline_mvp/parent_graph.py`
4. `src/airline_mvp/worker_graph.py`
5. `src/airline_mvp/tools.py`
6. `src/airline_mvp/evidence.py`
7. `src/airline_mvp/evaluation.py`
8. `src/airline_mvp/config.py`

## 安全边界

MVP 没有任何航司业务写工具。`execute_refund`、`change_booking`、`pay_compensation`
等能力不存在于 Tool Registry。旅客提出办理请求时，系统只调查和解释，
随后由确定性代码创建人工接管记录；LLM 无法直接写队列或声称操作成功。
