# 第一阶段验收标准

本文记录 `D:\rag_system` 第一阶段“项目骨架”的验收标准。第一阶段只验证项目结构、配置示例、基础设施计划和本地开发说明，不验证具体 RAG 业务能力。

## 根目录基础文件

- 根目录存在 `.gitignore`。
- 根目录存在 `.editorconfig`。
- 根目录存在 `.env.example`。
- 根目录存在 `README.md`。
- 存在 `docs/phase-1-acceptance.md`。
- 示例环境变量文件只包含本地开发占位值，不包含真实密钥。

## Docker 基础服务

- 后续 `infra/docker-compose.yml` 只包含 PostgreSQL + pgvector、Redis、MinIO。
- 后续 Compose 项目名固定为 `rag_system`。
- 后续所有 Docker Compose 命令统一使用 `docker compose --env-file .env -f infra/docker-compose.yml ...`。
- PostgreSQL 默认数据库配置统一为 `rag_system`、`rag_user`、`rag_password`。
- pgvector 第一阶段只作为第二阶段向量检索准备。
- MinIO bucket `rag-documents` 第一阶段由用户手动创建。

## 后端骨架

- FastAPI 后端后续在 Windows 本机运行，不放入 Docker Compose。
- 后端本机访问 PostgreSQL 使用 `localhost:5432`。
- 后端后续提供 `/health`、`/api/v1/health`、`/api/v1/health/services`。
- 服务依赖不可用时，不影响 FastAPI 应用启动。
- MinIO bucket 不存在时，服务健康检查只返回 warning。

## 前端骨架

- Next.js 前端后续在 Windows 本机运行，不放入 Docker Compose。
- 前端优先由用户手动执行 `create-next-app` 创建。
- 执行脚手架前必须确认 `frontend` 目录不存在或为空。
- 前端只通过 `NEXT_PUBLIC_API_BASE_URL` 访问 FastAPI。
- 前端不得直接连接 PostgreSQL、Redis 或 MinIO。

## 数据库与业务范围

- 第一阶段后续至少设计 documents、document_chunks、knowledge_entries、entry_versions、entry_review_records、qa_sessions、qa_messages、retrieval_logs。
- `knowledge_entries.review_status` 默认值为 `pending_review`。
- 第一阶段不创建真实 vector 字段。
- 第一阶段不创建向量索引。
- 第一阶段不实现 RAG 检索、问答链路、模型调用、Celery、Neo4j、Elasticsearch。

## 安全验收

- README 和 docs 只包含安全的本地开发说明。
- 不包含会删除 Docker volume、清空对象存储、清空数据库、删除数据库结构或丢弃本地改动的命令。
- 不启动服务作为验收的一部分。
- 不执行 Docker、Git、npm、pip、alembic 命令作为本次 Step 1 的一部分。
