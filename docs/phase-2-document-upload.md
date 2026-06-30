# 第二阶段：文档上传与基础知识库入库闭环

本文记录 `D:\rag_system` 第二阶段实施设计。第二阶段只打通“前端上传文件 -> FastAPI 接收上传请求 -> 后端基础校验 -> 原始文件保存到 MinIO -> documents 表写入元数据 -> 后端提供列表和详情接口 -> 前端展示列表和详情”的最小闭环。

## 1. 阶段目标

第二阶段实现文档上传与基础知识库入库闭环：

- 前端提供 `/documents` 文档管理页面。
- 前端提供 `/documents/[id]` 文档详情页。
- FastAPI 提供文档上传、文档列表和文档详情接口。
- 后端保存原始文件到 MinIO `rag-documents` bucket。
- 后端在 MinIO 上传成功后写入 `documents` 表。
- 前端展示文档名称、类型、大小、处理状态、上传时间和详情信息。

第二阶段继续使用本机 FastAPI、本机 Next.js、Docker 基础服务的开发架构。FastAPI 和 Next.js 不放入 Docker Compose。

## 2. 接口设计

新增文档业务接口统一使用以下响应结构：

```json
{
  "success": true,
  "data": {},
  "error": null
}
```

错误响应结构：

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "DOCUMENT_UPLOAD_FAILED",
    "message": "文件上传失败",
    "detail": null
  }
}
```

新增接口：

- `POST /api/v1/documents`
  - 请求类型：`multipart/form-data`
  - 文件字段名：`file`
  - 成功状态码：`201`
  - 用途：上传单个原始文档并写入 `documents` 元数据。
- `GET /api/v1/documents?limit=20&offset=0`
  - 用途：查询文档列表。
  - 默认排序：`created_at desc`。
- `GET /api/v1/documents/{document_id}`
  - 用途：查询单个文档详情。

以下第一阶段健康检查接口必须保持原响应结构，不改为统一响应结构：

- `GET /health`
- `GET /api/v1/health`
- `GET /api/v1/health/services`

这样可以避免破坏前端现有 `ServiceStatus` 组件。

## 3. documents 字段需求

第二阶段上传闭环需要 `documents` 表具备以下字段：

- `original_filename`
- `bucket_name`
- `object_key`
- `file_type`
- `mime_type`
- `file_size`
- `file_hash`
- `process_status`
- `error_message`
- `created_at`
- `updated_at`

Step 0 核对结果：

- 现有 `backend/app/models/document.py` 已包含：`original_filename`、`object_key`、`file_type`、`file_size`、`process_status`、`created_at`、`updated_at`。
- 现有 `backend/alembic/versions/0001_initial_schema.py` 的 `documents` 表已包含：`original_filename`、`object_key`、`file_type`、`file_size`、`process_status`、`created_at`、`updated_at`。
- `file_size` 当前为 `BigInteger`，满足第二阶段文件大小记录需求。
- 当前缺少：`bucket_name`、`mime_type`、`file_hash`、`error_message`。

因此第二阶段需要后续 Step 2 新增普通 Alembic 迁移，只补充 `bucket_name`、`mime_type`、`file_hash`、`error_message` 四个普通元数据字段。

该迁移不得：

- 修改 `process_status` 的 ORM 默认值或数据库默认值。
- 为第二阶段过度修改 `file_size` 等无关字段。
- 新增真实 `vector` 字段。
- 创建 HNSW、IVFFlat 或其他向量索引。
- 修改第一阶段已完成表结构之外的无关内容。

## 4. 状态设计

第二阶段 `documents.process_status` 只实际写入一个成功状态：

- `uploaded`：原始文件已成功保存到 MinIO，且 `documents` 表元数据已成功写入。

预留状态：

- `upload_failed`：仅作为后续扩展预留。本阶段 MinIO 上传失败时直接返回统一错误，不写入 `documents` 表。

第二阶段不修改 `process_status` 的 ORM 默认值或数据库默认值。上传服务在成功创建 `Document` 时由 service 层显式写入 `process_status="uploaded"`。

第二阶段暂不使用以下解析相关状态：

- `pending`
- `uploading`
- `pending_parse`
- `parsing`
- `parsed`
- `parse_failed`

## 5. 上传顺序

第二阶段采用简化流程：

1. 校验文件扩展名。
2. 辅助校验 `content_type`。
3. 一次性读取文件内容。
4. 校验文件大小。
5. 计算 `file_size`。
6. 计算 SHA-256 `file_hash`。
7. 生成 `document_id`。
8. 生成 MinIO `object_key`。
9. 上传原始文件到 MinIO。
10. MinIO 上传成功后写入 `documents` 表，显式写入 `process_status="uploaded"`。
11. MinIO 上传失败时直接返回统一错误响应，不写入 `documents` 表。

第二阶段不设计复杂事务补偿机制，不自动删除 MinIO 对象，不清空 bucket，不删除已有数据。

## 6. MinIO object key 规则

默认 object key 规则：

```text
raw/YYYY/MM/<document_id><original_extension>
```

示例：

```text
raw/2026/06/550e8400-e29b-41d4-a716-446655440000.pdf
```

规则说明：

- `raw/` 表示原始上传文件区域。
- `YYYY/MM` 用于按上传时间分组。
- `<document_id>` 使用后端生成的文档 UUID。
- `<original_extension>` 保留原始扩展名的小写形式。

详情页展示 `object_key` 仅用于本地开发调试；后续生产界面可隐藏。

## 7. 文件类型与大小限制

默认最大上传文件大小为 `50MB`，可通过环境变量覆盖：

```text
UPLOAD_MAX_FILE_SIZE_BYTES=52428800
```

第二阶段允许一次性读取文件内容，用于：

- 计算 `file_size`
- 计算 SHA-256 `file_hash`
- 上传到 MinIO

暂不实现流式上传。后续如需支持大文件，再单独设计流式上传方案。

默认允许扩展名：

- `.pdf`
- `.doc`
- `.docx`
- `.xls`
- `.xlsx`
- `.png`
- `.jpg`
- `.jpeg`
- `.bmp`
- `.tif`
- `.tiff`
- `.webp`

第二阶段主要依赖扩展名白名单和文件大小限制。`content_type` 仅作为辅助校验，不作为强安全判断；不做深度内容识别，暂不引入 `python-magic` 或文件解析器校验。

## 8. 前端范围

第二阶段前端只实现：

- `/documents` 文档管理页面。
- `/documents/[id]` 文档详情页。
- 文档上传表单。
- 文档列表表格。
- 文档详情展示组件。
- `AppShell` 中“文档管理”入口改为真实链接。

其他入口继续保留为占位，不新增知识库管理、知识条目库、智能问答、系统状态等页面。

第二阶段不提供：

- 文件下载
- 文件预览

## 9. 后端配置要求

后续实现时需要确认并补充：

- `backend/pyproject.toml` 中是否已有 `minio` 依赖；如没有，则新增。
- 新增 `python-multipart` 以支持 FastAPI multipart 上传。
- 不默认改用 `boto3`。
- 如需要 CORS，只允许：
  - `http://localhost:3000`
  - `http://127.0.0.1:3000`
- 不允许使用 `allow_origins=["*"]`。

## 10. 安全边界

第二阶段必须继续遵守第一阶段安全规则。

禁止执行或写入危险命令：

- `docker compose down -v`
- `docker volume rm`
- `docker volume prune`
- `docker system prune --volumes`
- 清空或删除 MinIO bucket
- 删除 MinIO 已有对象
- `DROP DATABASE`
- `DROP TABLE`
- `TRUNCATE`
- 无 `WHERE` 条件的 `DELETE`
- `npm audit fix --force`
- 重建或覆盖 `frontend`

所有 Docker Compose 命令必须由用户手动从项目根目录执行，并统一使用：

```powershell
docker compose --env-file .env -f infra/docker-compose.yml ...
```

如 Step 2 创建迁移文件，`alembic upgrade head` 也必须由用户手动执行，Codex 不得自动执行。

## 11. 测试计划

自动测试重点：

- 文档上传接口成功返回 `201`，响应为统一结构。
- 非白名单文件类型返回统一错误。
- 超过大小限制返回统一错误。
- 文档列表接口返回统一结构。
- 文档详情接口成功返回。
- 不存在的文档 ID 返回统一 `404`。
- `GET /health`、`GET /api/v1/health`、`GET /api/v1/health/services` 保持第一阶段原响应结构。

测试策略保持轻量，不一次性引入复杂测试夹具或大规模 mock 体系。

手动验收重点：

- 用户手动启动 Docker 基础服务。
- 用户手动确认 MinIO bucket `rag-documents` 已存在。
- 用户手动启动 FastAPI。
- 用户手动启动 Next.js。
- 上传 PDF、Word、Excel、图片各 1 个。
- 确认 MinIO 中出现对象。
- 确认前端文档列表出现记录。
- 确认文档详情页可打开。

## 12. 验收标准

第二阶段完成时应满足：

- `/documents` 页面可上传文件并展示文档列表。
- `/documents/[id]` 页面可展示单个文档详情。
- `POST /api/v1/documents` 能校验并上传文件到 MinIO。
- MinIO 上传成功后才写入 `documents` 表。
- `documents.process_status` 成功记录由 service 层显式写入 `uploaded`。
- 第二阶段不修改 `process_status` 的 ORM 默认值或数据库默认值。
- 文档元数据包含文件名、bucket、object key、类型、MIME、大小、hash、状态、上传时间、更新时间。
- 新增文档业务接口返回统一响应结构。
- 健康检查接口保持第一阶段原响应结构。
- CORS 不使用 wildcard，只允许本地 Next.js 开发地址。
- 不删除 MinIO 对象，不清空 bucket，不删除已有数据。
- 不新增真实 vector 字段，不创建向量索引。
- 本机 FastAPI、本机 Next.js、Docker 基础服务架构保持不变。

## 13. 第二阶段明确不实现

第二阶段不得实现：

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
- 生产级权限系统
- 文件下载
- 文件预览
- 大文件流式上传
- 深度文件内容识别
