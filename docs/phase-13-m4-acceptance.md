# Phase 13 M4 开发与验收记录

日期：2026-09-28。范围：同步会话 API、PostgreSQL thread 执行锁、幂等、正式发布与显式恢复。

## 验收状态

**M4 工程验收完成。** 最终完整安全后端 **1631 passed**；M1–M4 真实隔离 PostgreSQL
联合回归 **128 passed**，均为 0 failed / 0 errors / 0 skipped。
其中 M4 新增单元/API **36 项**、PostgreSQL **35 项**。计数为包含关系，不重复相加。

业务库仅通过 codex_ro SELECT 核验：rag_system、transaction_read_only=on、
head=0008_phase10_enforce，langgraph_checkpoints 不存在。实际配置 conversation_enabled=false。
没有业务迁移、实际 .env 编辑、生产启用/部署、外部存储真实删除、前端、SSE/WebSocket、后台队列。

## 本轮文件

相对 M4 开始时的未提交工作区，修改 6 个文件：

| 文件 | 修改 |
|---|---|
| backend/app/services/conversation_repository.py | 游标列表、重命名、活动/后续轮查询、同 attempt 中断恢复、原子 finalizing+publish |
| backend/app/rag/conversation_graph.py | bind_execution 复用 Graph；每次线性执行 max_concurrency=1，原四个接口签名不变 |
| backend/app/main.py | lifespan 创建/关闭 Conversations 执行池；异常路径同样释放 |
| backend/app/api/v1/router.py | 注册会话路由；旧 /rag/ask 不变 |
| backend/.env.example | 默认关闭及专用本机启动入口说明 |
| docs/phase-13-design.md | M4 实际实现契约 |

新增 13 个文件：

| 文件 | 职责 |
|---|---|
| backend/app/schemas/conversations.py | 严格请求、会话/分页/消息/请求状态/回答与删除来源 DTO |
| backend/app/services/conversations.py | CRUD、同步执行协调、幂等、正式发布、状态查询 |
| backend/app/services/conversation_recovery.py | same/new attempt 决策、错误分类、终态核验 |
| backend/app/services/conversation_history.py | 稳定游标和来源可见性重建，不调用外部模型/检索 |
| backend/app/db/conversation_lock.py | advisory lock、专用物理连接、ExecutionSession、ExecutionBoundSaver |
| backend/app/api/v1/conversations.py | 7 个 HTTP API、统一 envelope、安全异常、no-store |
| backend/app/local_server.py | 固定回环/无代理头重写的 Uvicorn 启动入口和访问边界 |
| backend/tests/test_conversations_schema.py | 输入约束、游标、客户端不能指定执行状态 |
| backend/tests/test_conversations_api.py | 路由开关、访问边界、422/409/404/503、安全异常 |
| backend/tests/test_conversation_execution_lock.py | UUID 锁映射、Checkpoint 身份各字段验证 |
| backend/tests/phase13_m4_support.py | 每个子进程重新验证隔离实例的合成执行/锁/真实 HTTP worker |
| backend/tests/phase13_integration/test_conversation_m4_postgresql.py | M4 真实 PG、事务、跨进程锁、崩溃和 HTTP 重启 |
| docs/phase-13-m4-acceptance.md | 本记录 |

M4 没有新增或修改迁移、ORM 表、State schema、长文本存储结构、依赖版本或模型实例。
M1–M3 既有未提交文件在开始时记录 SHA-256；必要扩展以外保持原样，包括 0009/0010、
Phase 12 RAG/Hybrid/BGE、删除链路、历史测试、原 M1/M2/M3 验收文档和实际 .env。

## HTTP 契约与 M5 接入

统一 `{success,data,error}`，错误 `error={code,message,detail}`，不暴露异常原文、SQL、Prompt 或凭据。
所有会话响应 `Cache-Control: no-store`。不提供通用 Checkpoint API。

| 方法与路径（统一 /api/v1 前缀） | 输入/响应 |
|---|---|
| POST /rag/sessions | `{request_id: UUID}` → SessionCreateResponse；后端生成 thread_id；重复创建返回原会话 |
| GET /rag/sessions | limit=50（1..50）、可选 cursor → SessionListResponse(items,next_cursor) |
| GET /rag/sessions/{thread_id} | SessionDetailResponse，含 active_request 或 null |
| PATCH /rag/sessions/{thread_id} | `{title: 1..255字符}` → 更新后的 SessionCreateResponse |
| GET /rag/sessions/{thread_id}/messages | limit=50、before_seq>0 → MessageHistoryResponse(items,next_before_seq) |
| POST /rag/sessions/{thread_id}/turns | TurnCreateRequest → 200 TurnCreateResponse 或 202 RequestStatusResponse |
| GET /rag/sessions/{thread_id}/requests/{request_id} | RequestStatusResponse，允许对 orphan 状态做短事务对账，不执行模型 |

列表游标包含 `(updated_at,id)`，按两者降序 seek；更新时间发生变化的会话可移动位置，M5 应按
thread_id 去重，游标不承诺跨多次请求的数据库快照隔离。消息按 sequence_no 稳定分页，每页正序。
默认标题为“新会话”，本轮仅手动重命名，不自动覆盖用户标题。legacy 消息保留角色、正文和序号，
不猜测 turn，turn_id/request_id/result 可为 null。

TurnCreateRequest：request_id、question（1..2000 字符、最多6000 UTF-8字节且受配置限制）、
limit（可省略，使用 rag_top_k；1..50）、可选 document_id。问题原文参与指纹，不静默裁剪。
禁止额外 history、checkpoint_id、attempt_no、thread_id 等字段。创建 request_id 与提问 request_id
分别由数据库唯一约束管理；UUID 是标识，不是身份认证。

RequestStatusResponse 含 thread_id、turn_id、request_id、status、outcome、error_code、can_retry、
execution_active、status_url、user_message_id、assistant_message_id、result。
202 带 Location、Retry-After: 1；M5 应查询 status_url，不能因浏览器超时自行改状态或换 request_id。
200/completed 只在最终业务提交后返回，三种 outcome 为 answer/no_context/clarification。

ConversationAnswer 复用 RagCitationItem、RagLlmInfo、RagGraphData 展示格式：question、answer、
context_status、citations、llm、graph，并新增 sources。没有复制检索候选全文到永久 HTTP 缓存。
sources 每个证据单元含 snapshot_id、kind、原 citation_id、document_ids、chunk_ids、status。
删除的 citation 不出现在 citations，其 sources tombstone 仍保留；剩余 citation_id 不重排。
图谱多来源任一失效即移除该单元；仍有效独立文本继续展示。已发布正文始终来自 qa_messages。

| HTTP/错误 | M5 行为 |
|---|---|
| 202，execution_active=true | 保留原 request_id，查询 Location/status_url |
| 409 IDEMPOTENCY_CONFLICT | 同 ID 的问题或参数已改变；不能把改过的请求当作原请求重试 |
| 409 THREAD_BUSY | 等待/查询活动轮次；不同问题尚未创建用户消息 |
| 409 QA_TURN_SUPERSEDED / QA_RECOVERY_CONFLICT | 禁止回改已有后续历史或绕过完整性错误 |
| 404 QA_SESSION_NOT_FOUND / QA_REQUEST_NOT_FOUND | 会话/请求不存在于指定 thread |
| 422 QA_REQUEST_INVALID / QA_PAGE_INVALID | 修正输入/游标 |
| 503 QA_SERVICE_UNAVAILABLE / QA_EXECUTION_LOCK_LOST / 中断码 | 查询请求状态，再依 can_retry 显式重试原请求 |
| QA_EVIDENCE_UNAVAILABLE / 删除相关码 | 旧草稿不能发布；允许时原请求显式重试，开启新 attempt |
| QA_FEATURE_DISABLED / QA_LOCAL_TRANSPORT_REQUIRED / QA_LOCAL_ACCESS_ONLY | 功能关闭或传输边界不满足；不能靠改 Host/XFF 绕过 |

既有 LLM 错误保持项目统一映射，例如 LLM_UNAVAILABLE=503、LLM_TIMEOUT=504、LLM_RATE_LIMITED=429。
状态接口内的安全错误码是恢复判断依据。提问失败 envelope 的 detail 可携带 status_url。

## 本机访问边界

实际只读检查到现有后端以 `uvicorn app.main:app --host 127.0.0.1 --port 8000` 启动，
未发现 nginx/caddy 进程；前端 API 客户端直接访问回环后端。没有改动这些进程或启动参数。
该旧命令未显式禁用 Uvicorn 默认 ProxyHeaders，故不能仅信 request.client 声称安全。

新入口 `python -m app.local_server` 固定 127.0.0.1:8000、proxy_headers=False，
检查 socket peer 并提供仅存在于 ASGI scope 的 capability。普通入口没有 capability，即使 feature
启用也拒绝会话 API。额外拒绝 Forwarded/X-Forwarded-* 和非本机 Host/Origin，防止误代理及浏览器
重绑定；CORS/Host/自报 IP 不作为身份认证。未来启用须人工安排端口和授权迁移，本次未部署。
任何反向代理公开转发此单用户入口都超出安全边界；远程/多用户开放必须先实现真正认证/所有权。

## 执行锁、事务和恢复

锁 key：SHA-256(`rag_system:phase13:thread_execution:v1:` + UUID.bytes) 的前 64 位有符号数。
固定命名域、无 Python hash；理论哈希碰撞只会保守串行，不会授予另一 thread 数据访问。
专用执行连接池由 Conversations 创建、lifespan 关闭，容量/等待时间复用已有池配置。
每个执行者独占一条 PostgreSQL session，锁覆盖创建/恢复、整个 Graph、终态校验及发布。
业务事务与 Saver 操作使用同一物理 psycopg 连接；SQLAlchemy Session 和 Saver 游标均为短事务。
连接之间不共用 thread 记忆。执行连接丢失时禁止换连接继续写；明确 unlock，异常连接 invalidate。

ExecutionSession 返回真实 Session，兼容原 Hybrid 的显式 close；连接互斥仅用于框架内部并行调用。
原 ThreadBoundSaver 的 thread/attempt 行锁校验继续生效。每次 invoke/resume max_concurrency=1，
在已安装上游实现中使 pending writes 和 sync checkpoint 顺序完成；各 Graph 有独立 executor。
本轮联合测试发现并修复了原配置下 pending writes 与下一模型调用偶发重叠的问题，未改旧断言。

参考实现语义：[PostgreSQL session advisory lock](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS)、
[SQLAlchemy 外部连接事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites)。
同时检查了本机冻结版 PostgresSaver、LangGraph executor 与 Uvicorn ProxyHeaders 源码。

正式发布前必须同时满足：业务 request/turn/attempt/指纹匹配；终态 Checkpoint 无 next、
terminal_status=result_staged、error_code=null；result_artifact_id 与 outcome 匹配；
StagedConversationResult.checkpoint_complete=true；没有后续 turn；草稿及所有证据来源有效。
publish_answer(finalize=True) 先取 Document 锁，再取 QA 锁，在同事务中完成 finalizing、
唯一 assistant、completed、outcome、sequence_no 与会话更新时间。任何异常整体回滚。
提交后从业务数据库读取 DTO；不将 get_result 的草稿直接当成功响应。

| 崩溃窗口 | 显式重试策略 | 真实验证 |
|---|---|---|
| user 已提交，无首次 CP | 原 turn/原 attempt invoke，不重复 user | 子进程立即退出，新进程恢复 |
| artifact 已提交，CP 未写 | resume/invoke 重用阶段 artifact | retrieval 已提交后子进程退出，Hybrid 总调用1次 |
| draft 已提交，终态 CP 未写 | 重用 draft，补齐 result/CP | generation 已提交后退出，LLM 总调用1次 |
| 终态 CP 已保存，未发布 | 校验后仅补最终业务事务 | 终态后退出，新进程未重新生成 |
| 发布已提交，HTTP 响应丢失 | 从业务表重读 completed | commit 后立即退出，消息数量仍为2 |

五个窗口均保留 attempt=1。运行中的同 request 返回202，不同 request 返回409；
获取锁到首次用户提交之间无法识别 request 的极短窗口返回 THREAD_BUSY，保留原 ID 重试即可。
无活动执行者的 running/finalizing 经状态查询转 needs_recovery；显式原请求恢复相同 attempt。
已确定结束的 failed，或 M3 needs_recovery 终态中的可重试外部/来源错误，才显式增加 attempt。
结构/身份错误保留 needs_recovery 且不可自动重试，阻止新的不同请求。failed 已有后续轮次不可回改。
外部模型返回而 draft 未提交时可重复模型调用，不承诺外部 exactly-once。

## 测试和环境证据

| 验证 | 最终实际结果 |
|---|---|
| 完整安全后端（排除两个真实集成目录） | 1631 passed，15.53秒 |
| M1–M4 联合 PostgreSQL | 128 passed（35+19+39+35），129.49秒 |
| M4 schema/API/锁身份单元 | 36 passed（17+13+6），包含在1631项中 |
| M4 PostgreSQL | 35 passed，包含在128项中 |
| 短事务修复、跨 thread 并行、真实 HTTP 重启定向复验 | 5 passed，63 deselected；不是最终套件跳过 |
| 终态不匹配/并发创建/新 attempt 定向复验 | 6 passed，29 deselected |
| pip check | No broken requirements found |

JUnit 实际输出保存在 `.phase13-m4.tmp/safe-final.xml` 与 `pg-joint-final.xml`。
安全回归明确包含原 Phase12 冻结22项、旧 RAG API29项/service20项、M1 persistence19项、
QA 清理3项和文档删除服务16项。联合回归保留 M1/M2/M3 全部93项，没有修改或跳过原断言。
初次联合运行出现 M3 短事务时序失败，修复后完整重跑通过；没有遗留历史失败。

35项 M4 PG 构成：CRUD1；代词/省略/切题3；幂等参数冲突3；澄清重建1；thread/no_context1；
已发布删除重读1；同 thread 并发1；不同 thread 并行1；三进程锁1；真实进程崩溃窗口5；
发布事务回滚1；三个来源失效时点3；旧轮次禁止回改1；连接终止/旧 attempt2；多源图谱清理1；
真实 HTTP 进程重启1；慢 pending-write1；并发创建/legacy1；终态身份不匹配4；超时新 attempt1；
同连接 Graph/短事务/发布1。

M4 专用用例覆盖 CRUD/游标/标题/legacy、三种成功结果、
代词/省略/新话题、跨 thread、幂等冲突、并行、三进程 advisory 竞争、五个真实退出窗口、
PG backend 连接终止、旧 attempt 迟到、发布回滚、终态身份四项不匹配、来源删除和新代次，
以及两次独立 Uvicorn 进程中的真实 HTTP/澄清补答/历史读取。外部 Provider/OpenSearch 使用合成实现。

本次新建实例：phase13-m4-test-859fea047bd1，purpose=phase13-m4-isolated-test，
端口127.0.0.1:60693，root DB=phase13_m1_test_859fea047bd1，role=phase13_m1，
cluster=phase13-m1-test-859fea047bd1。M1 URL/role/port/database/cluster 安全门不变，
worker 子进程重新验证 root 和随机测试数据库，禁止业务 DATABASE_URL 作默认目标。
0008→0009→0010、新库到0010、缺失0010拒绝且无DDL、迁移回滚保护由原联合 PG 用例验证。
连接终止仅针对先核实数据库/角色/idle 状态的精确测试 backend PID；os._exit 只发生在专属合成子进程。
所有测试数据保留，不删库/表/卷；本次 M4 测试容器已停止（Exited 0）。
卷 `bc0b1f3e5b8b3bd96b992acc60a9e142fd693abc4c62f8593f721d166968d9ef` 保留；
四个 rag_system 业务容器仍运行，旧 M1/M2/M3 测试实例状态未变。

在 backend 目录执行：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --ignore=tests/integration --ignore=tests/phase13_integration `
  --basetemp .phase13-m4.tmp/safe-final --junitxml=.phase13-m4.tmp/safe-final.xml

$env:PHASE13_TEST_DATABASE_URL = '<本次专属实例连接串，禁止使用业务 DATABASE_URL>'
$env:PHASE13_TEST_CLUSTER = 'phase13-m1-test-859fea047bd1'
$env:PHASE13_TEST_CONFIRMED_DATABASE = 'phase13_m1_test_859fea047bd1'
.\.venv\Scripts\python.exe -B -m pytest tests/phase13_integration -q `
  -p no:cacheprovider --tb=short --basetemp .phase13-m4.tmp/pg-joint-final `
  --junitxml=.phase13-m4.tmp/pg-joint-final.xml
.\.venv\Scripts\python.exe -m pip check
```

临时目录使用新的项目内路径；Windows ACL 限制时使用获准执行权限。不要复用旧 pytest basetemp
去触发自动清理。开发期夹具的目录、URL 拼接和插入位置错误均有单独修复与复验，没有放宽旧测试。

## 未执行与限制

- 未调用真实 LLM/Embedding/BGE 或真实 OpenSearch/Neo4j 作语义质量验收；未使用 Phase12 Final 数据或许可。
- 未运行 tests/integration 的真实多存储破坏性删除；其备份/专属资源/额外授权不在本轮。安全删除单测与合成 PG 清理已回归。
- 未做真实断电或网络分区；已实际终止测试子进程及精确测试 PG 连接，不能将其等同所有网络故障演练。
- 不提供自动后台恢复或过期清理。Checkpoint 记录数仍增长，State 单记录保持有界。
- 连接池满时返回安全503，客户端按原 ID 查询/重试；不启动第二个同 thread 执行者。
- 无真实用户鉴权，不可公开历史列表。远程/多用户部署需后续安全设计；本轮没有部署。

M5 可直接使用本文七个 API、schemas/conversations.py 类型、status_url/Location、can_retry、
三种 outcome、消息序号分页和 sources 删除标记；不需要向后端上传完整聊天历史。

## 完成标准核对

| 标准 | 证据/结论 |
|---|---|
| 七个会话和提问 API 可用 | 真实回环 HTTP + 两个新进程、PG消息/状态核验，通过 |
| 同 thread 全程互斥、不同 thread 并行 | 专用物理连接、三进程锁、模型并发屏障，通过 |
| 请求幂等真实成立 | 并发创建、参数冲突、成功重放、唯一消息与阶段计数，通过 |
| 三种 outcome 正式发布 | answer/no_context/clarification 均 completed，澄清重启补答通过 |
| 终态 CP 与最终发布协议 | 四种身份不匹配被拒绝；发布回滚/补交；提交前另一连接看不到assistant，通过 |
| 五个崩溃窗口可恢复 | 实际子进程退出 + 新进程恢复；同attempt且外部阶段计数不重复，通过 |
| 来源失效/旧attempt/锁断连安全 | 精确测试backend终止、旧执行者无法写回，新attempt与来源清理通过 |
| 历史及引用安全重建 | UUID归属、序号分页、legacy、多源删除tombstone，不调用历史模型/Neo4j，通过 |
| M1–M3及Phase12冻结回归 | 1631安全单元 +128联合PG通过 |
| 未迁移业务库、默认关闭 | codex_ro只读核验0008，Checkpoint schema不存在；实际开关false；.env哈希未变 |

没有待补的本阶段实现或关键测试。未来业务库0009/0010迁移与启用仍需用户单独授权和迁移前检查，
本次工程验收不能替代该授权，也不代表前端聊天产品或生产部署已完成。
