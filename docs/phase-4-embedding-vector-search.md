# 第四阶段：Embedding 生成与基础向量检索闭环

本文记录 `D:\rag_system` 第四阶段“Embedding 生成与基础向量检索闭环”的实现范围、配置、数据结构、接口、前端能力、手动验收和安全边界。

第四阶段数据流：

```text
document_chunks
-> 本地 Qwen3-Embedding-0.6B 生成 embedding
-> pgvector 存储
-> 基础向量检索
-> 前端轻量检索验证
```

第四阶段只打通本地 embedding 与基础向量检索闭环，不实现 RAG 问答、LLM 回答、reranker、混合检索、关键词检索、图谱检索或后台任务队列。

## 1. 本地模型配置

后端 `.env` 建议配置：

```text
EMBEDDING_PROVIDER=local_qwen3
EMBEDDING_MODEL=Qwen3-Embedding-0.6B
EMBEDDING_MODEL_PATH=D:/rag_system/models/Qwen3-Embedding-0.6B
EMBEDDING_DEVICE=auto
EMBEDDING_BATCH_SIZE=8
EMBEDDING_NORMALIZE=true
EMBEDDING_DIM=1024
EMBEDDING_LOCAL_FILES_ONLY=true
EMBEDDING_QUERY_INSTRUCTION=Given a search query about casting process knowledge, retrieve relevant document chunks that answer the query.
EMBEDDING_USE_QUERY_INSTRUCTION=true
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

说明：

- 使用本地路径 `D:/rag_system/models/Qwen3-Embedding-0.6B` 加载模型。
- 不访问外网，不下载模型。
- 默认 embedding 维度为 `1024`，local-only 维度探测已确认 `actual_dim=1024`。
- document chunk 生成 embedding 时使用 `encode_documents`。
- search query 生成 embedding 时使用 `encode_query`。
- query instruction 只用于 query 编码，不用于 document chunk 编码。
- document embedding 和 query embedding 必须使用同一模型、同一维度、同一归一化策略。
- Provider 采用懒加载，不在模块 import 或 FastAPI 启动时强制加载模型。

## 2. 数据库变化

第四阶段通过 Alembic 为 `document_chunks` 新增字段：

- `embedding vector(1024) nullable`
- `embedding_error_message text nullable`
- `embedding_updated_at timestamptz nullable`

已有字段继续使用：

- `embedding_model`
- `embedding_dim`
- `embedding_status`

`embedding_status` 状态流转：

- `not_started`：尚未生成 embedding。
- `embedding`：正在生成 embedding。
- `embedded`：已生成 embedding。
- `embed_failed`：生成失败。

约束和边界：

- `embedded` chunks 默认跳过，不覆盖。
- `embed_failed` chunks 可以重新生成。
- 第四阶段不支持 `force` 覆盖。
- 失败时写入 `embedding_error_message`。
- 成功时写入 `embedding_updated_at`。
- 第四阶段不写 `retrieval_logs`。
- 第四阶段不创建 HNSW / IVFFlat 或其他向量索引。
- 本地小规模数据使用 pgvector cosine distance 精确扫描。

## 3. 后端接口

### POST /api/v1/documents/{document_id}/embeddings

为单个文档的 chunks 生成 embedding。

行为：

- 只处理 `not_started` 和 `embed_failed` chunks。
- 跳过 `embedded` chunks。
- 全部已 `embedded` 时返回 `DOCUMENT_EMBEDDINGS_ALREADY_GENERATED`。
- 文档尚未解析、没有 chunks 时返回 `DOCUMENT_NOT_PARSED`。
- 不自动解析文档。
- 不自动触发检索。
- 不写 `retrieval_logs`。
- 不支持 `force` 覆盖。

返回统计：

- `document_id`
- `total`
- `embedded`
- `skipped`
- `failed`
- `model`
- `dim`
- `device`

### GET /api/v1/documents/{document_id}/embedding-status

查询某个文档 chunks 的 embedding 状态统计。

行为：

- 只返回状态统计。
- 不触发 embedding 生成。
- 不加载模型。
- 不写数据库。
- 文档不存在时返回 `DOCUMENT_NOT_FOUND`。

返回统计：

- `document_id`
- `total`
- `not_started`
- `embedding`
- `embedded`
- `embed_failed`
- `models`
- `dims`

### POST /api/v1/search/vector

基础向量检索接口。

输入：

- `query`
- `limit`
- 可选 `document_id`

行为：

- 使用 provider `encode_query` 生成 query embedding。
- 只检索 `embedding_status='embedded'` 且 `embedding IS NOT NULL` 的 chunks。
- 同时限定 chunk 的 `embedding_model` 和 `embedding_dim` 与 query embedding 一致。
- 如传入 `document_id`，只检索该文档。
- 按 cosine distance 升序排序。
- `distance` 越小越相似。
- 返回 `score = 1 - distance`。
- 没有命中时返回空 `items`，不作为错误。
- 不自动生成 embedding。
- 不自动解析文档。
- 不写 `retrieval_logs`。
- 不生成 RAG 回答。

每条结果包含：

- `chunk_id`
- `document_id`
- `original_filename`
- `chunk_index`
- `content`
- `chunk_type`
- `source_metadata`
- `embedding_model`
- `embedding_dim`
- `embedding_status`
- `distance`
- `score`

## 4. 前端能力

第四阶段新增前端能力：

- 文档详情页展示 embedding 状态。
- 文档详情页提供“生成 embedding”按钮。
- 生成成功后刷新详情页。
- `/search` 页面提供基础向量检索入口。
- `/search` 页面支持输入 query、limit 和可选 document_id。
- 检索结果展示 `original_filename`、`chunk_index`、`content` 预览、`source_metadata`、`embedding_model`、`embedding_dim`、`distance`、`score`。
- 页面说明 `distance 越小越相似`、`score = 1 - distance`。

前端不实现：

- 聊天界面。
- RAG 回答。
- 流式输出。
- reranker 控件。
- 混合检索控件。
- 关键词检索控件。
- 任务中心。
- 进度轮询。
- 多模型切换。

## 5. 第四阶段明确不实现

第四阶段仍不实现：

- RAG 问答完整链路。
- 大语言模型回答。
- reranker。
- 混合检索。
- 关键词检索。
- 图谱检索。
- 知识条目自动抽取。
- 专家审核。
- 真实 MinerU 深度解析。
- Celery / 后台任务队列。
- `retrieval_logs` 写入。
- HNSW / IVFFlat 或其他向量索引。
- 多模型调度。
- 生产级模型服务部署。
- 流式回答。
- 多轮问答记忆。

## 6. 手动验收步骤

以下步骤只供用户手动执行，Codex 不自动执行 Docker、Git、npm、pip、alembic、pytest、uvicorn、服务启动、数据库写入、模型加载或维度探测命令。

### 6.1 安装依赖

RTX 5060 / CUDA 12.8 环境建议先安装适配 Blackwell / `sm_120` 的 GPU 版 torch，再安装其余依赖，避免 pip 解析依赖时覆盖 GPU torch。

示例：

```powershell
cd D:/rag_system/backend
./.venv/Scripts/Activate.ps1
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
python -m pip install "pgvector>=0.3,<1.0" "sentence-transformers>=2.7.0,<6.0" "transformers>=4.51.0,<5.0"
python -m pip install -e . --no-deps
```

### 6.2 local-only 维度探测

探测前设置：

```powershell
$env:HF_HUB_OFFLINE="1"
$env:TRANSFORMERS_OFFLINE="1"
```

维度探测必须：

- 使用 `LocalQwen3EmbeddingProvider`。
- 使用本地路径。
- 不访问外网。
- 不下载模型。
- 不写数据库。
- 不创建迁移。
- 只确认 `actual_dim=1024`。

已确认结果：

```text
provider=local_qwen3
model=Qwen3-Embedding-0.6B
configured_dim=1024
actual_dim=1024
matches_config=True
```

### 6.3 Alembic

用户确认连接的是本地开发库 `rag_system` 后，在 `backend` 目录手动执行：

```powershell
alembic upgrade head
```

Codex 不自动执行迁移，PostgreSQL MCP 也不得用于执行迁移或写入。

### 6.4 功能验收

1. 启动 Docker 基础服务。
2. 启动 FastAPI 后端。
3. 启动 Next.js 前端。
4. 打开 `/documents` 上传或选择已有文档。
5. 进入文档详情页 `/documents/[id]`。
6. 如文档尚未解析，先点击解析文档。
7. 在文档详情页确认 embedding 状态统计。
8. 点击生成 embedding。
9. 确认 `embedded` 数量增加，`embedding_status` 更新为 `embedded`。
10. 打开 `/search`。
11. 输入 query 执行基础向量检索。
12. 确认结果包含文档名、chunk_index、content、source_metadata、distance 和 score。
13. 确认 distance 越小越相似，score 等于 `1 - distance`。

### 6.5 数据库只读核验建议

可通过 PostgreSQL MCP 做只读核验：

- 查询 `alembic_version`。
- 查询 `information_schema.columns` 确认 `document_chunks.embedding`、`embedding_error_message`、`embedding_updated_at`。
- 查询少量 `document_chunks.embedding_status`。
- 查询 `pg_indexes` 确认没有 HNSW / IVFFlat 向量索引。
- 查询 `retrieval_logs` 记录数量，确认第四阶段检索不写入日志。

PostgreSQL MCP 仅允许 SELECT、information_schema、pg_extension、pg_indexes 等只读查询。

## 7. 安全边界

继续严格禁止：

- `docker compose down -v`
- 删除 Docker volume。
- `docker volume prune`
- `docker system prune --volumes`
- 清空 MinIO bucket。
- 删除 MinIO bucket。
- 删除 MinIO 对象。
- `DROP DATABASE`
- `DROP TABLE`
- `TRUNCATE`
- 无 `WHERE` 条件的 `DELETE`
- 使用 PostgreSQL MCP 执行 `INSERT` / `UPDATE` / `DELETE`
- 使用 PostgreSQL MCP 执行 `ALTER` / `CREATE`
- 使用 PostgreSQL MCP 执行迁移、补数据、清空表或修改结构。
- `npm audit fix --force`
- 自动下载模型。
- 自动创建向量索引。
- 未经用户确认执行 Alembic。
