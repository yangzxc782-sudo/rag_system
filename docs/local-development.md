# 第一阶段本地开发说明

本文说明 `D:\rag_system` 第一阶段 Windows 本机开发方式。本文中的命令只供用户手动执行，Codex 不自动执行这些命令。

## 本机开发架构

- FastAPI 后端在 Windows 本机运行，不放入 Docker Compose。
- Next.js 前端在 Windows 本机运行，不放入 Docker Compose。
- PostgreSQL + pgvector、Redis、MinIO 运行在 Docker 容器中。
- Docker Compose 文件路径为 `infra/docker-compose.yml`。
- Compose 项目名为 `rag_system`。
- FastAPI 本机访问 PostgreSQL 使用 `localhost:5432`。
- 未来后端容器化后，容器内部才使用 `postgres:5432`。

默认本地配置：
- PostgreSQL：`rag_user` / `rag_password` / `rag_system`
- Redis 密码：`rag_redis_password`
- MinIO：`rag_minio` / `rag_minio_password`
- MinIO bucket：`rag-documents`

## 启动顺序

### 1. 准备根目录环境变量

用户在项目根目录手动执行：

```powershell
copy .env.example .env
```

真实环境变量文件不得提交 Git：
- `.env`
- `backend/.env`
- `frontend/.env.local`

示例文件应保留并提交：
- `.env.example`
- `backend/.env.example`
- `frontend/.env.local.example`

### 2. 启动 Docker 基础服务

所有 Docker Compose 命令都必须从项目根目录执行，并统一使用：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml config
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
docker compose --env-file .env -f infra/docker-compose.yml logs postgres
docker compose --env-file .env -f infra/docker-compose.yml logs redis
docker compose --env-file .env -f infra/docker-compose.yml logs minio
```

第一阶段 Docker 只运行 PostgreSQL + pgvector、Redis、MinIO。

### 3. 手动创建 MinIO bucket

- MinIO Console 地址：`http://localhost:9001`
- 默认 bucket：`rag-documents`
- bucket 第一阶段由用户手动创建。
- 不得自动清空或删除 bucket。
- `/api/v1/health/services` 中 bucket 不存在应显示 warning，而不是 failed。

### 4. 准备后端环境

用户进入后端目录后手动执行：

```powershell
cd backend
copy .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

后端 `.env` 中的默认数据库连接应为：

```text
DATABASE_URL=postgresql+psycopg://rag_user:rag_password@localhost:5432/rag_system
```

### 5. 执行数据库迁移

用户确认连接的是本地开发库 `rag_system` 后，手动执行：

```powershell
alembic upgrade head
```

第一阶段迁移会创建 8 张基础表，并启用 pgvector 扩展作为第二阶段向量检索准备。第一阶段业务表不创建真实 vector 字段，也不创建向量索引。

### 6. 启动 FastAPI 后端

用户在 `backend` 目录中手动执行：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`uvicorn` 是前台长期运行命令。验证完成后使用 Ctrl+C 停止。

健康检查接口：
- `GET http://127.0.0.1:8000/health`
- `GET http://127.0.0.1:8000/api/v1/health`
- `GET http://127.0.0.1:8000/api/v1/health/services`

### 7. 准备 Next.js 前端

如果需要重新创建前端脚手架，执行 `create-next-app` 前必须确认 `D:\rag_system\frontend` 不存在或为空。

如果 `frontend` 已存在且非空，不得删除或覆盖，必须由用户确认处理方式，例如备份、换目录、清理或在现有目录上合并。

当前第一阶段前端已经按 Next.js App Router + TypeScript + ESLint + npm 创建，并保留 Tailwind CSS 相关脚手架文件。

### 8. 启动 Next.js 前端

用户进入前端目录后手动执行：

```powershell
cd frontend
copy .env.local.example .env.local
npm run dev
```

`npm run dev` 是前台长期运行命令。验证完成后使用 Ctrl+C 停止。

前端只允许包含公开变量：

```text
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

前端不得包含数据库、Redis、MinIO 的账号、密码、连接串或 Secret。

## npm audit 说明

如果当前 `npm audit` 提示 Next.js 间接依赖 `postcss` 存在 moderate 漏洞，第一阶段不执行 `npm audit fix --force`。

原因是强制修复可能改变 Next.js、Tailwind 或 App Router 脚手架依赖结构，破坏第一阶段可运行骨架。该问题后续等待 Next.js 官方依赖更新后再统一处理。

## 安全限制

- 不删除、不重建、不清空 PostgreSQL、Redis、MinIO 的持久化 volume。
- 不清空 MinIO bucket。
- 不删除 MinIO bucket。
- 不清空数据库或共享数据。
- 不丢弃本地改动。
- 如确实需要重置环境，必须先说明影响范围、备份方式和恢复路径，并等待用户明确确认。

## 第一阶段不包含的内容

- 不实现 RAG 检索链路。
- 不实现问答链路。
- 不实现模型调用。
- 不实现 Celery Worker。
- 不配置 Neo4j。
- 不配置 Elasticsearch。
- 不实现生产级权限系统。
# 第二阶段补充：文档上传与基础知识库入库闭环

本文在第一阶段本地开发说明基础上，补充第二阶段“文档上传与基础知识库入库闭环”的本地运行、迁移、上传验证和安全边界说明。本节只补充第二阶段内容，不替代第一阶段启动、健康检查、pgvector 边界和 npm audit 安全说明。

## 第二阶段数据流

第二阶段文档上传闭环数据流：

```text
前端选择文件
-> POST /api/v1/documents
-> FastAPI 校验文件
-> MinIO 保存原始文件
-> documents 表写入元数据
-> GET /api/v1/documents 获取列表
-> GET /api/v1/documents/{id} 获取详情
```

前端入口：

- `/documents`：文档管理、上传和列表。
- `/documents/[id]`：文档详情。

后端接口：

- `POST /api/v1/documents`
- `GET /api/v1/documents`
- `GET /api/v1/documents/{document_id}`

## 第二阶段本地运行命令

以下命令只供用户手动执行。Codex 不得自动执行 Docker、Git、npm、pip、alembic、pytest、uvicorn 或长期运行服务命令。

Docker 基础服务必须从项目根目录手动执行：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
```

后端命令：

```powershell
cd backend
pip install -e ".[dev]"
alembic upgrade head
pytest
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`alembic upgrade head` 由用户确认连接本地开发库后手动执行。`uvicorn` 是前台长期运行命令，验证完成后使用 Ctrl+C 停止。

前端命令：

```powershell
cd frontend
npm run lint
npm run dev
```

`npm run dev` 是前台长期运行命令，验证完成后使用 Ctrl+C 停止。

## 第二阶段上传限制

默认最大上传文件大小为 `50MB`：

```text
UPLOAD_MAX_FILE_SIZE_BYTES=52428800
```

默认允许扩展名：

```text
.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp
```

`content_type` 仅作为辅助校验，不作为强安全判断。第二阶段不做深度文件内容识别，不引入文件解析器校验。

## MinIO object key 规则

第二阶段原始文件保存到 MinIO `rag-documents` bucket，object key 规则为：

```text
raw/YYYY/MM/<document_id><original_extension>
```

示例：

```text
raw/2026/06/550e8400-e29b-41d4-a716-446655440000.pdf
```

MinIO Console 地址：

```text
http://localhost:9001
```

用户需要手动确认 `rag-documents` bucket 已存在。第二阶段不自动创建、清空或删除 bucket，不删除已有对象。

## documents.process_status

第二阶段上传成功后，由 service 层显式写入：

```text
process_status=uploaded
```

第二阶段不修改 `process_status` 的 ORM 默认值，不修改数据库默认值。`upload_failed` 仅作为后续扩展预留状态。本阶段 MinIO 上传失败时直接返回错误，不写入新的 `documents` 记录。

## 第二阶段手动验收

1. 用户手动启动 Docker 基础服务。
2. 用户手动确认 PostgreSQL、Redis、MinIO 容器运行。
3. 打开 `http://localhost:9001`。
4. 确认 `rag-documents` bucket 已存在。
5. 用户手动执行 `alembic upgrade head`。
6. 用户手动启动 FastAPI。
7. 用户手动启动 Next.js。
8. 打开 `http://localhost:3000/documents`。
9. 上传 PDF、Word、Excel、图片各 1 个。
10. 确认前端文档列表出现记录。
11. 点击详情进入 `/documents/[id]`。
12. 确认详情页展示文件名、bucket、object key、类型、MIME、大小、hash、状态、上传时间、更新时间。
13. 在 MinIO 中确认 `raw/YYYY/MM/` 路径下出现对象。

## 第二阶段环境变量

后端示例：

```text
UPLOAD_MAX_FILE_SIZE_BYTES=52428800
UPLOAD_ALLOWED_EXTENSIONS=.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp
BACKEND_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
MINIO_BUCKET=rag-documents
```

前端示例：

```text
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

真实 `.env`、`backend/.env`、`frontend/.env.local` 不得提交 Git。示例 env 文件可以提交。前端不得暴露数据库、Redis、MinIO 密钥。

## 第二阶段明确不实现

第二阶段不实现：

- 文档真实解析
- `document_chunks` 切分入库
- embedding 生成
- 真实 `vector` 字段
- HNSW、IVFFlat 或其他向量索引
- RAG 问答
- 知识条目自动抽取
- Celery Worker
- Neo4j
- Elasticsearch
- 模型服务
- 文件下载
- 文件预览
- 大文件流式上传
- 深度文件内容识别

## 第二阶段安全边界

继续禁止删除 Docker volume、清空或删除 MinIO bucket、删除 MinIO 已有对象、清空数据库、删除数据库结构、执行无条件数据删除、强制修复 npm audit、重建或覆盖 `frontend`。

禁止项包括：`docker compose down -v`、`docker volume rm`、`docker volume prune`、`docker system prune --volumes`、`DROP DATABASE`、`DROP TABLE`、`TRUNCATE`、无 `WHERE` 条件的 `DELETE`、`npm audit fix --force`。

所有 Docker Compose 命令继续统一使用：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml ...
```

# 第三阶段补充：文档解析适配与基础切片可视化闭环

本文在第一、二阶段本地开发说明基础上，补充第三阶段“文档解析适配与基础切片可视化闭环”的本地运行、配置、迁移和手动验收说明。本节中的命令仍只供用户手动执行，Codex 不自动执行 Docker、Git、npm、pip、alembic、pytest、uvicorn、服务启动或数据库写入命令。

## 第三阶段数据流

第三阶段数据流：

```text
已上传 documents 记录
-> MinIO 原始文件 bytes 只读读取
-> Parser
-> ParsedDocument
-> chunker
-> document_chunks 入库
-> documents.process_status 更新
-> 前端文档详情页 chunk 可视化
```

新增后端接口：

- `POST /api/v1/documents/{document_id}/parse`
- `GET /api/v1/documents/{document_id}/chunks?limit=50&offset=0`

前端入口：

- `/documents`：单文件上传和文档列表。
- `/documents/[id]`：文档详情、解析按钮、chunk 统计和 chunk 列表。

## 第三阶段迁移说明

第三阶段 Step 3 只创建 Alembic 迁移文件：

```text
backend/alembic/versions/0003_add_document_chunk_source_metadata.py
```

实际运行解析前，用户需要手动确认连接的是本地开发库 `rag_system`，并在 `backend` 目录中手动执行：

```powershell
alembic upgrade head
```

该迁移为 `document_chunks` 增加 nullable JSONB 字段：

```text
source_metadata
```

Codex 不得自动执行 `alembic upgrade head`。PostgreSQL MCP 也不得用于执行迁移、改表或补数据。

## 第三阶段后端配置

后端运行前，需要确认 `backend/.env` 中包含或继承以下第三阶段配置：

```text
DOCUMENT_PARSER=simple
CHUNK_SIZE_CHARS=1000
CHUNK_OVERLAP_CHARS=100
MINERU_ENDPOINT=
MINERU_TIMEOUT_SECONDS=60
```

上传白名单已包含文本类文件：

```text
.txt,.md,.csv
```

完整允许扩展名示例：

```text
UPLOAD_ALLOWED_EXTENSIONS=.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp,.txt,.md,.csv
```

`content_type` 仅作为辅助信息或弱校验，不作为强安全判断。如果扩展名已在白名单中，但 `content_type` 为空、`application/octet-stream` 或浏览器返回不稳定类型，第三阶段不应直接拒绝上传，避免误拦截 `.txt`、`.md`、`.csv`。

MinIO bucket 仍需用户手动确认存在：

```text
rag-documents
```

第三阶段不自动创建 bucket，不清空 bucket，不删除 bucket，不删除、不移动、不覆盖 MinIO 原始对象。

## 第三阶段解析行为

`DOCUMENT_PARSER=simple` 时：

- `.txt`、`.md`、`.csv` 做轻量文本解析。
- 优先 UTF-8 解码。
- 解码失败时使用 `errors="replace"` 兜底。
- PDF、Word、Excel、图片生成占位解析结果。
- 不新增 `pypdf`、`python-docx`、`openpyxl`、OCR 或 MinerU SDK 等重型解析依赖。

`DOCUMENT_PARSER=mineru` 时：

- 当前只进入 `MinerUParser` 预留边界。
- 第三阶段不请求真实 MinerU 服务。
- 当前不可用时返回 `DOCUMENT_PARSER_UNAVAILABLE`。

`POST /parse` 是同步接口。`parsing` 状态用于后端状态流转；由于轻量解析通常较快，前端不保证一定能观察到 `parsing`。第三阶段不实现异步任务队列、实时进度、Celery Worker 或轮询进度。

## 重复解析策略

第三阶段采用保守策略：

- `parse_document` 先检查是否已有 chunks。
- 如果已有 chunks，直接返回 `DOCUMENT_ALREADY_PARSED` / HTTP 409。
- 已有 chunks 时不修改 `documents.process_status`。
- 不删除已有 chunks。
- 不覆盖已有 chunks。
- 不默认重新解析。
- 只有确认无 chunks 后，才进入 `parsing` 状态。

如果解析或 chunk 写入失败：

- rollback 本次 chunk 写入。
- 不留下半截 `document_chunks`。
- rollback 后再单独更新 `documents.process_status='parse_failed'` 和 `error_message`。
- 不删除、不修改、不移动 MinIO 原始对象。

`force` 覆盖或版本化重新解析不在第三阶段实现，后续必须单独设计并由用户确认。

## 第三阶段手动验收建议

以下步骤仅供用户手动执行，Codex 不自动执行：

1. 用户手动启动 Docker 基础服务。
2. 用户手动确认 PostgreSQL、Redis、MinIO 容器运行。
3. 打开 `http://localhost:9001`，确认 `rag-documents` bucket 已存在。
4. 用户手动进入 `backend` 并执行 `alembic upgrade head`。
5. 用户手动启动 FastAPI。
6. 用户手动启动 Next.js。
7. 打开 `http://localhost:3000/documents`。
8. 上传 `.txt`、`.md`、`.csv` 文本类文件。
9. 打开某个文档详情页 `/documents/[id]`。
10. 点击“解析文档”。
11. 确认详情页展示 chunk 总数、长度统计、chunk 列表、来源元数据和内容预览。
12. 再次点击解析，确认返回已解析提示，不覆盖已有 chunks。
13. 可上传 PDF、Word、Excel、图片，确认生成占位 chunk，内容提示后续由 MinerU 替换。

可由用户手动使用 PostgreSQL MCP 做只读核验：

- 查询 `alembic_version`。
- 查询 `information_schema.columns` 确认 `document_chunks.source_metadata`。
- 查询 `documents.process_status`。
- 统计某文档 chunks 数量。
- 查看 `chunk_index`、`chunk_type`、`embedding_status`、`source_metadata`。

禁止使用 PostgreSQL MCP 执行迁移、改表、补数据、删除数据、清空表或修改数据库结构。禁止通过 MCP 执行 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`TRUNCATE`、`ALTER`、`CREATE`。

## 第三阶段明确不实现

第三阶段不实现：

- 真实 MinerU 服务强制接入
- embedding 生成
- 真实 `vector` 字段
- HNSW / IVFFlat 或其他向量索引
- 语义检索
- RAG 问答
- 知识条目自动抽取
- Neo4j
- Elasticsearch
- Celery Worker
- 复杂可视化调参系统
- 文件下载
- 文件预览
- 多文件上传
- 拖拽上传
- 单 chunk 详情页

## 第三阶段安全边界

继续禁止：

- `docker compose down -v`
- 删除 Docker volume
- `docker volume prune`
- `docker system prune --volumes`
- 清空 MinIO bucket
- 删除 MinIO bucket
- 删除 MinIO 对象
- `DROP`
- `TRUNCATE`
- 无 `WHERE` 条件的 `DELETE`
- 通过 PostgreSQL MCP 执行 `INSERT` / `UPDATE` / `DELETE`
- 通过 PostgreSQL MCP 执行 `ALTER` / `CREATE`
- 通过 PostgreSQL MCP 执行迁移、补数据、清空表或修改结构
- `npm audit fix --force`

# 第四阶段补充：Embedding 与基础向量检索本地开发说明

本节补充第四阶段“Embedding 生成与基础向量检索闭环”的本地开发运行说明。本文中的命令仅供用户手动执行，Codex 不自动执行 pip、npm、Docker、alembic、pytest、uvicorn、模型加载、维度探测或数据库写入命令。

## 依赖安装建议

如果本机使用 NVIDIA GeForce RTX 5060 Laptop GPU，属于较新的 Blackwell / `sm_120` 架构，建议先单独安装适配 CUDA 12.8 / cu128 的 GPU 版 torch，避免 `pip install -e .` 自动解析依赖时安装 CPU 版 torch 或不支持 `sm_120` 的 CUDA 版本。

建议用户手动执行：

```powershell
cd D:/rag_system/backend
./.venv/Scripts/Activate.ps1
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```

安装后建议用户手动验证：

```powershell
python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0)); print(torch.cuda.get_arch_list()); print(torch.tensor([1.0], device='cuda'))"
```

确认内容：

- `torch.__version__` 包含 `+cu128`。
- `torch.version.cuda` 为 `12.8`。
- `torch.cuda.is_available()` 为 `True`。
- `torch.cuda.get_device_name(0)` 为 `NVIDIA GeForce RTX 5060 Laptop GPU`。
- `torch.cuda.get_arch_list()` 包含 `sm_120`。
- 可以创建 CUDA tensor。

torch 验证通过后，再安装第四阶段其余依赖：

```powershell
python -m pip install "pgvector>=0.3,<1.0" "sentence-transformers>=2.7.0,<6.0" "transformers>=4.51.0,<5.0"
python -m pip install -e . --no-deps
```

`--no-deps` 用于避免 pip 重新解析并覆盖已安装好的 GPU 版 torch。

## 离线环境变量

第四阶段本地模型必须使用本地路径和 local-only 逻辑。建议在后端 `.env` 或当前 PowerShell 会话中确认：

```text
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
EMBEDDING_LOCAL_FILES_ONLY=true
```

后端 embedding 配置示例：

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
```

模型路径：

```text
D:/rag_system/models/Qwen3-Embedding-0.6B
```

本阶段不访问外网，不下载模型。

## local-only 维度探测

第四阶段计划默认维度为 `1024`。依赖安装完成后，用户可手动执行一次 local-only 维度探测，要求：

- 使用 `LocalQwen3EmbeddingProvider`。
- 使用本地模型路径。
- 设置 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`。
- 不访问外网。
- 不下载模型。
- 不写数据库。
- 不执行 Alembic。
- 只确认 `actual_dim=1024`。

已通过的维度探测结果：

```text
provider=local_qwen3
model=Qwen3-Embedding-0.6B
configured_dim=1024
actual_dim=1024
matches_config=True
```

## Alembic

第四阶段通过 Alembic 为 `document_chunks` 增加 `embedding vector(1024)`、`embedding_error_message` 和 `embedding_updated_at`。用户确认连接的是本地开发库 `rag_system` 后，在 `backend` 目录手动执行：

```powershell
alembic upgrade head
```

Codex 不自动执行 `alembic upgrade head`。PostgreSQL MCP 也不得用于执行迁移、改表或补数据。

## 启动后端和前端后的验收路径

用户手动启动 FastAPI 和 Next.js 后，可按以下路径验收：

1. 打开 `/documents`。
2. 上传或选择已有文档。
3. 进入文档详情页 `/documents/[id]`。
4. 如文档尚未解析，先点击解析文档。
5. 在文档详情页查看 embedding 状态。
6. 点击生成 embedding。
7. 打开 `/search`。
8. 输入 query 执行基础向量检索。
9. 确认结果展示文档名、chunk_index、content、source_metadata、distance 和 score。
10. 确认 `distance` 越小越相似，`score = 1 - distance`。

第四阶段仍不实现 RAG 问答、大语言模型回答、reranker、混合检索、关键词检索、图谱检索、Celery 队列、`retrieval_logs` 写入或向量索引。
 
# 第五阶段补充：OpenSearch + analysis-ik 本地开发说明

第五阶段默认采用 `OpenSearch 3.6.0 + analysis-ik 3.6.0`，用于建设统一混合检索的派生索引闭环。PostgreSQL 仍然是主数据源，MinIO 仍然是原始文件存储，OpenSearch 只保存可从 PostgreSQL `documents` / `document_chunks` 重建的检索索引。

本地开发 OpenSearch 配置：

- 仅启动单节点 OpenSearch，不引入 OpenSearch Dashboards，降低本机资源占用。
- Docker Desktop 建议分配 `8-12GB` 内存。
- OpenSearch JVM heap 默认使用 `OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g`。
- 本地开发阶段关闭 security plugin，使用 HTTP：`SEARCH_ENGINE_URL=http://localhost:9200`。
- 如果后续启用 security plugin，真实用户名和密码只允许写入本机 `.env` / `backend/.env`，示例文件只能保留占位符。
- Windows / Docker Desktop / WSL2 环境下，OpenSearch 可能需要人工确认 `vm.max_map_count`；Codex 不自动执行 `sysctl` 或系统级修改。

OpenSearch 镜像与 IK 插件：

- 自定义镜像文件位于 `infra/opensearch/Dockerfile`。
- 镜像基于 `opensearchproject/opensearch:3.6.0`。
- Dockerfile 使用 `opensearch-plugin install --batch` 固化 `opensearch-analysis-ik-3.6.0.zip`。
- Dockerfile 不创建索引，不写入业务数据，不配置真实密码。

IK 词典维护：

- 自定义词典文件：`infra/opensearch/ik/custom.dic`。
- 停用词文件：`infra/opensearch/ik/stopword.dic`。
- 词典文件纳入项目版本管理，不硬编码在 Python 代码中。
- 当前 Docker Compose 将词典挂载到 `/usr/share/opensearch/config/analysis-ik/`；analysis-ik 插件版本或目录结构变化时，应在创建索引前重新确认该路径和 `IKAnalyzer.cfg.xml` 的生效方式。
- 词典更新后是否需要重启 OpenSearch 或刷新 analyzer，应以当前 analysis-ik 版本文档和实际 analyzer API 自测结果为准。

安全边界：

- 不要执行 `docker compose down -v`。
- 不要删除或重建 PostgreSQL、Redis、MinIO、OpenSearch 的 Docker volume。
- 不要清空或删除 MinIO bucket。
- OpenSearch 索引是派生数据；如索引损坏，应从 PostgreSQL 重建，而不是修改 PostgreSQL 主数据。
- 第五阶段仍不写 `retrieval_logs`，不生成 RAG 回答，不调用 LLM，不实现 reranker、图谱检索、知识条目抽取、专家审核、自动流水线或 Celery。

## 第五阶段 OpenSearch + IK 本地验收流程

第五阶段引入 OpenSearch 3.6.0 + analysis-ik 3.6.0，作为从 PostgreSQL `document_chunks` 重建的派生检索索引。PostgreSQL 仍是主数据源，MinIO 仍保存原始文件，OpenSearch 不替代主库。

本地资源建议：

- Docker Desktop 建议分配 8-12GB 内存。
- OpenSearch 使用单节点本地开发模式。
- OpenSearch JVM heap：`OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g`。
- 第五阶段暂不引入 OpenSearch Dashboards。
- 本地开发使用 HTTP，并关闭 security plugin。
- `.env.example` 和 `backend/.env.example` 只写占位符，不写真实密码。
- Windows / Docker Desktop 下 `vm.max_map_count` 可能需要人工处理，实施前按 OpenSearch 启动日志和官方文档确认。

IK 词典文件位置：

- `infra/opensearch/ik/custom.dic`
- `infra/opensearch/ik/stopword.dic`

词典文件纳入项目版本管理，不硬编码在 Python 代码中。词典更新后，可能需要重建 OpenSearch 镜像或重启 OpenSearch 服务；是否支持热加载需要按当前 analysis-ik 版本确认。

### 安全提醒

不要将以下命令作为常规开发命令：

- `docker compose down -v`
- `docker volume prune`
- `docker system prune --volumes`
- 删除 PostgreSQL / Redis / MinIO / OpenSearch volume
- 删除 MinIO bucket 或对象
- 未确认的 Alembic 迁移
- 未确认的 OpenSearch index 删除

新增 OpenSearch 服务不会删除已有 PostgreSQL / Redis / MinIO volume。

### 手动验收命令

以下命令仅作为用户手动验收参考，本阶段文档更新不会自动执行。

后端测试：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_search_engine.py tests/test_search_index.py tests/test_hybrid_search.py -q
```

前端检查：

```powershell
cd D:\rag_system\frontend
npm run lint
```

构建并启动基础服务和 OpenSearch：

```powershell
cd D:\rag_system
docker compose --env-file .env -f infra/docker-compose.yml build opensearch
docker compose --env-file .env -f infra/docker-compose.yml up -d postgres redis minio opensearch
docker compose --env-file .env -f infra/docker-compose.yml ps
```

后端启动：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

前端启动：

```powershell
cd D:\rag_system\frontend
npm run dev
```

### 索引创建、同步和状态检查

创建 OpenSearch index 和 alias：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/search/index/create"
```

全量同步可检索 chunks：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/search/index/rebuild" `
  -ContentType "application/json" `
  -Body '{"scope":"all"}'
```

查看索引状态：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8000/api/v1/search/index/status"
```

统一智能检索：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/search" `
  -ContentType "application/json" `
  -Body '{"query":"冒口如何保证热节补缩","limit":10}'
```

如果 `syncable_chunks=0`，需要先在文档详情页生成 embedding，再重新执行 index rebuild。

### 前端智能检索页面验证

1. 启动后端和前端。
2. 打开 `/search` 页面。
3. 页面标题应为“智能检索”。
4. 输入：`冒口如何保证热节补缩`。
5. 结果应展示 `retrieval_source`、`keyword_score`、`vector_score`、`hybrid_score`、`matched_keywords`。
6. 页面不应展示 RAG 回答、聊天界面、检索模式切换或索引管理入口。

### 常见问题

- OpenSearch 启动失败：检查 Docker Desktop 内存、JVM heap、端口占用和 `vm.max_map_count`。
- IK 插件未安装：确认使用自定义 OpenSearch 镜像，并检查 analysis-ik 3.6.0 插件安装日志。
- index 不存在：先调用 `POST /api/v1/search/index/create`。
- index 为空：先生成文档 embedding，再调用 `POST /api/v1/search/index/rebuild`。
- kNN 查询失败：检查 OpenSearch mapping 中 `embedding` 是否为 1024 维 `knn_vector`，并确认 kNN 已启用。
- 没有可同步 chunks：确认 `document_chunks` 中存在 `embedding_status='embedded'`、`embedding_dim=1024`、`embedding_model='Qwen3-Embedding-0.6B'` 且 embedding 非空的记录。
- 前端提示搜索引擎不可用：检查 OpenSearch 服务、后端 `SEARCH_ENGINE_URL` 和后端日志。
- 前端暂无命中：确认 index 已创建、已同步，并尝试包含标准编号、材料牌号或铸型术语的 query。

---

## 第六阶段本地开发：RAG 问答最小闭环

第六阶段新增 `/rag` 知识问答页面和 `POST /api/v1/rag/ask`。该链路基于第五阶段混合检索结果构建上下文，通过 OpenAI-compatible provider 调用本地 Ollama LLM，并返回答案与引用片段。

本节命令仅作为用户手动执行参考。自动化助手不得在未确认时执行 Docker、npm、pip、pytest、uvicorn、Ollama 或数据库相关命令。

### 1. Ollama 与模型

建议本地安装并启动 Ollama，然后确认模型存在：

```powershell
ollama list
```

用户本地已下载：

```text
qwen3.5:9b
```

如需手动运行模型：

```powershell
ollama run qwen3.5:9b
```

第六阶段建议后端环境变量：

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

- `LLM_BASE_URL=http://localhost:11434/v1` 对应 Ollama 的 OpenAI-compatible API。
- `LLM_API_KEY=ollama` 是本地兼容接口占位值。
- 前端不写死模型名，页面展示以后端返回的 `llm.model` 为准。

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

### 2. 后端依赖

第六阶段新增 OpenAI-compatible client 依赖：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pip install "openai>=1.0,<3.0"
```

不要由自动化助手自动执行依赖安装；依赖安装应由用户确认后手动执行。

### 3. 启动基础服务

从项目根目录启动本地基础服务：

```powershell
cd D:\rag_system
docker compose --env-file .env -f infra/docker-compose.yml up -d postgres redis minio opensearch
```

安全提醒：

- 不要执行 `docker compose down -v`。
- 不要删除 PostgreSQL、MinIO 或 OpenSearch volume。
- 不要清空 MinIO bucket。

### 4. 确认第五阶段索引状态

先确认第五阶段检索索引正常：

```http
GET /api/v1/search/index/status
```

检查要点：

- OpenSearch 可用。
- index 存在。
- alias 存在。
- index document count 正常。
- PostgreSQL 可同步 chunks 数正常。

再确认混合检索接口：

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

### 5. 启动后端

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

### 6. 启动前端

```powershell
cd D:\rag_system\frontend
npm run dev
```

访问：

```text
http://localhost:3000/rag
```

页面标题为“知识问答”。页面调用 `POST /api/v1/rag/ask`，展示 `answer`、`citations`、`retrieval.total`、`llm.provider` 和 `llm.model`。

### 7. 第六阶段 RAG API 验收

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
- 返回 `llm.provider` 和 `llm.model`。
- `context_status` 为 `ok` 或 `no_context`。
- no-context 时 HTTP 200，`success=true`，`citations=[]`。
- `retrieval_logs` 仍不写入。
- LLM 不可用时返回明确错误。

### 8. 测试与 lint 手动命令

第六阶段后端测试：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_llm_provider.py tests/test_rag_context.py tests/test_rag_service.py tests/test_rag_api.py -q
```

第五阶段回归测试：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_search_engine.py tests/test_search_index.py tests/test_hybrid_search.py -q
```

前端 lint：

```powershell
cd D:\rag_system\frontend
npm run lint
```

### 9. 常见问题

#### openai 包未安装

现象：

- 后端 import OpenAI-compatible provider 失败。

处理：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pip install "openai>=1.0,<3.0"
```

#### Ollama 未启动

现象：

- RAG API 返回 `LLM_UNAVAILABLE`。

处理：

- 确认 Ollama 已启动。
- 确认 `LLM_BASE_URL=http://localhost:11434/v1`。

#### 模型名不匹配

现象：

- LLM 生成失败或 Ollama 返回模型不存在。

处理：

```powershell
ollama list
```

确认 `.env` 中 `LLM_MODEL` 与列表中的模型名一致，例如：

```env
LLM_MODEL=qwen3.5:9b
```

#### LLM_TIMEOUT

现象：

- RAG API 返回 `LLM_TIMEOUT`。

处理：

- 确认模型已加载或首次加载时间较长。
- 可适当增大 `LLM_TIMEOUT_SECONDS`。
- 检查本机 CPU/GPU/内存资源。

#### LLM_UNAVAILABLE

现象：

- RAG API 返回 `LLM_UNAVAILABLE`。

处理：

- 确认 Ollama 服务可访问。
- 确认 `LLM_BASE_URL` 指向 `http://localhost:11434/v1`。
- 确认本机防火墙或代理没有拦截。

#### OpenSearch 不可用

现象：

- 搜索接口或 RAG API 返回 `SEARCH_ENGINE_UNAVAILABLE`。

处理：

- 确认 OpenSearch 容器运行。
- 确认 `SEARCH_ENGINE_URL=http://localhost:9200`。
- Windows / Docker Desktop 下确认资源和 `vm.max_map_count` 设置。

#### 索引为空

现象：

- `/api/v1/search/index/status` 中 index document count 为 0。
- RAG 返回 no-context。

处理：

- 先确认文档已解析、chunks 已生成、embedding 已生成。
- 再执行索引同步。
- 不要自动 rebuild index；由用户明确确认后手动执行。

#### no-context

现象：

- RAG API 返回 HTTP 200，`context_status="no_context"`。

说明：

- no-context 是正常状态，不是异常。
- 表示当前知识库未检索到足够依据。
- no-context 时不调用 LLM。
- no-context 时 `citations=[]`。

#### citations 为空

可能原因：

- 当前为 no-context。
- 检索结果为空。
- 上下文长度限制导致无可用片段进入上下文。

处理：

- 检查 `/api/v1/search` 是否有结果。
- 检查 `RAG_CONTEXT_MAX_CHARS` 是否过小。

#### 前端无法连接后端

现象：

- `/rag` 页面显示网络请求失败。

处理：

- 确认 FastAPI 后端已启动。
- 确认 `NEXT_PUBLIC_API_BASE_URL` 指向后端地址。
- 默认后端地址为 `http://127.0.0.1:8000`。
# 第七阶段本地开发补充：知识条目自动抽取与专家审核

第七阶段新增 `knowledge_items` 知识条目体系和 `/knowledge-items` 前端页面。它独立于第五阶段混合检索和第六阶段 RAG 问答链路：

- knowledge extraction 只从 PostgreSQL `document_chunks` 读取输入。
- knowledge extraction 复用第六阶段 OpenAI-compatible LLM provider。
- knowledge extraction 不调用 `/api/v1/rag/ask`。
- knowledge extraction 不调用 `hybrid_search_chunks()`。
- knowledge extraction 不调用 OpenSearch。
- knowledge extraction 不写 `retrieval_logs`。

## 配置项

第七阶段新增：

```env
KNOWLEDGE_EXTRACTION_MAX_CHUNKS=20
KNOWLEDGE_EXTRACTION_MAX_CHARS=12000
KNOWLEDGE_EXTRACTION_DEFAULT_STATUS=draft
```

说明：

- `KNOWLEDGE_EXTRACTION_DEFAULT_STATUS` 只能为 `draft`。
- `pending_review` 只能通过 extract 请求中的 `auto_submit=true` 触发。
- `approved` 永远不能作为抽取默认状态。
- Ollama / LLM 接入仍沿用第六阶段 OpenAI-compatible provider 配置。

## 迁移前安全检查

第七阶段用新 `knowledge_items` 四表体系替代旧 `knowledge_entries` 预留体系。执行 migration 前必须确认旧表为空或不存在。

建议由用户手动执行只读检查：

```sql
SELECT COUNT(*) FROM knowledge_entries;
SELECT COUNT(*) FROM entry_versions;
SELECT COUNT(*) FROM entry_review_records;
```

如果任一旧表非空，停止迁移并确认处理策略。第七阶段 migration 本身也包含 inspector 表存在检查和旧表非空保护。

用户单独确认后，才执行：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
alembic upgrade head
```

## 第七阶段测试

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
python -m pytest tests/test_knowledge_item_models.py tests/test_knowledge_item_service.py tests/test_knowledge_item_api.py tests/test_knowledge_extraction.py -q
```

第五阶段回归：

```powershell
python -m pytest tests/test_search_engine.py tests/test_search_index.py tests/test_hybrid_search.py -q
```

第六阶段回归：

```powershell
python -m pytest tests/test_llm_provider.py tests/test_rag_context.py tests/test_rag_service.py tests/test_rag_api.py -q
```

前端 lint：

```powershell
cd D:\rag_system\frontend
npm run lint
```

## 本地启动

基础服务：

```powershell
cd D:\rag_system
docker compose --env-file .env -f infra/docker-compose.yml up -d postgres redis minio opensearch
```

不要把 `docker compose down -v` 作为常规命令。

后端：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

前端：

```powershell
cd D:\rag_system\frontend
npm run dev
```

打开：

```text
http://localhost:3000/knowledge-items
```

## 常见问题

### 缺少 fastapi

如果 `python -c "import fastapi"` 失败，请按项目依赖说明安装后端依赖。不要在自动化任务中私自安装依赖。

### migration 因旧表非空停止

说明旧 `knowledge_entries` / `entry_versions` / `entry_review_records` 中存在数据。为避免误删，迁移会停止。需要用户确认是否备份、迁移或清理旧数据。

### extract LLM 不可用

第七阶段抽取复用第六阶段 OpenAI-compatible provider。请检查 Ollama 是否启动、模型是否存在、`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 是否正确。

### parser 失败

LLM 输出必须是 JSON object，顶层形如 `{"items": [...]}`。如果输出包含非法 `item_type`、空 title/content、未知 `source_chunk_ids`、`status` 或 `approved` 字段，parser 会失败且不写半成品。

### duplicate skipped

抽取时检测到同一来源文档、同一类型和相同 `content_hash` 的非 deprecated 条目，会跳过并返回 `skipped_duplicates`。手工创建重复仍返回 `409`。

### approved 不能编辑

`approved` 和 `deprecated` 禁止直接 `PATCH`。如需修改，使用 `revise` 创建新的 `draft` 修订版。

### source_text 为空或不可见

`source_text` 来自抽取或创建时的 `document_chunks.content` 快照。若手工创建时没有传 `source_chunk_ids`，则不会有来源片段快照。

### confidence 不等于可信度

`confidence` 是 LLM 抽取置信度，不代表专家审核可信度。只有 `status=approved` 才表示可信知识。

### retrieval_logs 不应增加

第七阶段不写 `retrieval_logs`。如果发现日志增加，需要确认是否由其他检索或 RAG 流程触发。

## 第八阶段：MinerU 文档解析本地开发流程

第八阶段将文档解析入库层升级为 MinerU API 主路径。以下内容只说明本地开发与手动验收流程，命令需由用户自行执行，本步骤不运行任何命令。

### 1. 启动本地基础服务

本地基础服务包括 PostgreSQL、Redis、MinIO 和 OpenSearch。所有 Docker Compose 命令必须从项目根目录执行，并显式指定 compose 文件：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
```

禁止使用：

```powershell
docker compose down -v
docker volume prune
docker system prune --volumes
```

### 2. 选择解析器

正式解析推荐：

```env
DOCUMENT_PARSER_PROVIDER=mineru_api
MINERU_API_BASE_URL=https://mineru.net
MINERU_API_KEY=<your-mineru-api-key>
MINERU_API_TIMEOUT_SECONDS=120
MINERU_API_POLL_INTERVAL_SECONDS=5
MINERU_API_MAX_POLL_ATTEMPTS=120
MINERU_PARSE_MODE=vlm
MINERU_ENABLE_OCR=true
MINERU_OUTPUT_PREFIX=parsed-assets
```

最小本地 fallback / 测试：

```env
DOCUMENT_PARSER_PROVIDER=basic
```

说明：

- `mineru_api` 是正式主路径。
- `basic` 仅用于 fallback、测试 parser 或简单文本类文档验证。
- 当 provider 为 `mineru_api` 且配置缺失时，后端会返回明确配置错误，不会静默 fallback 到 basic。
- 不要把真实 API key 提交到仓库。

`MINERU_API_BASE_URL` 只填写官方域名，后端自行拼接 V4 路径。当前本地文件流程先调用 `POST /api/v4/file-urls/batch`，再用签名地址 `PUT` 原始文件，随后轮询 `GET /api/v4/extract-results/batch/{batch_id}`。签名上传和结果 ZIP 下载不会携带 MinerU Bearer Token，上传时也不会主动设置 `Content-Type`。`MINERU_PARSE_MODE=auto` 仅作为兼容别名，实际按 `vlm` 请求；正式配置推荐直接写 `vlm`。

### 3. 文档上传与解析

本地开发的推荐顺序：

1. 启动后端服务。
2. 启动前端服务。
3. 上传测试文档。
4. 调用文档 parse API。
5. 查看 parse status。
6. 查看 blocks / assets。
7. 查看 chunks。
8. 后续手动触发 embedding。
9. 后续手动重建或同步 OpenSearch 索引。
10. 回归验证 search / RAG / knowledge-items。

示例 API：

```text
POST /api/v1/documents/{document_id}/parse
GET  /api/v1/documents/{document_id}/parse-status
GET  /api/v1/documents/{document_id}/parse-runs
GET  /api/v1/documents/{document_id}/blocks?limit=50&offset=0
GET  /api/v1/documents/{document_id}/assets?limit=50&offset=0
GET  /api/v1/documents/{document_id}/chunks
```

blocks / assets 查询默认分页，前端也不应一次性渲染巨大 JSON。

### 4. 后续 embedding 与 OpenSearch

第八阶段解析流程只生成 `document_chunks`，不自动生成 embedding，也不自动 rebuild OpenSearch。

手动验收时应在 parse 成功后再执行 embedding 相关流程，然后重建或同步 OpenSearch 索引，最后验证：

```text
/api/v1/search
/api/v1/rag/ask
/api/v1/knowledge-items
```

### 5. 测试数据重建注意事项

第八阶段 v1 不提供正式 reparse API。已有 chunks 的文档默认返回 `DOCUMENT_ALREADY_PARSED`。

如果本地开发库只有测试数据，且需要重新解析同一测试文档，必须先由用户确认并手动清理测试数据。不要提供或运行自动删除生产数据的脚本。清理前应确认：

- 当前是本地开发库。
- 目标 document_id 是测试数据。
- 相关 knowledge_items 也是测试数据，或没有需要保留的审核知识。
- 不删除 MinIO 原始上传文件。
- 不删除 PostgreSQL volume。
- 不删除 OpenSearch volume。

生产或正式数据不应直接删除 chunks，尤其不能删除已经被 `knowledge_item_chunks` 引用的 chunks。

## 第九阶段：Local/API LLM Provider 本地开发

第九阶段只改造 LLM generation。Embedding 继续固定为本地 `Qwen3-Embedding-0.6B`、1024 维；切换 LLM Provider 不会自动解析、embedding、同步或重建 OpenSearch。

### 1. Provider 选择与启动行为

正式配置值只有：

```text
LLM_PROVIDER=local
LLM_PROVIDER=api
```

`openai_compatible` 仍可作为 deprecated Local alias 使用一个兼容阶段；它会规范化为 `local` 并每进程警告一次。新配置不要继续使用该别名。

后端启动时，`create_app()` 会在导入 API router 前调用 `validate_active_llm_configuration()`。Factory 构造 Provider 时还会再次校验。只校验当前 active Provider：

- Local 不读取或要求 remote key/model/base URL；
- API 不读取 Local URL/model/key，也不 fallback 到 Local；
- 应用启动只校验配置，不提前创建 Provider、transport 或 OpenAI SDK client；
- Provider/client 在第一次真实生成时惰性创建并在进程内复用；
- FastAPI shutdown 会从缓存摘除 Provider，并在锁外幂等关闭 client；
- 修改 `.env` 后必须完整重启后端才能切换 Provider。

### 2. Local/Ollama 配置

以下为非秘密示例；模型名按本机 `ollama list` 的实际结果修改：

```env
LLM_PROVIDER=local
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=120
```

Local capability：

| Capability | 状态 |
|---|---:|
| system/user/assistant text | 支持 |
| JSON mode | 支持 |
| Ollama `think` | 支持，通过 `extra_body` |
| tools | 不支持，网络前拒绝 |
| parallel tool calls | 不支持，网络前拒绝 |
| image input | 不支持，网络前拒绝 |

`LLM_API_KEY` 为空时使用非秘密占位值 `ollama`。Knowledge extraction 会请求 `json_mode=True`、`think=False`；普通 RAG 不强制 JSON mode。

### 3. Generic OpenAI-compatible API 配置

以下域名、模型和空 key 都是假值：

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

说明：

- 真实 key 只写入本机 `backend/.env`，不得提交、复制到 issue、日志或测试快照；
- Remote JSON capability 默认 `false`，不会仅因服务“OpenAI-compatible”就假定支持；
- 普通 RAG 可以使用不支持 JSON mode 的 API；
- knowledge extraction 要求 JSON object。确认供应商真实兼容 `response_format={"type":"json_object"}` 后，才设置 `LLM_REMOTE_SUPPORTS_JSON_MODE=true`；
- capability 为 false 时，knowledge extraction 返回 `LLM_PARAMETER_UNSUPPORTED`，且不会发送网络请求；
- Generic API 忽略 advisory `think=True/False`，不发送 `think` 或 `extra_body`；`think_required=True` 会在网络前拒绝；
- SDK 使用 `max_retries=0`，系统不自动 retry，也不做 API→Local fallback。

### 4. Remote URL 安全规则

`LLM_REMOTE_BASE_URL` 必须满足：

- HTTPS 默认允许；
- HTTP 只对 `localhost`、`127.0.0.1`、`[::1]` 默认允许；
- 其他 HTTP 需要显式设置 `LLM_REMOTE_ALLOW_INSECURE_HTTP=true`；
- 非 loopback HTTP 会每进程警告一次，日志只含 `provider=api`、`scheme=http`、hostname 和事件名，不含完整 URL；
- 拒绝 userinfo，例如 `https://user:pass@example.invalid/v1`；
- 拒绝 query、fragment、缺失 host 和非 HTTP(S) scheme；
- 即使显式允许 insecure HTTP，也必须理解明文传输 API key 和消息内容的风险，正式远程服务应使用 HTTPS。

### 5. 密钥、错误与日志边界

- Remote key 在 Pydantic Settings 和 transport 中保持 `SecretStr`；
- 配置校验只在局部作用域短暂读取明文判断非空，不保存、不返回、不记录；
- SDK client 惰性创建时才再次读取并直接交给 client factory；
- Python 不能提供可验证的物理内存擦除，项目不作此类承诺；
- 错误、日志和 traceback 不包含 key、Authorization、完整 URL、messages/prompt、tool arguments/schema、image URL、上游 body 或 headers；
- 安全日志可包含 provider、model、operation、latency、usage token count、受控 request ID、error code、upstream status、capability、retryable 和脱敏 hostname；
- 429 返回 `LLM_RATE_LIMITED` 和 `retryable=true`，但不会自动重试。

### 6. 当前内部消息能力与 REST 边界

内部 `LLMGenerateRequest.messages` 支持 system/user/assistant 文本历史并保持原顺序。`LLMGenerateResult` 返回完整 assistant message，`result.text` 返回唯一文本 part。

Phase 9 没有开放 messages/history REST 字段：

- `/api/v1/rag/ask` 仍只接收 `question`、`limit`、`document_id`；
- `/api/v1/knowledge-items/extract` schema 不变；
- no-context 仍是 HTTP 200，并通过 metadata resolver 返回规范化 Provider/model，不创建 LLM client；
- 不存在对外 Chat API、会话持久化、多轮 RAG、LangChain/LangGraph、Agent/tool execution 或多模态传输。

### 7. 自动化验证命令

Backend 全量：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase9-m5-final-pytest-tmp
```

Phase 9 聚焦集：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe `
  tests/test_llm_messages.py `
  tests/test_llm_provider.py `
  tests/test_llm_config.py `
  tests/test_llm_startup.py `
  tests/test_api_llm_provider.py `
  tests/test_rag_service.py `
  tests/test_rag_api.py `
  tests/test_knowledge_extraction.py `
  tests/test_knowledge_item_api.py `
  -q -p no:cacheprovider
```

Frontend 当前没有 test script。正式命令与 Phase 9 文件定向 lint：

```powershell
cd D:\rag_system\frontend
npm.cmd run build
npm.cmd run lint
npx.cmd eslint lib/rag.ts lib/knowledge-items.ts
```

全量 build/ESLint 仍分别受既有 `KnowledgeItemsPanel.tsx` TypeScript 债务和 `DocumentParseResults.tsx` ESLint 债务影响；这两个文件不属于 Phase 9 diff，不应在 M5 顺带修复。

# 第十阶段：Document Hard Delete 本地开发与 rollout preflight

## 默认安全状态

开发、测试和 CI 默认必须保持：

```env
DOCUMENT_DELETION_EXECUTOR_ENABLED=false
```

disabled 时 DELETE/retry 对合法 Document↔job invariant 返回 `DOCUMENT_DELETION_EXECUTOR_DISABLED` / HTTP 503，不创建或重置 job，不改变 Document；status API 仍可读取已有状态。不得为了测试前端而在共享开发环境启用 executor。

## M7 真实集成双重门禁

真实集成测试默认全部 skip。只有项目负责人已经单独给出 rollout 授权，并在当前 shell 同时设置下列开关与全部 explicit confirmation 时才可运行：

```env
PHASE10_INTEGRATION_ENABLED=1
PHASE10_M7_ROLLOUT_AUTHORIZED=1
PHASE10_INTEGRATION_ENVIRONMENT=dedicated-local-test
```

还必须显式提供两个不同的 `phase10_*` PostgreSQL database URL/name confirmation、一个名称以 `phase10-` 开头且已启用 versioning 的专用 MinIO bucket 及 confirmation、专用 OpenSearch URL/index/alias 及 `index:alias` confirmation。测试 harness 没有默认 URL、bucket 或 index，不会 fallback 到 `rag_system`、`rag-documents` 或 `casting_chunks_current`。真实值只放本机未跟踪环境，不得提交。

所有 fixture 名称以 `phase10-hard-delete-<UUID>` 开头。测试 API 只能删除本次 factory 返回的 UUID，不能接收既有知识库 Document ID。

## M7-A 非写 preflight

以下命令只检查 Git、migration graph、offline SQL 与默认 skip，不执行 migration 或外部删除：

```powershell
cd D:\rag_system
git status --short
git diff --check

cd backend
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\alembic.exe history
.\.venv\Scripts\alembic.exe upgrade 0006_add_document_parse:0008_phase10_enforce --sql
.\.venv\Scripts\alembic.exe downgrade 0008_phase10_enforce:0007_phase10_expand --sql
.\.venv\Scripts\pytest.exe -m integration tests\integration -q -p no:cacheprovider
```

最后一条在没有双重门禁时必须全部 skip。`alembic current` 会连接配置的数据库，只允许在已经确认目标为专用测试数据库或使用项目允许的只读开发核验时执行；不得把 `alembic current` 当成 upgrade 授权。

## M7-B rollout gate（当前禁止执行）

只有收到精确授权 `PHASE10_M7_ROLLOUT_AUTHORIZED` 后才按顺序执行：

1. 停止后端写流量，确认 executor=false；
2. 记录 PostgreSQL backup 与已验证 restore 命令/位置，不在测试中自动 drop/restore；
3. 核验 `alembic current`、`heads`、migration chain 和 0007/0008 offline SQL；
4. 核验专用 MinIO bucket、versioning、OpenSearch alias/concrete index/mapping 1024 dimension；
5. 先在独立 migration database 验证 upgrade/downgrade，再对批准目标应用 migration；
6. executor=false 启动，验证 guards/API；
7. executor=true 后完整重启；
8. 只创建并删除唯一 `phase10-hard-delete-<UUID>` fixture；
9. 比较 PostgreSQL/MinIO/OpenSearch pre/post snapshot，验证目标 0 residual 与其他集合完全相同；
10. 任一 invariant、mapping checksum、alias、backup、确认值或外部服务状态不符，立即 abort，不猜测修复。

禁止清 bucket、删除 index、rebuild index、truncate、清 volume 或用字符串模糊匹配选择待删数据。
