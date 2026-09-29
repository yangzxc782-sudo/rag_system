# 数据库设计与阶段演进

## Phase 13 M3 阶段产物（无数据库迁移）

M3 继续使用 0009 业务表和 0010 Checkpoint 表，不新增表、列、索引或迁移。
业务库只读核验仍为 `0008_phase10_enforce`，`langgraph_checkpoints` schema 不存在。
本轮所有写入验收都在独立 Phase 13 PostgreSQL 实例完成。

`qa_turn_artifacts.details` 在原 M1 metrics、M2 rewrite 类型之外增加四个封闭判别类型：
`chat_retrieval_v1`、`chat_evidence_v1`、`chat_generation_v1`、`chat_result_v1`。
对应 key 为 `chat_<stage>:v1`，kind 为 retrieval/evidence/generation/result。
元数据只包含受限 query、ID/key、状态、计数/耗时/usage 和 Prompt 哈希，不包含 Prompt 或证据正文。
父产物链为 rewrite → retrieval → evidence → generation → result；clarification 直接从 rewrite
连接到 generation → result。no_context 同样保留 evidence 产物和无来源草稿。

正文均放既有 `qa_evidence_snapshots.payload`，candidate/citation 每片一个 snapshot，graph 每完整
图谱单元一个 snapshot，answer_draft 一份草稿；每份带完整规范化 document/chunk 依赖。
删除任意相关来源清理整个依赖单元，正常文档对应的其他独立 snapshot 保留。
新 `save_stage` 保持 Document→QA 锁序，并在调用方事务内原子写入 artifact 与 snapshot；
检索日志同事务保存，`evidence_generation=attempt_no`，来源失效后的新检索需显式新 attempt。
没有重新实现消息发布、没有修改模型约束或删除终结事务。已发布正文依然在 qa_messages。

Checkpoint 的 State v2 只增加阶段 UUID 和 generation，原 v1 保持可读；不做 SQL 回填。
Graph result_staged 不等于业务 completed。终态 Checkpoint、可发布草稿与正式助手消息是三个独立
事实，M4 执行协调器再通过原 publish_answer 完成原子发布。
详细契约见 [Phase 13 设计](phase-13-design.md) 和 [M3 验收](phase-13-m3-acceptance.md)。

## Phase 13 M2 Checkpoint 增量

迁移文件：`backend/alembic/versions/0010_phase13_langgraph_checkpoints.py`。
实际 revision 为 `0010_phase13_checkpoints`，父版本为 `0009_phase13_chat_expand`；
revision 缩短是为了兼容现有 Alembic `version_num VARCHAR(32)`，文件名保持阶段约定。
**现有业务库未执行 0009/0010，2026-09-28 只读核验仍为 `0008_phase10_enforce`。**

独立 schema `langgraph_checkpoints` 撤销 PUBLIC schema 权限，使用应用数据库账号的独立
psycopg pool 访问，不改变业务连接的 search_path。表结构冻结自实际安装的
`langgraph-checkpoint-postgres==3.1.2` 的十条上游迁移，SHA-256：
`b61d83ce19b67141d851d7fa29a73ecc6f5cd8e9d45c1aa0f6b6f9ebe50f26bf`。

| 表 | 主键及职责 |
|---|---|
| checkpoint_migrations | `v`；记录上游版本 0–9，与项目 Alembic 版本分开 |
| checkpoints | `(thread_id,checkpoint_ns,checkpoint_id)`；parent ID、checkpoint JSONB、metadata JSONB、type |
| checkpoint_blobs | `(thread_id,checkpoint_ns,channel,version)`；type、可空 BYTEA blob |
| checkpoint_writes | `(thread_id,checkpoint_ns,checkpoint_id,task_id,idx)`；channel、type、BYTEA blob、task_path |

后三表分别有 thread_id 索引。0010 仅在新 schema 建空表，使用事务内普通索引，
与上游最终结构等价，不需要上游为在线旧表使用的 CONCURRENTLY。
namespace 不存在是升级前提；已存在的未知 namespace 不会被自动接管。
不改 QA 数据、不重新回填历史、不建第二套消息表。运行时禁止 `.setup()`，启动只读检查
版本、表字段和项目 revision；缺少 0010 明确失败，不自动补表。

Checkpoint thread_id 为 `str(qa_sessions.id)`，namespace 固定空字符串。
保持上游 schema 形状，不添加到 QA 表的数据库外键：内部入口和 Saver 写入前通过
M1 Repository 校验 session/turn/request/attempt/指纹，业务行锁围住一次短 Checkpoint 写入。
业务事务和 Saver 事务独立，不能将其当作一个原子提交。

M2 对 `qa_turn_artifacts.details` 仅增加 `kind=rewrite` 的封闭
`RewriteArtifactDetails` 类型（`query_rewrite:v1`，schema_version=1）：保存独立问题、
决策、来源消息 ID、语言指代映射、澄清原因/选项及受限预算/usage。其他 kind 仍只收
M1 的 PersistenceMetrics；不增加数据库列，不允许 Prompt、检索正文或任意 JSON。

降级先对四表取排他锁，任何 checkpoints/blobs/writes 数据存在即拒绝。
只允许回滚未使用的空扩展；不会删除已有恢复状态。禁止 CASCADE；未知依赖也使降级失败回滚。
上线后的恢复方式是回退应用并保留 schema，清理/备份恢复须另行授权。
隔离 PostgreSQL 的实际验证及限制见 [M2 验收](phase-13-m2-acceptance.md)。

## Phase 13 M1 会话持久化增量

当前新增设计见 [Phase 13 M1](phase-13-design.md)，对应待执行迁移
`0009_phase13_chat_expand`（基于 `0008_phase10_enforce`）。下文保留各阶段历史记录。

| 表 | Phase 13 M1 变化 |
|---|---|
| qa_sessions | UUID 即未来 thread_id；创建请求幂等身份；next_turn_no / next_message_seq |
| qa_messages | turn_id 可空以保留 legacy；session 内 sequence_no 唯一；每 turn/role 唯一 |
| qa_turns | session/request 和 session/turn_no 唯一；请求指纹、状态、attempt、outcome |
| qa_turn_artifacts | 同 thread/turn 的不可变阶段产物与受限元数据，不存检索正文 |
| qa_evidence_snapshots | 可按来源清理的候选/引用/图谱/回答草稿 payload 与哈希 |
| qa_evidence_sources | 规范化 document/chunk 来源；源删除后保留 UUID 与 tombstone |
| retrieval_logs | nullable legacy turn/generation；新日志的消息及 turn 归属复合约束 |

已有消息按 `(created_at,id)` 在 session 内回填序号，原文和时间戳保留；不猜测旧轮次。
复合外键校验 thread/turn 归属，Repository 在短事务内分配序号和校验幂等冲突。
成功问答正文保留；文档删除时仅清除受控证据 payload 及恢复草稿。
本阶段不创建 LangGraph Checkpoint 表，不执行现有业务数据库迁移。

### 字段与约束

- `qa_sessions`：增加 `create_request_id UUID`、`create_fingerprint VARCHAR(64)`，两者同时为空或同时有值；
  创建请求 ID 全表唯一，适用于当前可信本地单用户边界。增加正数 `BIGINT` 计数器
  `next_turn_no`、`next_message_seq`，默认 1，由 Repository 持有 session 行锁后分配。
- `qa_turns`：UUID 主键；`session_id` 外键；`request_id`、`request_fingerprint`、`turn_no`、
  `question`、`retrieval_limit`、可空 `document_id` 请求参数；`attempt_no`、`status`、`outcome`、
  `error_code` 和创建/更新/完成时间。`(session_id,id)` 是复合外键目标；
  `(session_id,request_id)`、`(session_id,turn_no)` 唯一；部分唯一索引保证同 session
  最多一个 running/finalizing/needs_recovery 轮次。完成、失败字段组合有 CHECK 约束。
- `qa_messages`：增加 `sequence_no BIGINT NOT NULL`、可空 `turn_id`、可空 `answer_snapshot_id`。
  `(session_id,sequence_no)` 唯一；新轮次仅允许 user/assistant，`(turn_id,role)` 唯一。
  用户消息不可带回答快照，助手消息必须引用同 session/turn 的快照；legacy 不绑定快照。
  不保存占位助手消息：回答发布时一次写入完整正文，执行状态统一从关联 turn 读取。
- `qa_turn_artifacts`：UUID 主键；session/turn、attempt、artifact_key、kind、schema_version、
  输入/产物 SHA-256、可空 parent_artifact_id、details JSONB 和创建时间。
  `(turn_id,attempt_no,artifact_key)` 唯一；父产物复合外键必须属于同 session/turn。
  details 只接受 `PersistenceMetrics` 的白名单字段；没有任意文本或完整响应缓存字段。
- `qa_evidence_snapshots`：UUID 主键；session/turn/artifact、snapshot_key、kind、schema_version、
  payload JSONB、内容 SHA-256、status、redacted_document_id、redacted_at 和创建时间。
  `(artifact_id,snapshot_key)` 唯一；artifact 归属复合外键。available 必须有非 SQL NULL payload；
  source_deleted 必须为 SQL NULL payload，并带删除来源与时间。正文快照由 Repository 再校验 JSON 和来源。
- `qa_evidence_sources`：UUID 主键；session/turn/snapshot、document_id、可空 chunk_id、status、
  deleted_at 和创建时间。无 chunk 时 `(snapshot_id,document_id)` 唯一，有 chunk 时
  `(snapshot_id,chunk_id)` 唯一；另建 document/snapshot 查询索引。来源 UUID 不设保留文档/分块的外键，
  避免阻止源文档硬删除；写入时通过 DocumentOperationGuard 与 chunk 所属校验补足来源有效性。
- `retrieval_logs`：增加可空 turn_id 和 evidence_generation。legacy 两者均空；新日志要求
  正数 generation 和 message_id，`(turn_id,evidence_generation)` 唯一。
  session/message 及 session/turn/message 复合外键防止跨 thread 和跨 turn 关联。

### 迁移前置检查与回滚边界

迁移在事务内锁住旧 QA 三表，检查孤立消息、孤立或跨会话检索日志。历史 `result_summary`
只自动接受 SQL NULL、JSON null 或空对象；其他内容没有规范化来源，必须停止并报告日志 UUID，
由人工审阅和单独迁移，不能自动抹去或猜测来源。正常 legacy 日志及消息内容保持不变。

新结构一旦保存过 Phase 13 会话创建身份、轮次、产物或证据，downgrade 即拒绝删除。
此时恢复方式是回退应用、保留 schema 和数据，或经单独授权从备份恢复。
只有未使用的新结构允许回到 0008，legacy 问答及检索日志仍然保留。

# 第一阶段数据库设计

本文说明 `D:\rag_system` 第一阶段数据库设计。第一阶段目标是建立可迁移、可扩展的关系型数据骨架，不实现检索链路、问答链路、模型调用或生产级审核流程。

## 总体策略

- 数据库使用 PostgreSQL，基础服务镜像包含 pgvector。
- 第一阶段启用 pgvector 只是为第二阶段向量检索做基础设施准备。
- 第一阶段不创建真实 `vector` 类型字段。
- 第一阶段不创建 HNSW、IVFFlat 或其他向量索引。
- embedding 只保留元数据字段：`embedding_model`、`embedding_dim`、`embedding_status`。
- 第二阶段确定 embedding 模型和维度后，再通过新的迁移加入真实向量字段和向量索引。

## documents

用途：保存原始文档元数据，对应 MinIO 中的上传对象，是文档解析、切片和知识抽取的来源。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `original_filename`：用户上传时的原始文件名。
- `object_key`：MinIO 对象 key。
- `file_type`：文件类型或扩展名。
- `file_size`：文件大小。
- `process_status`：文档处理状态，默认建议为 `pending`。
- `created_at`、`updated_at`：创建和更新时间。

主要关系：
- 一对多关联 `document_chunks.document_id`。
- 一对多关联 `knowledge_entries.source_document_id`。

后续关系：文档上传后先进入 `pending`，后续解析、切片、知识抽取流程会更新处理状态并生成切片和候选知识条目。

## document_chunks

用途：保存文档切片文本，是后续 embedding、检索召回和知识条目溯源的基础。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `document_id`：关联 `documents.id`。
- `chunk_index`：文档内切片序号。
- `content`：切片正文。
- `token_count`：估算 token 数。第三阶段不做真实 token 统计，写入 chunk 时保持为空。
- `page_start`、`page_end`：页码范围。
- `section_title`：章节标题。
- `chunk_type`：切片类型。
- `source_metadata`：第三阶段新增的 nullable JSONB 来源元数据，用于记录解析器、来源类型、占位标记和字符位置等信息。
- `embedding_model`、`embedding_dim`、`embedding_status`：embedding 元数据，不保存真实向量。
- `created_at`、`updated_at`：创建和更新时间。

主要关系：
- 多对一关联 `documents`。
- 一对多关联 `knowledge_entries.source_chunk_id`。

默认值：
- `embedding_status` 默认建议为 `not_started`。

后续关系：第二阶段确定 embedding 模型后，可基于该表新增真实向量字段和向量索引，用于语义检索。

## knowledge_entries

用途：保存从文档和切片中抽取出的知识条目，是后续专家审核、版本管理和问答知识源的核心表。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `entry_type`：知识条目类型。
- `subject`：知识主体。
- `condition`：适用条件。
- `conclusion`：结论或工艺知识内容。
- `source_document_id`：关联 `documents.id`。
- `source_chunk_id`：关联 `document_chunks.id`。
- `source_text`：来源原文。
- `confidence`：自动抽取置信度。
- `review_status`：审核状态，默认值必须为 `pending_review`。
- `version`：当前版本号，默认值为 `1`。
- `embedding_model`、`embedding_dim`、`embedding_status`：embedding 元数据，不保存真实向量。
- `created_at`、`updated_at`：创建和更新时间。

主要关系：
- 多对一关联 `documents`。
- 多对一关联 `document_chunks`。
- 一对多关联 `entry_versions.entry_id`。
- 一对多关联 `entry_review_records.entry_id`。

默认值：
- `review_status` 默认值为 `pending_review`。
- `version` 默认值为 `1`。
- `embedding_status` 默认建议为 `not_started`。

后续关系：自动抽取的知识条目不得直接进入正式库，应先进入 `pending_review`，由专家审核后再进入后续可用状态。

## entry_versions

用途：保存知识条目的版本快照，支持知识条目编辑、审核修订和历史追溯。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `entry_id`：关联 `knowledge_entries.id`。
- `version`：版本号。
- `snapshot`：JSONB 快照，保存该版本的知识条目内容。
- `change_reason`：变更原因。
- `created_at`：创建时间。

主要关系：
- 多对一关联 `knowledge_entries`。

后续关系：当知识条目被修改、审核修订或结构化增强时，应写入新的版本快照。

## entry_review_records

用途：保存专家审核记录，记录知识条目从候选状态到审核状态的操作历史。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `entry_id`：关联 `knowledge_entries.id`。
- `action`：审核动作，例如 approve、reject、revise。
- `reviewer`：审核人。
- `comment`：审核意见。
- `created_at`：创建时间。

主要关系：
- 多对一关联 `knowledge_entries`。

后续关系：专家审核流程应通过该表记录审核动作，不直接覆盖历史判断。

## qa_sessions

用途：保存问答会话元数据，用于组织一轮或多轮智能问答。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `title`：会话标题。
- `created_at`、`updated_at`：创建和更新时间。

主要关系：
- 一对多关联 `qa_messages.session_id`。
- 一对多关联 `retrieval_logs.session_id`。

后续关系：问答功能上线后，每个会话会关联用户问题、助手回答和检索日志。

## qa_messages

用途：保存问答消息，用于记录 user、assistant、system 等角色的消息内容。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `session_id`：关联 `qa_sessions.id`。
- `role`：消息角色，例如 user、assistant、system。
- `content`：消息内容。
- `created_at`：创建时间。

主要关系：
- 多对一关联 `qa_sessions`。
- 可被 `retrieval_logs.message_id` 关联。

后续关系：问答链路上线后，用户问题和系统回答都应进入该表，便于上下文追踪和质量分析。

## retrieval_logs

用途：保存检索日志，用于记录问答或知识检索过程中的 query、top_k、耗时和结果摘要。

核心字段：
- `id`：PostgreSQL UUID 主键。
- `session_id`：关联 `qa_sessions.id`。
- `message_id`：可为空，关联 `qa_messages.id`。
- `query`：检索查询文本。
- `retrieval_type`：检索类型。
- `top_k`：召回数量。
- `result_summary`：JSONB 结果摘要。
- `latency_ms`：检索耗时，单位毫秒。
- `created_at`：创建时间。

主要关系：
- 多对一关联 `qa_sessions`。
- 可选多对一关联 `qa_messages`。

后续关系：第二阶段加入向量检索、关键词检索或混合检索后，该表用于记录检索质量、耗时和调试信息。

## 普通索引策略

第一阶段可以创建普通业务索引，例如：
- `document_chunks.document_id`
- `knowledge_entries.source_document_id`
- `knowledge_entries.source_chunk_id`
- `knowledge_entries.review_status`
- `entry_versions.entry_id`
- `entry_review_records.entry_id`
- `qa_messages.session_id`
- `retrieval_logs.session_id`
- `retrieval_logs.message_id`

第一阶段不得创建向量索引。向量字段和向量索引必须等第二阶段确认 embedding 模型和维度后再通过新迁移添加。
# 第二阶段 documents 上传元数据补充

第二阶段“文档上传与基础知识库入库闭环”只补充 `documents` 表的普通上传元数据字段，用于记录原始文件在 MinIO 中的存储位置、上传类型信息、文件哈希和错误信息预留。

新增字段：

- `bucket_name`：MinIO bucket 名称，默认 `rag-documents`。
- `mime_type`：上传文件 MIME 类型，来自上传请求的 `content_type`，仅作为辅助校验和展示信息。
- `file_hash`：SHA-256 文件哈希，用于后续去重和完整性校验预留。
- `error_message`：上传或后续处理失败时的错误信息预留字段。

约束说明：

- 第二阶段只补充普通元数据字段。
- 第二阶段不新增真实 `vector` 字段。
- 第二阶段不创建 HNSW、IVFFlat 或其他向量索引。
- 本步骤不修改 `process_status` 的 ORM 默认值或数据库默认值。
- 第二阶段上传成功时由 service 层显式写入 `process_status="uploaded"`。

# 第三阶段 document_chunks 来源元数据补充

第三阶段“文档解析适配与基础切片可视化闭环”只补充 `document_chunks` 表的普通 JSONB 来源元数据字段，用于记录 chunk 由哪个解析器、哪类来源内容和哪段字符范围生成。

新增字段：

- `source_metadata`：JSONB，可为空。建议保存 `parser_name`、`parser_version`、`source_type`、`placeholder`、`char_start`、`char_end`、`character_count`、`original_extension`、`original_filename` 等来源元数据。

第三阶段写入 chunk 时：

- `embedding_model` 保持为空。
- `embedding_dim` 保持为空。
- `embedding_status` 仍保持 `not_started`。
- `token_count` 保持为空，不做真实 token 统计。
- 第三阶段不生成 embedding。

约束说明：

- 第三阶段只补充 `document_chunks.source_metadata` 普通 JSONB 字段。
- `source_metadata` 是第三阶段新增的普通 JSONB 元数据字段，不参与向量检索。
- 第三阶段不新增真实 `vector` 字段。
- 第三阶段不创建 HNSW、IVFFlat 或其他向量索引。
- 第三阶段不生成 embedding。
- 第三阶段不修改 `embedding_status` 的 ORM 默认值或数据库默认值。

# 第四阶段 document_chunks embedding 字段补充

第四阶段“Embedding 生成与基础向量检索闭环”在第三阶段 `document_chunks` 已入库的基础上，新增真实 pgvector 向量字段和 embedding 失败追踪字段，用于本地 Qwen3-Embedding-0.6B embedding 生成和基础向量检索。

新增字段：

- `embedding vector(1024)`：nullable，保存本地 `Qwen3-Embedding-0.6B` 生成的 1024 维向量。
- `embedding_error_message text`：nullable，保存 embedding 生成失败原因。
- `embedding_updated_at timestamptz`：nullable，记录 embedding 生成或更新的时间。

已有字段继续使用：

- `embedding_model`：记录生成 embedding 的模型名，例如 `Qwen3-Embedding-0.6B`。
- `embedding_dim`：记录生成 embedding 的维度，第四阶段为 `1024`。
- `embedding_status`：字符串状态字段。

`embedding_status` 第四阶段状态：

- `not_started`：尚未生成 embedding。
- `embedding`：正在生成 embedding。
- `embedded`：已生成 embedding。
- `embed_failed`：生成失败。

生成策略：

- `embedded` chunks 默认跳过，不覆盖。
- `embed_failed` chunks 可以重新生成。
- 第四阶段不支持 `force` 覆盖。
- 失败时写入 `embedding_error_message`。
- 成功时写入 `embedding_model`、`embedding_dim`、`embedding_status='embedded'`、`embedding_updated_at`。

当前检索与存储边界（2026-09-23 更新）：

- 旧 `POST /api/v1/search/vector` 及 pgvector cosine distance 查询实现已移除；该地址返回 404。
- `document_chunks.embedding` 及其模型、维度、状态字段继续保留，数据库 schema 和历史迁移不变。
- OpenSearch 索引同步仍读取 `embedding_status='embedded'`、embedding 非空且模型、维度匹配的正常文档 chunks。
- `POST /api/v1/search` 使用 OpenSearch 关键词召回和 kNN 向量召回，再进行 weighted RRF 融合；不使用旧 `distance` / `score = 1 - distance` 返回结构。
- 本次退役不创建或删除 PostgreSQL 向量索引，也不要求重建 OpenSearch index。

`retrieval_logs` 边界：

- 第四阶段不写 `retrieval_logs`。
- 当前 `retrieval_logs.session_id` 为非空约束，尚无 search session 设计。
- 不为了写日志而伪造 `session_id`。
- 后续如需记录基础检索日志，应先设计 `search_session`，或通过 Alembic 改造 `retrieval_logs.session_id` 约束。

第四阶段仍不实现 RAG 问答、大语言模型回答、reranker、混合检索、关键词检索、图谱检索、知识条目自动抽取、专家审核、真实 MinerU 深度解析、Celery 队列、多模型调度或生产级模型服务部署。

## 第五阶段数据库边界与搜索索引结构

第五阶段引入 OpenSearch + IK 统一混合检索索引闭环，但没有新增 PostgreSQL 表，也没有新增 PostgreSQL 字段。

PostgreSQL 侧保持以下边界：

- PostgreSQL 仍是主数据源。
- MinIO 仍保存原始文件。
- 第五阶段不修改 `documents` 表结构。
- 第五阶段不修改 `document_chunks` 表结构。
- `document_chunks.embedding vector(1024)` 仍是第四阶段新增字段。
- `document_chunks.embedding_status`、`embedding_model`、`embedding_dim`、`embedding_error_message`、`embedding_updated_at` 仍沿用第四阶段设计。
- 第五阶段不写入 `retrieval_logs`。
- `retrieval_logs.session_id` 当前仍为 NOT NULL；如后续要记录检索日志，应先设计 search session 或调整迁移。

OpenSearch index 不是 PostgreSQL schema。它是从 PostgreSQL `documents` 和 `document_chunks` 派生出来的检索索引，可以重建，不作为主数据存储。

### 搜索索引结构：casting_chunks_v1

默认物理索引名：

- `casting_chunks_v1`

默认查询别名：

- `casting_chunks_current`

索引文档为 chunk 级结构，建议字段包括：

- `chunk_id`
- `document_id`
- `original_filename`
- `chunk_index`
- `content`
- `chunk_type`
- `page_start`
- `page_end`
- `section_title`
- `source_metadata`
- `exact_terms`
- `embedding`
- `embedding_model`
- `embedding_dim`
- `embedding_status`
- `document_process_status`
- `created_at`
- `updated_at`

字段说明：

- `content` 使用 IK 中文分词，索引阶段默认 `ik_max_word`，查询阶段默认 `ik_smart`。
- `exact_terms` 为 `keyword` 数组，用于标准编号、材料牌号、工艺参数和专业术语精确匹配。
- `source_metadata` 为 `enabled=false` object，仅用于返回展示，不参与检索或聚合。
- `embedding` 为 1024 维 `knn_vector`，用于 OpenSearch kNN 向量召回。
- `embedding_model` 固定匹配 `Qwen3-Embedding-0.6B`。
- `embedding_dim` 固定匹配 `1024`。

第五阶段只同步满足以下条件的 chunks：

- `embedding_status='embedded'`
- `embedding IS NOT NULL`
- `embedding_dim=1024`
- `embedding_model='Qwen3-Embedding-0.6B'`

OpenSearch 文档 `_id` 使用 `chunk_id`，以支持重复同步幂等。

按 `document_id` 重建索引时，应先删除搜索索引中该 `document_id` 下的旧索引文档，再写入 PostgreSQL 中当前最新的 embedded chunks，避免文档重解析后旧 chunk 残留。
# 第七阶段数据库结构补充：knowledge_items 新体系

第七阶段删除未使用的旧 `knowledge_entries` / `entry_versions` / `entry_review_records` 预留体系，并以新的 `knowledge_items` 四表体系替代。

## 旧表替换

旧表：

- `knowledge_entries`
- `entry_versions`
- `entry_review_records`

说明：

- 这些旧表是早期预留体系，未正式承载业务数据。
- 第七阶段 migration 会删除旧表。
- 删除旧表前使用 inspector 判断旧表是否存在。
- 如果旧表存在且任一非空，migration 会停止并提示用户确认处理策略。
- 删除范围仅限旧知识条目体系，不影响 `documents`、`document_chunks`、`retrieval_logs`、uploaded files、search、RAG 或 OpenSearch。

## 新表：knowledge_items

用途：保存结构化知识条目当前版本。

关键字段：

- `id`
- `item_type`
- `title`
- `content`
- `content_hash`
- `structured_data`
- `entities`
- `parameters`
- `conditions`
- `confidence`
- `status`
- `source_document_id`
- `source_filename`
- `created_by`
- `reviewed_by`
- `review_comment`
- `version`
- `reviewed_at`
- `created_at`
- `updated_at`
- `revises_item_id`

约束与索引说明：

- `content_hash` 是普通索引，不是唯一约束。
- 重复检测在 service 层执行。
- 不设置唯一约束是为了允许 `revise` 新 `draft` 与原 `approved` / `deprecated` 条目拥有相同 hash。

语义说明：

- `confidence` 是 LLM 抽取置信度，不等于知识可信度。
- `confidence` ORM 层使用 `Numeric(5,4)`，API 层统一输出 `float | None`。
- 只有 `status=approved` 才表示可信知识。
- `approved` 当前暂不进入第六阶段 RAG 回答链路。

## 新表：knowledge_item_chunks

用途：保存知识条目与来源 chunk 的追溯关系。

字段：

- `id`
- `knowledge_item_id`
- `chunk_id`
- `document_id`
- `chunk_index`
- `source_text`
- `created_at`

说明：

- `source_text` 是抽取时 `document_chunks.content` 的原文快照。
- `source_text` 不替代 `document_chunks.content`。
- 即使后续 chunk 重新切分、重建或更新，仍可追溯当时抽取依据。
- `source_text` 用于专家审核、citation 和后续图谱来源追溯。

## 新表：knowledge_item_reviews

用途：保存专家审核动作。

字段：

- `id`
- `knowledge_item_id`
- `review_action`
- `from_status`
- `to_status`
- `review_comment`
- `reviewer`
- `created_at`

说明：

- 审核记录只记录状态流转和审核意见。
- 审核记录不替代内容版本快照。

## 新表：knowledge_item_versions

用途：保存内容版本快照。

字段：

- `id`
- `knowledge_item_id`
- `version`
- `snapshot`
- `change_reason`
- `created_by`
- `created_at`

说明：

- 创建条目时写 `version=1`。
- `PATCH` 编辑后 `version + 1`。
- `revise` 创建新 `draft` 时写新条目的 `version=1`。
- 审核状态变化主要写 `knowledge_item_reviews`，后续可扩展为同时写版本快照。

## retrieval_logs

第七阶段仍不写 `retrieval_logs`。知识条目 CRUD、审核、修订和抽取不应增加检索日志。

## 第八阶段：MinerU 解析中间层

第八阶段以 MinerU API 作为正式主解析器，新增解析中间层表，用于承接 MinerU 的结构化输出，再生成最终用于检索与 RAG 的 `document_chunks`。

核心关系：

```text
documents
-> document_parse_runs
-> document_blocks / document_assets
-> document_chunks
-> document_chunk_blocks
```

后续检索链路仍保持：

```text
document_chunks
-> embedding
-> OpenSearch
-> /api/v1/search
-> /api/v1/rag/ask
-> /api/v1/knowledge-items
```

### document_parse_runs

`document_parse_runs` 记录真实 MinerU 解析任务，当前 provider 为 `mineru_api`。Basic 已退出；未来 Markdown Native 不创建 ParseRun。

主要字段：

- `id`：解析任务 ID。
- `document_id`：关联 `documents.id`。
- `parser_provider`：解析器来源，当前为 `mineru_api`；不迁移或重写历史记录。
- `parser_version`：解析器版本。
- `parse_mode`：解析模式，例如 `auto`。
- `status`：`pending` / `running` / `succeeded` / `failed`。
- `is_active`：默认 `false`；只有完整入库成功后才可为 `true`。
- `input_file_key`：原始上传文件对象 key。
- `output_prefix`：解析产物保存前缀。
- `output_markdown_key` / `output_json_key`：解析产物 key。
- `page_count` / `block_count` / `asset_count`：解析统计。
- `error_message`：简短失败摘要，不保存完整原文、完整 MinerU JSON 或 API key。
- `source_metadata`：任务级元数据摘要，例如 MinerU 任务 ID、API 版本、解析模式、产物状态摘要。
- `started_at` / `completed_at` / `created_at` / `updated_at`：时间字段。

状态语义：

- `status=succeeded` 不只表示 MinerU API 调用成功，而是表示 MinerU 解析、产物保存策略、assets 写入、blocks 写入、chunks 写入、chunk-block 映射全部成功。
- `is_active=true` 只应出现在完整成功的解析任务上；service 层应避免同一文档出现多个 active parse run。
- `output_markdown_status` / `output_json_status` 可为 `saved`、`download_deferred`、`unavailable`。
- `failure_status_persisted=false` 表示解析失败后，失败状态本身未能可靠持久化，需要人工排查。

### document_blocks

`document_blocks` 保存 MinerU 输出标准化后的结构块，不作为直接检索表。

主要字段：

- `id`
- `document_id`
- `parse_run_id`
- `block_index`
- `block_key`
- `block_type`
- `page_start` / `page_end`
- `bbox`
- `text`
- `markdown`
- `html`
- `latex`
- `caption`
- `parent_block_key`
- `section_path`
- `confidence`
- `source_metadata`
- `created_at`

约束和索引：

- `unique(parse_run_id, block_index)` 保证同一次解析内顺序稳定。
- `index(parse_run_id)`
- `index(document_id)`
- `index(block_type)`
- `block_key` 第八阶段 v1 不强制唯一。
- `parent_block_key` 是弱关联，不做自引用强外键。

说明：

- `text` 用于纯文本表达。
- `markdown` 可保存表格或结构化段落。
- `html` 可保存表格 HTML。
- `latex` 可保存公式。
- `caption` 可保存图片说明。
- `bbox`、`section_path`、`source_metadata` 使用 JSON 结构保存必要摘要。
- `document_blocks` 不直接进入 RAG；必须先生成 `document_chunks`。

### document_assets

`document_assets` 保存 MinerU 输出资产和 MinIO 资产元数据，不保存大二进制。

主要字段：

- `id`
- `document_id`
- `parse_run_id`
- `asset_type`
- `page_number`
- `asset_key`
- `filename`
- `mime_type`
- `size_bytes`
- `caption`
- `source_block_key`
- `source_metadata`
- `created_at`

约束和索引：

- `unique(parse_run_id, asset_key)`
- `index(parse_run_id)`
- `index(document_id)`
- `index(asset_type)`
- `source_block_key` 是弱关联，不与 `document_blocks` 形成双向强外键。

资产建议存储前缀：

```text
parsed-assets/{document_id}/{parse_run_id}/...
```

`document_assets` 不直接进入 RAG，也不直接参与检索；它用于解析产物追溯、前端轻量展示和未来多模态扩展。

### document_chunk_blocks

`document_chunk_blocks` 记录最终 chunk 与来源 block 的映射。

主要字段：

- `id`
- `chunk_id`
- `block_id`
- `block_order`
- `created_at`

约束和索引：

- `index(chunk_id)`
- `index(block_id)`
- `unique(chunk_id, block_order)`

说明：

- 一个 chunk 可以由多个 blocks 合并而来。
- 一个 block 在拆分场景下可以被多个 chunks 引用。
- `block_order` 用于在回溯时恢复同一 chunk 内的 block 顺序。
- 外键不使用危险级联删除；测试数据清理必须按依赖顺序手动执行。

### document_chunks 扩展字段

第八阶段小幅扩展 `document_chunks`，但不改变它作为最终检索/RAG chunk 的定位。

新增字段：

- `parse_run_id`：nullable，表示该 chunk 来源于哪次解析；兼容历史无 ParseRun 数据和未来 Markdown Native chunks。
- `chunk_method`：当前 MinerU 为 `mineru_block_merge`；历史 `basic_text_split` 仅作为存量 provenance，不再生成。
- `content_format`：例如 `plain_text` / `markdown` / `mixed`。

保留规则：

- 不删除 `embedding`。
- 不删除 `source_metadata`。
- 不破坏 `document_chunks.id`。
- 不破坏 `knowledge_item_chunks.chunk_id` 依赖。
- `document_chunks` 仍是 `/api/v1/search`、`/api/v1/rag/ask` 和 `/api/v1/knowledge-items` 的共同基础。

# 第十阶段数据库结构：Document Hard Delete

Phase 10 使用两个有序 migration：

- `0007_phase10_expand`：增加 `documents.deletion_status`、`document_deletion_jobs`、`knowledge_item_sources`，安全回填既有 singular source，并让所有 Knowledge 写路径在同一事务 dual-write；
- `0008_phase10_enforce`：先 fail-closed preflight，再为 `knowledge_item_chunks(knowledge_item_id, document_id)` 增加指向 `knowledge_item_sources(knowledge_item_id, document_id)` 的 composite FK。

`0007` 本身不创建 composite FK；`0008` 不补猜数据、不修改 0007 backfill，且 FK 不使用 `ON DELETE CASCADE`。source-less/manual Knowledge Item 在没有 chunk relation 时合法。

## documents.deletion_status

允许值为 `normal`、`deleting`、`delete_failed`，默认并回填为 `normal`。非 normal Document 被写路径 guard 拒绝，也不会进入 Hybrid/RAG 的正常候选集合；旧 PostgreSQL 纯向量查询已退役。

## document_deletion_jobs

核心字段：`id`、唯一 `document_id` snapshot（无 FK）、`status`、`current_step`、`step_attempts`、`max_attempts`、versioned JSONB `manifest`、`locked_by`、`lease_token`、`locked_at`、`lease_expires_at`、`next_retry_at`、`last_error_code`、`created_at`、`updated_at`。

job 不通过 Document cascade 删除。Document 已不存在但 job 仍存在是合法 crash-recovery 状态；只有整个 Saga 的 final PostgreSQL transaction 成功时，Document 与 job 才按固定顺序一起消失。成功后不保留 succeeded job。

所有 lease、due、expired 与 retry deadline 判定使用 PostgreSQL database time。`max_attempts` 是 job 创建时的每-step 自动尝试上限快照；进入下一 step 时 `step_attempts` 清零。

## knowledge_item_sources

`knowledge_item_sources` 以唯一 `(knowledge_item_id, document_id)` 表达来源身份，`document_id` 是唯一身份事实；`source_filename` 只是 relation 创建时不可变的 provenance snapshot。`knowledge_items.source_document_id/source_filename` 继续作为 Phase 7 REST compatibility projection，不参与 orphan 判定。

Document Knowledge cleanup 在稳定 UUID 顺序锁定 cycle-safe revision closure 后删除该 Document 的 chunk/source relations，flush，再从 DB recount 剩余来源。仍有来源的 Item、versions、reviews 保留并切换 deterministic projection；无来源 Item 的 chunks、versions、reviews 与 Item 删除。surviving version snapshot 只按明确 provenance 字段/path 清理，不按正文 value 做全局扫描。

## PostgreSQL finalization 顺序

外部 OpenSearch 和 MinIO 已验证无残留后，单一事务完成：lease/fencing 复验、Knowledge cleanup、`document_chunk_blocks`、`knowledge_item_chunks` 防御性清理、`document_chunks`（含 inline 1024 维 vector）、`document_assets`、`document_blocks`、`document_parse_runs`、`documents`、最后 `document_deletion_jobs`。任何 commit 前失败都整体 rollback。
