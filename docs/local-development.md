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
