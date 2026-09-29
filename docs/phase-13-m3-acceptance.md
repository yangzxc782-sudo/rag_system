# Phase 13 M3 开发与工程验收报告

日期：2026-09-28。结论：**M3 工程验收完成**。

内部同步 ConversationGraph 已具备“有限历史 → 问题理解 → Hybrid/BGE → 当前文本和图谱证据
→ 多消息 LLM 回答 → 可发布结果暂存 → 终态 PostgreSQL Checkpoint”的完整链路。
新阶段使用真实隔离 PostgreSQL、真实 Repository/PostgresSaver/StateGraph 和原 Hybrid 编排验收；
Embedding、OpenSearch、Neo4j、BGE 和生成 Provider 使用合成替身，未将其称为真实模型质量验收。

本轮没有会话 HTTP API、前端、多用户授权、完整执行协调器、SSE/WebSocket、后台任务或共享 Store。
Graph 不发布助手消息；因此不宣称已交付前端可用的多轮聊天产品。

## 工作区与范围

开始前检查了适用 AGENTS.md、设计和 M1/M2 验收、数据库文档，以及真实调用链、模型、
Repository、schema、Provider、lifespan、删除清理和测试。初始已有 **41 个未提交文件**。
保存了逐文件 SHA-256；M3 在其中 9 个相关文件上增量扩展，另 32 个保持逐字节不变。
没有覆盖、丢弃、格式化无关修改，没有 commit/push，没有编辑实际 `.env`。
M1 模型、0009/0010、Checkpointer、问题改写、有限历史和文档删除实现保持原样。

### 本轮修改文件（11）

| 路径 | M3 增量 |
|---|---|
| backend/app/services/rag.py | 提取共享 retrieve_and_rerank、generate_request；旧调用也用同一逻辑 |
| backend/app/rag/prompt.py | 原图谱 system 规则提为常量，旧 Prompt 文本不变 |
| backend/app/rag/conversation_graph.py | 版本化编译、M3 条件边、结果读取、失败 attempt 边界、v1 恢复 |
| backend/app/rag/conversation_state.py | 保留封闭 v1，新增封闭 State v2 和阶段引用 |
| backend/app/services/conversation_repository.py | 封闭阶段元数据；复用原校验的 save_stage、get_snapshot_by_key |
| backend/app/main.py | 将 lifespan 已有 GraphRetrievalService 交给 ConversationGraph |
| backend/app/core/config.py | M3 图版本及回答总预算配置，整个功能仍默认关闭 |
| backend/.env.example | 上述配置示例和可选已确认模型窗口说明 |
| backend/tests/phase13_m2_support.py | 原 M2 夹具显式指定 phase13_m2_v1，保留原测试断言及历史边界 |
| docs/phase-13-design.md | 添加 M3 当前契约、State、阶段、预算和 M4 接入说明 |
| docs/database-schema.md | 说明 M3 只扩展封闭 JSON 契约，不引入迁移 |

### 本轮新增文件（11）

| 路径 | 用途 |
|---|---|
| backend/app/rag/conversation_nodes.py | 五个 M3 节点、阶段重放、来源/身份验证、结果读取 |
| backend/app/rag/conversation_prompt.py | 多消息回答 Prompt、最终序列化预算和指纹 |
| backend/app/services/chat_evidence.py | 复用 RAG 的检索、文本选取、图谱和预算适配 |
| backend/app/schemas/conversation_rag.py | 检索/证据/生成/结果封闭元数据、snapshot payload、内部结果类型 |
| backend/tests/test_conversation_m3_state.py | v1/v2 闭合结构、重置和三条路由；5 项 |
| backend/tests/test_conversation_m3_retrieval.py | K/C 和 busy/timeout/异常的原冻结契约；7 项 |
| backend/tests/test_conversation_m3_prompt.py | 总预算、图谱降级/映射、历史引用隔离；5 项 |
| backend/tests/test_conversation_m3_contracts.py | 禁止正文元数据、结果引用和执行指纹；7 项 |
| backend/tests/phase13_m3_support.py | 合成外部服务；实际 Hybrid、实际 PG 来源过滤和 M1 发布模拟 |
| backend/tests/phase13_integration/test_conversation_m3_postgresql.py | 真实 Graph/Repository/Checkpoint/snapshot 组合；39 项 |
| docs/phase-13-m3-acceptance.md | 本报告 |

未改 `backend/app/api/v1/rag.py`、`backend/app/schemas/rag.py`、LLM Provider、Hybrid 实现、
BGE 生命周期/配置校验、前端、删除安全门或任何旧测试断言。原 M1/M2 测试文件保留。
`pyproject.toml`、constraints、模型栈依赖没有 M3 增量。

## 依赖、生命周期与数据库

沿用 M2 已验收组合：langgraph **1.2.12**、langgraph-checkpoint **4.2.0**、
langgraph-checkpoint-postgres **3.1.2**、psycopg/binary **3.3.4**、psycopg-pool **3.3.3**。
本机 Python **3.13.9**，隔离 PostgreSQL **16**。`pip check`：No broken requirements found。
无依赖安装/升级；没有增加生成 tokenizer，也没有加载实际 BGE/Embedding 模型。

lifespan 继续拥有 M2 的独立 psycopg pool 和既有 Provider factory 创建的客户端。
M3 向节点传入同一个 Provider 和 app.state.graph_retrieval，不新增 BGE 或图谱服务实例。
每次执行使用独立 Saver，共用连接池；关闭和重新创建 FastAPI app 后可读取已保存结果。
启动健康检查仍只读，不执行 `.setup()`，没有新 DDL 或迁移文件。
新/非空数据库 0008→0009→0010、缺少0010拒绝启动、非空 Checkpoint 禁止降级的 M2 测试仍通过。

现有业务库经 PostgreSQL MCP **只读**核验：

```text
database = rag_system
role = codex_ro
transaction_read_only = on
migration_head = 0008_phase10_enforce
checkpoint_schema = NULL
```

实际配置 `CONVERSATION_ENABLED=false`；未开启生产功能。业务库 **未执行 0009/0010**。
启用前仍需用户单独批准迁移、备份和非空旧数据审阅；本轮没有待执行的 M3 数据库变更。

## Graph、版本与内部结果

```text
START → load_context → understand_question
    standalone/rewritten → retrieve_and_rerank → build_evidence
        有当前文本 → generate_answer → stage_result → END
        无当前文本 → stage_result(no_context) → END
    clarify → stage_clarification → END
```

检索/证据/生成/结果节点的业务错误保存受限 error_code，终态为 needs_recovery，不伪装正常回答。
不可预期的事务/进程异常向外抛出；原 Saver 的 pending-write 只存安全错误码，后续从已提交阶段重放。
M3 的成功 outcome 为 answer/no_context/clarification；terminal_status=result_staged。

State v2 / graph phase13_m3_v2 保留 thread/turn/request/attempt、请求指纹、当前消息 ID、
最多12个历史 ID、待澄清引用、rewrite ID；新增 retrieval/evidence/generation/result artifact ID
和 evidence_generation。完整正文、Prompt、图谱数据、历史对象、数据库连接、Provider 不进入 State。
所有字段每次新 turn/attempt 显式重置，没有 append reducer，State 硬上限8192字节。

原封闭 v1 可以读；v1 resume 会从业务状态重新进入 v2，复用同一 query_rewrite:v1 UUID。
不会用新增字段强行解释 v1，也不会续跑已移除的 ready_for_retrieval 节点。
共用节点指定对应版本的 input_schema，防止 M2 方法注解让 LangGraph 丢掉 v2 字段。
M2 兼容夹具明确选择原 graph_version，所以原19项 PG、45项单元验收继续检验原语义。

`ConversationGraph.get_result(session_id, turn_id, request_id, attempt_no)` 返回
`StagedConversationResult`：原问题、answer/outcome、当前引用、已保存 GraphContext、
Provider/model、result_artifact_id、draft_snapshot_id，以及 **checkpoint_complete**。
它只读受控快照；恢复已保存回答不重新访问 Neo4j，也不重新调用 LLM。
快照失效则拒绝返回可发布结果。已发布正文继续通过 M1 get_answer/list_messages 读取。

## 检索、重排和当前证据

共享函数只提取原编排，没有第二套 Hybrid 或 BGE：

| 分支 | 本次真实 Graph + 原 Hybrid 的合成服务验证 |
|---|---|
| 重排关闭 | Hybrid(K) 一次，BGE 0次 |
| 开启，K≤C | Hybrid(C) 一次，BGE一次，按最终排序保留K |
| K>C | Hybrid(K) 一次，BGE 0次，保留原跳过原因 |
| busy/timeout/异常 | 回退已有候选，Hybrid仍一次，原RRF分数不变 |

问题理解后的独立 query 用于检索，当前 turn 的 document_id/K 用于过滤。
“冒口有什么作用？”→“那它尺寸怎么确定？”实际传给 Embedding/Hybrid 的是“冒口尺寸怎么确定？”。
省略追问“有哪些限制条件？”传入“冒口有哪些限制条件？”。
新话题“铝合金热裂产生的原因是什么？”不带旧历史，新的 document_id/K 不继承上一轮。
“冒口和冷铁”后问“它”暂存澄清，检索/生成均不执行；澄清由 M1 模拟发布后，新 Graph 的
“冒口”补答能恢复原问并完成 RAG。thread B 无 A 的历史时，不能把“它”解析成冒口。

文本复用原 builder 和 citation builder，根据最终排序、最终预算得到连续 [1]...[N]。
图谱只从最终保留文本的 kg_refs 查询；所有 provenance 必须落在同一轮实际文本引用中。
每个图谱单元独立 snapshot，依赖多个来源时保留完整 document/chunk 集合。
任一来源删除即清理该整个单元；不清理其他独立有效来源的 snapshot。
图谱失败保留有效文本回答；Embedding/OpenSearch/LLM失败得到明确错误终态。

## Prompt 与总预算

多消息 Prompt 分清系统规则、有限历史、原始用户问题、独立检索问题、当前文本和当前图谱。
历史用于语言理解，不作为本轮检索证据；历史 [n] 改成“历史引用n”。工艺参数/条件不足时
遵循原谨慎回答规则，不把历史助手内容当参数依据。改写校验仍完全复用 M2。

核验当前 active Provider 为 API、model 为 `gpt-4o-mini`，输出配置1024。
OpenAI Docs 公布该模型上下文128000、输出上限16384；这些是公开规格，不能证明本机兼容
服务实际部署窗口。[官方模型说明](https://developers.openai.com/api/docs/models/gpt-4o-mini)
现有 Provider 没有窗口元数据，环境也未安装 tiktoken；未把别名规格硬编码到服务。

回答输入预算默认8192，图谱子预算1024；历史最多6轮且继续受4096估算单位/12000字节限制。
均来自集中配置。若部署配置了 CONVERSATION_MODEL_CONTEXT_WINDOW，再取
`min(输入上限, 窗口 - LLM_MAX_TOKENS - 1024安全余量)`。
预算单位是完整消息 JSON 的UTF-8字节加保守消息开销，标识 estimated_utf8_bytes_v1，
不宣称生成模型精确 token，不使用 BGE/Embedding tokenizer。

先保留系统、两种当前问题和必要历史；固定内容超限才淘汰较旧普通整轮，保护澄清原问。
文本字符上限12000只是旧 builder 的上限，实际按序列化总预算收缩，不与历史/图谱预算简单相加。
文本定稿后才查询图谱；图谱超限丢弃完整证据单元，再检查完整 Prompt。
固定输入也超限则返回 QA_PROMPT_BUDGET_EXCEEDED；没有当前文本则 no_context，避免无依据生成。
实际 usage 另存 metrics，不用它冒充预测预算。

## 持久化、重放和删除

四个阶段 key 为 chat_retrieval:v1、chat_evidence:v1、chat_generation:v1、chat_result:v1，
父引用依次连接 rewrite→retrieval→evidence→generation→result。澄清从 rewrite 直接连接
generation→result。每份产物校验 thread/turn/request/attempt/版本/父ID/输入指纹。
metadata 禁止额外正文；只允许独立 query、有限引用、状态、计数、耗时、usage、Prompt SHA-256。

新 save_stage 复用原 save_artifact/save_snapshots 的验证和写入实现，在调用方短事务中原子
写 artifact + snapshot；检索日志一起提交。若日志写失败，之前 flush 的阶段和 snapshot 全部回滚。
源文档锁先于 session/turn/snapshot；删除清理器没有增加反向 QA 锁。真实 SQL 锁序有断言。
Embedding/搜索/生成替身入口断言业务连接池无借出的连接，验证外部等待不持有业务事务。

阶段提交后、节点返回/Checkpoint前注入崩溃，再新建 Graph/pool 恢复：

| 已提交阶段 | 恢复行为 |
|---|---|
| retrieval | 从candidate snapshots恢复，不重复Hybrid/BGE |
| evidence | 从citation/graph snapshots恢复，不重复图谱查询 |
| generation | 从answer_draft恢复，不重复LLM |
| result | 草稿可读但checkpoint_complete=false；重放补齐终态Checkpoint |

生成成功但草稿事务未提交时崩溃可能重新调用LLM；外部服务没有 exactly-once 承诺。
同 attempt 在没有产物时被并发启动仍可能重复外部调用，完整执行锁属于 M4。
不同 thread 能同时进入生成屏障；旧 attempt 的迟到回答不能写入草稿或覆盖新 Checkpoint。

evidence_generation 固定对应 attempt_no。已知失败终态再次 invoke 不重新Hybrid；
M4 显式重试推进 attempt 后才生成不同代次。证据/Prompt配置变化导致已有产物不再有效时，
安全失败而不静默覆盖不可变产物。业务状态转换由未来协调器决定，M3节点不会擅自发布。

来源在检索后、检索产物提交后、证据提交后、生成期间、草稿提交后或结果提交后失效，都不能
恢复已清理正文或发布失效草稿。结果提交后才删除时，Checkpoint可能仍记录result_staged，
但 get_result 和 M1 publish_answer 的实时来源校验会拒绝；终态Checkpoint本身不是发布许可。
已发布用户/助手正文保留，相关candidate/citation/graph/answer_draft payload为空并保留审计UUID；
另一个有效来源的独立candidate/citation仍可读。

## 实际测试结果

| 分组 | 实际结果 |
|---|---|
| 原RAG/接口/重排/图谱定向回归 | 182 passed，3.56秒 |
| M3单元（4文件） | 24 passed，最终1.56秒 |
| M3真实隔离PostgreSQL | 39 passed，最终33.07秒 |
| M1+M2+M3联合隔离PostgreSQL | **93 passed（35+19+39）**，80.94秒 |
| 完整安全后端，排除两类集成目录 | **1595 passed**，18.94秒；包含M3的24项及全部原安全测试 |
| 最终新增断言：新Graph恢复已有图谱，不查询Neo4j | 2 passed，37 deselected，3.89秒；是39项中的2项增强复验 |
| pip check | 无依赖冲突 |
| git diff --check / Python AST及新增文件空白检查 | 通过 |

计数不重复相加：24项包含在1595项中，39项包含在93项中。
原Phase12冻结22项、旧RAG API29项/service20项、M1 persistence19项、QA清理3项、
删除服务16项均包含在完整安全回归中。无未解决测试失败或历史失败；专用套件没有跳过。
开发期新测试夹具的mock目标错误和State输入投影问题已修正，未放宽旧阶段约束。

39项M3 PostgreSQL关键场景组成：完整生成1；代词/省略/新话题3；澄清重启1；thread隔离1；
no_context1；四阶段崩溃重放4；重排开关/K>C/三类回退6；图谱成功/失败及恢复2；
三类必需外部服务失败3；六个来源失效时点6；删除过滤先于BGE1；旧attempt晚返回1；
新代次重试1；已发布正文及多源清理1；发布前失效1；事务回滚1；锁序/无长事务1；
v1→v2及新turn重置1；长对话1；不同thread并发1；真实FastAPI lifespan1。

长对话实际执行24轮，全部48条业务消息保留，每次历史ID≤12，State序列化<3000字节，
所有保存的channel_values<4096字节；后段状态大小差<600字节（含独立/追问两种工作历史）。
Checkpoint blobs/writes 不包含合成检索/生成正文哨兵。Checkpoint记录数量仍随运行增长，
本轮没有自动TTL或清理任务。

### 测试命令与资源边界

本次新建容器 `phase13-m3-test-238a0f79551a`，purpose标签为 `phase13-m3-isolated-test`，
端口 `127.0.0.1:59014`，root DB `phase13_m1_test_238a0f79551a`，
cluster `phase13-m1-test-238a0f79551a`，role `phase13_m1`。
沿用M1 URL/role/port/database/cluster三重确认，不读取业务DATABASE_URL作默认目标。
M1使用随机独立schema；M2/M3复用已验收helper，在再次校验的同一实例新建随机测试数据库。
0009/0010仅在这些隔离合成数据库执行。未清空现有库、未删库/删卷，测试数据保留。
测试结束只停止本次M3容器；业务容器、M1/M2容器和卷不变。

从 `backend` 执行（临时目录须使用新的路径，Windows权限限制时用已获准的测试执行权限）：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --basetemp .phase13-m3.tmp/safe-full1 `
  --ignore=tests/integration --ignore=tests/phase13_integration

.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  tests/test_conversation_m3_state.py tests/test_conversation_m3_retrieval.py `
  tests/test_conversation_m3_prompt.py tests/test_conversation_m3_contracts.py `
  --basetemp .phase13-m3.tmp/unit-final

$env:PHASE13_TEST_DATABASE_URL = '<专属隔离实例的已确认连接串，禁止业务 DATABASE_URL>'
$env:PHASE13_TEST_CLUSTER = 'phase13-m1-test-238a0f79551a'
$env:PHASE13_TEST_CONFIRMED_DATABASE = 'phase13_m1_test_238a0f79551a'
.\.venv\Scripts\python.exe -B -m pytest tests/phase13_integration -q `
  -p no:cacheprovider --tb=short --basetemp .phase13-m3.tmp/pg-first/full1
.\.venv\Scripts\python.exe -B -m pytest tests/phase13_integration/test_conversation_m3_postgresql.py `
  -q -p no:cacheprovider --tb=short --basetemp .phase13-m3.tmp/pg-first/final-m3
.\.venv\Scripts\python.exe -m pip check
```

测试门槛未降低。原Phase10真实多存储删除测试目录 `tests/integration` 没有执行，其专属资源、
备份、迁移和独立授权条件本轮未满足；M1删除终结事务的真实PG和安全单测已全部回归。

## 未执行项与剩余限制

- 未执行业务库迁移/生产启用、真实MinIO/OpenSearch/Neo4j删除。
- 未调用真实生成/改写模型，没有评估真实回答质量、幻觉率、响应时间或供应商实际窗口。
  测试验证确定性编排、Prompt、来源校验和持久化；不能据此宣称真实模型中文语义质量验收。
- 未运行实际GPU/BGE评测或访问Phase12 Final held-out。冻结行为由原契约及新增适配测试验证。
- 未做真实主机断电/进程强杀、其他Python/PG版本验证；本轮故障注入、新连接、新Graph、新FastAPI
  app重初始化均是真实PG验证。
- 同attempt的全程互斥、访问授权、错误状态协调和正式发布协议属于M4。当前仍为可信本地单用户边界；
  thread隔离不能替代会话所有权鉴权。M3只使用内部UUID接口。
- 保守预算不是精确tokenizer；配置/Prompt变化时恢复会安全拒绝不匹配产物，须显式开始新attempt。
- 业务artifact事务与Saver事务不构成分布式原子事务；通过阶段幂等和终态检测恢复，不承诺外部exactly-once。

上述为明确阶段/环境边界，不是跳过了本轮要求的真实PostgreSQL验收。

## M4 可直接复用

1. `ConversationGraph.invoke/resume/get_state/get_result`：同步完整编排及结果/Checkpoint读取。
2. `StagedConversationResult`：draft_snapshot_id、当前引用/图谱、outcome和checkpoint_complete。
3. `ConversationRagNodes`、`ChatEvidenceService`：已验证的阶段边界和受控证据重放。
4. `StateContractV2`、封闭阶段schemas和stage_fingerprint：完整执行身份、attempt及版本隔离。
5. 原M1请求幂等/状态转换/publish_answer，加save_stage/get_snapshot_by_key，及原M2池/历史/改写。

M4应先鉴权与协调同thread执行，再确认当前attempt终态Checkpoint且草稿仍有效，最后通过
M1短事务原子发布assistant，返回已提交业务结果。这个发布协议尚未由M3节点代办。
