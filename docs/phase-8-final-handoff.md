# 第八阶段最终交接摘要：MinerU API 文档解析增强与 OpenSearch 索引同步

> 新 Codex 对话请先阅读本摘要，并以当前仓库代码、Git 状态和运行环境的重新核验结果为最终依据。第八阶段已经由项目负责人验收；下一对话不得重新设计第八阶段，也不得直接开始第九阶段实现。

## 1. 项目目标和当前阶段

项目路径：`D:\rag_system`

项目是面向铸型工艺知识管理场景的本地 RAG 系统，核心基础设施和业务链路包括 FastAPI、Next.js、PostgreSQL/pgvector、MinIO、Redis、OpenSearch、IK 中文分词、本地 Qwen3 embedding、OpenAI-compatible LLM、RAG 问答和知识条目专家审核。

已完成并验收：

1. FastAPI、Next.js、PostgreSQL、MinIO、Redis 基础骨架；
2. 文档上传、基础解析和切片；
3. 本地 embedding 与 pgvector；
4. OpenSearch BM25/kNN 混合检索；
5. 基于混合检索的单轮 RAG；
6. `knowledge_items` 自动抽取与专家审核闭环；
7. Phase 8：以 MinerU API 为正式主解析器的文档解析入库层、解析中间层、block-aware chunking、解析结果查询 UI，以及显式 OpenSearch 索引同步补充修复。

当前停点：Phase 8 已验收，等待在新对话中先调查和规划 Phase 9。不得直接实现 Phase 9。

## 2. 当前最新 Git 状态

- Branch：`main`
- HEAD：`58d11b625d70b02899dadd2f66c5f4b9700ead19`
- HEAD message：`phase 8: add document parsing with mineru parser`
- `origin/main` 与当前 HEAD 一致。
- 本摘要创建前：staged、tracked modifications、untracked 均为空。
- 本摘要创建后：仅 `docs/phase-8-final-handoff.md` 是新建未跟踪文件；未 commit、push 或 amend。
- `git diff --check` 在摘要创建前通过；创建后需再次核验。
- Phase 8 commit 共修改/新增 49 个文件，范围集中在文档解析、MinerU adapter、解析模型/API/UI、测试/文档，以及已验收的 OpenSearch 显式同步补充修复。
- Phase 8 commit 未修改 `/api/v1/rag/ask`、`/api/v1/knowledge-items`、`hybrid_search_chunks()`、embedding service 或 RAG/knowledge-items 主逻辑。

## 3. Phase 8 最终实现范围

已实现：

- `DOCUMENT_PARSER_PROVIDER=mineru_api` 的正式主解析路径；
- MinerU 官方 V4 本地文件精准解析协议；
- MinerU ZIP 安全读取和稳定版 `content_list.json` 解析；
- MinerU 输出标准化为 `document_blocks` / `document_assets`；
- block-aware chunking 和 `document_chunk_blocks` 溯源映射；
- 表格、公式原子保护；
- 原始解析产物和图片资产写入 MinIO；
- parse run 完整成功/失败状态流转；
- parse-runs、parse-status、blocks、assets 查询 API；
- 文档详情页的轻量解析结果展示；
- embedding 与 OpenSearch 索引同步保持两个显式操作；
- 文档级 OpenSearch 幂等同步入口和 UI；
- eligible chunk 过滤、稳定 `_id=chunk_id`、bulk 部分失败识别；
- embedding 生成与索引同步按钮互斥，新增 embedding 后提示重新同步索引。

未实现或明确不做：

- 正式 reparse API；
- 解析完成后自动 embedding；
- embedding 完成后自动 OpenSearch 同步；
- 解析完成后自动 RAG 或 knowledge item 抽取；
- `document_blocks` 直接检索；
- `document_assets` 直接进入 RAG；
- `content_list_v2.json` 转换；
- Neo4j、LangGraph、正式 reranker、多模态问答；
- 生产级认证、异步任务队列和动态 provider 切换。

## 4. 关键架构与数据流

```text
上传文件
-> MinIO 原始文件
-> documents
-> document_parse_runs
-> MinerU V4 / basic parser
-> document_blocks + document_assets
-> block-aware chunking
-> document_chunks + document_chunk_blocks
-> 用户显式生成 embedding
-> PostgreSQL document_chunks.embedding
-> 用户显式同步 OpenSearch
-> casting_chunks_current
-> /api/v1/search
-> /api/v1/rag/ask
-> /api/v1/knowledge-items/extract
```

必须保持的边界：

- 文档解析不等于 embedding；
- embedding 不等于 OpenSearch 索引同步；
- MinerU `done` 只表示远端任务完成，不表示系统入库完成；
- `parse_run.status=succeeded` 和 `is_active=true` 只有在产物保存、assets、blocks、chunks、chunk-block mappings 全部成功后才能设置；
- `document_blocks` 是解析中间层，不是最终检索单元；
- `document_chunks` 是 Search、RAG 和 Knowledge Items 的共同基础；
- MinerU 结果不得直接变成 approved knowledge item。

## 5. 新增和修改的主要文件

解析与 MinerU：

- `backend/app/ingestion/mineru/client.py`
- `backend/app/ingestion/mineru/v4_transport.py`
- `backend/app/ingestion/mineru/archive_reader.py`
- `backend/app/ingestion/mineru/models.py`
- `backend/app/ingestion/mineru/normalizer.py`
- `backend/app/ingestion/block_chunker.py`
- `backend/app/services/document_parsing.py`

解析中间层模型和 service：

- `backend/app/models/document_parse_run.py`
- `backend/app/models/document_block.py`
- `backend/app/models/document_asset.py`
- `backend/app/models/document_chunk_block.py`
- `backend/app/services/document_parse_runs.py`
- `backend/app/services/document_blocks.py`
- `backend/app/services/document_assets.py`

API、schema 和索引同步：

- `backend/app/api/v1/documents.py`
- `backend/app/schemas/document.py`
- `backend/app/services/search_index.py`
- `backend/tests/test_search_index.py`

前端：

- `frontend/app/documents/[id]/page.tsx`
- `frontend/components/DocumentParseResults.tsx`
- `frontend/components/DocumentEmbeddingPanel.tsx`
- `frontend/lib/documents.ts`
- `frontend/lib/search.ts`

专项测试和文档：

- `backend/tests/test_mineru_*.py`
- `backend/tests/test_document_parse_models.py`
- `backend/tests/test_document_parsing_mineru.py`
- `backend/tests/test_documents_parse_api.py`
- `backend/tests/test_block_chunker.py`
- `docs/phase-8-mineru-parser.md`
- `docs/manual-acceptance.md`
- `docs/test-data-reset.md`

## 6. 数据库 migration 和模型

Migration：`backend/alembic/versions/0006_add_document_parse_intermediate_tables.py`

- revision：`0006_add_document_parse`
- down revision：`0005_knowledge_items`
- Alembic head：`0006_add_document_parse`
- 当前数据库 migration：`0006_add_document_parse (head)`
- `alembic check`：`No new upgrade operations detected.`

新增表：

- `document_parse_runs`：一次解析任务；`is_active` 默认 false，完整成功后才 true；任务级摘要放入 nullable JSONB `source_metadata`。
- `document_blocks`：标准化结构块；稳定顺序由 `unique(parse_run_id, block_index)` 保证；`parent_block_key` 是弱关联。
- `document_assets`：MinIO 资产元数据；`unique(parse_run_id, asset_key)`；`source_block_key` 是弱关联。
- `document_chunk_blocks`：chunk/block 映射；`unique(chunk_id, block_order)`，按 block order 回溯。

扩展 `document_chunks`：

- `parse_run_id` nullable，外键 `RESTRICT`；
- `chunk_method` nullable；
- `content_format` nullable；
- 保留既有 `id`、`content`、`source_metadata`、embedding 字段和 `knowledge_item_chunks.chunk_id` 依赖。

所有新外键采用 `RESTRICT`，没有危险 cascade delete。Migration 只建表、加字段/索引/约束，不清理数据。

## 7. 关键 API

文档和解析：

- `POST /api/v1/documents`
- `GET /api/v1/documents`
- `GET /api/v1/documents/{document_id}`
- `POST /api/v1/documents/{document_id}/parse`
- `GET /api/v1/documents/{document_id}/parse-runs`
- `GET /api/v1/documents/{document_id}/parse-status`
- `GET /api/v1/documents/{document_id}/blocks?parse_run_id=&block_type=&limit=&offset=`
- `GET /api/v1/documents/{document_id}/assets?parse_run_id=&asset_type=&limit=&offset=`
- `GET /api/v1/documents/{document_id}/chunks`
- `POST /api/v1/documents/{document_id}/embeddings`
- `GET /api/v1/documents/{document_id}/embedding-status`

搜索索引和检索：

- `POST /api/v1/search/index/create`
- `POST /api/v1/search/index/rebuild`，支持 `scope=document`；
- `GET /api/v1/search/index/status`
- `POST /api/v1/search`
- `POST /api/v1/search/vector`

保持可用且未重构：

- `POST /api/v1/rag/ask`
- `/api/v1/knowledge-items` 全套 CRUD、extract、review、revise API。

当前没有正式 reparse API；已有 chunks 的文档默认仍返回 `DOCUMENT_ALREADY_PARSED`。

## 8. 关键 `.env` 配置

正式 Phase 8 配置示例：

```dotenv
DOCUMENT_PARSER_PROVIDER=mineru_api
MINERU_API_BASE_URL=https://mineru.net
MINERU_API_KEY=
MINERU_API_TIMEOUT_SECONDS=120
MINERU_API_POLL_INTERVAL_SECONDS=5
MINERU_API_MAX_POLL_ATTEMPTS=120
MINERU_PARSE_MODE=vlm
MINERU_ENABLE_OCR=true
MINERU_OUTPUT_PREFIX=parsed-assets

EMBEDDING_MODEL=Qwen3-Embedding-0.6B
EMBEDDING_DIM=1024
SEARCH_INDEX_NAME=casting_chunks_v1
SEARCH_INDEX_ALIAS=casting_chunks_current
```

规则：

- 真实 MinerU Token 只放本地、被 Git 忽略的 `.env`；
- `.env.example` 和 `backend/.env.example` 的 `MINERU_API_KEY` 保持空值；
- provider 为 `mineru_api` 且配置缺失时明确报错，不静默 fallback；
- `basic` 只用于显式 fallback、简单文本开发路径和测试；
- 旧 `DOCUMENT_PARSER`、`MINERU_ENDPOINT`、`MINERU_TIMEOUT_SECONDS` 不参与 V4 正式路径。

## 9. MinerU V4 正式协议

内部稳定接口：

```text
MinerUClient.parse_file(MinerUParseRequest) -> MinerUParseResult
```

外部协议完全封装在 ingestion 层：

1. `POST /api/v4/file-urls/batch`；
2. JSON 请求中使用 `files[].name`、`files[].is_ocr` 和顶层 `model_version`；
3. 从 `data.batch_id`、`data.file_urls` 取得任务和签名上传地址；
4. 对签名 URL 执行无 Bearer、无主动 Content-Type 的原始 bytes `PUT`；
5. `GET /api/v4/extract-results/batch/{batch_id}` 轮询；
6. 处理 `waiting-file`、`pending`、`running`、`converting`、`done`、`failed`；
7. `done` 后下载 `full_zip_url`；
8. 安全读取 ZIP，提取 `full.md`、稳定版 `*_content_list.json`、可选中间文件和 `images/` 资产；
9. 转成稳定 `MinerUParseResult`，官方响应结构不泄漏到上层。

`auto` 映射为 `vlm`；支持 `vlm`、`pipeline`、`MinerU-HTML`；未知值报配置错误。`save_intermediate` 仅是系统内部保存策略，不发给 MinerU。

ZIP 防护包括下载大小、成员数量、单成员大小、总解压大小、单资产大小、压缩比、路径穿越、绝对路径、Windows drive、空字节、符号链接、加密成员和不支持压缩算法拒绝。只支持稳定 `content_list.json`；v2-only ZIP 明确失败，不把开发版结构直接交给 normalizer。

## 10. OpenSearch、mapping 与 embedding

当前运行态：

- 物理索引：`casting_chunks_v1`
- alias：`casting_chunks_current -> casting_chunks_v1`
- health：yellow；单节点下 1 primary + 1 replica 的副本无法分配，不是 Phase 8 故障。
- 当前 docs.count：105
- unique `chunk_id`：105
- duplicate `chunk_id` buckets：0

关键 mapping：

- `document_id` / `chunk_id`：keyword；
- `chunk_index`：integer；
- `content`：text，index analyzer `ik_max_word`，search analyzer `ik_smart`；
- `embedding`：`knn_vector`，dimension 1024，`cosinesimil`；
- OpenSearch document `_id`：稳定 `chunk_id`；
- `source_metadata`：object，`enabled=false`。

Embedding：

- 只使用本地 `Qwen3-Embedding-0.6B`；
- dimension 1024；
- 向量保存在 PostgreSQL `document_chunks.embedding`；
- eligible 索引条件要求 `embedding_status=embedded`、embedding 非空、model/dimension 与当前配置一致；
- OpenSearch 失败不回退或删除 PostgreSQL embedding；
- 文档级同步先删除该 document 的旧索引记录再 bulk upsert，重复同步保持稳定 ID 和数量；该操作不是原子替换。

## 11. 自动化测试命令与实际结果

Phase 8 聚焦测试：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_document_parse_models.py tests/test_mineru_client.py tests/test_mineru_v4_transport.py tests/test_mineru_archive_reader.py tests/test_mineru_normalizer.py tests/test_document_blocks_assets.py tests/test_block_chunker.py tests/test_document_chunk_blocks.py tests/test_document_parsing.py tests/test_document_parsing_mineru.py tests/test_documents_parse_api.py tests/test_search_index.py -q -p no:cacheprovider --basetemp=.pytest_cache\phase8-final-focused
```

结果：`177 passed, 2 warnings`。

后端全量：

```powershell
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase8-final-pytest-tmp
```

结果：`411 passed, 1 failed, 2 warnings`。唯一失败：

- `tests/test_llm_provider.py::test_generate_passes_optional_json_mode_and_think_parameters`
- 原因：fake completion call 中没有 `think`，断言触发 `KeyError: 'think'`。
- 该测试和实现属于既有 LLM provider/Phase 7 范围，不在 Phase 8 commit 中，因此不阻断已完成的 Phase 8 验收。

依赖检查：

```powershell
.\.venv\Scripts\python.exe -m pip check
```

结果：`No broken requirements found.`

Phase 8 前端 ESLint：

```powershell
cd D:\rag_system\frontend
npx.cmd eslint "app/documents/[id]/page.tsx" components/DocumentEmbeddingPanel.tsx components/DocumentParseResults.tsx lib/documents.ts lib/search.ts
```

结果：1 个错误，`DocumentParseResults.tsx:254` 的 `react-hooks/set-state-in-effect`。这是已知前端规则问题，不影响真实解析/查询验收，但应后续单独修复。

Frontend build：

```powershell
npm.cmd run build
```

结果：Next.js production compile 成功，TypeScript 被既有 `components/KnowledgeItemsPanel.tsx:270` 泛型联合类型错误阻断。该文件不在 Phase 8 commit 中，因此不属于 Phase 8 引入。

Alembic：

```powershell
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe check
```

结果：heads/current 均为 `0006_add_document_parse`，check 无待生成 upgrade 操作。

## 12. 真实人工验收结果

PDF `test1.pdf`：

- document_id：`a540a819-d0fc-48f0-ac58-37a8bb3a8235`
- active parse_run：`5f574d15-af67-469e-a761-0bb9648a557a`
- MinerU VLM succeeded、active=true；4 pages、88 blocks、15 assets；output markdown/json 均 saved；
- PostgreSQL：25 chunks、25 embedded、25 vectors；
- OpenSearch：该 document_id 25 条。

PDF `test.pdf`：

- document_id：`6685fd7f-db71-407f-a47f-d852cfcdea90`
- active parse_run：`bc0833e6-78ff-40c6-a5a7-da297b843786`
- MinerU VLM succeeded、active=true；15 pages、183 blocks、17 assets；output markdown/json 均 saved；
- PostgreSQL：78 chunks、78 embedded、78 vectors；
- OpenSearch：该 document_id 78 条。

两文档 embedding model 均为 `Qwen3-Embedding-0.6B`，dimension 均为 1024。OpenSearch 当前总计 105 条，其中两个早期测试 chunks 继续保留；105 个 `chunk_id` 全部唯一。

当前重新调用 `/api/v1/search`，限定 `test1.pdf` document_id 后可通过向量检索命中该 PDF；返回正确 document_id/chunk_id/chunk_index，embedding model/dimension 正确。异常中文字间空格仍导致该查询的 keyword score 可能为 null，matched keywords 为空。

MinIO 解析产物路径遵循：

```text
parsed-assets/{document_id}/{parse_run_id}/output.md
parsed-assets/{document_id}/{parse_run_id}/output.json
parsed-assets/{document_id}/{parse_run_id}/...
```

## 13. 当前遗留问题

- MinerU 中文字间异常空格和 `<sup>` 等简单 HTML 标签影响 IK 关键词检索；向量检索可用。
- 没有正式 reparse API；已有 chunks 默认阻止重复解析。
- PostgreSQL 与 MinIO 不是同一事务；失败可能留下 orphan `parsed-assets`，尚无自动清理任务。
- document-scope OpenSearch 同步采用 delete-then-upsert，不是原子替换；bulk 失败可见且不破坏 embedding，但短暂缺口仍可能存在。
- 没有持久化独立 indexing 状态；前端只展示显式操作结果。
- MinerU 轮询和文档解析目前是同步请求编排，生产环境需要异步任务、重试、限流和观测能力。
- OpenSearch 单节点 yellow 是 replica 配置现象。
- Phase 8 前端 ESLint 有 1 个 effect/setState 规则错误。
- 全量 build 被既有 Knowledge Items TypeScript 错误阻断。
- 全量后端有 1 个既有 LLM provider `think` 转发测试失败。
- 当前系统没有生产级认证、权限、密钥轮换和部署加固。

## 14. 新对话不得破坏的 Phase 8 边界

禁止重新设计或破坏：

- `documents -> parse_runs -> blocks/assets -> chunks -> mappings` 数据流；
- MinerU 官方 V4 batch upload / signed PUT / poll / ZIP 协议封装；
- `MinerUClient.parse_file() -> MinerUParseResult` 稳定内部接口；
- ZIP 安全限制和敏感异常链隔离；
- `document_chunks` 作为最终检索单元；
- formula/table 原子保护；
- `knowledge_item_chunks.chunk_id/source_text` 溯源；
- `/api/v1/search`、`/api/v1/search/vector`、`hybrid_search_chunks()`、weighted RRF；
- `/api/v1/rag/ask`、RAG prompt/context/citations；
- `/api/v1/knowledge-items` 和审核状态机；
- embedding 与 OpenSearch 的显式操作边界；
- 1024 维 OpenSearch 向量空间和稳定 `_id=chunk_id`；
- parse success 不自动触发 embedding、OpenSearch、RAG 或 knowledge items；
- retrieval_logs 当前不写入的边界。

继续禁止：删除 PostgreSQL/MinIO/OpenSearch volume 或 bucket/index、`docker compose down -v`、无保护 DROP/TRUNCATE/DELETE、自动清理正式数据、接入 Neo4j/LangGraph/reranker、让未审核知识参与权威回答、在日志或 Git 中写入密钥。

## 15. Phase 9 初步范围（仅用于下一对话调查和规划）

Phase 9 初步目标：

- embedding 继续只使用本地模型；
- 保持 `Qwen3-Embedding-0.6B`；
- 保持 embedding dimension 1024；
- 不增加 API embedding；
- 不重新向量化已有数据；
- 不修改当前 OpenSearch 向量空间；
- LLM 支持 `local` 和 `api` 两种 provider；
- 通过 `.env` 选择当前 LLM provider；
- 第一版 API LLM 优先支持 OpenAI-compatible 协议；
- 修改 `.env` 后通过重启后端生效；
- 不实现运行时动态切换；
- API Key 只存在于本地 `.env`；
- `.env.example` 只能使用空占位符；
- 新阶段必须先只读调查现有 LLM 调用链，再形成设计和实施计划；
- 当前对话不实施 Phase 9。

新对话建议第一步只读检查：

- `backend/app/llm/provider.py`
- `backend/app/llm/openai_compatible.py`
- `backend/app/services/rag.py`
- `backend/app/services/knowledge_extraction.py`
- `backend/app/core/config.py`
- `backend/app/api/v1/rag.py`
- `backend/tests/test_llm_provider.py`
- `backend/tests/test_rag*`
- `backend/tests/test_knowledge_extraction.py`
- LLM 相关 `.env.example` 和文档。

调查时必须先解释当前 `local` 实际是否已经通过 OpenAI-compatible Ollama adapter 工作、`think`/JSON mode 的请求级行为、RAG 与 knowledge extraction 如何共享 provider，以及真实 API provider 与现有实现之间还缺什么。完成调查和计划后等待项目负责人批准，不能直接写代码。

## 最终交接状态

`READY_FOR_PHASE_9_HANDOFF`
