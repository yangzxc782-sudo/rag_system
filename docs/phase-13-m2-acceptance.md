# Phase 13 M2 开发与验收记录

日期：2026-09-28。范围：LangGraph、PostgreSQL Checkpoint、thread 隔离、有限历史、问题理解与改写。

## 结论与授权边界

M2 完成标准已在固定依赖及新建隔离 PostgreSQL 16 中验证。未进入 M3。

- 完整安全后端回归：**1571 passed / 0 failed / 0 errors**，包含原有 1526 项和新增 M2 45 项。
- Phase 13 隔离 PostgreSQL：**54 passed / 0 failed / 0 errors**，包含 M1 原有 35 项和 M2 新增 19 项。
- `pip check`：No broken requirements found。安装前后原有已安装 distribution 版本对比差异为 `{}`。
- 业务库仅由 `codex_ro` 进行 SELECT；最终核验 `transaction_read_only=on`，
  `rag_system` head 仍是 **0008_phase10_enforce**，`langgraph_checkpoints` 不存在。
- 未执行业务库 0009/0010、真实数据清理、外部存储删除或任何共享资源破坏性操作。

开始时 M1 的 25 个文件仍未提交。保留这些成果；M2 仅扩展其中的 Repository、依赖声明和文档。
M1 模型、0009、persistence schema、删除服务及所有 M1/Phase 12 旧测试均未改动。
没有编辑实际 `.env`，没有提交或推送。

## 文件交付

相对于本轮开始，修改 7 个文件：

| 文件 | 变化 |
|---|---|
| backend/app/main.py | lifespan 创建/关闭 Graph 资源；默认关闭；显式配置与业务 Session 同库检查 |
| backend/app/core/config.py | M2 开关、pool、历史与 rewrite 预算/timeout/温度 |
| backend/.env.example | 配置示例及“先授权迁移再启用”说明 |
| backend/pyproject.toml | 增加 LangGraph、Postgres checkpoint、psycopg pool 三项直接依赖 |
| backend/app/services/conversation_repository.py | 封闭 typed rewrite artifact；bounded completed-history 查询和执行身份校验 |
| docs/database-schema.md | 0010 四表、版本来源、权限、降级保护和业务库未迁移事实 |
| docs/phase-13-design.md | 实际 M2 契约、State/节点/预算、短事务、澄清发布边界和 M3 接口 |

新增 13 个文件：

| 文件 | 职责 |
|---|---|
| backend/app/db/langgraph.py | pool 生命周期、只读 readiness、thread-bound Saver 和 attempt 写入保护 |
| backend/app/rag/conversation_state.py | version=1 的封闭引用式 State 和 ExecutionIdentity |
| backend/app/rag/history_budget.py | 整轮历史选择/淘汰、预算估算、澄清上下文和 ID 重验 |
| backend/app/rag/query_rewrite.py | 独立 Prompt、快速路径、严格 parser、来源/参数校验、fallback |
| backend/app/rag/conversation_graph.py | StateGraph、四个节点、invoke/get_state/resume 内部入口 |
| backend/app/schemas/query_rewrite.py | RewriteResult、ResolvedReference、RewriteArtifactDetails |
| backend/alembic/versions/0010_phase13_langgraph_checkpoints.py | 固定上游 schema 的独立 Alembic 迁移 |
| backend/constraints-phase13.txt | 验证组合及必要传递依赖锁定，保持既有模型栈版本 |
| backend/tests/phase13_m2_support.py | 合成 Provider/业务夹具；只在通过 M1 安全门的实例创建测试库 |
| backend/tests/test_conversation_rewrite.py | 38 项改写、预算、状态、parser 和来源校验单元测试 |
| backend/tests/test_conversation_lifecycle.py | 7 项开关、资源释放、启动失败和版本/同库校验测试 |
| backend/tests/phase13_integration/test_langgraph_postgresql.py | 19 项真实 PostgreSQL Graph/Checkpoint/历史/迁移验证 |
| docs/phase-13-m2-acceptance.md | 本记录 |

没有新增第二套 session/message/turn/repository，没有新增 HTTP API、前端、SSE、WebSocket、后台任务、
Hybrid/Reranker 调用或回答生成链路。`ready_for_retrieval` 只是 M3 接入边界。

## 固定依赖与选择依据

| 包 | 实际验证版本 |
|---|---|
| Python | 3.13.9 |
| langgraph | 1.2.12 |
| langgraph-checkpoint | 4.2.0 |
| langgraph-checkpoint-postgres | 3.1.2 |
| psycopg / psycopg-binary | 3.3.4 / 3.3.4（原版本未变） |
| psycopg-pool | 3.3.3 |
| SQLAlchemy / Pydantic | 2.0.51 / 2.13.4（原版本未变） |

选择依据：已发布包的实际依赖元数据可与现有环境共同解析，支持当前同步调用架构，
无需升级已冻结的 Embedding/BGE 模型栈。先固定所有原有包进行 pip dry-run，再安装新增包；
真实导入、StateGraph compile/invoke、PostgresSaver 读写、重建连接和完整回归均成功。

检查了安装包源码的 PostgresSaver 构造、连接借还、实例 Lock、pipeline、put/put_writes 和迁移列表。
上游材料：[LangGraph 1.2.12](https://pypi.org/project/langgraph/1.2.12/)、
[Postgres checkpoint 3.1.2](https://pypi.org/project/langgraph-checkpoint-postgres/3.1.2/)、
[psycopg pool 文档](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)。

约束文件同时固定必须随 LangGraph 安装的 langchain-core/prebuilt/sdk/langsmith 等传递包。
应用未使用这些包建立额外业务链路；执行期间显式关闭外部 tracing。原 Transformers 4.57.6、
sentence-transformers 5.6.0、Torch 2.11.0+cu128、OpenAI 2.44.0 保持不变。
安装方式为 `python -m pip install -c constraints-phase13.txt -e '.[dev]'`；
约束中的 CUDA Torch 对应本项目已有环境，新机器仍需遵循既有模型栈安装方式。

## 架构与数据流

调用方先用 M1 `create_session` / `start_turn` 保存用户问题；
`thread_id=str(qa_sessions.id)`，传入 turn/request/attempt。图结构：

```text
START → load_context → understand_question
                         ├─ standalone / rewritten → ready_for_retrieval → END
                         └─ clarify → stage_clarification → END
```

State 字段为 thread_id、turn_id、request_id、attempt_no、state_schema_version、graph_version、
input_fingerprint、current_message_id、history_message_ids、pending_clarification_turn_id、
rewrite_artifact_id、stage、terminal_status、outcome、error_code。

State 只保存身份、版本、有限 ID 和阶段状态；不保存消息正文、Prompt、检索证据、图谱、连接、
Session、Provider、模型或历史 artifact 列表。schema 禁止额外字段，每次新执行替换全部 State 字段。
state_schema_version=1 / graph_version=phase13_m2_v1，State 上限 8192 字节；框架写入包络另限 32768 字节。

业务表是事实来源。Checkpoint 是执行恢复边界，两种提交不是一个分布式事务。
同 attempt 的 `query_rewrite:v1` 已提交时，节点重新执行先校验并复用，不再次调用 LLM。
节点的业务读取/保存各用短事务；模型等待期间不持有 Session 或行锁。
Saver 每次写入先按 M1 锁序校验当前 session/turn/request/attempt/指纹；过期 attempt 无法覆盖新状态。

### 有限历史和 rewrite

最近最多 6 个 completed 轮次，仅完整 user/已发布 assistant 对。按 sequence_no 取序，
超过 12000 字节或 4096 estimated tokens 时移除较旧整轮。
待澄清原问优先保留；最新一轮或必要澄清过大时明确澄清，不套用更旧对象。
每次由 ID 获取正文均重新限定 session、completed、早于当前轮和单条字节上限。

rewrite 输入含 system Prompt 和消息 ID/正文包络，另外限 20000 字节/8192 estimated tokens。
没有可靠生成模型 tokenizer，按 UTF-8 字节及协议开销保守估算并标注 estimated；不使用 BGE tokenizer。
默认输出最多 768 tokens/8192 字节，温度 0，timeout 15 秒，所有可调预算在配置中。

完整首轮和新话题不调用 LLM；无先行对象或多个合理对象时澄清。
其他追问通过一次既有 Provider 调用返回文本 JSON，按真实 capability 选择 json_mode/think。
parser 拒绝重复键、NaN、Markdown、额外字段、非法 UUID、错误 schema 和超长输出。
改写必须由有来源的 surface→referent 替换或省略补全构成，不允许自由增加工艺内容。
数字、单位、材料/牌号和常见参数词须有当前/历史 user 来源；assistant 仅能提供短对象/方法标签。

LLM timeout、非法 JSON/schema、无来源参数或不属于本 thread 的引用 ID均有确定 fallback：
完整问题保持原问，依赖历史的问题 clarify；不保存原始错误、响应或 Prompt。
artifact 保存 typed RewriteResult、历史 ID、澄清关系和预算/usage，State 只保存它的 UUID。

澄清节点不发布 assistant。集成测试通过 M1 保存 source-free draft 并 publish_answer，模拟未来
M4 发布；重新初始化 Graph 后用户回答“冒口”，可从已完成业务状态恢复原始问题。

### Checkpoint schema、连接和迁移

独立 `langgraph_checkpoints` schema 的 checkpoints/checkpoint_blobs/checkpoint_writes/
checkpoint_migrations 结构来自已安装 Saver 的十条迁移，schema SHA-256 固定在代码和 0010。
0010 使用新表普通索引实现最终等价结构，不更新 QA 业务数据。
实际 revision 缩短为 **0010_phase13_checkpoints**，避免超过原 Alembic version_num 的 32 字符限制。

lifespan 默认不启用 M2；启用后创建独立同步 psycopg pool（1–8 连接，autocommit、dict_row、
prepare_threshold=0、固定 schema search_path），每次 Graph 调用使用独立 Saver。
startup 只读 health 检查，缺少 0010 或版本不符明确失败。没有 `.setup()` 和隐式 DDL。
正常退出、pool readiness 失败、Provider 构造失败均有关闭测试。

downgrade 在锁住表后检查是否有恢复数据；存在任意 checkpoint/blob/write 即拒绝并回滚。
空扩展安全回到 0009 保留 QA 数据，然后可重新升级。不会隐式删除已使用的 Checkpoint。

## 实际测试及关键证据

| 分组 | 数量及证据 |
|---|---|
| M2 单元测试 | 45；完整首轮、话题切换、代词、省略、双对象、澄清选择、assistant 方法指代、参数来源、parser、预算、State、lifespan |
| M2 PostgreSQL 集成 | 19；见下表，使用真实 PostgresSaver 和真实 M1 Repository |
| M1 PostgreSQL 回归 | 原 35 全通过，覆盖十项 M1 验收、并发唯一约束、回滚、legacy 和删除清理事务 |
| M1/删除安全单测 | persistence 19 + QA 清理 3 + 删除服务 16，包含在完整回归中 |
| 旧 RAG 回归 | API 29 + service 20，包含在完整回归中；旧 /rag/ask 调用链未改 |
| Phase 12 冻结契约 | reranker contracts 22 全通过，包括 once-only Hybrid K/C、fail-open、删除先于模型、引用顺序；其余模型/检索测试也包含在完整回归 |
| 完整安全后端 | 1571 passed，最终用时 17.45 秒；无历史遗留失败或跳过 |
| Phase 13 隔离套件 | 联合运行 54 passed / 40.18 秒；最终 artifact-key 校验后 M2 再验 19 passed / 14.17 秒；无失败/跳过 |

M2 集成场景：

1. thread A 的“那它的尺寸呢？”改写成“冒口的尺寸呢？”，无 A 历史的 B 对同问返回澄清；artifact/Checkpoint 分离。
2. pool 关闭并新建 Graph/连接后，两 thread 状态分别仍能读取。
3. 两个完整新问题的快速路径不调用 Provider、不继承 800℃ 旧参数（2 个参数化用例）。
4. “冒口和冷铁”后的歧义必须澄清，M1 发布后新 Graph 接收“冒口”恢复完整原问。
5. 注入 artifact 提交后异常，新 Graph resume 成功，已有 artifact UUID 不变，Provider 调用总计仍为一次。
6. 阻塞旧 attempt 的 Provider，数据库推进 attempt=2 并完成新 Graph；释放旧请求后它被拒绝，新 Checkpoint ID 不变。
7. 两个 thread 同时进入 Provider 屏障，证明不同 thread 没被单个 Saver 全局锁串行化。
8. 历史查询拒绝外 thread message ID，failed 用户问题和外 thread 内容不进入上下文。
9. running/finalizing/failed/needs_recovery 各自带有未发布草稿，全部不作为已完成历史（4 个参数化用例）。
10. 最新历史超预算，返回 context_budget_exceeded，不用旧冒口替代新冷铁。
11. 真实 FastAPI lifespan 关闭并重新创建 app 后读取已保存状态；pool/provider 正常释放且 setup 被测试禁止。
12. 30 轮保留全部 60 条业务消息；每轮最多 12 个历史 ID，State JSON <2 KiB、解码 channel_values <4 KiB，
    稳定窗口大小差 <30 字节；Checkpoint blobs/writes 中不存在合成证据正文哨兵。
13. 上游迁移 SHA、四表、nullable blob、task_path 验证；新数据库从零升级到 0010。
14. 非空 0008→0009→0010 保留 legacy 消息内容、sequence_no=1、turn_id=NULL。
15. 缺少 0010 readiness 失败且 schema 仍不存在；空扩展可安全降级/再升级，QA session 保留。
16. 非空 Checkpoint 降级拒绝，事务回滚后状态和 revision 仍可读取。

### 复现命令和隔离门槛

本次新建 `phase13-m2-test-c738479353cd`，PostgreSQL 16 镜像，端口 `127.0.0.1:65322`。
root database=`phase13_m1_test_c738479353cd`，cluster=`phase13-m1-test-c738479353cd`，
role=`phase13_m1`。保留 M1 原安全门的命名约束，容器标注 M2 专用。
原业务容器、M1 容器和数据卷未修改；测试数据库/数据保留，不运行 DROP DATABASE 或清库。
测试结束后停止本次 M2 容器，保留其数据卷和数据库以供检查。

从 backend 执行：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --basetemp .phase13-m2.tmp/safe_final `
  --ignore=tests/integration --ignore=tests/phase13_integration

# 三项变量必须指向重新确认的专属实例。URL 从本次本地测试记录取得；禁止用业务 DATABASE_URL。
$env:PHASE13_TEST_DATABASE_URL = '<confirmed dedicated test URL>'
$env:PHASE13_TEST_CLUSTER = 'phase13-m1-test-c738479353cd'
$env:PHASE13_TEST_CONFIRMED_DATABASE = 'phase13_m1_test_c738479353cd'
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --basetemp .phase13-m2.tmp/integration_03 tests/phase13_integration
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --basetemp .phase13-m2.tmp/integration_final tests/phase13_integration/test_langgraph_postgresql.py
.\.venv\Scripts\python.exe -m pip check
```

根实例验证 URL、端口、role、database、cluster 后才可建合成数据。
M2 独立 schema 需固定名称，因此 helper 仅在再次验证通过的同一实例中新建随机测试数据库，
不复用业务 public，不放宽原安全门。M1 仍按原方式创建每测试独立 schema。
pytest 临时文件/实例元数据在已忽略的 `.phase13-m2.tmp`，没有加入交付代码。

开发期间发现并修复：指代正则把“应该”的“该”误判；新增开关对旧 SimpleNamespace Settings
的兼容性；两处合成失败状态夹具缺少 M1 要求的 error_code。未修改或放宽旧阶段测试。

## 未执行项与技术限制

- 未执行现有业务库 0009/0010。部署前须另行批准、备份并审核 0009 非空数据前置条件，之后才启用 M2。
- 未运行 `tests/integration` 的 Phase 10 真实多存储破坏性验收；其独立资源/备份/授权门槛未满足。
  原 gate 保持要求 0009，未为本轮降低；M1 PostgreSQL 删除终结事务和安全删除单测全部回归。
- 未调用真实外部/本机生成模型；改写语义验收用合成 Provider，实际 Provider 请求类型、capabilities、
  timeout/usage 与旧实现兼容。真实模型的改写成功率、延迟和工程语料准确率尚未做线上评测。
- 未跑 GPU Final held-out/BGE 质量评估；其冻结行为由原回归契约保护，本阶段没有模型栈升级。
- Python 3.11/3.12、其他 PostgreSQL 版本、真实进程被强制终止/主机掉电未另测；已真实验证新连接、
  新 Graph 和新 FastAPI app 初始化恢复，以及节点提交后故障注入。
- 预算是保守估算；参数/语言指代采用有限词法检查加抽取式来源约束，陌生复杂表达可能要求澄清。
- Checkpoint 单条状态有界，但记录数量会增长；没有自动 TTL/删除。业务表与 checkpoint 不是原子分布式事务。
- 本阶段没有多用户鉴权、HTTP 恢复协调器、全程执行锁。同 attempt 同时执行在尚无 artifact 时仍可能
  产生两次模型调用；已有 artifact 重放和旧 attempt 拒绝已验证。该调度职责留给 M4。

以上限制未替代本轮要求的真实 PostgreSQL 验证；M2 没有未完成的关键技术验证。

## M3 可直接复用

- `ConversationGraph` / `build_conversation_graph` / `ConversationNodes`：在 ready_for_retrieval 边界接入现有 RAG。
- `ConversationState` / `StateContract` / `ExecutionIdentity`：继续保留版本化引用式状态和 attempt 身份。
- `select_history` / `rehydrate_history` / `trim_history`：当前 thread 有预算的已发布历史。
- `QueryRewriter` / `RewriteResult` / `RewriteArtifactDetails`：独立查询、澄清和可重放产物。
- `CheckpointPool` / `ThreadBoundSaver`：生命周期、readiness 和短事务写入校验。
- 继续使用原 M1 `get_artifact_by_key`、`save_artifact`、`save_snapshots`、`get_snapshot`、
  `transition_turn`、`publish_answer`、`record_retrieval`。M3 新长文本仍必须用 snapshot 来源约束，
  不向 State/artifact 任意 JSON 字段塞证据；M2 没有改动现有 Hybrid Search、BGE 或单轮 RAG。
