# 第八阶段测试数据重建说明

本文档只说明本地开发环境中的测试数据重建思路。不要将本文档中的示例用于生产环境。本文档不提供自动 reparse API，不提供自动清理脚本，也不表示已经执行任何 SQL、Docker、OpenSearch 或 MinIO 操作。

## 1. 当前边界

第八阶段 v1 不实现正式 reparse API。

已有 `document_chunks` 的文档默认返回：

```text
DOCUMENT_ALREADY_PARSED
```

如果需要重新解析同一测试文档，需要用户手动确认后清理测试数据，再重新 parse、embedding 和 rebuild OpenSearch。

## 2. 适用范围

仅适用于：

- 本地开发库；
- 明确没有正式数据；
- 明确 document_id 对应的是测试文档；
- 明确相关 knowledge_items 也是测试数据，或不存在需要保留的知识条目；
- 用户已经完成备份；
- 用户理解清理后需要重新 parse、embedding、OpenSearch rebuild。

不适用于：

- 生产环境；
- 已经接入正式资料的环境；
- 已被 `knowledge_item_chunks` 引用的正式 chunks；
- 已审核通过且需要保留的 `approved` knowledge_items。

## 3. 禁止事项

禁止：

- `docker compose down -v`
- `docker volume prune`
- `docker system prune --volumes`
- 删除 PostgreSQL volume
- 删除 MinIO bucket
- 删除 OpenSearch volume
- 删除 MinIO 原始上传文件
- 手写无保护 `DROP`
- 手写无保护 `TRUNCATE`
- 无 `WHERE` 条件的 `DELETE`
- 清理正式数据
- 删除被正式 knowledge_items 引用的 chunks

## 4. 清理前检查

清理前必须确认：

1. 当前连接的是本地开发库。
2. 已经备份。
3. 已确认目标 `document_id`。
4. 已确认目标数据是测试数据。
5. 已确认没有要保留的 `approved` knowledge_items。
6. 已确认不删除 MinIO 原始上传文件。
7. 已确认清理后会重新执行 parse、embedding 和 OpenSearch rebuild。

## 5. 第一次 migration 前的旧测试数据清理

如果第八阶段新表尚未存在，不要清理以下表，因为它们尚未创建：

- `document_parse_runs`
- `document_blocks`
- `document_assets`
- `document_chunk_blocks`

如果存在测试知识条目，建议顺序：

1. 确认这些知识条目均为测试数据。
2. 清理测试 `knowledge_item_chunks`。
3. 清理测试 `knowledge_item_reviews`。
4. 清理测试 `knowledge_item_versions`。
5. 清理测试 `knowledge_items`。
6. 清理测试 `document_chunks`。
7. 处理测试 OpenSearch index，或在后续 rebuild 中覆盖。

## 6. 第八阶段新表已存在后的重新测试清理

如果第八阶段新表已经存在，重新测试清理建议顺序：

1. 确认所有待清理数据均为测试数据。
2. 清理测试 `knowledge_item_chunks`。
3. 清理测试 `knowledge_item_reviews`。
4. 清理测试 `knowledge_item_versions`。
5. 清理测试 `knowledge_items`。
6. 清理测试 `document_chunk_blocks`。
7. 清理测试 `document_chunks`。
8. 清理测试 `document_blocks`。
9. 清理测试 `document_assets`。
10. 清理测试 `document_parse_runs`。
11. rebuild OpenSearch index。

说明：

- “清理测试 chunks 及其 embeddings”指清理测试 `document_chunks` 记录及其内置 `embedding` 字段，不代表存在独立 embedding 表。
- `document_chunk_blocks.chunk_id` / `block_id` 外键不使用危险级联删除，因此必须按依赖顺序手动执行。
- 正式阶段不应直接删除被 `knowledge_item_chunks` 引用的 chunks。

## 7. 危险 SQL 示例

以下 SQL 仅作为“需要怎样的 WHERE 保护”的示意。不要直接复制执行。执行前必须备份，并替换为明确的测试 `document_id`。不得用于生产。

> 警告：以下内容是危险维护示例，仅限本地开发测试库，并且必须由用户手动确认后执行。

```sql
-- 仅示例，不要直接执行。
-- BEGIN;
--
-- -- 1. 确认目标 document_id 是测试数据。
-- -- SELECT id, original_filename, process_status FROM documents WHERE id = '<TEST_DOCUMENT_ID>';
--
-- -- 2. 如存在测试 knowledge_items，先按依赖顺序清理。
-- -- DELETE FROM knowledge_item_chunks
-- -- WHERE knowledge_item_id IN (
-- --   SELECT id FROM knowledge_items WHERE source_document_id = '<TEST_DOCUMENT_ID>'
-- -- );
-- --
-- -- DELETE FROM knowledge_item_reviews
-- -- WHERE knowledge_item_id IN (
-- --   SELECT id FROM knowledge_items WHERE source_document_id = '<TEST_DOCUMENT_ID>'
-- -- );
-- --
-- -- DELETE FROM knowledge_item_versions
-- -- WHERE knowledge_item_id IN (
-- --   SELECT id FROM knowledge_items WHERE source_document_id = '<TEST_DOCUMENT_ID>'
-- -- );
-- --
-- -- DELETE FROM knowledge_items
-- -- WHERE source_document_id = '<TEST_DOCUMENT_ID>';
--
-- -- 3. 第八阶段新表存在后，按依赖顺序清理解析中间层。
-- -- DELETE FROM document_chunk_blocks
-- -- WHERE chunk_id IN (
-- --   SELECT id FROM document_chunks WHERE document_id = '<TEST_DOCUMENT_ID>'
-- -- );
-- --
-- -- DELETE FROM document_chunks
-- -- WHERE document_id = '<TEST_DOCUMENT_ID>';
-- --
-- -- DELETE FROM document_blocks
-- -- WHERE document_id = '<TEST_DOCUMENT_ID>';
-- --
-- -- DELETE FROM document_assets
-- -- WHERE document_id = '<TEST_DOCUMENT_ID>';
-- --
-- -- DELETE FROM document_parse_runs
-- -- WHERE document_id = '<TEST_DOCUMENT_ID>';
--
-- -- COMMIT;
-- -- 如果发现误选数据，应 ROLLBACK。
```

## 8. 重建流程

清理测试数据后，重新验收的顺序：

1. 确认配置 `DOCUMENT_PARSER_PROVIDER`。
2. 调用 parse API。
3. 检查 `document_parse_runs`。
4. 检查 `document_blocks`。
5. 检查 `document_assets`。
6. 检查 `document_chunks`。
7. 手动触发 embedding。
8. rebuild 或同步 OpenSearch index。
9. 验证 `/api/v1/search`。
10. 验证 `/api/v1/rag/ask`。
11. 验证 `/api/v1/knowledge-items` 抽取仍可基于新的 chunks 工作。
12. 确认第八阶段没有写入 `retrieval_logs`。

## 9. 测试命令说明

以下命令仅供用户后续手动执行，本步骤不运行。

后端全部测试：

```powershell
cd backend
pytest
```

文档解析专项测试：

```powershell
cd backend
pytest tests/test_document_parse_models.py tests/test_mineru_client.py tests/test_mineru_normalizer.py tests/test_document_blocks_assets.py tests/test_block_chunker.py tests/test_document_parsing_mineru.py tests/test_documents_parse_api.py
```

前端检查：

```powershell
cd frontend
npm run lint
npm run build
```

测试通过与否必须以用户实际执行结果为准。
