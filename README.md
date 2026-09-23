# 铸型工艺知识库 RAG 管理系统

## 当前 ingestion 边界（2026-09-09，Phase 11 P0）

Basic Parser 已退出。生产解析 provider 仅接受 `DOCUMENT_PARSER_PROVIDER=mineru_api`；其他值在配置加载时拒绝。
最终 ingestion 仅有 MinerU 与 Markdown Native。Markdown Native 尚未实现：`.md` 可上传，但 parse 返回
`DOCUMENT_PARSER_UNAVAILABLE` / HTTP 503，且不会读写解析产物或调用 MinerU。

当前上传允许 `.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.webp,.md`。
`.txt/.csv` 已停止支持；`.tif/.tiff` 未获当前官方 V4 上传契约支持，也明确拒绝。
上传拒绝使用 `INVALID_FILE_TYPE` / HTTP 415；已存 unsupported 文件的 parse 同样拒绝，既有文档与 chunks 不清理。
部署的 `UPLOAD_ALLOWED_EXTENSIONS` 只能收窄上述范围，旧 `.env` 的额外扩展名不会重新启用已退出格式。

详见 [Basic Retirement 审计与验收](docs/phase-11-basic-parser-retirement.md)。
下文按阶段保存历史记录；第三阶段的 SimpleParser、字符切块与旧配置不再代表当前能力。


本项目是“铸型工艺知识库大型 RAG 系统”。第一阶段目标是建立可持续扩展的本地开发骨架，先把后端、前端、基础服务、迁移和文档边界搭清楚，不实现具体 RAG 业务功能。

本文中的命令只供用户手动执行。Codex 不自动执行 Docker、Git、npm、pip、alembic、pytest、uvicorn、next 等命令，也不启动长期运行服务。

## 第一阶段范围

第一阶段包含：

- FastAPI 后端骨架，本机运行。
- Next.js 前端骨架，本机运行。
- PostgreSQL + pgvector、Redis、MinIO 基础服务，运行在 Docker 容器中。
- PostgreSQL 初始数据表设计、SQLAlchemy ORM 模型和 Alembic 迁移基础。
- 根目录、后端、前端环境变量示例文件。
- 基础健康检查接口和本地验证说明。


## 本地开发架构

- FastAPI 后端在 Windows 本机运行，不放入 Docker Compose。
- Next.js 前端在 Windows 本机运行，不放入 Docker Compose。
- PostgreSQL + pgvector、Redis、MinIO 运行在 Docker 容器中。
- FastAPI 本机访问 PostgreSQL 使用 `localhost:5432`。
- 未来后端容器化后，容器内部才使用 `postgres:5432`。
- Docker Compose 文件路径固定为 `infra/docker-compose.yml`。
- Compose 项目名固定为 `rag_system`。

默认基础服务配置：

- `POSTGRES_DB=rag_system`
- `POSTGRES_USER=rag_user`
- `POSTGRES_PASSWORD=rag_password`
- `DATABASE_URL=postgresql+psycopg://rag_user:rag_password@localhost:5432/rag_system`
- `REDIS_PASSWORD=rag_redis_password`
- `MINIO_ROOT_USER=rag_minio`
- `MINIO_ROOT_PASSWORD=rag_minio_password`
- `MINIO_BUCKET=rag-documents`

## 目录结构

```text
D:\rag_system
├─ .env.example
├─ README.md
├─ backend/
│  ├─ app/
│  │  ├─ api/
│  │  ├─ core/
│  │  ├─ db/
│  │  ├─ models/
│  │  ├─ schemas/
│  │  ├─ services/
│  │  ├─ rag/
│  │  ├─ ingestion/
│  │  ├─ retrieval/
│  │  ├─ extraction/
│  │  └─ tasks/
│  ├─ alembic/
│  ├─ tests/
│  ├─ .env.example
│  └─ pyproject.toml
├─ frontend/
│  ├─ app/
│  ├─ components/
│  ├─ lib/
│  ├─ public/
│  ├─ .env.local.example
│  └─ package.json
├─ infra/
│  ├─ docker-compose.yml
│  ├─ postgres/
│  └─ minio/
└─ docs/
```

## 环境变量文件

真实环境变量文件不得提交 Git：

- `.env`
- `backend/.env`
- `frontend/.env.local`

示例文件应保留并提交：

- `.env.example`
- `backend/.env.example`
- `frontend/.env.local.example`

前端示例文件只允许包含公开变量，例如：

```text
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

前端不得包含数据库、Redis、MinIO 的账号、密码、连接串或 Secret。

## Docker 基础服务启动

用户在项目根目录手动执行：

```powershell
copy .env.example .env
docker compose --env-file .env -f infra/docker-compose.yml config
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
docker compose --env-file .env -f infra/docker-compose.yml logs postgres
docker compose --env-file .env -f infra/docker-compose.yml logs redis
docker compose --env-file .env -f infra/docker-compose.yml logs minio
```

第一阶段 Docker Compose 只包含 PostgreSQL + pgvector、Redis、MinIO。

## MinIO bucket 手动创建

- MinIO Console 地址：`http://localhost:9001`
- 默认本地开发账号来自根目录 `.env`。
- 默认 bucket：`rag-documents`
- 不得自动清空或删除 bucket。
- `/api/v1/health/services` 中 bucket 不存在应显示 warning，而不是 failed。

## 后端环境准备

用户手动执行：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

后端采用 Python 3.11+、FastAPI、同步 SQLAlchemy 2.x、Alembic 和 Pydantic Settings。

## 数据库迁移

用户确认连接的是本地开发库 `rag_system` 后，在 `backend` 目录中手动执行：

```powershell
alembic upgrade head
```

## 后端启动

用户在 `backend` 目录中手动执行：

```powershell
pytest
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`uvicorn` 是前台长期运行命令。验证完成后使用 Ctrl+C 停止。Codex 不应长期阻塞执行 `uvicorn`。

## 前端启动

当前 `frontend` 已由用户手动 `create-next-app` 创建，后续不应随意重建。

如需重新创建前端脚手架：

- 执行 `create-next-app` 前必须确认 `D:\rag_system\frontend` 不存在或为空。
- 如果 `frontend` 已存在且非空，不得删除或覆盖。
- 需要由用户确认是备份、换目录、清理还是在现有目录上合并。

用户手动执行：

```powershell
cd frontend
copy .env.local.example .env.local
npm install
npm run dev
```

`npm run dev` 是前台长期运行命令。验证完成后使用 Ctrl+C 停止。Codex 不应长期阻塞执行 `npm run dev`。

## 健康检查接口

用户在后端启动后手动验证：

```powershell
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/health/services
```

健康检查语义：

- `/health` 只检查 FastAPI 应用进程。
- `/api/v1/health` 只检查 API v1 应用状态。
- `/api/v1/health/services` 检查 PostgreSQL、Redis、MinIO。
- PostgreSQL、Redis、MinIO 不可用不应影响 FastAPI 启动。
- MinIO 服务不可达为 failed。
- MinIO bucket 不存在为 warning。

## 禁止操作

在没有用户单独、明确授权且没有备份的情况下，禁止：

- 删除 volume。
- 清空 MinIO bucket。
- 删除 MinIO bucket。
- 删除或覆盖 `frontend` 脚手架。
- 未确认情况下执行破坏性数据库操作。
- 丢弃本地改动。
- 重建或清空 PostgreSQL、Redis、MinIO 的持久化数据。

如确实需要重置环境，必须先说明影响范围、备份方式和恢复路径，并等待用户明确确认。

## 已知开发提示

- 如果 `npm audit` 提示 Next.js 间接依赖 `postcss` 存在 moderate 漏洞，第一阶段不执行 `npm audit fix --force`。
- 不强制修复的原因是避免破坏 Next.js App Router、Tailwind CSS 或脚手架依赖结构。
- 后续等待 Next.js 官方依赖更新后再统一处理。
- Docker、Git、npm、pip、alembic、pytest、uvicorn 等命令可以写入文档供用户手动执行，但 Codex 不自动执行。
# 第二阶段补充：文档上传与基础知识库入库闭环

第二阶段在第一阶段本地开发骨架基础上，新增“文档上传与基础知识库入库闭环”。本阶段只实现原始文件上传、MinIO 存储、`documents` 表元数据入库、文档列表和文档详情。

## 第二阶段功能范围

第二阶段已补充：

- 文档上传：前端通过 `/documents` 页面选择单个文件上传。
- 对象存储：后端将原始文件保存到 MinIO `rag-documents` bucket。
- 元数据入库：后端在 MinIO 上传成功后写入 `documents` 表。
- 文档列表：前端 `/documents` 展示文件名、类型、大小、处理状态和上传时间。
- 文档详情：前端 `/documents/[id]` 展示文件基础元数据。
- 后端上传接口：`POST /api/v1/documents`。
- 后端列表接口：`GET /api/v1/documents`。
- 后端详情接口：`GET /api/v1/documents/{document_id}`。

# 第三阶段补充：文档解析适配与基础切片可视化闭环

第三阶段在第二阶段文档上传闭环基础上，新增“文档解析适配与基础切片可视化闭环”。本阶段从已上传到 MinIO 的原始文件出发，打通同步解析、基础字符切块、`document_chunks` 入库和前端轻量查看 chunk 效果。

## 第三阶段历史能力（已被后续阶段替代）

第三阶段已补充：

- MinIO 原始文件只读读取能力。
- Parser 适配层：统一 `Parser` Protocol 和 `ParsedDocument` 输出结构。
- `SimpleParser`：支持 `.txt`、`.md`、`.csv` 轻量文本解析，优先 UTF-8 解码，失败时使用 `errors="replace"` 兜底。
- 复杂格式占位解析：PDF、Word、Excel、图片暂时生成明确占位解析结果，后续由 MinerU 替换。
- `MinerUParser` 预留边界：保留 endpoint、timeout 和统一接口，但第三阶段不强制接入真实 MinerU 服务。
- 基础字符 chunker：默认 `CHUNK_SIZE_CHARS=1000`，`CHUNK_OVERLAP_CHARS=100`。
- `document_chunks.source_metadata` JSONB 来源元数据字段迁移文件。
- 同步解析接口：`POST /api/v1/documents/{document_id}/parse`。
- chunk 列表接口：`GET /api/v1/documents/{document_id}/chunks?limit=50&offset=0`。
- 前端文档详情页展示解析按钮、chunk 总数、长度统计、来源元数据、内容预览和展开查看。
- 上传白名单和前端 `accept` 已包含 `.txt`、`.md`、`.csv`，用于第三阶段真实文本切块验收。

第三阶段数据流：

```text
MinIO 原始文件 bytes
-> Parser
-> ParsedDocument
-> chunker
-> document_chunks
-> documents.process_status
-> 前端 chunk 可视化
```

## 第三阶段 PostgreSQL MCP 边界

PostgreSQL MCP 仅允许只读核验，例如：

- 查询 `alembic_version`。
- 查询 `information_schema.columns`。
- 查询 `documents.process_status`。
- 统计某文档 chunks 数量。
- 查看 `chunk_index`、`chunk_type`、`embedding_status`、`source_metadata`。

禁止使用 PostgreSQL MCP 执行迁移、改表、补数据、删除数据、清空表或修改数据库结构。禁止通过 MCP 执行 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`TRUNCATE`、`ALTER`、`CREATE`。

# 第四阶段补充：Embedding 生成与基础向量检索闭环

第四阶段在第三阶段 `document_chunks` 入库基础上，补充本地 embedding 生成、pgvector 存储、基础向量检索和前端轻量检索验证能力。详细说明见 `docs/phase-4-embedding-vector-search.md`。

> 当前检索入口（2026-09-23 更新）：旧纯向量接口 `POST /api/v1/search/vector` 及其专用实现已移除，请使用 `POST /api/v1/search`。旧地址返回 404；客户端迁移时应适配 Hybrid 的 `hybrid_score`、`keyword_score`、`vector_score`，不能沿用旧 `distance` / `score` 语义。Hybrid 的 OpenSearch 关键词召回、向量召回和 RRF，以及 RAG 中可选 BGE reranker、context、citations、graph 均保持原行为。PostgreSQL embedding 存储及索引同步继续保留，无需因本次退役执行迁移或重建索引。
>
> 下文按阶段记录演进。`docs/superpowers/` 历史计划与设计、已完成验收结果及 `final-run-lock.json` 中的旧接口、旧测试和文件哈希引用保留为历史证据，不代表当前接口或测试命令。

第四阶段引入且继续保留的 embedding 能力：

- 使用本地 `Qwen3-Embedding-0.6B`，模型路径为 `D:/rag_system/models/Qwen3-Embedding-0.6B`。
- 通过 `.env` 配置 `EMBEDDING_PROVIDER=local_qwen3`、`EMBEDDING_DIM=1024`、`EMBEDDING_LOCAL_FILES_ONLY=true`。
- `document_chunks.embedding` 使用 `vector(1024)` 保存真实 embedding。
- `document_chunks.embedding_error_message` 记录 embedding 失败原因。
- `document_chunks.embedding_updated_at` 记录 embedding 生成或更新时间。
- 提供单文档 embedding 生成接口：`POST /api/v1/documents/{document_id}/embeddings`。
- 提供 embedding 状态接口：`GET /api/v1/documents/{document_id}/embedding-status`。
- 前端文档详情页展示 embedding 状态并提供生成按钮。
- 前端 `/search` 页面当前调用 Hybrid Search，展示来源文档、chunk 序号、内容、source_metadata 和混合检索分数。

第四阶段当时是本地开发闭环，尚未实现 RAG 问答、大语言模型回答、reranker、混合检索、关键词检索、图谱检索、知识条目自动抽取、专家审核、Celery 队列、多模型调度或生产级模型服务部署。

第四阶段不写 `retrieval_logs`，原因是当前 `retrieval_logs.session_id` 非空，尚未设计 search session；后续如需记录检索日志，应先设计 search session 或通过 Alembic 改造约束。第四阶段也不创建 HNSW / IVFFlat 或其他向量索引；当时使用的 pgvector 精确 cosine scan 查询已退役，embedding 存储仍保留。

## 第五阶段：OpenSearch + IK 统一混合检索索引闭环

第五阶段已在第四阶段真实 embedding 与 pgvector 基础检索能力之上，引入 OpenSearch 3.6.0 + analysis-ik 3.6.0 作为派生检索索引，并提供统一智能检索接口。

当前新增能力包括：

- OpenSearch + IK 中文分词检索索引规划与本地 Docker 配置。
- IK 自定义词典，用于铸型工艺术语、缺陷名称、材料牌号、标准编号和工艺参数。
- chunk 级搜索索引 mapping，包含 `content`、`exact_terms`、`source_metadata`、`embedding` 等字段。
- 搜索索引创建、重建和状态查询 API。
- 统一混合检索接口 `POST /api/v1/search`。
- 内部执行 BM25 关键词召回、kNN 向量召回和 weighted RRF 融合。
- 前端 `/search` 页面已改为“智能检索”，调用统一混合检索接口。

第五阶段新增或保留的搜索接口：

- `POST /api/v1/search`：统一混合检索接口。
- `POST /api/v1/search/index/create`：创建 OpenSearch index 和 alias。
- `POST /api/v1/search/index/rebuild`：同步 PostgreSQL 中可检索 chunks 到 OpenSearch。
- `GET /api/v1/search/index/status`：查看搜索索引状态。

当前能力边界：

- 当前系统仍是检索系统，不是完整 RAG 问答系统。
- 当前不生成 RAG 回答。
- 当前不调用 LLM。
- 当前不做 reranker。
- 当前不写入 `retrieval_logs`。
- OpenSearch 是派生索引，PostgreSQL 仍是主数据源，MinIO 仍保存原始文件。
- PostgreSQL pgvector 字段继续保留，供 embedding 存储及 OpenSearch 索引同步使用；旧纯向量调试接口已移除。

第五阶段详细说明见 `docs/phase-5-hybrid-search-index.md`。

---

## 第六阶段：基于混合检索的 RAG 问答最小闭环

当前项目已完成第六阶段最小闭环：在第五阶段 OpenSearch + IK 统一混合检索基础上，新增单轮 RAG 问答链路。

当前已支持：

- 通过 OpenAI-compatible provider 调用本地 LLM。
- 默认面向 Ollama OpenAI-compatible API：`http://localhost:11434/v1`。
- 建议本地模型：`qwen3.5:9b`，实际运行时可配置 `LLM_MODEL=qwen3.5:9b`。
- 新增后端接口：`POST /api/v1/rag/ask`。
- 新增前端页面：`/rag` 知识问答。
- 回答基于当前知识库检索片段生成。
- 返回 `answer`、`citations`、`retrieval` 和 `llm` 信息。
- no-context 作为正常状态返回，HTTP 200，`context_status="no_context"`。

仍需明确：

- PostgreSQL 仍是主数据源。
- OpenSearch 仍是派生检索索引，可从 PostgreSQL 重建。
- 第六阶段 RAG 复用第五阶段 `hybrid_search_chunks()`。
- 不重新实现 BM25、OpenSearch kNN 或 weighted RRF。
- 不修改 OpenSearch 索引结构。
- 不修改 `POST /api/v1/search` 返回结构。
- 旧纯向量调试接口已移除，RAG 继续复用 Hybrid Search。
- citations 只来自检索到的 chunks，不允许 LLM 自行生成来源。
- 当前仍不写 `retrieval_logs`。

当前仍不是复杂 Agent 系统，暂不支持：

- LangGraph。
- 多轮记忆。
- Agent 工具调用。
- MinerU。
- reranker 正式接入。
- 流式输出。
- 模型管理页面。
- prompt 编辑页面。
- 索引管理前端。

第六阶段详细说明与手动验收流程见：

- `docs/phase-6-rag-minimal-chain.md`
# 第七阶段状态：知识条目自动抽取与专家审核闭环

第七阶段已完成“知识条目自动抽取与专家审核闭环”，在不修改第五阶段混合检索和第六阶段 RAG 问答链路的前提下，新增独立的 `knowledge_items` 知识条目体系。

本阶段新增能力：

- 新增 `/knowledge-items` 前端页面，用于知识条目库、来源追溯、版本快照、审核记录和自动抽取入口。
- 新增 `/api/v1/knowledge-items` 系列 API，覆盖 CRUD、chunks、versions、reviews、submit、approve、reject、deprecate、revise 和 extract。
- 新增四表体系：`knowledge_items`、`knowledge_item_chunks`、`knowledge_item_reviews`、`knowledge_item_versions`。
- 旧 `knowledge_entries` / `entry_versions` / `entry_review_records` 早期预留体系已被新 `knowledge_items` 体系替代。
- 支持从 `document_chunks` 自动抽取候选知识条目，默认保存为 `draft`。
- `auto_submit=true` 时抽取结果进入 `pending_review`，永远不会直接创建 `approved`。
- 支持专家审核状态流转：`draft/rejected -> pending_review -> approved/rejected`，以及 `approved -> deprecated`。
- 支持 `revise` 从 `approved` / `deprecated` 创建新的 `draft` 修订版，不直接修改原可信条目。
- 支持 `source_chunk_ids` 与 `source_text` 来源追溯。
- 支持 `knowledge_item_versions` 内容版本快照和 `knowledge_item_reviews` 审核记录。
- `approved` 是可信知识状态，但当前暂不接入第六阶段 RAG 回答。

第七阶段仍不做：

- 不接入 Neo4j。
- 不引入 LangGraph。
- 不做 Graph-enhanced RAG。
- 不正式接入 reranker。
- 不让知识条目直接影响 `/api/v1/rag/ask`。
- 不修改 `/api/v1/search`。
- 旧纯向量接口已单独授权退役，不影响本阶段的 Knowledge API。
- 不写 `retrieval_logs`。

详细说明和手动验收流程见 `docs/phase-7-knowledge-items-review.md`。

## 第八阶段：MinerU API 文档解析增强

当前系统已经完成 FastAPI/PostgreSQL/MinIO/Redis 基础骨架、文档上传与基础解析、Qwen3-Embedding + pgvector、OpenSearch 混合检索、最小 RAG 问答闭环，以及 `knowledge_items` 知识条目抽取与专家审核闭环。第八阶段新增的是文档解析入库层增强：以 MinerU API 作为正式主解析器，提升 PDF、扫描 PDF、图文混排、表格、公式和图片类文档的解析质量。

解析器配置边界：

- `DOCUMENT_PARSER_PROVIDER=mineru_api` 是正式主路径。
- `DOCUMENT_PARSER_PROVIDER=basic` 仅用于 fallback、单元测试、本地最小开发验证或 MinerU 不可用时的受控兜底。
- 当 provider 为 `mineru_api` 且 MinerU API 配置缺失时，后端应返回明确配置错误。
- 第八阶段 v1 不实现正式 reparse API；已有 `document_chunks` 的文档默认仍返回 `DOCUMENT_ALREADY_PARSED`。

第八阶段核心数据流：

```text
documents
-> document_parse_runs
-> document_blocks / document_assets
-> document_chunks
-> document_chunk_blocks
-> embedding
-> OpenSearch
-> search / RAG / knowledge_items
```

`document_chunks` 仍是 `/api/v1/search`、`/api/v1/rag/ask` 和 `/api/v1/knowledge-items` 的共同基础。`document_blocks` 和 `document_assets` 是 MinerU 输出的解析中间层，不直接进入 RAG 检索；MinerU 输出也不会直接变成 `approved` 知识条目，知识条目仍必须经过第七阶段审核闭环。

## 第九阶段：Local/API 双通道 LLM Provider

第九阶段把第六、七阶段仅面向本地 Ollama 的 LLM 调用升级为 Local/API 双通道，同时保持 embedding、OpenSearch、MinerU 和既有 REST schema 不变。

### 已实现

- `LLM_PROVIDER=local`：通过 OpenAI-compatible Chat Completions 调用本地 Ollama；
- `LLM_PROVIDER=api`：调用 Generic OpenAI-compatible Chat Completions API；
- 修改 `.env` 后重启后端切换 Provider，不支持运行时热切换或自动 fallback；
- 内部正式契约以 `messages` 为输入，支持 system/user/assistant 文本历史；
- 当前 RAG 与 knowledge extraction 通过 `LLMGenerateRequest.from_prompt()` 接入同一 Provider；
- 结果同时提供完整 assistant `message` 与只读 `text` 属性；
- Local 支持 JSON mode 与 Ollama `think` 扩展；
- Generic API 的 JSON mode 由 `LLM_REMOTE_SUPPORTS_JSON_MODE` 声明，默认 `false`；
- tools、parallel tool calls 和 image input 在内部有受控类型，但当前 Provider 均在网络调用前明确拒绝；
- Provider/SDK client 在进程内惰性复用，并在 cache clear 或 FastAPI shutdown 时幂等关闭；
- `create_app()` 主动校验 active 配置，factory 再防御性复验；
- 远程 URL 安全、错误分类、敏感信息脱敏和安全结构化日志已落地；
- `/api/v1/rag/ask` 与 `/api/v1/knowledge-items/extract` 的公开请求/响应 schema 不变。

### Local 配置

以下只是假值示例。复制到本机真实 `backend/.env` 后按实际安装模型修改，不要提交真实 `.env`：

```env
LLM_PROVIDER=local
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=120
```

Local key 可以为空；Adapter 会在 SDK 创建边界使用非秘密占位值 `ollama`。Remote 配置为空不会阻止 Local 模式启动。

### Generic API 配置

```env
LLM_PROVIDER=api
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
LLM_REMOTE_BASE_URL=https://api.example.invalid/v1
LLM_REMOTE_API_KEY=
LLM_REMOTE_MODEL=example-chat-model
LLM_REMOTE_TIMEOUT_SECONDS=60
LLM_REMOTE_SUPPORTS_JSON_MODE=false
LLM_REMOTE_ALLOW_INSECURE_HTTP=false
```

- 真实 remote key 只写入本机未跟踪的 `backend/.env`；示例、日志、响应和前端中保持为空；
- 普通 RAG 不要求 JSON mode；
- knowledge extraction 固定要求 JSON object，因此 remote Provider 必须真实支持该能力，并显式设置 `LLM_REMOTE_SUPPORTS_JSON_MODE=true`；
- capability 为 `false` 时，knowledge extraction 在网络调用前返回 `LLM_PARAMETER_UNSUPPORTED`；
- 切换配置后必须完整重启后端；不存在 API 失败后自动改用 Local 的行为；
- LLM Provider 配置不改变本地 `Qwen3-Embedding-0.6B`、1024 维 embedding 或 OpenSearch mapping。

### Remote URL 与密钥安全

- HTTPS 默认允许；
- `http://localhost`、`http://127.0.0.1`、`http://[::1]` 允许；
- 其他 HTTP 只有设置 `LLM_REMOTE_ALLOW_INSECURE_HTTP=true` 才允许，并产生一次只含 scheme/hostname 的警告；
- 拒绝 userinfo、query、fragment 和非 HTTP(S) scheme；
- Remote key 在 Settings/transport 中保持 `SecretStr`，仅在局部非空校验和惰性创建 SDK client 时短暂读取；
- Python 运行时不能承诺物理擦除密钥内存；安全边界是不持久化、不返回、不记录；
- 429 的 `retryable=true` 是分类信息，不代表自动重试；SDK 使用 `max_retries=0`。

### 尚未实现

第九阶段没有实现对外 Chat API、REST 多轮 history、会话持久化、多轮 RAG、查询重写、LangChain、LangGraph、Agent loop、工具执行、tool calling wire transport、多模态传输、streaming/SSE、自动 retry、fallback、运行时 Provider 切换或 API embedding。内部 tool/image 类型不能视为这些业务能力已经可用。

详细设计、实施状态和人工验收见：

- `docs/superpowers/specs/2026-07-20-phase-9-local-api-llm-provider-design.md`
- `docs/superpowers/plans/2026-07-20-phase-9-local-api-llm-provider-implementation-plan.md`
- `docs/manual-acceptance.md`
- `docs/phase-9-finished.md`

# 第十阶段：Document Hard Delete

第十阶段实现文档及其独占派生数据的持久化、可恢复彻底删除：PostgreSQL deletion job 是事实来源，进程内 Executor 通过 DB-time claim/lease、fencing、heartbeat 和 step-local retry 驱动 OpenSearch、MinIO 与 PostgreSQL Saga。共享 Knowledge Item 按 `knowledge_item_sources` 的剩余来源决定保留或清理；旧 singular source 字段仅是 REST 兼容投影。

公开接口包括文档删除、删除状态和人工重试。删除中或删除失败的 Document 不能继续 parse、embedding、index sync 或 knowledge mutation；Hybrid/RAG 会在 RRF 与 context 构建前通过短 PostgreSQL session 过滤非 `normal` Document。前端只有在 status API 返回 204 后才认为删除完成。

安全默认值保持 `DOCUMENT_DELETION_EXECUTOR_ENABLED=false`。真实 rollout 还需要 M7-B 单独授权；在收到 `PHASE10_M7_ROLLOUT_AUTHORIZED` 前，不得执行 0007/0008 真实迁移、真实 Document DELETE、MinIO 删除或 OpenSearch delete-by-query。当前完成状态见 `docs/phase-10-finished.md`，其状态在真实跨存储验收前必须保持 `REAL_ROLLOUT_PENDING`。

设计与唯一实施计划：

- `docs/superpowers/specs/2026-08-27-phase-10-document-hard-delete-design.md`
- `docs/superpowers/plans/2026-08-27-phase-10-document-hard-delete-implementation-plan.md`
- `docs/manual-acceptance.md`
- `docs/phase-10-finished.md`
