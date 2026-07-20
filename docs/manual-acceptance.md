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
