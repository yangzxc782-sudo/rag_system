# 铸型工艺知识库 RAG 系统

本项目是“铸型工艺知识库大型 RAG 系统”。第一阶段目标是建立可持续扩展的本地开发骨架，不实现具体业务功能。

## 第一阶段范围

- FastAPI 后端骨架，后续在 Windows 本机运行。
- Next.js 前端骨架，后续在 Windows 本机运行。
- Docker 基础服务后续只包含 PostgreSQL + pgvector、Redis、MinIO。
- PostgreSQL 初始数据表设计与 Alembic 迁移基础。
- 根目录、后端、前端的环境变量示例文件。
- 基础健康检查接口和本地验证说明。

第一阶段暂不实现 RAG 检索、问答链路、模型调用、Celery、Neo4j、Elasticsearch 或生产级权限系统。

## Docker 基础服务

Docker Compose 配置固定放在 `infra/docker-compose.yml`。后续所有 Docker Compose 命令都必须从项目根目录执行，并统一使用显式环境文件和 Compose 文件：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml config
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
docker compose --env-file .env -f infra/docker-compose.yml logs postgres
docker compose --env-file .env -f infra/docker-compose.yml logs redis
docker compose --env-file .env -f infra/docker-compose.yml logs minio
```

后续 Compose 服务范围只包括：

- PostgreSQL + pgvector：本机端口默认 `5432`。
- Redis：本机端口默认 `6379`。
- MinIO API：本机端口默认 `9000`。
- MinIO Console：本机端口默认 `9001`。

默认数据库配置统一为：

- `POSTGRES_DB=rag_system`
- `POSTGRES_USER=rag_user`
- `POSTGRES_PASSWORD=rag_password`

pgvector 第一阶段只作为第二阶段向量检索的基础设施准备。第一阶段业务表不创建真实向量字段，也不创建向量索引。

## 后端启动说明

FastAPI 后端后续在 `backend/` 目录中本机运行，不放入 Docker Compose。后端本机连接 PostgreSQL 时使用 `localhost:5432`；未来后端容器化后，才使用 Compose service name。

后续本机开发命令示例：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
alembic upgrade head
pytest
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`uvicorn` 是前台长期运行命令。人工验证完成后，可使用 Ctrl+C 停止。

## 前端启动说明

Next.js 前端后续在 `frontend/` 目录中本机运行，不放入 Docker Compose。前端只通过 `NEXT_PUBLIC_API_BASE_URL` 访问 FastAPI，不直接连接 PostgreSQL、Redis 或 MinIO。

优先由用户手动执行 `create-next-app` 创建 `frontend`。执行前必须确认 `D:\rag_system\frontend` 不存在或为空；如果目录已存在且非空，不覆盖、不清理，先确认处理方式。

后续本机开发命令示例：

```powershell
cd frontend
copy .env.local.example .env.local
npm install
npm run dev
```

`npm run dev` 是前台长期运行命令。人工验证完成后，可使用 Ctrl+C 停止。

## MinIO bucket 手动创建

第一阶段不在 Docker Compose 中自动创建 bucket。基础服务启动后，用户手动打开 `http://localhost:9001`，使用 `.env` 中的 MinIO 本地占位账号登录，并创建 bucket：

- `rag-documents`

MinIO 本地开发默认登录账号统一为：

- `MINIO_ROOT_USER=rag_minio`
- `MINIO_ROOT_PASSWORD=rag_minio_password`

健康检查需要区分 MinIO 服务可达性和 `rag-documents` bucket 是否存在。MinIO 服务不可达应报告失败；bucket 不存在只报告 warning，不影响后端启动。

## 健康检查接口

第一阶段后续预留以下接口：

- `GET /health`：检查 FastAPI 应用进程。
- `GET /api/v1/health`：检查 API v1 应用状态。
- `GET /api/v1/health/services`：检查 PostgreSQL、Redis、MinIO 服务状态。

依赖服务异常不应阻止 FastAPI 应用启动。`/api/v1/health/services` 只负责报告依赖状态。

## 禁止操作说明

在没有用户单独、明确授权且没有备份的情况下，禁止执行会删除 Docker volume、清空对象存储、清空数据库、删除数据库结构、丢弃本地改动或重建持久化数据的操作。

第一阶段 README 和文档只记录安全的本地开发命令。涉及环境重置、数据清理或不可恢复操作时，必须先说明影响范围、备份方式和恢复路径，并等待用户明确确认。
