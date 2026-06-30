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
- `token_count`：估算 token 数。
- `page_start`、`page_end`：页码范围。
- `section_title`：章节标题。
- `chunk_type`：切片类型。
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
