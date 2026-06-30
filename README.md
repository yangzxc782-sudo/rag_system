# 铸型工艺知识库 RAG 管理系统

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

第一阶段暂不配置或实现：

- Neo4j。
- Elasticsearch。
- Celery Worker。
- 模型服务容器。
- RAG 检索链路。
- 问答链路。
- 知识抽取。
- 文件上传。
- 生产级权限系统。

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
- 第一阶段 bucket 由用户手动创建。
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

第一阶段 PostgreSQL 容器启用 pgvector 扩展，仅作为第二阶段向量检索准备。第一阶段业务表不创建真实 `vector` 字段，不创建 HNSW、IVFFlat 或其他向量索引。embedding 只保留 `embedding_model`、`embedding_dim`、`embedding_status` 等元数据字段。

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

## 第一阶段验收标准

- 根目录基础文件存在，环境变量示例完整且不包含真实密钥。
- Docker Compose 后续只包含 PostgreSQL + pgvector、Redis、MinIO。
- FastAPI 后端可在 Windows 本机运行。
- Next.js 前端可在 Windows 本机运行。
- 后端本机访问 PostgreSQL 使用 `localhost:5432`。
- Alembic 首个迁移创建 8 张基础表。
- 自动抽取的知识条目默认进入 `pending_review`。
- 不实现 RAG 检索、问答链路、知识抽取、文件上传、Celery、Neo4j、Elasticsearch、模型调用或生产级权限系统。
- 不创建真实 `vector` 字段，不创建向量索引。

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
