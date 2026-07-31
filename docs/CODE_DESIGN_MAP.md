# 代码—设计章节映射

代码中的模块级 Docstring 和关键分支都标注了 `Design §N`。以下表格用于快速定位：

| 代码位置 | 对应设计 | 负责内容 |
|---|---|---|
| `models.py` | §10, §11, §15, §23 | 跨 Agent/工具/Trace Pydantic 合同 |
| `state.py` | §10 | Parent/Worker State 与并行 Reducer |
| `domain_config.py` | §4, §12, §13 | 领域 Agent 配置和权限 |
| `config.py` | §8, §16–§18 | Mock/真实后端环境变量和快速失败校验 |
| `model_gateway.py` | §4, §8, §11, §13, §19 | 离线决策与真实结构化模型调用 |
| `parent_graph.py` | §9, §10, §14, §20 | Coordinator、动态路由、并行、质检、接管 |
| `worker_graph.py` | §12–§15, §19 | 通用 DomainWorkerGraph 与工具循环 |
| `tools.py` | §15, §25 | Tool Registry、Schema、权限、重试 |
| `fixtures.py` | §15, §21, §25 | 版本化只读业务 Adapter |
| `knowledge.py` | §16, §19 | Chroma/Local RAG、Mock/真实 Embedding、原文下钻 |
| `evidence.py` | §19 | ToolResult 到 EvidenceItem |
| `quality.py` | §19, §20, §25 | 引用、执行宣称和接管硬规则 |
| `checkpointing.py` | §17, §18 | Memory/SQLite/PostgreSQL Checkpoint |
| `persistence.py` | §18, §20, §23 | Case/Evidence/Handoff/Trace SQLite/PostgreSQL |
| `service.py` | §8, §17, §21 | 依赖装配与单一调用入口 |
| `api.py` | §17, §23 | FastAPI 传输层 |
| `evaluation.py` | §19, §24 | Offline + Trace-level 评测 |
| `evals/airline_mvp_cases.json` | §21, §24, §25 | 固定回归数据集 |
| `tests/` | §24, §25 | 单元、集成和端到端验收 |

## 面试代码走读路径

1. 从 `parent_graph.py` 解释为什么 Coordinator 没有业务工具。
2. 从 `Send` 解释两个领域任务如何并行且通过 Reducer 汇合。
3. 从 `worker_graph.py` 解释“Agent = 通用图 + 领域配置 + ModelGateway”。
4. 从 `tools.py` 解释为什么 LLM 选择工具不等于获得执行权限。
5. 从 `knowledge.py`/`evidence.py` 解释为什么 RAG 摘要不能直接变成事实。
6. 从 `quality.py`/`persistence.py` 解释只读边界和人工接管幂等。
7. 从 `evaluation.py` 展示轨迹级指标，不只看最终回复。
