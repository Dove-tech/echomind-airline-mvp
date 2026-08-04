# EchoMind Airline Care — Interview MVP

一个面向普通旅客的航司智能客服 MVP。它不是“多个 Prompt 串起来”的演示，
而是一个可运行、可追踪、可评测的 LangGraph 多智能体控制闭环：

- `ServiceCoordinatorAgent`：理解、计划、路由和面向旅客的最终回复，不持有业务工具；
- `JourneyServiceAgent`：只读查询航班、PNR、客票、航变和相关知识；
- `RefundServiceAgent`：只读查询退款、支付链路和相关知识；
- 统一 Tool Registry：Schema、域权限、身份边界、超时重试和标准状态；
- 原生 Function Calling：真实模型只提议工具和扁平参数，执行权仍在服务端；
- RAG：PostgreSQL FTS + pgvector + RRF，支持官网快照、版本和原文下钻；
- Embedding：默认真实本地 FastEmbed 中文模型，也可切换远程 API；
- 数据库：业务数据、Trace、知识和 LangGraph Checkpoint 全部使用 PostgreSQL；
- 模型：面试运行档使用 OpenAI-compatible Chat API，离线测试使用确定性 Mock；
- Quality Gate：硬规则阻止无证据引用和“已退款/已改签”等越权宣称；
- Offline Eval：路由、轨迹、证据和安全指标分别评分。

## 快速开始：完整真实基础设施

先复制配置模板，并在 `.env` 中保留/填写你自己的真实 LLM URL、Key 和模型：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,offline]"
Copy-Item .env.example .env
.\scripts\enable_postgres_stack.ps1
docker compose up --build -d
```

第一次启动会下载约 90MB 的中文 Embedding 模型，然后把 6 个官网来源、24 个
官网切块和 8 条内部规则同步到 PostgreSQL。检查状态：

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/health
```

若 8000 已被本机其他服务占用，在 `.env` 中设置
`AIRLINE_MVP_API_HOST_PORT=18000`，并将下方请求地址改为 18000。

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
  "message": "EK302 航班 2026-08-15 已取消，PNR EK7D3M。请查询 TKT3001 的退款进度，并说明 TKT3002 可以如何退款或改签。",
  "verified_subject_id": "subject_demo"
}
```

离线测试仍显式使用确定性 Model Gateway，不访问开发者 `.env` 或 PostgreSQL；
面试运行档使用真实模型。真实模型接入点是
`StructuredLLMGateway`；真实模式会参与理解、工具决策、调查结论和最终旅客
回复。其中领域工具决策使用原生 Function Calling，其他模型边界使用结构化
输出；两者都不会绕过 ToolExecutor、Evidence 和 QualityGate。

## 离线测试与 Eval

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\airline-mvp-eval
```

容器使用 PostgreSQL 17 + pgvector 0.8.2。空数据卷会创建业务、Trace、知识、
导入审计和 LangGraph Checkpoint 表，并写入 5 类演示 Case。已有数据卷通过
`04_migrate_postgres_rag.sql` 只做前向迁移，不会删除记录。

完整配置、原理、验证方式和 Windows/Docker 取舍见：
[真实基础设施接入指南](docs/REAL_BACKENDS_GUIDE.md)。

## 阅读顺序

1. [精简型多轮问答多 Agent 目标架构](docs/QA_MULTI_AGENT_ARCHITECTURE.md)
2. [现有完整设计（待按目标架构改造）](docs/DESIGN.md)
3. [意图识别架构原始方案](docs/INTENT_RECOGNITION_ARCHITECTURE.md)
4. [Function Calling 改造与完整链路](docs/FUNCTION_CALLING_ARCHITECTURE.md)
5. [代码与设计章节映射](docs/CODE_DESIGN_MAP.md)
6. `src/airline_mvp/parent_graph.py`
7. `src/airline_mvp/worker_graph.py`
8. `src/airline_mvp/tools.py`
9. `src/airline_mvp/evidence.py`
10. `src/airline_mvp/evaluation.py`
11. `src/airline_mvp/config.py`

## 安全边界

MVP 没有任何航司业务写工具。`execute_refund`、`change_booking`、`pay_compensation`
等能力不存在于 Tool Registry。旅客提出办理请求时，系统只调查和解释，
随后由确定性代码创建人工接管记录；LLM 无法直接写队列或声称操作成功。
