# 第五阶段：OpenSearch + IK 统一混合检索索引闭环

## 阶段定位

第五阶段在第四阶段 PostgreSQL + pgvector 基础向量检索闭环之上，引入 OpenSearch 作为派生检索索引，并通过 IK 中文分词、BM25 关键词召回、kNN 向量召回和 weighted RRF 融合，形成统一的智能检索入口。

本阶段的数据定位保持清晰：

- PostgreSQL 是主数据源。
- MinIO 保存原始上传文件。
- `document_chunks` 保存解析切块、chunk 元数据和 embedding。
- OpenSearch 是派生检索索引，可从 PostgreSQL 中的 documents 与 document_chunks 重建。
- 不使用 OpenSearch 替代 PostgreSQL 主库。
- 不删除 PostgreSQL 中的 pgvector embedding 字段。
- `POST /api/v1/search/vector` 保留为第四阶段 pgvector 调试/回退能力，前端不再调用。

## 本阶段新增能力

- OpenSearch 3.6.0 本地单节点服务规划。
- analysis-ik 3.6.0 中文分词插件固化到自定义 OpenSearch 镜像。
- IK 自定义词典文件：
  - `infra/opensearch/ik/custom.dic`
  - `infra/opensearch/ik/stopword.dic`
- `backend/app/search_engine/client.py`：搜索引擎 client 懒加载封装。
- `backend/app/search_engine/index_schema.py`：chunk 级索引 mapping / settings 构造。
- `backend/app/services/search_index.py`：索引创建、重建、状态查询和 chunk payload 构造。
- `backend/app/services/hybrid_search.py`：关键词召回、向量召回和 weighted RRF 融合。
- 索引管理 API。
- 统一智能检索 API：`POST /api/v1/search`。
- 前端 `/search` 页面改造为“智能检索”。

## 后端接口

第五阶段涉及以下搜索接口：

- `POST /api/v1/search`：第五阶段统一混合检索接口。
- `POST /api/v1/search/vector`：第四阶段遗留 pgvector 调试接口，前端不再调用。
- `POST /api/v1/search/index/create`：创建不存在的 OpenSearch index 和 alias。
- `POST /api/v1/search/index/rebuild`：按 scope 重建派生搜索索引。
- `GET /api/v1/search/index/status`：查询搜索引擎、index、alias 和可同步 chunks 状态。

前端不暴露索引管理 API。索引管理 API 仅用于本地开发或管理员操作。

## OpenSearch 索引设计

默认索引规划：

- 物理索引：`casting_chunks_v1`
- 查询别名：`casting_chunks_current`
- `content`：IK 中文分词字段。
- `content` 索引 analyzer 默认 `ik_max_word`，提高召回。
- `content` 查询 analyzer 默认 `ik_smart`，提高查询精度。
- `exact_terms`：`keyword` 数组，用于标准编号、材料牌号、工艺参数和专业术语精确匹配。
- `source_metadata`：`enabled=false` object，仅用于返回展示，不参与 dynamic mapping、检索或聚合。
- `embedding`：`knn_vector`。
- embedding 维度：1024。
- vector space：`cosine` / OpenSearch kNN 对应 cosine similarity 配置。
- `embedding_model`：`Qwen3-Embedding-0.6B`。
- `embedding_dim`：1024。

索引文档建议字段：

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

## 索引同步规则

只同步满足以下条件的 chunks：

- `embedding_status='embedded'`
- `embedding IS NOT NULL`
- `embedding_dim=1024`
- `embedding_model='Qwen3-Embedding-0.6B'`

OpenSearch 文档 `_id` 使用 `chunk_id`，重复同步应保持幂等。

`scope=document` 同步规则：

1. 先通过 `delete_by_query` 删除该 `document_id` 下的旧索引文档。
2. 再从 PostgreSQL 读取当前最新的 embedded chunks。
3. 使用 bulk upsert/index 写入 OpenSearch。

`scope=all` 用于全量同步当前可同步 chunks。OpenSearch 仍是派生数据，同步失败不能破坏 PostgreSQL 主数据。

## exact_terms v1

`exact_terms` 在 `search_index` 同步阶段构造，并随 chunk payload 一起写入 OpenSearch index。

第五阶段 v1 使用内置铸型术语表和简单规则/正则抽取，例如：

- 标准编号：`GB/T`、`GJB`、`HB`、`ASTM`、`ISO`
- 材料牌号：`HT250`、`QT450`、`ZG25`
- 铸型工艺术语：冒口、冷铁、热节、补缩、浇注系统、浇口、横浇道、直浇道、内浇道、砂芯、芯盒、分型面
- 缺陷术语：缩孔、缩松、夹渣、夹砂、气孔、卷气、冷隔、裂纹、粘砂

本阶段不引入 jieba / HanLP / 其他 NLP 依赖，不提前实现知识条目抽取。后续可将 exact_terms 抽取升级为词典文件驱动。

## 混合检索流程

统一接口 `POST /api/v1/search` 的内部流程：

```text
query
-> encode_query(query)
-> OpenSearch IK + BM25 关键词召回
-> OpenSearch kNN 向量召回
-> chunk_id 去重
-> weighted RRF 融合
-> 返回 chunks
```

query embedding 必须继续使用第四阶段 embedding provider，并调用 `encode_query(query)`，不得使用 `encode_documents(query)`。

关键词召回和向量召回都过滤：

- `embedding_status = embedded`
- `embedding_dim = 1024`
- `embedding_model = Qwen3-Embedding-0.6B`

如传入 `document_id`，两路召回都增加对应过滤条件。

## Weighted RRF 融合

融合公式：

```text
hybrid_score =
keyword_weight / (rrf_k + keyword_rank)
+
vector_weight / (rrf_k + vector_rank)
```

说明：

- `keyword_rank` 和 `vector_rank` 从 1 开始。
- 某一路未命中时，该路贡献为 0。
- 最终排序依据 `hybrid_score`。
- 不直接相加 BM25 原始分数和 vector 原始分数，因为两路原始分数不在同一量纲。
- `keyword_score` 和 `vector_score` 仅作为前端展示字段。
- `vector_score` 不要求等同第四阶段 pgvector 的 `1 - distance`。
- `retrieval_source` 可为 `both`、`keyword`、`vector`。

默认配置：

- `HYBRID_KEYWORD_WEIGHT=0.5`
- `HYBRID_VECTOR_WEIGHT=0.5`
- `HYBRID_RRF_K=60`
- `HYBRID_KEYWORD_TOP_K=50`
- `HYBRID_VECTOR_TOP_K=50`
- `HYBRID_FINAL_LIMIT=10`

## 前端说明

前端 `/search` 页面已经改为“智能检索”：

- 调用 `POST /api/v1/search`。
- 展示 `retrieval_source`。
- 展示 `keyword_score`、`vector_score`、`hybrid_score`。
- 展示 `matched_keywords`。
- 提示当前不生成 RAG 回答。
- 不提供 vector / keyword / hybrid 模式切换。
- 不暴露索引管理入口。
- 不自动生成 embedding。
- 不自动触发 index rebuild。

## 本阶段未实现

第五阶段仍不实现：

- RAG 问答
- LLM 回答
- reranker
- 图谱检索
- 知识条目自动抽取
- 专家审核
- Celery / 后台任务队列
- 自动解析-切块-向量化-索引流水线
- `retrieval_logs` 写入
- OpenSearch Dashboards
- 权重调参前端页面
- 生产级安全配置

## 安全边界

继续严格禁止将以下操作作为常规开发命令：

- `docker compose down -v`
- `docker volume prune`
- `docker system prune --volumes`
- 删除 PostgreSQL / Redis / MinIO / OpenSearch volume
- 删除 MinIO bucket 或对象
- `DROP DATABASE`
- `DROP TABLE`
- `TRUNCATE`
- 无 WHERE 的 `DELETE`
- 未经用户确认执行 Alembic 迁移
- 未经用户确认删除 OpenSearch index
- 未经用户确认修改生产或共享数据库

## 手动验收清单

本步骤只整理验收命令，不自动执行。

### 后端测试

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_search_engine.py tests/test_search_index.py tests/test_hybrid_search.py -q
```

### 前端检查

```powershell
cd D:\rag_system\frontend
npm run lint
```

### Docker / OpenSearch 本地服务

不要执行 `docker compose down -v`。

```powershell
cd D:\rag_system
docker compose --env-file .env -f infra/docker-compose.yml build opensearch
docker compose --env-file .env -f infra/docker-compose.yml up -d postgres redis minio opensearch
docker compose --env-file .env -f infra/docker-compose.yml ps
```

### 后端启动

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

### 前端启动

```powershell
cd D:\rag_system\frontend
npm run dev
```

### 索引 API 验收

创建 index 和 alias：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/search/index/create"
```

全量同步索引：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/search/index/rebuild" `
  -ContentType "application/json" `
  -Body '{"scope":"all"}'
```

查询索引状态：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8000/api/v1/search/index/status"
```

统一混合检索：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/search" `
  -ContentType "application/json" `
  -Body '{"query":"冒口如何保证热节补缩","limit":10}'
```

如果 `syncable_chunks=0`，需要先在文档详情页生成 embedding，再重新执行 index rebuild。

### 前端页面验收

1. 打开前端 `/search` 页面。
2. 页面标题应为“智能检索”。
3. 输入：`冒口如何保证热节补缩`。
4. 结果应展示 `retrieval_source`、`keyword_score`、`vector_score`、`hybrid_score`、`matched_keywords`。
5. 页面不应展示 RAG 回答、聊天界面、检索模式切换或索引管理入口。

### IK analyzer 自测

OpenSearch 启动并确认 index/mapping 后，可使用 analyzer API 验证自定义词典是否生效。示例文本：

```text
冒口设计应保证热节区域有效补缩
```

期望至少能切出：

- 冒口
- 热节
- 补缩

具体 analyzer API 命令应在 OpenSearch 服务可用后由用户手动执行。
