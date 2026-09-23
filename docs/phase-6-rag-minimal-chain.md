# 第六阶段：基于混合检索的 RAG 问答最小闭环

## 1. 阶段定位

第六阶段基于第五阶段已经验收通过的 OpenSearch + IK 统一混合检索能力，新增单轮 RAG 问答最小闭环。

当前系统边界保持不变：

- PostgreSQL 仍是主数据源。
- MinIO 仍保存原始文件。
- `document_chunks` 仍保存解析切块和 embedding。
- OpenSearch 仍是派生检索索引，可从 PostgreSQL 重建。
- 第六阶段复用第五阶段 `hybrid_search_chunks()`。
- 第六阶段不重写 BM25。
- 第六阶段不重写 OpenSearch kNN。
- 第六阶段不修改 weighted RRF 融合规则。
- 第六阶段不修改 OpenSearch 索引结构。
- 第六阶段不修改 `POST /api/v1/search` 返回结构。
- 旧 `POST /api/v1/search/vector` 已于 2026-09-23 单独授权退役，调用返回 404；RAG 仍复用 Hybrid Search，现有 RRF、可选 BGE reranker 和 context builder 链路不变。

第六阶段新增的问答链路为：

用户问题 -> 混合检索 chunks -> 构建 RAG 上下文 -> 调用本地 LLM -> 生成基于上下文的回答 -> 返回答案和引用。

Ollama 只作为本地 LLM 服务。后端不写死 Ollama SDK，而是通过 OpenAI-compatible provider 接入。前端通过 `POST /api/v1/rag/ask` 获取 `answer + citations + retrieval + llm`。

## 2. 第六阶段新增能力

第六阶段新增：

- OpenAI-compatible LLM provider。
- Ollama 本地 LLM 接入路径。
- RAG context builder。
- RAG prompt 模板。
- citation builder。
- RAG answer service。
- `POST /api/v1/rag/ask`。
- 前端 `/rag` 知识问答页面。
- no-context 正常返回。
- 返回 `answer + citations + retrieval + llm` 信息。

## 3. 新增后端模块

- `backend/app/llm/provider.py`：定义 LLM provider 抽象、生成请求/结果对象，以及 `get_llm_provider()`。
- `backend/app/llm/openai_compatible.py`：实现 OpenAI-compatible LLM provider，默认可连接 Ollama 的 `/v1` 兼容接口。
- `backend/app/llm/__init__.py`：导出 LLM 相关对象，不在 import 时创建 provider 或 client。
- `backend/app/rag/context_builder.py`：基于混合检索结果构建 RAG 上下文，分配 citation_id，控制上下文长度。
- `backend/app/rag/prompt.py`：构建铸型工艺知识库问答专用 prompt。
- `backend/app/rag/citations.py`：从上下文 chunks 构造 citations。
- `backend/app/rag/__init__.py`：导出 RAG 基础对象，不调用外部服务。
- `backend/app/services/rag.py`：编排单轮 RAG answer 链路，直接复用 `hybrid_search_chunks()`。
- `backend/app/api/v1/rag.py`：提供 `POST /api/v1/rag/ask`。
- `backend/app/schemas/rag.py`：定义 RAG API 请求和响应 schema。

## 4. 新增前端模块

- `frontend/lib/rag.ts`：封装 `POST /api/v1/rag/ask`，定义前端类型与友好错误提示。
- `frontend/components/RagAskPanel.tsx`：提供问题输入、limit 设置、答案展示、引用片段展示和 no-context 展示。
- `frontend/app/rag/page.tsx`：新增 `/rag` 知识问答页面。
- `frontend/components/AppShell.tsx`：新增 `/rag` 导航入口，同时保留 `/search` 智能检索入口。

## 5. 新增配置

LLM 配置：

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3.5:9b
LLM_API_KEY=ollama
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
LLM_TIMEOUT_SECONDS=120
```

说明：

- `LLM_PROVIDER` 当前只支持 `openai_compatible`。
- `LLM_BASE_URL=http://localhost:11434/v1` 对应 Ollama 的 OpenAI-compatible API。
- `LLM_API_KEY=ollama` 是本地兼容接口占位值。
- 用户本地已下载 `qwen3.5:9b`，实际运行建议使用 `LLM_MODEL=qwen3.5:9b`。
- 前端不写死模型名，实际展示以后端返回的 `llm.model` 为准。

RAG 配置：

```env
RAG_TOP_K=8
RAG_CONTEXT_MAX_CHARS=12000
RAG_REQUIRE_CITATIONS=true
RAG_NO_CONTEXT_MESSAGE=当前知识库中未检索到足够依据，无法可靠回答该问题。
RAG_SYSTEM_PROMPT_NAME=casting_rag_default
```

Reranker 预留配置：

```env
RERANKER_ENABLED=false
RERANKER_PROVIDER=local_qwen3
RERANKER_MODEL=bge-reranker-v2-m3
RERANKER_MODEL_PATH=D:/rag_system/models/bge-reranker-v2-m3
RERANKER_TOP_K=8
```

说明：

- 第六阶段不正式实现 reranker。
- `RERANKER_ENABLED=true` 时应返回明确配置错误。
- 前端不提供 reranker 配置入口。

## 6. RAG 链路

第六阶段 RAG 链路：

```text
question
-> hybrid_search_chunks()
-> optional_rerank_chunks()，当前默认 no-op
-> build_rag_context()
-> build_rag_prompt()
-> OpenAI-compatible LLM provider
-> build_citations()
-> answer + citations + retrieval + llm
```

必须保持：

- `retrieve_chunks()` 直接复用第五阶段 `hybrid_search_chunks()`。
- 不重新实现 BM25。
- 不重新实现 kNN。
- 不重新实现 weighted RRF。
- 不修改 OpenSearch index schema。
- 不修改 `POST /api/v1/search`。
- 保留 Hybrid 的 OpenSearch 向量召回；旧纯向量接口的阶段保留要求已被退役决定替代。
- 不写 `retrieval_logs`。

no-context 规则：

- 检索为空或上下文不足不是异常。
- API 返回 HTTP 200。
- `success=true`。
- `context_status="no_context"`。
- `answer` 使用 `RAG_NO_CONTEXT_MESSAGE`。
- `citations=[]`。
- `retrieval` 返回实际检索结果，完全无命中时 `total=0`、`items=[]`。
- no-context 时不调用 LLM。
- no-context 时不写 `retrieval_logs`。

## 7. Prompt 原则

第六阶段 prompt 必须遵循：

- 回答必须基于检索上下文。
- 不允许脱离上下文编造。
- 上下文不足时必须提示无法可靠回答。
- 引用使用 `[1]`、`[2]` 等片段编号。
- citation 编号不是文献编号。
- 不得伪造标准条文。
- 不得输出上下文中没有的工艺参数。
- 如果检索片段冲突，应提示“检索片段中存在不一致，需要人工核验”。
- 不暴露系统内部配置。
- 不暴露 API key。

## 8. API 说明

新增接口：

```http
POST /api/v1/rag/ask
```

请求体示例：

```json
{
  "question": "冒口如何保证热节补缩？",
  "limit": 8,
  "document_id": null
}
```

响应体主要字段：

- `question`：本次问题。
- `answer`：模型生成的回答，或 no-context 提示。
- `context_status`：`"ok"` 或 `"no_context"`。
- `citations`：引用片段列表。
- `retrieval`：复用第五阶段混合检索结果结构。
- `llm`：LLM provider 与 model 信息。

citation 字段：

- `citation_id`
- `chunk_id`
- `document_id`
- `original_filename`
- `chunk_index`
- `content`
- `hybrid_score`
- `retrieval_source`

错误映射：

- `RAG_QUERY_EMPTY` -> 400
- `RAG_CONFIG_INVALID` -> 400
- `RAG_ANSWER_FAILED` -> 500
- `LLM_CONFIG_INVALID` -> 400
- `LLM_UNAVAILABLE` -> 503
- `LLM_TIMEOUT` -> 504
- `LLM_GENERATION_FAILED` -> 500
- 搜索相关错误沿用第五阶段映射，例如 `SEARCH_ENGINE_UNAVAILABLE` -> 503，`SEARCH_INDEX_NOT_FOUND` -> 404，`HYBRID_SEARCH_FAILED` -> 500。

明确边界：

- no-context 不作为错误。
- no-context 返回 200。
- no-context 返回 `success=true`。
- no-context 返回 `context_status="no_context"`。

## 9. 前端说明

第六阶段新增 `/rag` 页面：

- 页面标题为“知识问答”。
- 调用 `POST /api/v1/rag/ask`。
- 展示 `answer`。
- 展示 `citations`。
- 展示 `retrieval.total`。
- 展示 `llm.provider` / `llm.model`。
- no-context 作为正常状态展示。
- 不做聊天历史。
- 不做流式输出。
- 不做模型选择。
- 不做 prompt 编辑。
- 不暴露索引管理入口。

页面关系：

- `/search` 仍是“智能检索”页面，只做混合检索结果验证。
- `/rag` 是“知识问答”页面，调用 RAG API 生成回答并展示引用。

## 10. 当前未实现内容

第六阶段仍不做：

- LangGraph。
- 复杂多轮推理。
- Agent。
- MinerU。
- reranker 正式接入。
- 长期记忆。
- 问题改写。
- 多步检索。
- 图谱检索。
- 知识条目抽取。
- 专家审核。
- 流式输出。
- `retrieval_logs` 写入。
- 生产级权限认证。
- 模型管理页面。
- prompt 编辑页面。
- 索引管理前端。

## 11. 测试文件

第六阶段新增测试：

- `backend/tests/test_llm_provider.py`
- `backend/tests/test_rag_context.py`
- `backend/tests/test_rag_service.py`
- `backend/tests/test_rag_api.py`

测试边界：

- 使用 fake client / monkeypatch。
- 不连接真实 Ollama。
- 不连接真实 OpenSearch。
- 不访问真实数据库。
- 不写 `retrieval_logs`。

## 12. 手动验收流程

以下命令仅作为用户手动验收参考。本阶段文档更新不自动执行任何命令。

### 12.1 后端依赖安装

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pip install "openai>=1.0,<3.0"
```

### 12.2 后端第六阶段测试

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_llm_provider.py tests/test_rag_context.py tests/test_rag_service.py tests/test_rag_api.py -q
```

### 12.3 第五阶段回归测试

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_search_engine.py tests/test_search_index.py tests/test_hybrid_search.py -q
```

### 12.4 前端 lint

```powershell
cd D:\rag_system\frontend
npm run lint
```

### 12.5 Ollama 验收

```powershell
ollama list
ollama run qwen3.5:9b
```

如果模型已经存在，可直接确认 `ollama list` 中包含 `qwen3.5:9b`。

OpenAI-compatible API 手动验证示例：

```powershell
$body = @{
  model = "qwen3.5:9b"
  messages = @(
    @{ role = "user"; content = "请用一句话说明冒口的作用。" }
  )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri "http://localhost:11434/v1/chat/completions" `
  -Method Post `
  -Headers @{ Authorization = "Bearer ollama" } `
  -ContentType "application/json" `
  -Body $body
```

### 12.6 基础服务启动

```powershell
cd D:\rag_system
docker compose --env-file .env -f infra/docker-compose.yml up -d postgres redis minio opensearch
```

不要把 `docker compose down -v` 作为常规命令。

### 12.7 后端启动

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

### 12.8 前端启动

```powershell
cd D:\rag_system\frontend
npm run dev
```

### 12.9 第五阶段索引状态确认

```http
GET /api/v1/search/index/status
```

验收要点：

- 搜索引擎可用。
- index 存在。
- alias 存在。
- index document count 大于 0。
- PostgreSQL 可同步 chunks 数正常。

### 12.10 第五阶段混合检索确认

```http
POST /api/v1/search
```

请求示例：

```json
{
  "query": "冒口如何保证热节补缩",
  "limit": 8,
  "document_id": null
}
```

验收要点：

- 返回混合检索结果。
- `items` 包含 `hybrid_score`、`retrieval_source`、`matched_keywords`。
- 旧 `/api/v1/search/vector` 不在 OpenAPI 中，调用返回 404。

### 12.11 第六阶段 RAG API 验收

```http
POST /api/v1/rag/ask
```

请求示例：

```json
{
  "question": "冒口如何保证热节补缩？",
  "limit": 8,
  "document_id": null
}
```

验收要点：

- 返回 `answer`。
- 返回 `citations`。
- `citations` 均来自 `retrieval.items` 对应 chunks。
- `context_status` 为 `ok` 或 `no_context`。
- no-context 时 HTTP 200。
- no-context 时 `success=true`、`citations=[]`。
- `retrieval_logs` 仍不写入。
- LLM 不可用时返回明确错误。
- 前端 `/rag` 能展示 answer 和 citations。

## 13. 安全边界

继续禁止：

- `docker compose down -v`
- `docker volume prune`
- `docker system prune --volumes`
- 删除 PostgreSQL volume
- 删除 MinIO bucket
- 删除 OpenSearch volume，除非用户明确确认
- `DROP`
- `TRUNCATE`
- 无 `WHERE` 的 `DELETE`
- 未确认 Alembic 迁移
- 未确认修改数据库结构
- 未确认删除 OpenSearch index
- 未确认写 `retrieval_logs`
- 未确认引入 LangGraph
- 未确认接入 MinerU
- 未确认正式接入 reranker
- 未确认删除其他搜索接口（旧纯向量接口已单独授权退役）
