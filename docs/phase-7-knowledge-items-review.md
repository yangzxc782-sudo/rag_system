# 第七阶段：知识条目自动抽取与专家审核闭环

## 1. 阶段定位

第七阶段在不修改第五阶段混合检索和第六阶段 RAG 问答链路的前提下，新增独立的 `knowledge_items` 知识条目体系。

本阶段目标是实现：

```text
document_chunks
-> LLM 自动抽取候选知识条目
-> draft / pending_review
-> 专家审核、修改、批准、驳回
-> approved 可信知识条目库
```

第七阶段边界：

- 不修改 `POST /api/v1/search`。
- 不修改 `POST /api/v1/rag/ask`。
- 旧 `POST /api/v1/search/vector` 已于 2026-09-23 单独授权退役；该变更不影响 Hybrid/RAG 或 Knowledge API。
- 不修改 OpenSearch index schema。
- 不修改 weighted RRF。
- 不写 `retrieval_logs`。
- 不接入 Neo4j。
- 不引入 LangGraph。
- 不正式接入 reranker。
- `approved` 条目暂不参与第六阶段 RAG 回答。

## 2. 旧知识条目体系替换说明

旧 `knowledge_entries` / `entry_versions` / `entry_review_records` 是早期预留体系。用户已确认旧体系未正式使用、无业务数据、不需要保留。

第七阶段已删除旧 ORM model：

- `backend/app/models/knowledge_entry.py`
- `backend/app/models/entry_version.py`
- `backend/app/models/entry_review_record.py`

第七阶段 migration 会删除旧表：

- `entry_review_records`
- `entry_versions`
- `knowledge_entries`

迁移保护规则：

- 删除旧表前使用 SQLAlchemy inspector 判断旧表是否存在。
- 如果旧表不存在，跳过删除。
- 如果旧表存在，会先检查是否为空。
- 如果任一旧表非空，migration 会停止并提示用户确认处理策略。
- 删除顺序为 `entry_review_records -> entry_versions -> knowledge_entries`。
- 删除范围仅限旧知识条目体系。
- 不影响 `documents`、`document_chunks`、`retrieval_logs`、OpenSearch、RAG、search 等能力。

正式运行 `alembic upgrade head` 前必须由用户单独确认。

## 3. 新数据模型

第七阶段新增四张表：

- `knowledge_items`
- `knowledge_item_chunks`
- `knowledge_item_reviews`
- `knowledge_item_versions`

### knowledge_items

字段：

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

说明：

- `content_hash` 只做普通索引，不加唯一约束。
- `content_hash` 去重在 service 层完成。
- 不加唯一约束是为了允许 `revise` 新 `draft` 与原 `approved` / `deprecated` 条目拥有相同 hash。
- `confidence` ORM 层为 `Numeric(5,4)`，API 层输出 `float | None`。
- `confidence` 是 LLM 抽取置信度，不等于知识可信度。
- 只有 `status=approved` 才表示可信知识。

### knowledge_item_chunks

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
- `source_text` 用于专家审核、citation 和后续图谱来源追溯。
- 即使后续 chunk 重新切分，也能追溯当时抽取依据。

### knowledge_item_reviews

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

- `reviews` 只记录审核动作。
- `reviews` 不替代内容版本。

### knowledge_item_versions

字段：

- `id`
- `knowledge_item_id`
- `version`
- `snapshot`
- `change_reason`
- `created_by`
- `created_at`

说明：

- 创建知识条目时写入 `version=1`。
- `PATCH` 编辑成功后 `version + 1`。
- `revise` 创建新 `draft` 时写新条目的 `version=1`。
- 审核状态变化主要写 `knowledge_item_reviews`，后续可扩展为同时写版本快照。

## 4. 知识条目类型

- `process_rule`：工艺规则。例如冒口应设置在热节附近，以保证顺序凝固和有效补缩。
- `parameter_recommendation`：参数建议。例如特定铸件的浇注温度、冒口尺寸、冷铁位置建议。
- `defect_cause`：缺陷原因。例如缩孔可能由补缩不足、热节孤立、冒口模数不足导致。
- `defect_solution`：缺陷解决方案。例如通过增大冒口、设置冷铁或优化浇注系统改善缩松。
- `material_property`：材料属性。例如合金成分、收缩倾向、热物性相关知识。
- `standard_requirement`：标准或规范要求。只能来自原文，不得由模型编造。
- `term_definition`：术语定义。例如热节、模数、顺序凝固等概念解释。
- `case_experience`：案例经验。例如某类铸件缺陷分析与工艺调整经验。

## 5. 状态流转与编辑规则

状态枚举：

- `draft`
- `pending_review`
- `approved`
- `rejected`
- `deprecated`

允许状态流转：

- `draft -> pending_review`
- `rejected -> pending_review`
- `pending_review -> approved`
- `pending_review -> rejected`
- `approved -> deprecated`
- `approved/deprecated -> revise -> new draft`

编辑规则：

- `draft` 可 `PATCH`。
- `rejected` 可 `PATCH`。
- `pending_review` 禁止 `PATCH`。
- `approved` 禁止 `PATCH`。
- `deprecated` 禁止 `PATCH`。
- `approved` / `deprecated` 的修改必须通过 `revise` 创建新 `draft`。
- `revise` 不修改原条目。
- `revise` 不自动 `deprecated` 原条目。
- 原 `approved` 条目是否 `deprecated` 由用户手动执行。

## 6. 后端 API

CRUD：

- `GET /api/v1/knowledge-items`
- `GET /api/v1/knowledge-items/{id}`
- `POST /api/v1/knowledge-items`
- `PATCH /api/v1/knowledge-items/{id}`
- `GET /api/v1/knowledge-items/{id}/chunks`
- `GET /api/v1/knowledge-items/{id}/versions`

审核：

- `POST /api/v1/knowledge-items/{id}/submit`
- `POST /api/v1/knowledge-items/{id}/approve`
- `POST /api/v1/knowledge-items/{id}/reject`
- `POST /api/v1/knowledge-items/{id}/deprecate`
- `GET /api/v1/knowledge-items/{id}/reviews`

修订：

- `POST /api/v1/knowledge-items/{id}/revise`

抽取：

- `POST /api/v1/knowledge-items/extract`

关键行为：

- `POST` 创建默认 `draft`。
- `POST` 禁止创建 `approved` / `rejected` / `deprecated`。
- `PATCH` 只允许 `draft` / `rejected`。
- `submit` 只允许 `draft` / `rejected -> pending_review`。
- `approve` 只允许 `pending_review -> approved`。
- `reject` 只允许 `pending_review -> rejected`。
- `deprecate` 只允许 `approved -> deprecated`。
- `extract` 默认 `draft`。
- `auto_submit=true` 时最终 `pending_review`。
- `extract` 永远不得 `approved`。

## 7. LLM 自动抽取

第七阶段自动抽取复用第六阶段 OpenAI-compatible LLM provider。

边界：

- 输入只来自 `document_chunks`。
- 不调用 `/api/v1/rag/ask`。
- 不调用 `hybrid_search_chunks()`。
- 不调用 OpenSearch。
- 不调用 embedding。
- 不调用 reranker。

代码位置：

- prompt：`backend/app/extraction/knowledge_prompt.py`
- parser：`backend/app/extraction/knowledge_parser.py`
- service：`backend/app/services/knowledge_extraction.py`

LLM 输出必须是 JSON object：

```json
{"items": []}
```

parser 行为：

- 支持去除可选 ```json 代码围栏。
- 校验 `item_type`、`title`、`content`、`confidence`、`source_chunk_ids`。
- 禁止 `status` / `approved` 字段。
- `source_chunk_ids` 必须来自输入 chunks。
- parser 失败不写半成品。
- LLM 调用失败不写半成品。
- 重复项进入 `skipped_duplicates`。
- `items=[]` 不是错误。

抽取请求体示例：

```json
{
  "mode": "chunks",
  "document_id": null,
  "chunk_ids": ["..."],
  "item_types": ["process_rule", "defect_solution"],
  "auto_submit": false,
  "max_chunks": 10,
  "created_by": "system"
}
```

规则：

- `mode=document` 时必须提供 `document_id`。
- `mode=chunks` 时必须提供 `chunk_ids`。
- v1 一次抽取限定同一 document。
- `max_chunks` 受配置限制。
- 总字符数受配置限制。
- `auto_submit=false` 默认 `draft`。
- `auto_submit=true` 最终 `pending_review`。
- 永远不直接 `approved`。

## 8. 配置项

新增配置：

```env
KNOWLEDGE_EXTRACTION_MAX_CHUNKS=20
KNOWLEDGE_EXTRACTION_MAX_CHARS=12000
KNOWLEDGE_EXTRACTION_DEFAULT_STATUS=draft
```

说明：

- `KNOWLEDGE_EXTRACTION_DEFAULT_STATUS` 只能为 `draft`。
- `pending_review` 只能通过 `auto_submit=true` 触发。
- `approved` 永远不能作为抽取默认状态。

## 9. 错误码

新增或使用的错误码：

- `KNOWLEDGE_ITEM_NOT_FOUND`
- `KNOWLEDGE_ITEM_INVALID_STATUS`
- `KNOWLEDGE_ITEM_INVALID_TRANSITION`
- `KNOWLEDGE_ITEM_VALIDATION_FAILED`
- `KNOWLEDGE_ITEM_EXTRACTION_FAILED`
- `KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED`
- `KNOWLEDGE_ITEM_REVIEW_FAILED`
- `KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND`
- `KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND`
- `KNOWLEDGE_ITEM_CONFIG_INVALID`
- `KNOWLEDGE_ITEM_DUPLICATE`
- `KNOWLEDGE_ITEM_VERSION_FAILED`
- `KNOWLEDGE_ITEM_LEGACY_TABLE_REMOVED`

HTTP 映射：

- `NOT_FOUND -> 404`
- `INVALID_STATUS -> 400`
- `INVALID_TRANSITION -> 409`
- `VALIDATION_FAILED -> 400`
- `EXTRACTION_FAILED -> 500`
- `EXTRACTION_PARSE_FAILED -> 500`
- `REVIEW_FAILED -> 500`
- `SOURCE_CHUNK_NOT_FOUND -> 404`
- `SOURCE_DOCUMENT_NOT_FOUND -> 404`
- `CONFIG_INVALID -> 400`
- `DUPLICATE` 手工创建为 `409`，抽取重复为 `skipped_duplicates`
- `VERSION_FAILED -> 500`

## 10. 前端页面

新增文件：

- `frontend/lib/knowledge-items.ts`
- `frontend/components/KnowledgeItemsPanel.tsx`
- `frontend/app/knowledge-items/page.tsx`

AppShell 新增入口：

- `/knowledge-items`
- 页面标题：知识条目库

页面能力：

- 列表。
- `status` / `item_type` / `source_filename` / `source_document_id` 筛选。
- 详情。
- source chunks / `source_text`。
- versions。
- reviews。
- `draft` / `rejected` 编辑。
- `pending_review` 审核。
- `approved` / `deprecated` 禁止直接编辑。
- `submit` / `approve` / `reject` / `deprecate`。
- `revise`。
- `extract` 抽取入口。
- `skipped_duplicates` 展示。
- `confidence` 说明。
- 状态标签。

前端不得：

- 不调用 `/api/v1/rag/ask`。
- 不调用 `/api/v1/search`。
- 不调用 `/api/v1/search/vector`。
- 不直接调用 Ollama。
- 不直接调用 OpenSearch。
- 不做模型选择。
- 不做 prompt 编辑。
- 不做索引管理。
- 不暴露旧 `knowledge_entries` 入口。

## 11. 测试文件

新增 / 修改测试：

- `backend/tests/test_knowledge_item_models.py`
- `backend/tests/test_knowledge_item_service.py`
- `backend/tests/test_knowledge_item_api.py`
- `backend/tests/test_knowledge_extraction.py`

覆盖范围：

- 新四表 metadata。
- 旧 model 移除。
- migration 非空保护。
- `content_hash` 计算。
- `content_hash` 无唯一约束。
- `source_chunk_ids` 追溯。
- `source_text` 快照。
- version 快照。
- 状态流转。
- illegal transition。
- `revise`。
- `extract` 默认 `draft`。
- `auto_submit` -> `pending_review`。
- parser 成功 / 失败。
- duplicate skipped。
- 不连接真实 Ollama。
- 不连接 OpenSearch。
- 不调用 RAG。
- 不写 `retrieval_logs`。

## 12. 手动验收流程

以下步骤用于第七阶段完成后的人工验收。本文件只记录流程，不自动执行命令。

### A. 安装 / 环境确认

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -c "import fastapi; print('fastapi ok')"
```

如果缺少依赖，请按项目依赖说明安装；不要由自动化脚本私自安装。

### B. 后端静态 / 单元测试

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_knowledge_item_models.py tests/test_knowledge_item_service.py tests/test_knowledge_item_api.py tests/test_knowledge_extraction.py -q
```

### C. 第五阶段回归测试

```powershell
python -m pytest tests/test_search_engine.py tests/test_search_index.py tests/test_hybrid_search.py -q
```

### D. 第六阶段回归测试

```powershell
python -m pytest tests/test_llm_provider.py tests/test_rag_context.py tests/test_rag_service.py tests/test_rag_api.py -q
```

### E. 前端 lint

```powershell
cd D:\rag_system\frontend
npm run lint
```

### F. 迁移前数据库安全检查

用户需手动确认数据库为本地开发库。

建议手动检查旧表是否为空：

```sql
SELECT COUNT(*) FROM knowledge_entries;
SELECT COUNT(*) FROM entry_versions;
SELECT COUNT(*) FROM entry_review_records;
```

如果表不存在或为空，才能继续。如果任一非空，停止迁移并确认处理策略。

### G. 执行 Alembic migration

必须由用户单独确认后才执行：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
alembic upgrade head
```

### H. 检查新表

```sql
SELECT to_regclass('public.knowledge_items');
SELECT to_regclass('public.knowledge_item_chunks');
SELECT to_regclass('public.knowledge_item_reviews');
SELECT to_regclass('public.knowledge_item_versions');
```

### I. 启动基础服务

```powershell
cd D:\rag_system
docker compose --env-file .env -f infra/docker-compose.yml up -d postgres redis minio opensearch
```

不要把 `docker compose down -v` 作为常规命令。

### J. 启动后端

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

### K. 启动前端

```powershell
cd D:\rag_system\frontend
npm run dev
```

### L. API 手动验收

- 创建 draft：`POST /api/v1/knowledge-items`
- 列表：`GET /api/v1/knowledge-items`
- 详情：`GET /api/v1/knowledge-items/{id}`
- chunks：`GET /api/v1/knowledge-items/{id}/chunks`
- versions：`GET /api/v1/knowledge-items/{id}/versions`
- 提交审核：`POST /api/v1/knowledge-items/{id}/submit`
- 批准：`POST /api/v1/knowledge-items/{id}/approve`
- 驳回：`POST /api/v1/knowledge-items/{id}/reject`
- 废弃：`POST /api/v1/knowledge-items/{id}/deprecate`
- 审核记录：`GET /api/v1/knowledge-items/{id}/reviews`
- 修订：`POST /api/v1/knowledge-items/{id}/revise`
- 抽取：`POST /api/v1/knowledge-items/extract`

### M. 前端手动验收

打开：

```text
http://localhost:3000/knowledge-items
```

验收：

- 能看到“知识条目库”。
- 能查看列表。
- 能筛选。
- 能查看详情。
- 能查看 `source_text`。
- 能查看 versions。
- 能查看 reviews。
- `draft` / `rejected` 可编辑。
- `approved` / `deprecated` 不可直接编辑。
- 能 `submit` / `approve` / `reject` / `deprecate`。
- 能 `revise`。
- 能 `extract`。
- 能看到 `skipped_duplicates`。
- 页面说明 `confidence` 不等于审核可信度。
- 页面不做模型选择。
- 页面不做 prompt 编辑。
- 页面不暴露索引管理。

### N. 回归验收

确认：

- `/api/v1/search` 正常。
- `/api/v1/rag/ask` 正常。
- 旧 `/api/v1/search/vector` 不在 OpenAPI 中，调用返回 404。
- `/search` 页面仍正常。
- `/rag` 页面仍正常。
- `retrieval_logs` 没有因为第七阶段增加。

## 13. 当前未实现内容

第七阶段仍未实现：

- `approved` 条目接入 RAG。
- 知识条目 OpenSearch 索引。
- Neo4j。
- Graph-enhanced RAG。
- LangGraph。
- reranker。
- 模型选择。
- prompt 编辑。
- 权限系统。
- 多人审核工作流。
- 复杂语义去重。
- embedding 去重。
- 图谱同步。
- production 权限隔离。
- `retrieval_logs` 写入。

## 14. 后续阶段建议

第八阶段建议：

- Neo4j 知识图谱构建与同步。
- 从 `status=approved` 的 `knowledge_items` 同步实体和关系。
- 保留 `source_chunk_ids` / `source_text` / `source_document_id`。
- 建立 PostgreSQL `knowledge_items` 与 Neo4j 节点映射。

第九阶段建议：

- Graph-enhanced RAG。
- `hybrid_search` 结果映射到图谱节点。
- 根据 `chunk_id` / entity 做图谱扩展。
- 融合 text evidence 和 graph evidence。
- 返回 answer + text citations + graph citations。

第十阶段建议：

- LangGraph 化流程编排。
- 多步检索。
- 条件分支。
- 工具调用。
- 失败回退。

## 15. 安全边界

继续禁止：

- `docker compose down -v`
- `docker volume prune`
- `docker system prune --volumes`
- 删除 PostgreSQL volume
- 删除 MinIO bucket
- 删除 OpenSearch volume 或 index
- 手写 `DROP` / `TRUNCATE` SQL
- 无 `WHERE DELETE`
- 未确认 `alembic upgrade`
- 删除 `documents` / `document_chunks` / `retrieval_logs` / uploaded files / search 相关表
- 修改 `/api/v1/search`
- 修改 `/api/v1/rag/ask`
- 删除其他搜索接口（旧纯向量接口已单独授权退役）
- 写 `retrieval_logs`
- 接入 Neo4j
- 引入 LangGraph
- 正式接入 reranker
- 让未审核知识参与权威回答
- 在日志中输出完整 prompt 或 API key
