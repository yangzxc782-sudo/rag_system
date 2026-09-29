# Phase 13 M1 开发与验收记录

日期：2026-09-28。范围：PostgreSQL 会话模型、Repository、迁移、QA 证据删除及测试。

## 结论

M1 的十项功能验收已在新建隔离 PostgreSQL 16 实例中通过。
最终安全回归 **1526 passed / 0 failed / 0 errors**；隔离 PostgreSQL 测试
**35 passed / 0 failed / 0 errors**。业务库升级未执行，部署仍需人工确认。

开始时 Git 工作区干净；没有覆盖已有未提交修改，没有提交或推送。
本次不实现 LangGraph、Checkpointer、问题改写、多轮 RAG 编排、HTTP API、前端、
SSE/WebSocket 或任务调度。Phase 12 Hybrid/BGE/RAG 生产逻辑及其测试约束未修改。
没有增加依赖；`pyproject.toml` 仅注册新的 pytest marker。

## 实际文件清单

修改 9 个现有文件：

| 路径 | 修改内容 |
|---|---|
| `backend/app/models/qa_session.py` | 创建请求幂等身份、轮次/消息计数器 |
| `backend/app/models/qa_message.py` | 轮次、消息序号、回答快照引用和归属约束 |
| `backend/app/models/retrieval_log.py` | 轮次/generation、消息归属复合约束 |
| `backend/app/models/__init__.py` | 导出四个新模型 |
| `backend/app/services/document_deletion.py` | 在现有 PostgreSQL 终结事务中调用 QA 清理 |
| `backend/pyproject.toml` | 注册 `phase13_integration` marker |
| `backend/tests/test_document_deletion_service.py` | 原有删除顺序断言保留，增加 QA 清理调用断言 |
| `backend/tests/integration/conftest.py` | 运行时删除测试严格要求 0009；原授权和隔离门槛不变 |
| `docs/database-schema.md` | M1 字段、约束、legacy 迁移和回滚说明 |

新增 16 个文件：

| 路径 | 职责 |
|---|---|
| `backend/app/models/qa_turn.py` | 独立轮次、请求指纹、状态及 attempt |
| `backend/app/models/qa_turn_artifact.py` | 不可变阶段身份和受限指标 |
| `backend/app/models/qa_evidence_snapshot.py` | 可按来源清除的受控证据/草稿正文 |
| `backend/app/models/qa_evidence_source.py` | 文档/分块来源 UUID 和删除审计 |
| `backend/app/schemas/conversation_persistence.py` | 内部持久化输入、视图、指标和草稿类型 |
| `backend/app/services/conversation_repository.py` | 调用方事务中的会话和轮次数据访问 |
| `backend/app/services/document_qa_evidence_deletion.py` | 精确来源清理，不锁 thread/session |
| `backend/alembic/versions/0009_phase13_chat_expand.py` | 独立冻结 DDL、回填、前置检查、回滚保护 |
| `backend/tests/test_conversation_persistence.py` | 模型、离线 SQL、元数据和安全门槛测试 |
| `backend/tests/test_document_qa_evidence_deletion.py` | 清理范围、锁边界、审计幂等及异常传播 |
| `backend/tests/phase13_support.py` | 隔离实例验证和显式连接上的 Alembic 运行器 |
| `backend/tests/phase13_integration/__init__.py` | 测试包入口 |
| `backend/tests/phase13_integration/conftest.py` | 专属实例门槛及每测试独立 schema |
| `backend/tests/phase13_integration/test_conversation_postgresql.py` | 真实 PostgreSQL 持久化、并发、迁移和删除事务测试 |
| `docs/phase-13-design.md` | M1 实施契约、锁序、状态和复用接口 |
| `docs/phase-13-m1-acceptance.md` | 本记录 |

## 核心功能与数据库变更

`qa_sessions.id` 直接作为未来 thread_id。复用旧 QA 三表，新增四表。
同 session 的 request_id、turn_no、sequence_no 分别唯一；每 turn/role 至多一条消息。
消息、日志、父产物、快照、来源用复合外键约束同 session/turn，Repository 再做查询范围校验。

session 创建使用 PostgreSQL ON CONFLICT；轮次创建在 session 行锁内比较请求指纹、
分配序号并写入唯一用户消息。重复请求返回原轮次，不同参数返回 QA_REQUEST_CONFLICT。
不同未完成请求返回 QA_THREAD_BUSY，部分唯一索引再提供数据库保护。

状态支持 running → finalizing → completed，以及 failed、needs_recovery。
只有 `publish_answer` 可以完成轮次：助手正文、消息计数器、完成时间和 outcome 同事务提交。
失败/待恢复轮次可在没有后续轮次时重试并增加 attempt，旧 attempt 的写入会被拒绝。
执行恢复策略和 Checkpoint 协调留给后续阶段。

Repository 不创建连接、不 commit/rollback，调用方使用短 `session.begin()`。
已验证生产同款 `autoflush=False`。来源写入按 Document UUID 排序先锁文档，
再锁 session/turn/snapshot；同一事务已经取得 QA 锁后再要求文档锁会被拒绝。
没有跨模型调用的锁，也没有 advisory lock。

候选片段、引用、图谱和可恢复回答草稿只放 snapshot.payload。
artifact.details / retrieval_logs.result_summary 只允许受限指标结构。
清理器通过规范化来源定位快照，将 payload 设为 SQL NULL，保留来源 UUID、哈希、删除标记和时间。
任一来源被删会清除整个相关图谱单元及依赖草稿；其他独立证据、仍有效来源行和已发布消息保留。
文档已进入 deleting/delete_failed 或已不存在时，安全读取不会返回相关证据正文。

迁移 `0008_phase10_enforce → 0009_phase13_chat_expand` 在事务内进行前置检查和增量扩展。
旧消息按 `(created_at,id)` 在 session 内回填序号，turn_id 保持 NULL，原文/角色/时间戳保留。
不推测旧轮次配对。孤立或跨会话关系会阻止升级，并报告 UUID。
历史检索摘要仅自动接受 SQL NULL、JSON null、空对象；非空内容缺少规范化来源时停止，
需要另行审阅迁移，不能自动删除。新数据已使用时 downgrade 会拒绝；未使用扩展的回滚保留 legacy 数据。

## 十项验收证据

| 验收项 | 实际验证 | 结果 |
|---|---|---|
| 创建后从 PostgreSQL 读取会话 | 独立 Session 读取、创建请求重放和参数冲突 | 通过 |
| 消息顺序稳定 | user/assistant/后续 user 的 1/2/3 序号、游标、legacy 相同时间戳回填 | 通过 |
| 重复 request 不重复写入 | 串行重放、四个并发相同请求、并发创建/发布 | 通过 |
| 相同 request 参数冲突 | question、limit、document_id 分别改变 | 通过 |
| thread 关联隔离 | Repository 拒绝错误归属；原始 SQL 五类跨 thread FK 拒绝 | 通过 |
| 并发约束有效 | 同 thread 不同请求互斥；绕过 Repository 并发验证 request/turn_no/sequence/role 唯一性 | 通过 |
| 中途异常回滚 | 用户插入故障回滚轮次及计数器；发布后故障回滚助手及状态 | 通过 |
| 非空迁移保留历史 | legacy 正常回填/回滚；不安全历史拒绝且版本、数据不变 | 通过 |
| 删除来源保留正文、清除证据 | 真实 PostgreSQL finalizer 清理引用、共享图谱和草稿，保留问答及另一来源 | 通过 |
| 清理失败共同回滚 | 清理后注入异常，文档/分块/job/snapshot/source 全部回滚 | 通过 |

还覆盖 attempt 失效、非法状态、后续轮次阻止旧轮重试、产物/快照不可覆盖、
chunk 与 document 不匹配、锁序反转、源删除可见性、删除与写入两种先后顺序、
实际 schema 与模型列/空值性/命名约束匹配，以及已使用 schema 拒绝降级。

## 执行环境与命令

测试只使用本轮新建的 `pgvector/pgvector:pg16` 隔离实例：

- 容器及 cluster_name：`phase13-m1-test-80119a4c809b`。
- 数据库：`phase13_m1_test_80119a4c809b`；专用角色：`phase13_m1`。
- 本轮端口：`127.0.0.1:61522`；独立持久卷，无业务资源挂载。
- 首次写入前核对数据库、角色、cluster 标记及空库；每个测试使用新的 schema。
- 测试运行器不加载应用 `.env` 或 Alembic env.py，不自动复用业务连接。
- 测试结束已停止该容器，保留容器、持久卷、各测试 schema 和合成数据；未执行资源清理。

最终安全回归（backend 工作目录）：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --basetemp .pytest_cache/phase13_m1_regression_03 `
  --ignore=tests/integration --ignore=tests/phase13_integration
```

结果：1526 passed，耗时 15.47s；1 条既有 Starlette/httpx 弃用提示。
首次完整回归有 40 项因 Windows 沙箱临时目录 PermissionError 未进入测试逻辑，
换新目录并通过执行权限审核后全部通过。没有为通过测试修改 Phase 12 约束。

隔离 PostgreSQL 验证（必须事先设置三项显式隔离环境变量）：

```powershell
# PHASE13_TEST_DATABASE_URL：新建隔离实例的专用 URL，不使用 DATABASE_URL。
# PHASE13_TEST_CLUSTER：与实例 cluster_name 一致。
# PHASE13_TEST_CONFIRMED_DATABASE：与专属数据库名完全一致。
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --basetemp .pytest_cache/phase13_m1_pg_05 `
  tests/phase13_integration/test_conversation_postgresql.py
```

结果：35 passed，耗时 34.75s。初轮两个删除测试的合成 raw object key 不符合既有
manifest 安全规则，修正测试数据后通过，校验逻辑未放宽。
另在清除上述三项环境变量后执行整个新集成目录：35 skipped，未连接数据库，确认默认拒绝执行。
最后新增门槛等定向测试为 38 passed，已包含在 1526 的最终安全回归中，不重复计数。

`git diff --check` 及新增文件行尾检查通过。最终差异仅涉及上述 M1 文件。
现有业务库仅通过 codex_ro 只读查询核验，结束时仍是 0008，QA 三表仍各 0 行。

## 未执行项及部署门槛

- 未在现有 `rag_system` 执行 upgrade/downgrade 或生产数据清理，符合本轮授权范围。
- 未运行 Phase 10 的真实 MinIO/OpenSearch/Neo4j 删除与 Knowledge 并发验收；
  这些需要原有独立资源与授权，既有 Knowledge 锁序问题不在 M1 修复范围。
  M1 已实际运行 PostgreSQL finalizer，但不将合成测试等同于外部存储端到端验收。
- 未重跑 Phase 12 Final 数据集或真实 GPU/LLM 验收；冻结链路由安全回归覆盖。
- 未执行 UI 刷新、后端进程重启后的 HTTP 恢复、LangGraph Checkpoint 恢复；
  对应功能尚属于后续里程碑，M1 验证的是跨事务/Session 的数据库持久化。

人工部署前需审阅 0009 和本记录，确认备份及恢复路径，停止或隔离旧 QA 写入，
明确批准目标数据库升级。批准后在 backend 目录由负责人执行
`alembic upgrade 0009_phase13_chat_expand`，再上线调用新清理器的应用版本。
不能先运行新删除链路而让数据库停留在 0008。迁移发现不安全历史时必须停止审阅，不能绕过检查。
已使用新数据时只回退应用并保留数据库扩展；本轮没有执行需要人工确认的业务操作。

## M2 可直接复用

- `qa_sessions.id`、QATurn/request_id/attempt_no、稳定 sequence_no 和复合归属约束。
- `create_session`、`start_turn`、`get_turn_by_request`、`list_messages`。
- `save_artifact` / `get_artifact_by_key`，无需进程内缓存即可找到阶段身份。
- `save_snapshots` / `get_snapshot` / `list_snapshots`，以及规范化来源和安全读取视图。
- `transition_turn`、`publish_answer`、`get_answer`、`record_retrieval`。
- `SnapshotInput`、`EvidenceSourceRef`、`AnswerDraft`、`PersistenceMetrics`。

后续阶段仍须实现执行协调、Checkpoint 一致性和真实故障恢复，不应把这些接口误认为已有恢复器。
当前仍为可信本地单用户边界；没有因新增 thread_id 而获得用户鉴权能力。
