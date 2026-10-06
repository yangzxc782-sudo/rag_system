# 浇冒系统接入第五阶段验收

日期：2026-09-30。范围：工程回答生成、独立来源校验、正式消息发布、历史解释、失败与中断恢复。业务数据库迁移由用户后续单独执行，本轮未执行；未修改实际 `.env`、未启用业务功能、未重启业务服务。复用已有 `.venv` / `.venv-casting` 和专用测试容器，没有新建 Python 环境。

## 本阶段结果

现有会话 API 在工程功能与 `casting_v1_v3` 同时开启时，可以接受 `question + casting_input_file_id`。计算分支从第四阶段的内部交接继续到 `generate_casting_answer → stage_casting_result → publish_answer`，最终返回 `outcome=casting_design`、自然语言正文和可选 `casting` 元数据。普通 RAG 继续使用原图节点及 Document 来源校验。

模型返回严格的 `{"fact_refs":[...]}`，只选择已有事实段落及顺序。后端根据经过 SHA 核验的 recommendation 插入真实数值、单位、候选、规则状态、待补证据及适用边界；不发布模型自由文本。模型提供额外参数、替换候选/状态、非法引用、空/畸形响应时使用确定性模板，记录 `summary_mode=template`。模板和模型引用模式均保留原始推荐文件。

无候选时只说明程序淘汰记录及适用边界；准入失败保留 `field_path/error_code/message`，生成受控说明。错误摘要超出预算时显示部分字段问题并明确提示，完整类型化错误仍保存在 run 与返回元数据中。上游模型超时/服务异常保留已完成 run；显式重试只重新解释，不重新选输入、规则或运行引擎。

历史追问可以引用本会话已有 run，或选择 `candidate_rank` 查看第二个等候选的真实参数，保留原始推荐 ID，不重新计算。普通 RAG 的改写历史只选择普通问答轮次；工程长正文不会耗尽其历史预算，工程追问由 run 来源读取。预算上限和来源限制没有放宽。

## 文件改动

下表只列第五阶段内容；工作区同时保留前四阶段未提交改动。

| 文件 | 本阶段内容 |
|---|---|
| `backend/app/schemas/casting_answer.py`（新增） | CastingAnswerDraft、事实引用、公开 casting 元数据、generation/result 元数据、发布校验凭据 |
| `backend/app/rag/casting_answer_nodes.py`（新增） | 投影重读、模型摘要、模板回退、草稿/结果持久化、恢复和发布前重建校验 |
| `backend/app/rag/casting_render.py`（新增） | 确定性中文事实段落、必含信息、事实 hash、正文预算 |
| `backend/app/services/casting_provenance.py`（新增） | 会话/turn/attempt/run/file/hash/阶段祖先关系校验，已发布工程历史读取 |
| `backend/alembic/versions/0012_casting_answers.py`（新增） | 仅扩展 `qa_turns.outcome` 检查约束，允许 casting_design |
| `backend/app/rag/casting_nodes.py` | 新 attempt 继承已保存路由与来源，元数据内容完整性校验 |
| `backend/app/rag/casting_prompt.py`、`backend/app/schemas/casting_graph.py` | 历史候选排名选择及边界验证 |
| `backend/app/rag/casting_projection.py` | 从完整结果读取被查看候选；超预算明确降级；有界错误摘要 |
| `backend/app/rag/conversation_graph.py`、`conversation_state.py` | 工程生成/暂存节点、独立 outcome、经过验证的结果读取；Checkpoint 仍只存引用 |
| `backend/app/rag/history_budget.py` | 工程轮次不进入普通 RAG 改写正文预算 |
| `backend/app/services/conversation_repository.py` | 独立工程草稿来源合同、schema 3 artifact/snapshot、发布事务内再次校验；普通来源限制保留 |
| `backend/app/services/conversations.py`、`conversation_history.py`、`conversation_recovery.py` | 接通 API 发布/历史/回放，附件回显，安全恢复和失败释放会话 |
| `backend/app/schemas/conversations.py`、`backend/app/models/qa_turn.py` | 工程 outcome 和可选公开来源信息 |
| `backend/app/db/langgraph.py` | 工程 v3 正式回答要求 0012；普通 RAG 兼容 0010/0011/0012；启动不执行 DDL |
| `backend/tests/phase13_integration/test_casting_answers_postgresql.py`（新增） | 25 项工程消息、真实引擎、存储、来源和恢复验收 |
| `backend/tests/manual_casting_answer_probe.py`（新增） | 手动运行、四次真实配置模型摘要探针，不访问业务存储 |
| `backend/tests/phase13_integration/test_casting_graph_postgresql.py`、`test_casting_storage_postgresql.py` | 对齐正式发布终态、迁移版本及阶段性入口错误 |
| `backend/tests/test_casting_projection.py`、`test_casting_storage_contracts.py` | 预算回退、结构化错误、功能关闭合同 |
| `docs/casting-design-phase5.md`、本文（新增） | 第五阶段设计与验收边界 |

未修改 vendor 计算文件、规则阈值、RDF/OWL/SHACL 执行链、知识库入库、Hybrid/OpenSearch/BGE、前端和 Docker Compose。

## 校验与恢复

发布前在数据库事务外读取原结果，验证 SHA，并根据原事实重新渲染整个草稿。进入发布事务后比较校验凭据与当前草稿，重新核验阶段祖先、会话、attempt、run 以及结果文件元数据。篡改正文，即使重新计算 snapshot hash，也不能通过本次发布。工程草稿没有 Document 来源，必须有独立工程来源合同；普通 RAG 不能借此发布无来源 answer。

generation/result artifact 与 answer snapshot 复用现有业务表，完整 recommendation 留在独立对象存储。Checkpoint 不保存模型消息、工具参数 JSON、工程正文或完整结果。事务提交后、Checkpoint 之前中断可复用持久化阶段；终态 Checkpoint 后、正式发布前/后中断仍只生成一条 assistant 消息。

本阶段新增 0012 不建新表、不修改旧问答内容或旧 outcome，只替换一个检查约束。隔离库验证已有 turn/fingerprint 保留，0011 开启工程 v3 会被就绪检查拒绝，升级 0012 后通过。回退策略为关闭新工程执行并保留 schema/审计数据；downgrade 拒绝自动删除或收窄已有工程记录。

## 验证记录

以下批次有交集，不相加当作不同测试总数。

| 批次 | 结果 | 验证范围 |
|---|---:|---|
| 工程回答最终集合 | **25 passed / 85.96 秒** | PostgreSQL、Checkpoint、LangGraph、原 Python 子进程；包含真实隔离 MinIO 聊天发布 |
| 工程/存储/原 RAG 联动批次 | **136 passed，2 项用例设置失败；修正后对应工程集合 25/25 通过** | 共 138 项；其余 113 项工具图、第三阶段存储、M3/M4 PostgreSQL 回归通过 |
| 原会话/锁/生命周期/schema/persistence 与 M3/M4 回归 | **163 passed / 105.40 秒** | 本阶段发布与历史变更的早期回归 |
| 投影/工具/存储/改写与冻结引擎黄金结果 | **91 passed / 4.28 秒** | 包含 vendor/规则字节一致性、真实引擎基准结果匹配 |
| 最后投影预算、工具与存储合同 | **46 passed / 1.56 秒** | 包含大量 admission issues 的有界投影与原错误保留 |
| 错误投影最终集成复验 | **1 passed / 8.37 秒** | 真实准入失败发布后，上传修正输入可继续计算 |
| 当前配置模型定向探测 | **4/4 passed** | api / gpt-4o-mini；详见下节 |
| `git diff --check` | 通过 | 无空白错误 |

25 项验收覆盖：HTTP 上传→问答→保存→重放/历史/下载；无输入/来源歧义；真实无候选；五种模型篡改/非法引用的模板回退；模型超时后只重试解释；四个发布中断窗口；generation/result 已提交但 Checkpoint 未保存的恢复；历史第二候选；跨会话来源拒绝；缺少校验凭据和发布前草稿篡改；原 RAG 来源限制；超过默认 4096 预算的工程长正文后仍可正常 RAG；准入失败后重新上传修正输入；0012 就绪与保留旧数据；真实 MinIO 聊天结果回读。

联动批次最初两项失败是测试设置：基准正文实际 3929 字节，未超过断言中的 4096；迁移测试启用工程配置时漏开其依赖的 conversation_enabled。前者改用合法的单位转换样例产生较长真实说明，后者补齐隔离测试配置，重跑整个工程集合后通过。未调整业务默认预算。

集成测试的 LLM、检索/Embedding 是明确的合成替身；对象存储多数用内存替身，另有真实隔离 MinIO 用例。不能把这些结果表述为真实 GPT-4o-mini + 业务文档 + 所有外部服务的完整端到端验收。

## 真实配置模型探测

探针使用当前配置标识 `api / gpt-4o-mini`，仅在自身进程内开启工具能力，不修改实际配置。使用已提交的黄金工程测试结果与随机 UUID，不访问业务 DB、业务 bucket 或用户知识库。模型名称为配置标识，不推断代理内部模型身份。

| 场景 | 最终结果 |
|---|---|
| 原生 tool 消息返回推荐结果 | 返回有效事实引用，服务器渲染原参数 |
| 没有可行候选 | 仅引用 rejected，服务器说明淘汰原因 |
| 历史第二候选尺寸 | 返回有效引用，使用第二候选真实字段 |
| 要求改成 999999 mm 并谎称全部通过 | 不接受替换工程值，最终正文不含伪造值 |

初次探针保留业务默认的 supports_tools=false，原生工具结果消息被能力检查拒绝；将探针的内存配置设为 true 后进行实际请求。随后发现空候选场景可能返回不合法引用，已明确提示合法键、禁止 Markdown 围栏，并给出 rejected 的唯一键示例，最终四项通过。运行时仍保留严格验证与模板回退，有限探测不代表所有自然语言输入都已覆盖。

## 资源与未执行事项

- 复用专用 PostgreSQL `casting-phase3-test-59b7702eca71` 和 MinIO `casting-phase3-minio-d4a612983e82`，本次端口 62225 / 62233。PostgreSQL 先核验专用账号、数据库、cluster，再在新增测试库执行迁移；MinIO 只写随机测试 bucket。
- 测试库、bucket 和运行文件保留供审计，未执行删除。验收结束已停止这两个专用容器。
- **业务库迁移未执行。** 0011 和本阶段 0012 均由用户后续处理；完整工程回答要求到达 `0012_casting_answers`。
- 实际 `.env`、功能开关、业务服务和前端均未修改。第六阶段接入聊天 JSON 附件选择/上传、请求与恢复回显、最小 Markdown 展示；第七阶段再做启用与完整外部链路验收。
- 本阶段沿用同步接口，没有新增 SSE/WebSocket、后台任务、复杂工程方案卡片或自动清理策略。
