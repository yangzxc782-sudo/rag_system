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
