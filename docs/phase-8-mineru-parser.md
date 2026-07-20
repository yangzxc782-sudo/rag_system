# 第八阶段：MinerU API 文档解析增强与解析中间层重构

## 1. 阶段目标

第八阶段的目标是重构文档解析入库层，以 MinerU API 作为正式主解析器，提升 PDF、扫描 PDF、图文混排文档、表格、公式、图片等复杂文档的解析质量。

第八阶段解决的是“文档进入知识库前的解析质量和结构化表达”问题，不重构 search、RAG 或 knowledge_items 主链路。

## 2. 设计边界

第八阶段不做：

- 不重构 `/api/v1/search` 返回结构。
- 不重构 `hybrid_search_chunks()` 主逻辑。
- 不重构 weighted RRF 主逻辑。
- 不重构 `/api/v1/rag/ask` 生成逻辑。
- 不修改 RAG prompt。
- 不修改 RAG context builder 主接口。
- 不重构 `/api/v1/knowledge-items` 主逻辑。
- 不修改 knowledge_items 审核状态机。
- 不接入 Neo4j。
- 不引入 LangGraph。
- 不正式接入 reranker。
- 不让未审核知识参与权威回答。

核心原则：

```text
MinerU 负责提高 document_chunks 的生成质量；
RAG 仍然消费 document_chunks；
RAG 不直接消费 MinerU 原始 JSON 或 document_blocks。
```

## 3. 核心数据流

第八阶段解析入库链路：

```text
documents
-> document_parse_runs
-> document_blocks / document_assets
-> document_chunks
-> document_chunk_blocks
```

后续既有检索和问答链路：

```text
document_chunks
-> embedding
-> OpenSearch
-> search / RAG / knowledge_items
```

职责说明：

- `documents`：上传文件主记录。
- `document_parse_runs`：一次 MinerU / basic 解析任务记录。
- `document_blocks`：MinerU 输出标准化后的结构块。
- `document_assets`：MinerU 输出资产和 MinIO 资产元数据。
- `document_chunks`：最终检索与 RAG chunk。
- `document_chunk_blocks`：chunk 与 block 的来源映射。
- `embedding`：仍只对 `document_chunks.content` 生成。
- `OpenSearch`：仍只索引 `document_chunks`。
- `knowledge_items`：仍通过 `chunk_id` / `source_text` 溯源。

`document_blocks` 和 `document_assets` 不直接进入 RAG 检索。MinerU 输出不得直接变成 approved 知识条目。

## 4. MinerU API 主路径

正式配置推荐：

```env
DOCUMENT_PARSER_PROVIDER=mineru_api
MINERU_API_BASE_URL=https://mineru.net
MINERU_API_KEY=
MINERU_API_TIMEOUT_SECONDS=120
MINERU_API_POLL_INTERVAL_SECONDS=5
MINERU_API_MAX_POLL_ATTEMPTS=120
MINERU_PARSE_MODE=vlm
MINERU_ENABLE_OCR=true
MINERU_OUTPUT_PREFIX=parsed-assets
```

`DOCUMENT_PARSER`、`MINERU_ENDPOINT`、`MINERU_TIMEOUT_SECONDS` 是旧版历史配置。Phase 8 MinerU V4 正式路径不读取这些字段，也不应继续配置。正式路径只读取 `DOCUMENT_PARSER_PROVIDER`、`MINERU_API_BASE_URL`、`MINERU_API_KEY`、`MINERU_API_TIMEOUT_SECONDS`、`MINERU_API_POLL_INTERVAL_SECONDS`、`MINERU_API_MAX_POLL_ATTEMPTS`、`MINERU_PARSE_MODE`、`MINERU_ENABLE_OCR` 和 `MINERU_OUTPUT_PREFIX`。`MINERU_API_BASE_URL` 只配置官方 origin `https://mineru.net`，由 transport 自行拼接 `/api/v4/...` 路径。

当 provider 为 `mineru_api` 时：

- MinerU 配置缺失应返回明确配置错误。
- 不静默 fallback 到 basic。
- 不在日志或错误中输出 API key。
- 不在日志中输出完整原始文档内容。
- 不在日志中输出完整 MinerU JSON。

系统内部统一接口：

```text
MinerUClient.parse_file(...)
-> MinerUParseResult
```

当前正式适配 MinerU 精准解析 API V4，本地单文件沿用官方 batch 上传协议：

1. `POST /api/v4/file-urls/batch` 申请 `batch_id` 和签名上传地址；
2. 使用不携带 MinerU Authorization、且不主动设置 `Content-Type` 的 `PUT` 上传原始字节；
3. 上传完成后不调用额外提交接口；
4. `GET /api/v4/extract-results/batch/{batch_id}` 轮询 `extract_result`；
5. `state=done` 后下载 `full_zip_url`，安全读取 `full.md`、稳定版 `*_content_list.json`、可选 `*_middle.json` / `*_model.json` 和 `images/` 资产。

`batch_id`、`file_urls`、`extract_result`、`state`、`full_zip_url` 和 ZIP 目录差异均封装在 `ingestion/mineru` 内，不泄漏到 `document_parsing.py`。`pipeline` 与 `vlm` 的原始中间结构不同，但两者都先转换为稳定的 `MinerUParseResult`。

Phase 8 v1 只以官方稳定版 `content_list.json` 作为标准化输入。`content_list_v2.json` 仍按开发格式处理：当 ZIP 只有 v2、没有稳定版 content list 时，当前实现会明确报错，不会把 v2 原始结构直接交给 normalizer。后续如需支持 v2，必须先增加独立转换层和对应的格式、兼容性及回归测试。

ZIP 内的 `full.md` 和稳定版 `content_list.json` 分别以系统 canonical key `output.md`、`output.json` 保存；原始 ZIP 相对路径仅作为安全的 `source_path` 摘要保留。图片等资产从 `images/` 目录以内联 bytes 交给既有 MinIO 编排上传，二进制不写入 PostgreSQL，也不进入 `raw_metadata`。

ZIP reader 在读取正文前统一检查路径、符号链接、成员数量、成员与总解压大小、压缩比、加密标志和压缩算法。Phase 8 v1 只接受 `ZIP_STORED` 与 `ZIP_DEFLATED`；加密成员、BZIP2、LZMA 及未知压缩算法均明确拒绝。

解析模式映射为官方 `model_version`：`auto -> vlm`、`vlm -> vlm`、`pipeline -> pipeline`、`MinerU-HTML -> MinerU-HTML`。`enable_ocr` 仅映射到 `files[0].is_ocr`；`save_intermediate` 只是本系统是否保留 middle/model 等产物的内部开关，不发送给 MinerU API。

官方依据：

- [MinerU 精准解析 API 文档](https://mineru.net/apiManage/docs)
- [MinerU 输出文件格式](https://opendatalab.github.io/MinerU/reference/output_files/)

## 5. basic parser fallback 路径

`DOCUMENT_PARSER_PROVIDER=basic` 仅用于：

- 单元测试；
- 本地最小开发验证；
- 简单 txt / md 等文本文件 fallback；
- MinerU API 不可用时的受控兜底。

basic parser 不应成为正式复杂文档的主解析路径。

## 6. parse_run 状态流转

`document_parse_runs.status` 建议状态：

- `pending`
- `running`
- `succeeded`
- `failed`

`parse_run.status=succeeded` 不能只表示 MinerU API 调用成功。只有以下流程全部成功后，才能标记为 succeeded，并将 `is_active` 置为 true：

1. MinerU API 解析成功；
2. MinerU 原始产物 output.md / output.json 等已按规则保存到 MinIO，或记录明确保存状态；
3. `document_assets` 写入成功；
4. `document_blocks` 写入成功；
5. block-aware chunking 成功；
6. `document_chunks` 写入成功；
7. `document_chunk_blocks` 映射写入成功；
8. `documents.process_status` 可同步更新为 `parsed`。

如果 MinerU API 成功，但 assets、blocks、chunks 或 mapping 任一步失败：

- `parse_run.status=failed`；
- `parse_run.is_active=false`；
- `documents.process_status=parse_failed`；
- `error_message` 只写简短摘要；
- 不写完整原文、完整 MinerU JSON、API key 或完整 prompt。

service 层应避免同一 `document_id` 出现多个 active parse run。

## 7. document_blocks 设计

`document_blocks` 保存 MinerU 标准化结构块。

支持的 block type：

- `title`
- `text`
- `list`
- `table`
- `formula`
- `image`
- `caption`
- `footnote`
- `header`
- `footer`
- `unknown`

字段语义：

- `text` 保存纯文本表达。
- `markdown` 保存表格或结构化段落。
- `html` 可保存表格 HTML。
- `latex` 保存公式。
- `caption` 保存图片说明。
- `bbox` 保存页面坐标摘要。
- `section_path` 保存标题层级路径。
- `source_metadata` 保存必要 MinerU 原始字段摘要。

第八阶段 v1 中，`block_key` 不强制唯一；稳定顺序以 `unique(parse_run_id, block_index)` 为准。`parent_block_key` 是弱关联，不做自引用强外键。

## 8. document_assets 设计

`document_assets` 保存解析产物和资产元数据，不保存大二进制。

支持的 asset type：

- `image`
- `table_image`
- `formula_image`
- `page_image`
- `markdown`
- `json`
- `layout_json`
- `other`

资产 key 推荐：

```text
parsed-assets/{document_id}/{parse_run_id}/...
```

第八阶段 v1 避免 `document_assets` 与 `document_blocks` 形成双向强外键。`source_block_key` 只做弱关联。

## 9. block-aware chunking 设计

`document_chunks` 由 `document_blocks` 生成。

规则：

- title 块尽量与其下正文合并。
- text/list 块按章节与长度合并。
- table 块不随意切断。
- 超长 table 优先作为独立 chunk。
- formula 块作为原子块处理，不切断 LaTeX。
- 超长 formula 允许生成独立超长 formula chunk。
- image/caption 优先合并为 image_caption 或 mixed chunk。
- header/footer 默认不进入 chunk，除非明确有价值。
- unknown 块保守转入 text/mixed chunk。
- 超长普通文本再按段落安全切分。

建议 chunk_type：

- `text`
- `table`
- `formula`
- `image_caption`
- `mixed`

建议 content_format：

- `plain_text`
- `markdown`
- `mixed`

`source_metadata` 应包含：

```json
{
  "parser_provider": "mineru_api",
  "parse_run_id": "...",
  "block_ids": ["..."],
  "block_keys": ["..."],
  "block_types": ["title", "text"],
  "asset_keys": ["..."],
  "section_path": ["..."],
  "chunk_method": "mineru_block_merge",
  "content_format": "markdown"
}
```

不得包含 API key、完整原文、完整 MinerU JSON、图片二进制或巨大对象。

## 10. document_chunk_blocks 映射

`document_chunk_blocks` 用于从最终 chunk 回溯到来源 blocks。

约束：

- 一个 chunk 可以由多个 blocks 合并。
- 一个 block 可在拆分场景中被多个 chunks 引用。
- `block_order` 在同一个 chunk 内稳定递增。
- 按 `block_order` 排序可恢复同一 chunk 的来源顺序。
- 外键不使用危险级联删除。

## 11. download_deferred 语义

解析产物状态可分为：

- `saved`：产物已保存。
- `download_deferred`：兼容 fake/旧结果模型时，产物下载或保存被延后；官方 V4 正常完成路径会先下载并解析 ZIP，再进入完整入库成功状态。
- `unavailable`：产物不可用或未返回。

如果 output.md / output.json 实际没有保存到 MinIO，不能误导为已完整保存。Step 6 查询 API 和前端展示应区分这些状态。

## 12. failure_status_persisted 语义

`failure_status_persisted=false` 表示解析失败后，系统尝试记录 failed 状态时也失败了。此时需要人工排查数据库事务、连接或持久化问题。

该状态不得被解释为解析成功，也不得将 document 标记为 parsed。

## 13. reparse 边界

第八阶段 v1 不实现正式 reparse API。

已有 chunks 的文档默认返回：

```text
DOCUMENT_ALREADY_PARSED
```

开发阶段如需重测，只能在用户明确确认“仅测试数据”后手动清理测试数据。正式数据未来应采用非破坏式 reparse、active parse_run 或 chunk versioning 机制。

## 14. 未来扩展方向

后续可扩展：

- 正式 reparse API。
- active parse_run 切换机制。
- chunk versioning。
- 多模态资产预览。
- 基于 block/page/bbox 的更丰富 citation 展示。
- parsed-assets orphan 维护清理。

这些都不属于第八阶段 v1 的必须范围。
