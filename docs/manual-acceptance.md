# 手动验收清单

本文档记录本地开发环境的手动验收建议。以下步骤只作为检查清单和命令说明，不表示本步骤已经执行，也不表示测试已经通过。

## 第八阶段 MinerU 文档解析验收

### 1. 基础服务与配置

- [ ] 后端健康检查正常。
- [ ] 数据库 migration 状态符合预期。
- [ ] MinIO bucket 存在。
- [ ] OpenSearch 健康状态正常。
- [ ] `.env` / `backend/.env` 中 `DOCUMENT_PARSER_PROVIDER` 设置符合当前验收目标。
- [ ] 未在仓库或日志中写入真实 MinerU API key。

### 2. basic parser 验收

- [ ] 设置 `DOCUMENT_PARSER_PROVIDER=basic`。
- [ ] 上传简单 txt / md 测试文档。
- [ ] 调用 `POST /api/v1/documents/{document_id}/parse`。
- [ ] parse 成功。
- [ ] `GET /api/v1/documents/{document_id}/chunks` 仍可用。
- [ ] basic parser 路径不要求写入 `document_blocks` / `document_assets`。

### 3. MinerU 配置错误验收

- [ ] 设置 `DOCUMENT_PARSER_PROVIDER=mineru_api`。
- [ ] 故意缺省 `MINERU_API_BASE_URL` 或 `MINERU_API_KEY`。
- [ ] 调用 parse API。
- [ ] 返回明确配置错误，例如 `DOCUMENT_PARSER_CONFIG_INVALID`。
- [ ] 不静默 fallback 到 basic。
- [ ] error detail 不泄露 API key、完整原文或完整 MinerU JSON。

### 4. MinerU 成功解析验收

- [ ] 设置 `DOCUMENT_PARSER_PROVIDER=mineru_api`。
- [ ] 配置有效 MinerU API 参数。
- [ ] 上传测试 PDF 或扫描 PDF。
- [ ] 调用 `POST /api/v1/documents/{document_id}/parse`。
- [ ] parse 成功。
- [ ] `documents.process_status=parsed`。
- [ ] `document_parse_runs.status=succeeded`。
- [ ] `document_parse_runs.is_active=true`。
- [ ] `parse_run.status=succeeded` 只在 MinerU、assets、blocks、chunks、mapping 全部成功后出现。

### 5. parse-status 验收

- [ ] `GET /api/v1/documents/{document_id}/parse-status` 返回 `latest_parse_run`。
- [ ] 返回 `active_parse_run`。
- [ ] 展示 `parser_provider`。
- [ ] 展示 `parse_mode`。
- [ ] 展示 `status`。
- [ ] 展示 `is_active`。
- [ ] 展示 `block_count`。
- [ ] 展示 `asset_count`。
- [ ] 展示 `page_count`。
- [ ] 展示 `output_markdown_status`。
- [ ] 展示 `output_json_status`。
- [ ] `output_*_status` 能区分 `saved` / `download_deferred` / `unavailable`。
- [ ] 展示 `failure_status_persisted`。
- [ ] 展示 `error_message`，且不泄露敏感信息。

### 6. blocks / assets API 验收

- [ ] `GET /api/v1/documents/{document_id}/blocks?limit=50&offset=0` 可用。
- [ ] blocks API 支持 `parse_run_id` 过滤。
- [ ] blocks API 支持 `block_type` 过滤。
- [ ] blocks API 分页可用。
- [ ] blocks API 不一次性返回巨大 JSON。
- [ ] `GET /api/v1/documents/{document_id}/assets?limit=50&offset=0` 可用。
- [ ] assets API 支持 `parse_run_id` 过滤。
- [ ] assets API 支持 `asset_type` 过滤。
- [ ] assets API 分页可用。
- [ ] assets API 只返回资产元数据，不返回二进制内容。
- [ ] parse_run 不属于当前 document 时返回明确错误。

### 7. 前端验收

- [ ] 文档详情页能展示解析中间层面板。
- [ ] 能看到最近 parse run。
- [ ] 能看到 active parse run。
- [ ] 能看到 provider / status / is_active。
- [ ] 能看到 block_count / asset_count / page_count。
- [ ] 能看到 output markdown/json 状态。
- [ ] 能看到 failure_status_persisted。
- [ ] blocks 折叠列表可用。
- [ ] assets 折叠列表可用。
- [ ] source_metadata 默认折叠或只显示摘要。
- [ ] text / markdown / html / latex 字段截断展示。
- [ ] 旧 chunks 面板仍可用。
- [ ] embedding 面板仍可用。
- [ ] 不做 PDF 坐标框可视化。
- [ ] 不做图片大图预览系统。
- [ ] 不做 MinerU API key 配置页面。

### 8. embedding / OpenSearch / search 回归

- [ ] 不触发 embedding 的情况下，`embedding_status` 保持 `not_started` 或 `None`。
- [ ] 手动触发 embedding 后状态正常。
- [ ] 手动重建或同步 OpenSearch 后，`/api/v1/search` 正常。
- [ ] `/api/v1/search/vector` 仍保留。
- [ ] `/api/v1/rag/ask` 正常。
- [ ] `/api/v1/knowledge-items` 正常。
- [ ] 第八阶段没有写入 `retrieval_logs`。

### 9. 失败场景验收

- [ ] MinerU client 失败时，`documents.process_status=parse_failed`。
- [ ] normalizer 失败时，`parse_run.status=failed`。
- [ ] assets 写入失败时，`parse_run.status=failed`。
- [ ] blocks 写入失败时，`parse_run.status=failed`。
- [ ] chunking 失败时，`parse_run.status=failed`。
- [ ] chunks 写入失败时，`parse_run.status=failed`。
- [ ] chunk-block mapping 写入失败时，`parse_run.status=failed`。
- [ ] 失败场景 `parse_run.is_active=false`。
- [ ] 失败场景不会误标记为 parsed。
- [ ] `error_message` 不泄露 API key、完整原文、完整 MinerU JSON 或完整 prompt。

## 建议测试命令

以下命令仅供用户后续手动执行，本步骤不运行。

后端单元测试：

```powershell
cd backend
pytest
```

文档解析相关测试：

```powershell
cd backend
pytest tests/test_document_parse_models.py tests/test_mineru_client.py tests/test_mineru_normalizer.py tests/test_document_blocks_assets.py tests/test_block_chunker.py tests/test_document_parsing_mineru.py tests/test_documents_parse_api.py
```

前端 lint / build：

```powershell
cd frontend
npm run lint
npm run build
```

验收结论必须以用户实际执行结果为准。

## 第九阶段 Local/API LLM Provider 人工验收

本节是负责人在 M5 文档审查后执行的安全验收清单。自动化证据使用 fake client、OpenAI SDK 和 HTTPX MockTransport；本轮未调用真实 Ollama 或远程 API。Remote 步骤只有在负责人提供临时测试 key 并明确授权后执行。

### 1. 验收前保护性快照

- [ ] 确认工作区没有未说明的业务代码 diff。
- [ ] 记录 PostgreSQL `document_chunks` 总数、`embedded` 数量、非空 vector 数量、model 和 dimension。
- [ ] 按稳定 `chunk_id` 顺序计算现有 vector checksum，只读查询，不更新数据。
- [ ] 记录 `test1.pdf` 与 `test.pdf` 的 active parse run、blocks、assets、chunks、embedded 和 vectors 数量。
- [ ] 记录 OpenSearch alias 目标、document count、mapping checksum 和重复 `chunk_id` 数量。
- [ ] 不调用 embedding endpoint。
- [ ] 不调用 OpenSearch create/rebuild/sync endpoint。
- [ ] 不调用 document parse/reparse endpoint。

M5 自动快照基准：

```text
PostgreSQL chunks / embedded / vectors = 110 / 106 / 106
embedding model = Qwen3-Embedding-0.6B
embedding dimension = 1024
embedding SHA-256 = 37468b3772d0188d74512254342f672c53093171581fac59942c88c9cf27a9fb

OpenSearch alias = casting_chunks_current
OpenSearch target = casting_chunks_v1
OpenSearch documents = 105
duplicate chunk_id buckets = 0
mapping SHA-256 = ca3156f6515f3a5b864f997997da14bd855a24b9ce0ac631010c9a36afdd4e8a
```

该 embedding checksum 是 M5 前快照，不是 Phase 8 历史 checksum；Phase 8 文档没有记录全库向量哈希。它只能与本次验收后的同算法结果比较。

### 2. Local 模式配置与启动

在未跟踪的本机 `backend/.env` 中配置非秘密 Local 参数：

```env
LLM_PROVIDER=local
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=120
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
```

- [ ] Remote key 保持空值也能启动后端。
- [ ] 启动时只校验配置，不提前加载 LLM client。
- [ ] `/health` 与 `/api/v1/health` 正常。
- [ ] 后端响应或日志不显示 key、Authorization 或完整 prompt。
- [ ] 返回的 Provider metadata 为 `local`，model 为实际 `LLM_MODEL`。

### 3. Local 普通 RAG

- [ ] 使用有检索结果的问题调用 `POST /api/v1/rag/ask`。
- [ ] 返回 HTTP 200、`context_status=ok`、answer、citations、retrieval 和 `llm.provider/model`。
- [ ] citations 仍来自检索 chunks。
- [ ] 连续两次请求复用进程内 Provider/client；不为每次请求建立新的全局实例。
- [ ] 不触发 document embedding、index rebuild/sync 或 MinerU parse。

示例请求：

```powershell
$body = @{
  question = "冒口如何保证热节补缩？"
  limit = 8
  document_id = $null
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/rag/ask" `
  -ContentType "application/json" `
  -Body $body
```

### 4. Local knowledge extraction、JSON mode 与 think

- [ ] 对已有 chunk 调用 `POST /api/v1/knowledge-items/extract`。
- [ ] Local 请求使用 JSON mode。
- [ ] Ollama 最终 wire JSON 顶层包含 `"think": false`；SDK Python 调用参数仍使用 `extra_body={"think": false}`。
- [ ] 合法 JSON object 正常进入领域 parser。
- [ ] 抽取结果默认仍是 `draft`；`auto_submit=true` 才进入 `pending_review`，不直接批准。
- [ ] 日志不记录 chunk 正文、完整 extraction prompt 或模型完整响应。

### 5. Local no-context

- [ ] 使用确定无命中的问题，或在受控测试环境把检索 fake 为零结果。
- [ ] 返回 HTTP 200、`context_status=no_context`、`citations=[]`。
- [ ] `llm.provider=local`，model 为 active Local model。
- [ ] 不构造 Provider、transport 或 OpenAI client。
- [ ] 不产生 `llm_generation_completed/failed` 日志。

### 6. Generic API 默认 JSON capability=false

只有在负责人提供临时测试 key 并授权后，才把真实值写入本机 `backend/.env`：

```env
LLM_PROVIDER=api
LLM_REMOTE_BASE_URL=https://api.example.invalid/v1
LLM_REMOTE_API_KEY=
LLM_REMOTE_MODEL=example-chat-model
LLM_REMOTE_TIMEOUT_SECONDS=60
LLM_REMOTE_SUPPORTS_JSON_MODE=false
LLM_REMOTE_ALLOW_INSECURE_HTTP=false
```

- [ ] 修改配置后完整重启后端。
- [ ] 普通 RAG 成功，`llm.provider=api` 且 model 为 `LLM_REMOTE_MODEL`。
- [ ] 检索仍使用本地 embedding/OpenSearch；远程 Provider 只生成最终答案。
- [ ] knowledge extraction 返回 HTTP 400 / `LLM_PARAMETER_UNSUPPORTED`。
- [ ] capability detail 只包含 provider、parameter 和 required capability。
- [ ] capability 拒绝发生在远程调用前。
- [ ] API 不读取 Local URL/model/key，不 fallback 到 Local。

### 7. Generic API JSON capability=true

仅当供应商明确兼容 Chat Completions 的 `response_format={"type":"json_object"}` 时设置：

```env
LLM_REMOTE_SUPPORTS_JSON_MODE=true
```

- [ ] 重启后端后 knowledge extraction 成功。
- [ ] Remote wire payload 含 `response_format={"type":"json_object"}`。
- [ ] Remote wire payload不含 `think` 或 `extra_body`。
- [ ] Provider/model metadata 正确。
- [ ] 供应商若声称 JSON mode 但返回非 object，系统返回 `LLM_JSON_INVALID`，不静默修复。

### 8. API 错误、无 fallback 与脱敏

优先使用受控 mock/stub endpoint 验证错误，不要故意对付费生产服务制造失败：

- [ ] 401 → `LLM_AUTHENTICATION_FAILED` / HTTP 502；
- [ ] 403 → `LLM_PERMISSION_DENIED` / HTTP 502；
- [ ] 404 → `LLM_MODEL_NOT_FOUND` / HTTP 502；
- [ ] timeout → `LLM_TIMEOUT` / HTTP 504；
- [ ] connection → `LLM_UNAVAILABLE` / HTTP 503；
- [ ] 429 → `LLM_RATE_LIMITED` / HTTP 429，`retryable=true`；
- [ ] 5xx → `LLM_UPSTREAM_FAILED` / HTTP 502；
- [ ] 其他 4xx → `LLM_REQUEST_REJECTED` / HTTP 502；
- [ ] 任一错误都不自动 retry，不自动切回 Local；
- [ ] 429 不返回 Retry-After、headers 或 body；
- [ ] API response、日志、traceback 和浏览器中不出现 key、Authorization、完整 URL、model prompt、上游 body 或 headers。

### 9. URL 安全

- [ ] HTTPS remote URL 正常启动。
- [ ] `http://localhost`、`http://127.0.0.1`、`http://[::1]` 可用于受控本地兼容服务。
- [ ] 非 loopback HTTP 在默认配置下启动失败。
- [ ] 只有显式 `LLM_REMOTE_ALLOW_INSECURE_HTTP=true` 时才允许，并产生一次警告。
- [ ] 警告只含 scheme、hostname、provider 和安全事件名，不含完整 URL。
- [ ] userinfo、query、fragment 和非 HTTP(S) scheme 被拒绝。

### 10. Provider 切换与关闭生命周期

- [ ] Local 运行后停止后端，修改 `.env` 为 API，再重新启动。
- [ ] 切换只在重启后生效，没有热切换。
- [ ] shutdown 关闭已创建的 Provider/client；连续 clear/close 不报错且不会二次关闭旧 client。
- [ ] 切换前后的 REST schema 没有新增 messages/history。
- [ ] API 失败时没有 fallback 到 Local。

### 11. 验收后保护性复核

- [ ] 用与第 1 节相同的只读算法重新计算 embedding checksum，必须完全相同。
- [ ] PostgreSQL embedding count/model/dimension 不变。
- [ ] OpenSearch alias、target、count 和 mapping checksum 不变。
- [ ] `test1.pdf` 与 `test.pdf` parse run、blocks、assets、chunks 状态不变。
- [ ] 没有执行重新 embedding、OpenSearch rebuild/sync 或 MinerU reparse。
- [ ] `git status --short` 不出现真实 `.env`、key 或非预期业务文件。

### 12. 人工验收结论边界

未提供远程 key或未实际启动 Local/Remote 服务时，只能记录“自动化与文档通过，真实端到端人工验收待授权”，不得把 MockTransport 结果写成真实供应商验收通过。

Phase 9 内部已经支持文本多轮和 assistant 历史的 Provider wire contract，但当前 REST API 不接收 history，也没有会话持久化、多轮 RAG、LangChain、LangGraph、Agent、工具执行、多模态转发或 streaming。人工验收不得把这些预留边界写成已实现。
