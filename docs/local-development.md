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
