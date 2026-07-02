# 第三阶段：文档解析适配与基础切片可视化闭环

本文记录 `D:\rag_system` 第三阶段实施设计与当前实现边界。第三阶段只打通“MinIO 原始文件读取 -> 解析适配 -> 标准化 Markdown / text / metadata -> 基础文本切块 -> document_chunks 入库 -> 更新 documents.process_status -> 前端轻量查看切块效果”的本地开发闭环。

第三阶段继续保持本机 FastAPI、本机 Next.js、Docker 基础服务的开发架构。FastAPI 和 Next.js 不放入 Docker Compose。

第三阶段实际数据流：

```text
MinIO 原始文件 bytes
-> Parser
-> ParsedDocument
-> chunker
-> document_chunks
-> documents.process_status
-> 前端 chunk 可视化
```

第三阶段 Step 3 只创建 Alembic 迁移文件。实际运行解析前，需要用户手动确认并执行 `alembic upgrade head`，Codex 不自动执行迁移。

## 1. 第三阶段范围

第三阶段实现以下能力：

- 从 `documents` 表读取已上传文档元数据。
- 从 MinIO `rag-documents` bucket 只读读取原始文件 bytes。
- 建立统一 `Parser` Protocol 和 `ParsedDocument` 标准输出结构。
- 提供 `SimpleParser` 作为轻量解析实现。
- 预留 `MinerUParser` 适配层边界，但不强制接入真实 MinerU 服务。
- 基于标准化解析结果执行基础字符切块。
- 写入 `document_chunks`。
- 更新 `documents.process_status`。
- 在前端文档详情页展示解析按钮、解析状态、chunk 数量、chunk 列表、chunk 长度、source metadata 和内容预览。

第三阶段新增后端接口：

- `POST /api/v1/documents/{document_id}/parse`
- `GET /api/v1/documents/{document_id}/chunks?limit=50&offset=0`

第三阶段暂不实现单独 chunk 详情接口。

## 2. MinerU 适配层边界

第三阶段只预留 MinerU 适配层，不安装、部署或调用真实 MinerU 服务。

`MinerUParser` 只负责固定以下边界：

- 与 `SimpleParser` 实现同一个 Parser 接口。
- 接收原始文件 bytes、文件名、扩展名、MIME 和文档元数据。
- 未来输出同一种 `ParsedDocument`。
- 预留 `MINERU_ENDPOINT`、`MINERU_TIMEOUT_SECONDS` 等配置项。
- 当前如被选择且不可用，应返回明确业务错误，例如 `DOCUMENT_PARSER_UNAVAILABLE`。

后续接入真实 MinerU 时，不应重构 chunker、`document_chunks` 入库逻辑或前端 chunk 可视化逻辑。

## 3. SimpleParser 轻量解析边界

第三阶段 `SimpleParser` 不新增重型解析依赖。

文本类文件：

- `.txt`
- `.md`
- `.csv`

上述扩展名应加入后端 `UPLOAD_ALLOWED_EXTENSIONS`，并同步加入前端上传表单 `accept` 白名单，用于第三阶段真实文本解析和切块验收。

文本类文件解析规则：

- 从 MinIO 读取原始 bytes。
- 优先按 UTF-8 解码。
- 解码失败时允许使用 `errors="replace"` 兜底。
- 输出真实 `text` 和可等价展示的 `markdown`。

复杂格式文件：

- PDF
- Word
- Excel
- 图片

第三阶段对复杂格式不做真实内容解析，不新增 `pypdf`、`python-docx`、`openpyxl`、OCR 或 MinerU SDK。复杂格式生成明确占位解析结果。

占位内容至少包含：

- 原始文件名；
- 文件类型；
- `parser_name`；
- `source_type`；
- 当前为轻量解析占位；
- 后续由 MinerU 替换。

占位 `source_metadata` 至少包含：

- `placeholder=true`
- `parser_name`
- `parser_version`
- `source_type`
- `original_extension`

`content_type` 仍只作为辅助信息或弱校验，不作为强安全判断。如果扩展名已在白名单中，但 `content_type` 为空、`application/octet-stream` 或浏览器返回不稳定类型，第三阶段不应直接拒绝上传，避免误拦截 `.txt`、`.md`、`.csv`。

## 4. ParsedDocument 标准结构

Parser 统一输出 `ParsedDocument`，建议字段如下：

- `markdown`：标准化 Markdown 内容。
- `text`：标准化纯文本内容。
- `metadata`：解析来源、文件名、扩展名、占位标记等结构化元数据。
- `parser_name`：解析器名称，例如 `simple` 或 `mineru`。
- `parser_version`：解析器版本，例如 `0.1.0`。
- `source_type`：来源类型，例如 `text`、`markdown`、`csv`、`pdf`、`word`、`excel`、`image`。

`Parser` 采用统一 Protocol，`parse` 输入原始 bytes、文件名、扩展名或文件类型，以及可选 MIME 类型，输出 `ParsedDocument`。`SimpleParser` 和 `MinerUParser` 都遵循该接口。

chunker 只能依赖 `ParsedDocument` 的 `markdown`、`text` 和 `metadata`，不得直接依赖 `SimpleParser`、`MinerUParser` 或具体文件格式实现。

## 5. chunker 策略

第三阶段使用基础字符切块策略：

- 默认 `chunk_size_chars=1000`。
- 默认 `chunk_overlap_chars=100`。
- 按 `chunk_index` 从 0 开始连续递增。
- 按原始顺序生成 chunk。

chunker 必须校验：

- `chunk_size_chars > 0`
- `chunk_overlap_chars >= 0`
- `chunk_overlap_chars < chunk_size_chars`

如果配置不合法，应返回 `DOCUMENT_CHUNK_CONFIG_INVALID`，不能进入死循环。

第三阶段不做：

- 多切块算法在线对比；
- 拖拽调整 chunk 边界；
- 复杂可视化调参系统；
- token 精确统计；
- embedding 生成。

`token_count` 第三阶段保持为空。后端和前端统计不依赖 `token_count`，只基于 `source_metadata.character_count` 或 `len(content)` / `content.length`。

chunker 输出的 `source_metadata` 至少包含：

- `parser_name`
- `parser_version`
- `source_type`
- `placeholder`
- `char_start`
- `char_end`
- `character_count`
- `original_extension`
- `original_filename`

## 6. document_chunks 字段使用方式

当前 `DocumentChunk` 模型和首个迁移已具备第三阶段基础切片入库所需字段：

- `document_id`
- `chunk_index`
- `content`
- `token_count`
- `page_start`
- `page_end`
- `section_title`
- `chunk_type`
- `embedding_model`
- `embedding_dim`
- `embedding_status`
- `created_at`
- `updated_at`

第三阶段唯一新增数据库字段：

- `source_metadata JSONB`

写入 `DocumentChunk` 时建议：

- `document_id`：当前文档 ID。
- `chunk_index`：从 0 开始连续递增。
- `content`：chunk 文本内容。
- `token_count`：第三阶段不做真实 token 统计，保持为空。
- `page_start` / `page_end`：SimpleParser 无法获得页码时为空。
- `section_title`：无法识别章节标题时为空。
- `chunk_type`：文本解析写 `text`，占位解析写 `placeholder`。
- `embedding_model`：保持为空。
- `embedding_dim`：保持为空。
- `embedding_status`：保持 `not_started`。
- `source_metadata`：保存来源元数据。

第三阶段不修改 `embedding_model`、`embedding_dim`、`embedding_status` 默认值。

## 7. source_metadata 字段设计

`document_chunks.source_metadata` 用于保存 chunk 来源信息，建议为 JSONB。

建议字段：

- `parser_name`
- `parser_version`
- `source_type`
- `placeholder`
- `char_start`
- `char_end`
- `character_count`
- `original_extension`
- `original_filename`

文本解析 chunk 示例：

```json
{
  "parser_name": "simple",
  "parser_version": "0.1.0",
  "source_type": "markdown",
  "placeholder": false,
  "char_start": 0,
  "char_end": 1000,
  "character_count": 1000,
  "original_extension": ".md",
  "original_filename": "casting-process.md"
}
```

占位解析 chunk 示例：

```json
{
  "parser_name": "simple",
  "parser_version": "0.1.0",
  "source_type": "pdf",
  "placeholder": true,
  "char_start": 0,
  "char_end": 120,
  "character_count": 120,
  "original_extension": ".pdf",
  "original_filename": "process-spec.pdf"
}
```

## 8. process_status 状态流转

第三阶段建议使用以下 `documents.process_status`：

- `uploaded`：第二阶段上传成功后的状态。
- `parsing`：正在同步解析和切块。
- `parsed`：解析和切块完成。
- `parse_failed`：解析或切块失败。

第三阶段不修改 `process_status` 的 ORM 默认值，不修改数据库默认值。状态由 service 层显式更新。

`POST /parse` 为同步接口，第三阶段暂不引入 Celery Worker。`parsing` 状态用于后端状态流转；由于同步解析较快，前端不保证一定能观察到 `parsing`。

后续如果解析耗时变长，再单独设计 Celery Worker 或任务队列。

## 9. 重复解析阻止策略

第三阶段默认采用保守策略：

1. `parse_document` 先检查文档是否存在。
2. 再检查是否已有 `document_chunks`。
3. 如果已有 chunks，直接返回 `DOCUMENT_ALREADY_PARSED 409`。
4. 已有 chunks 时不修改 `documents.process_status`。
5. 不删除已有 chunks。
6. 不覆盖已有 chunks。
7. 不默认重新解析。
8. 只有确认无 chunks 后，才进入 `parsing` 状态。

如果解析或 chunk 写入失败：

- rollback 本次 chunk 写入；
- 不留下半截 `document_chunks`；
- rollback 后再单独更新 `documents.process_status='parse_failed'` 和 `error_message`；
- 不删除、不修改、不移动 MinIO 原始对象。

后续如果需要覆盖 chunks 或版本化重新解析，必须单独设计 `force` 参数或版本管理机制，并由用户确认。

## 10. API 接口和错误码

第三阶段新增接口：

- `POST /api/v1/documents/{document_id}/parse`：同步触发解析与切块，成功返回文档 ID、处理状态、chunk 数量、解析器名称和解析器版本。
- `GET /api/v1/documents/{document_id}/chunks?limit=50&offset=0`：按 `chunk_index asc` 获取 chunk 列表、分页信息和长度统计。

`GET /chunks` 参数约束：

- `limit` 默认 50，范围 1-100。
- `offset` 默认 0，最小 0。

第三阶段暂不实现：

- `GET /api/v1/documents/{document_id}/chunks/{chunk_id}`
- 单 chunk 详情页

第三阶段新增或使用的错误码：

- `DOCUMENT_NOT_FOUND` -> 404
- `DOCUMENT_ALREADY_PARSED` -> 409
- `DOCUMENT_CHUNK_CONFIG_INVALID` -> 400
- `DOCUMENT_PARSER_UNAVAILABLE` -> 503
- `DOCUMENT_SOURCE_FILE_NOT_FOUND` -> 503
- `DOCUMENT_PARSE_FAILED` -> 500
- `MINIO_BUCKET_NOT_FOUND` -> 503
- `MINIO_SERVICE_UNAVAILABLE` -> 503

文档业务接口继续使用第二阶段统一响应 envelope：

```json
{
  "success": true,
  "data": {},
  "error": null
}
```

健康检查接口保持第一阶段原结构，不强制包裹为 `success/data/error`。

## 11. 前端轻量 chunk 可视化范围

第三阶段前端在 `/documents/[id]` 文档详情页展示轻量 chunk 可视化。

展示内容：

- 文档解析状态；
- 解析按钮；
- chunk 总数；
- chunk 长度统计；
- `chunk_index`；
- 字符数；
- `chunk_type`；
- `embedding_status`；
- `source_metadata`；
- 内容预览；
- 列表内展开查看 chunk 内容。

前端统计基于 `character_count` 或 `content.length`，不依赖 `token_count`。

前端上传表单只同步更新 `accept` 白名单，加入 `.txt,.md,.csv`，方便第三阶段上传文本类文件验收。不得实现多文件上传、拖拽上传、预签名直传或其他新上传功能。

第三阶段不做：

- 单独 chunk 详情页；
- 文件下载；
- 文件预览；
- PDF 原文与 chunk 双栏对照；
- embedding 可视化；
- 检索命中可视化；
- 复杂切块调参界面。

## 12. 本地运行和手动验收提示

实际运行第三阶段解析前，用户需要手动确认并执行：

```powershell
cd backend
alembic upgrade head
```

后端 `.env` 需要确认：

```text
DOCUMENT_PARSER=simple
CHUNK_SIZE_CHARS=1000
CHUNK_OVERLAP_CHARS=100
MINERU_ENDPOINT=
MINERU_TIMEOUT_SECONDS=60
```

手动验收建议：

1. 用户手动确认 `rag-documents` bucket 存在。
2. 用户手动启动后端和前端。
3. 上传 `.txt`、`.md`、`.csv`。
4. 进入 `/documents/[id]`。
5. 点击“解析文档”。
6. 确认 chunk 总数、长度统计、`source_metadata` 和内容预览展示正常。
7. 再次解析同一文档，确认返回已解析提示，不覆盖已有 chunks。
8. 上传 PDF、Word、Excel 或图片，确认生成占位 chunk，内容说明后续由 MinerU 替换。

## 13. 明确不实现内容

第三阶段仍不得实现：

- 真实 MinerU 服务强制接入；
- embedding 生成；
- 真实 `vector` 字段；
- HNSW / IVFFlat 向量索引；
- 语义检索；
- RAG 问答；
- 知识条目自动抽取；
- Neo4j；
- Elasticsearch；
- Celery Worker；
- 复杂可视化调参系统；
- 拖拽调整 chunk 边界；
- 多切块算法在线对比；
- PDF 原文与 chunk 双栏对照；
- embedding 可视化；
- 检索命中可视化。

## 14. 安全边界

第三阶段继续禁止：

- `docker compose down -v`
- 删除 Docker volume
- `docker volume prune`
- `docker system prune --volumes`
- 清空 MinIO bucket
- 删除 MinIO bucket
- 删除 MinIO 对象
- `DROP`
- `TRUNCATE`
- 无 WHERE 条件的 `DELETE`
- 通过 PostgreSQL MCP 执行 `INSERT` / `UPDATE` / `DELETE`
- 通过 PostgreSQL MCP 执行 `ALTER` / `CREATE`
- `npm audit fix --force`

PostgreSQL MCP 仅允许只读核验：

- 查询 `alembic_version`
- 查询 `information_schema.columns`
- 查询 `documents.process_status`
- 统计某文档 chunks 数量
- 查看 `chunk_index`、`chunk_type`、`embedding_status`、`source_metadata`

禁止通过 PostgreSQL MCP 执行：

- `INSERT`
- `UPDATE`
- `DELETE`
- `DROP`
- `TRUNCATE`
- `ALTER`
- `CREATE`
- 迁移
- 补数据
- 改表
- 删除数据
- 清空表
- 修改数据库结构
