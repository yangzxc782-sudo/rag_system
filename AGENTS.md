# AGENTS.md

本文件是 `D:\rag_system` 项目的 Agent 协作规范。所有自动化助手、脚本生成和后续开发任务都必须优先遵守本文件。

## 1. 项目定位

本项目是“铸型工艺知识库大型 RAG 系统”。第一阶段目标是完成可持续扩展的项目骨架，不实现具体业务功能。

第一阶段范围包括：

- FastAPI 后端骨架，本机运行；
- Next.js 前端骨架，本机运行；
- PostgreSQL + pgvector、Redis、MinIO 基础服务，运行在 Docker 容器中；
- PostgreSQL 数据表设计与迁移基础；
- Docker Compose 基础服务配置。

第一阶段暂不配置：

- Neo4j；
- Elasticsearch；
- 模型服务容器；
- Celery Worker 容器；
- 复杂业务流程、检索链路、问答链路或生产级权限系统。

## 2. 目录结构规范

推荐项目根目录结构如下。创建文件或目录时，应尽量保持该结构清晰稳定。

```text
D:\rag_system
├─ AGENTS.md
├─ .env.example
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
│  │  ├─ tasks/
│  │  └─ main.py
│  ├─ tests/
│  ├─ alembic/
│  ├─ pyproject.toml
│  └─ .env.example
├─ frontend/
│  ├─ app/
│  ├─ components/
│  ├─ lib/
│  ├─ styles/
│  ├─ public/
│  ├─ package.json
│  └─ .env.local.example
├─ infra/
│  ├─ docker-compose.yml
│  ├─ postgres/
│  │  └─ init/
│  └─ minio/
├─ docs/
└─ scripts/
```

目录使用规则：

- `backend/` 只放后端应用、后端测试、后端迁移与后端配置示例。
- `frontend/` 只放前端应用、前端组件、前端静态资源与前端配置示例。
- `infra/` 只放本地基础设施配置，例如 Docker Compose、数据库初始化脚本、MinIO 初始化说明。
- `docs/` 放架构说明、数据表设计说明、接口草案和阶段验收记录。
- `scripts/` 放可重复执行的辅助脚本，脚本必须默认安全，不得执行清库、删卷、删对象存储等危险操作。
- 不要把后端、前端、数据库脚本混放在项目根目录。

环境变量示例文件规则：

- 根目录 `.env.example` 用于说明项目级本地基础设施变量，例如 PostgreSQL、Redis、MinIO 的端口、用户名、密码、bucket 名称，以及 Docker Compose 会读取的变量。
- `backend/.env.example` 用于说明 FastAPI 后端运行所需变量，例如 `DATABASE_URL`、`REDIS_URL`、MinIO endpoint、Access Key、Secret Key 和后端服务配置。
- `frontend/.env.local.example` 用于说明 Next.js 本机开发变量，只允许包含前端运行需要的变量，例如 `NEXT_PUBLIC_API_BASE_URL`。
- `frontend/.env.local.example` 中不得出现数据库密码、MinIO Secret Key、Redis 密码等服务端密钥。
- 示例文件只能放占位值，不得写入真实密钥或真实生产连接信息。

## 3. 后端代码规范

后端采用 FastAPI，第一阶段只建立骨架和基础连接能力。

后端开发规则：

- 使用 Python 3.11 或更高版本。
- FastAPI 应用入口放在 `backend/app/main.py`。
- API 路由放在 `backend/app/api/`，建议使用 `/api/v1` 作为统一前缀。
- 配置管理放在 `backend/app/core/`，通过环境变量和 `.env` 加载，不允许硬编码数据库密码、Access Key、Secret Key。
- 数据库连接、会话、迁移相关代码放在 `backend/app/db/`。
- ORM 模型放在 `backend/app/models/`。
- Pydantic 请求和响应模型放在 `backend/app/schemas/`。
- 业务编排逻辑放在 `backend/app/services/`，路由层不得堆积复杂业务逻辑。
- 数据库访问应通过统一 session 或 repository/service 层完成，不要在任意文件中临时创建数据库连接。
- 第一阶段优先使用同步 SQLAlchemy 2.x、Alembic、Pydantic v2。
- 新增接口时必须考虑基础异常处理、日志记录和类型标注。
- 测试放在 `backend/tests/`，推荐使用 `pytest`。

第一阶段后端验收重点是“能启动、结构清楚、配置可读、能连接基础服务”，不是实现完整 RAG 业务。

## 4. 前端代码规范

前端采用 Next.js，第一阶段只建立可运行骨架和基础页面结构。

前端开发规则：

- 使用 Next.js App Router。
- 使用 TypeScript。
- 页面放在 `frontend/app/`。
- 可复用组件放在 `frontend/components/`。
- API 客户端、工具函数、类型辅助放在 `frontend/lib/`。
- 样式文件放在 `frontend/styles/`，或遵循项目后续选定的样式方案。
- 静态资源放在 `frontend/public/`。
- 前端通过 `NEXT_PUBLIC_API_BASE_URL` 访问本机 FastAPI 后端。
- 前端不得直接连接 PostgreSQL、Redis 或 MinIO。
- 未明确技术选型前，不要引入大型 UI 框架、状态管理库或复杂可视化依赖。
- 页面文案、组件命名和目录命名应围绕“铸型工艺知识库”和“RAG 管理系统”保持一致。

第一阶段前端验收重点是“能启动、能展示基础壳、能预留后端 API 对接位置”，不是实现复杂交互。

## 5. Docker 使用规则

第一阶段 Docker 只用于本地基础服务：

- PostgreSQL + pgvector；
- Redis；
- MinIO。

Docker 使用规则：

- FastAPI 后端本机运行，不放入 Docker Compose。
- Next.js 前端本机运行，不放入 Docker Compose。
- 暂时不要在 Docker Compose 中加入 Neo4j、Elasticsearch、模型服务、Celery Worker。
- Docker Compose 配置固定放在 `infra/docker-compose.yml`。
- 所有 Docker Compose 命令必须从项目根目录执行，并显式使用 `-f infra/docker-compose.yml`。
- 数据服务必须使用持久化 volume。
- 服务端口、用户名、密码、bucket 名称等应通过 `.env` 或 `.env.example` 说明。
- 常用安全命令示例包括：
  - `docker compose -f infra/docker-compose.yml up -d`
  - `docker compose -f infra/docker-compose.yml ps`
  - `docker compose -f infra/docker-compose.yml logs`
  - `docker compose -f infra/docker-compose.yml stop`
  - `docker compose -f infra/docker-compose.yml restart`
- 修改 Docker Compose 前，必须确认不会导致已有 volume 被删除或重建。
- 第一阶段默认 MinIO bucket 名称为 `rag-documents`。
- 不得删除 MinIO bucket，不得清空 bucket 数据。

MinIO 用于保存：

- 原始上传文档；
- 解析后的 Markdown；
- 表格 JSON；
- 图片资源；
- 后续 OCR 或文档解析中间结果。

## 6. 数据库连接规则

PostgreSQL 运行在 Docker 容器中，FastAPI 后端从本机连接 PostgreSQL。

数据库连接规则：

- 后端本机连接 PostgreSQL 时，host 使用 `localhost` 或 `127.0.0.1`。
- 容器内部服务互联时，才使用 Docker Compose service name。
- 数据库连接字符串必须来自环境变量，例如 `DATABASE_URL`。
- 第一阶段优先使用同步 SQLAlchemy 2.x session，不使用异步 session。
- 数据库连接格式使用 `postgresql+psycopg://<user>:<password>@localhost:<port>/<database>`。
- 不要混用 `asyncpg` 连接字符串和同步 SQLAlchemy session；除非后续阶段明确切换到异步数据库访问，否则不要引入 `postgresql+asyncpg://`。
- pgvector 扩展必须通过初始化脚本或 Alembic migration 显式创建。
- 所有表结构变更必须通过迁移脚本管理，不得手工随意修改生产或共享数据库。
- 迁移脚本必须可读、可回滚或至少有清晰的恢复说明。
- 初始化数据只能放入明确标记的 seed 脚本，不得与结构迁移混在一起。
- 文档、分块、向量、任务状态等核心表应在 `docs/` 中先有设计说明，再落到迁移脚本。

## 7.第一阶段初始数据表

第一阶段至少预留以下 ORM 模型和迁移：

- documents：原始文档元数据；
- document_chunks：文档切片；
- knowledge_entries：知识条目；
- entry_versions：知识条目版本；
- entry_review_records：专家审核记录；
- qa_sessions：问答会话；
- qa_messages：问答消息；
- retrieval_logs：检索日志。

自动抽取的知识条目默认状态必须是 `pending_review`。

所有知识条目必须预留以下字段：

- source_document_id
- source_chunk_id
- source_text
- confidence
- review_status
- version
- created_at
- updated_at

## 8. 禁止执行的危险命令

任何 Agent、脚本或开发者在本项目中都禁止执行以下危险操作，除非用户单独、明确、逐条授权，并且已经完成备份。

Docker 禁止命令：

- `docker compose down -v`
- `docker compose -f infra/docker-compose.yml down -v`
- `docker volume rm ...`
- `docker volume prune`
- `docker system prune --volumes`
- 删除 PostgreSQL、Redis、MinIO 相关 volume 的任何命令
- 删除 MinIO 数据目录或 bucket 数据的任何命令

数据库禁止命令：

- `DROP DATABASE`
- `DROP TABLE`
- 未经确认的 `TRUNCATE`
- 未经确认且无 `WHERE` 条件的 `DELETE`
- 任何会清空 schema、表、索引、扩展或迁移记录的命令

文件系统禁止操作：

- 删除整个项目目录；
- 删除 `infra/` 下的数据挂载目录；
- 删除数据库、Redis、MinIO 的持久化数据；
- 使用不受控的递归删除命令清理项目。

Git 禁止操作：

- 未经用户明确要求，不得执行 `git reset --hard`。
- 未经用户明确要求，不得执行会丢弃本地改动的 checkout、clean 或 rebase 操作。

如果确实需要重置环境，必须先说明影响范围、备份方式和可恢复路径，并等待用户明确确认。

## 9. 第一阶段验收标准

第一阶段完成时，应满足以下标准：

- 项目根目录结构清晰，后端、前端、基础设施、文档分层明确。
- FastAPI 后端可在本机启动，并提供基础健康检查能力。
- Next.js 前端可在本机启动，并能展示基础应用壳。
- Docker Compose 可启动 PostgreSQL + pgvector、Redis、MinIO。
- Docker Compose 不包含 Neo4j、Elasticsearch、模型服务、Celery Worker。
- PostgreSQL 数据库连接配置通过环境变量管理。
- PostgreSQL 表结构已有清晰设计，并通过 Alembic 或 SQL 初始化脚本管理。
- pgvector 扩展有明确启用方式。
- Redis 和 MinIO 有基础连接配置说明。
- `.env.example` 或等价配置示例完整，不包含真实密钥。
- README 或 `docs/` 中有本地启动说明。
- 没有执行业务代码实现、复杂检索逻辑、模型调用逻辑或生产级部署配置。
- 没有执行 `docker compose down -v`，没有删除 volume，没有执行 `DROP DATABASE` 或 `DROP TABLE`。
