# Phase 11 P0 — Basic Parser Retirement

审计与执行日期：2026-09-09。起点：`feature/phase11-graph-retrieval`，
`9ddfd182e562e3f58a2153d3559325ed1d824eda`。起点 `git status --short` 和
`git diff --check` 均为空；已检查最近 15 个提交。Phase 10 完成状态以负责人确认优先，
不改写旧 handoff 的 REAL_ROLLOUT_PENDING，也不重新宣称本轮完成真实删除验收。

**Basic Parser retired before Phase 11 M1.** 本提交只退出 Basic，不实现 Markdown AST、
KG Anchor、ChunkDraft/Writer、Neo4j Repository、Graph Retrieval 或 RAG fusion。

## BASIC_PARSER_DEPENDENCY_MATRIX

| symbol/file（删除前） | current consumers | production/test/docs | can delete directly? | required migration | risk |
|---|---|---|---|---|---|
| ingestion/chunker.py: ParsedChunk, chunk_parsed_document, validate_chunk_config | ingestion exports、document_parsing、test_ingestion | 生产/测试/历史 docs | 否 | 先删除 Basic route、ORM conversion、exports；纯实现测试随功能退出 | 中 |
| parsers/base.py: ParsedDocument, Parser | SimpleParser、旧 MinerUParser stub、character chunker、service annotation | 生产导入/测试/历史 docs | 否 | 退出上述消费者；没有真正 MinerU 消费者，无抽象需要迁出 | 低 |
| parsers/simple.py: SimpleParser, normalize_extension, source-type constants | exports、Basic factory、test_ingestion | 生产/测试/历史 docs | 否 | 删除 factory/branch；上传与解析采用明确格式策略 | 中 |
| parsers/mineru.py: MinerUParser | 仅包 re-export 与 test_ingestion | 旧占位 stub/测试 | 是，在移除 exports 后 | 始终抛 unavailable，未接入真实 MinerU；随旧 adapter 移除 | 低 |
| ingestion/__init__.py、parsers/__init__.py | document_parsing 导入旧 DTO/factory | 生产 | 否 | ingestion 包保留无副作用说明，parsers exports 删除 | 中 |
| _parse_document_with_basic | parse_document 的 provider 分支 | 生产 | 否 | 删除；md 单独返回已有 unavailable；支持的非 md 才进入 MinerU | 中 |
| _get_configured_basic_parser、_build_document_chunks | Basic route | 生产 | 是，在退出 route 后 | 删除，不创建 Basic → ChunkDraft adapter | 低 |
| _is_document_deletion_guard_error、_mark_document_parse_failed | **真实 MinerU 仍使用** | 生产 | **否** | 原样保留在 service；不是 Basic 专有 helper | 高 |
| document_parser_provider | Settings、parse dispatch、测试和 env example | 生产/配置/测试/docs | 不删除配置 | 限定 Literal["mineru_api"]，旧 basic/simple/未知值在配置加载时拒绝 | 中 |
| document_parser、mineru_endpoint、mineru_timeout_seconds | 已无真实生产消费者；旧测试配置仍填写 | dead config/测试/历史 docs | 是 | 删除字段和旧测试赋值；真实 .env 不改 | 低 |
| chunk_size_chars、chunk_overlap_chars | MinerU _mineru_chunker_config | 生产 | **否** | 保留，不改 BlockChunkerConfig 或切块算法 | 中 |
| test_ingestion.py | 14 个纯旧 parser/chunker/stub 参数化用例 | 测试 | 随功能删除 | 另加 26 项 routing/config/removal 契约；不能以删除测试代替验收 | 中 |
| test_document_parsing.py、test_documents.py | 成功/失败/重复解析/删除屏障/读取/API 契约 | 测试 | **否** | 迁移到 MinerU 与 P0 routing；保留历史 chunk metadata 读取 fixture | 中 |

真实 MinerU 使用 `ingestion/mineru/models.py` 自有 DTO；client、transport、archive reader、
normalizer、block chunker 没有依赖旧 Parser/ParsedDocument。故不存在需要迁出的通用抽象。
Embedding、OpenSearch、Hybrid、Vector、RAG、Knowledge、Hard Delete 消费持久化 chunks，
不依赖被删除模块。存量 Basic chunks 不清理、不重写，继续可读、可检索和删除。

## FILE_TYPE_ROUTING_MATRIX

删除前所有类型均可上传；parser 由全局 provider 决定，没有扩展名分流。
`basic` 对 txt/md/csv 解码，对其他格式生成占位结果；`mineru_api` 会把输入送入 V4。

| extension | upload allowed before | current parser before | P0 upload / parse | future parser / Phase 11 action |
|---|---|---|---|---|
| .md | 是 | Basic 文本或全局 MinerU | 上传允许；parse 503 unavailable，无状态/产物写入 | Markdown Native，M3 接通；永不走 MinerU |
| .txt | 是 | 只有 Basic 提供正式文本处理 | upload 415；既有未解析文档 parse 415 | 停止支持，不转换 Markdown |
| .csv | 是 | 只有 Basic 提供正式文本处理 | upload 415；既有未解析文档 parse 415 | 停止支持，不转换 Markdown |
| .pdf | 是 | Basic 占位 / MinerU | 允许 / 原 MinerU | 保留；仓库有历史真实 PDF 验证记录 |
| .doc, .docx | 是 | Basic 占位 / MinerU | 允许 / 原 MinerU | 保留；官方 V4 契约支持，新增 DOCX orchestration 回归 |
| .xls, .xlsx | 是 | Basic 占位 / MinerU | 允许 / 原 MinerU | 保留已有产品类型，官方 V4 契约支持 |
| .png, .jpg, .jpeg, .bmp, .webp | 是 | Basic 占位 / MinerU | 允许 / 原 MinerU | 保留已有产品类型，官方 V4 契约支持 |
| .tif, .tiff | 是 | Basic 占位 / 未经证实的 V4 路由 | upload / parse 415 | 当前官方 V4 上传契约未列出，不猜测支持 |
| .ppt, .pptx, .jp2, .gif, .html 等 | 否 | 无项目支持承诺 | 仍拒绝 | 本轮不扩大产品范围 |

依据：[MinerU 官方 V4 API 文档](https://mineru.net/apiManage/docs?openApplyModal=true)，2026-09-09 查阅。
这证明接口声明的格式能力，不等同于本轮逐格式远程解析验证；本轮没有向远程 MinerU 上传文件。

## 最小实现与兼容边界

- 删除生产文件：`backend/app/ingestion/chunker.py`；`parsers/{__init__,base,simple,mineru}.py`。
- 新增极小 `backend/app/ingestion/file_types.py`：产品支持扩展名常量，不是 parser 或新 ingestion framework。
- `parse_document` 保留文档不存在与已有 chunks 检查；之后按实际文件扩展名分派。
- `.md` 使用现有 `DOCUMENT_PARSER_UNAVAILABLE` / HTTP 503；不读取原文件，不创建 ParseRun/Block/Chunk，不修改 process_status。
- 不支持类型用现有 `INVALID_FILE_TYPE` / HTTP 415 拒绝。已有 chunks 的 409 保护先于新解析分派，存量数据不受影响。
- `DOCUMENT_PARSER_PROVIDER` 保留以避免未知旧配置被忽略，但唯一允许值是 `mineru_api`；无 fallback 分支。
- `UPLOAD_ALLOWED_EXTENSIONS` 是部署级收窄配置，与产品支持集合取交集。即使真实旧 .env 仍列 txt/csv，也不能重新开放这些类型。
- 默认与示例白名单：`.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.webp,.md`。
- 前端只同步上传 accept、格式提示与解析说明，保持布局、交互和 API；不扩展为 UI 重构。
- API 路由及错误 envelope 无需改动；真实 .env、migrations、数据模型、共享服务不修改。
- AST 对比起点与最终 document_parsing：仅存活的 `parse_document` 改变，其他 **29 个**存活定义相同；只删除 3 个 Basic 私有 helper。
- MinerU client/transport/normalizer/block chunker 与 Hard Delete 核心生产文件没有 diff。

## TDD 与真实验证记录

所有测试均为 mock/local；没有执行 destructive integration、迁移、真实模型调用或服务写入。

| Gate | 结果 |
|---|---|
| 修改前 ingestion/parse focused | 190 passed |
| 原始 full baseline 命令 | 917 passed，28 deselected，6 setup errors；全部是默认 pytest 临时目录 WinError 5 |
| 隔离临时目录的 fresh full baseline | **923 passed，28 deselected** |
| 新 retirement RED（生产代码修改前） | **15 failed，11 passed**，覆盖 provider/removal/md/unsupported 路由 |
| retirement GREEN | **26 passed** |
| 调整中回归 | 3 个用例捕获误删的公共异常分类 helper；已原样恢复，后续 79 passed |
| 最终 Basic/parse/ingestion focused | **203 passed** |
| 最终 MinerU regression | **116 passed** |
| 最终 Embedding/Search/Vector/RAG | **159 passed** |
| 最终 Hard Delete/guard/Phase 10 helpers | **306 passed** |
| 最终 Backend full | **936 passed，28 deselected** |
| 前端改动文件 ESLint | 通过 |
| 前端全量 TypeScript | 未通过；KnowledgeItemsPanel.tsx:270 的既有 TS2345，见下文 |

测试数量解释：923 - 14 个退出功能的纯实现用例 + 26 个 retirement 契约 + 1 个 DOCX 参数化 case = 936。
3 项原有 service 业务测试迁移到真实 MinerU orchestration（fake transport），并未删除业务断言。
前后 full suite 都排除相同的 28 个 integration 用例，没有新增 skip/xfail；不把它们算作通过。

运行目录 `D:\rag_system\backend`，使用 `.\.venv\Scripts\pytest.exe`。
focused 选择 `test_basic_parser_retirement.py`、`test_document_parsing*.py`、`test_documents*.py`、
`test_document_parse_models.py`、`test_document_blocks_assets.py`、`test_document_chunk_blocks.py`、
`test_block_chunker.py`、`test_mineru*.py`；baseline 对应使用旧 `test_ingestion.py`。
MinerU 回归选择 `test_mineru*.py` 与 `test_document_parsing_mineru.py`。
Search/RAG 回归选择 `test_document_embeddings.py`、`test_embeddings.py`、`test_search_engine.py`、
`test_search_index.py`、`test_vector_search.py`、`test_hybrid_search.py`、`test_rag_context.py`、
`test_rag_service.py`、`test_rag_api.py`。
Hard Delete 回归选择 `test_document_deletion*.py`、`test_document_knowledge_deletion.py`、
`test_document_operation_guard.py` 与 `test_phase10*.py`。

Full selection 与负责人要求一致：

```powershell
.\.venv\Scripts\pytest.exe -q -m "not integration and not phase11_integration"
```

实际重跑增加唯一 `--basetemp` 和 `-o cache_dir=...`，先确认临时路径不存在；不清理旧目录，
不改变测试选择或断言。其他不使用 tmp_path 的 focused runs 使用 `-p no:cacheprovider`。
尚有既有 Starlette/httpx deprecation warning，未安装或升级依赖。

前端 `tsc --noEmit --incremental false` 报 KnowledgeItemsPanel.tsx:270 的 TS2345。
通过 TypeScript compiler host 在内存中以 Git HEAD 内容替换本轮两个前端文件，重新核验基线，
得到完全相同的唯一错误。因此不修改 KnowledgeItemsPanel，不宣称前端全量 typecheck 通过。
本轮两个 TSX 文件单独 ESLint exit 0；没有运行浏览器或扩大 UI 验收范围。

## Basic residual scan 与历史说明

生产 `.py` AST/import 扫描无旧 Basic modules、DTO、SimpleParser、character chunker 引用。
`git grep` 作为本机 rg 的等价工具核验 `SimpleParser|chunk_parsed_document|DOCUMENT_PARSER_PROVIDER.*basic|document_parser_provider.*basic`，
生产目录应无命中。测试中保留名称仅用于负向退出契约，以及标明历史 provenance 的读取 fixture。

- README：顶部现行 P0 规则优先；第三阶段 Basic 内容明确标为历史。
- local-development：旧阶段示例明确标为历史；最新 MinerU 流程删除 fallback 配置说明。
- manual-acceptance：旧 Basic 成功验收替换为当前配置拒绝、upload 415 与 Markdown 503 验收。
- phase-3-document-parsing-chunks、phase-8-final-handoff、phase-8-mineru-parser：保留历史事实，顶部标明 Basic 描述已失效。
- database-schema：说明当前 ParseRun 仅由 MinerU 创建；历史 basic_text_split 仅代表存量 provenance，不再生成。
- migrations 不改写；没有新增生产 Basic path，也没有为未来设计保留 Basic adapter。

## 提交边界与下一步

本轮独立提交：`chore: retire basic document parser`，不包含 Phase 11 M1—M7 功能。
提交前检查 diff/check/status/文件清单；提交后先确认工作区为空，再创建负责人指定的两份 Phase 11 文档草案。
草案单独留待审查，不混入 retirement commit。

最终两路：MinerU existing output → 后续 adapter → ChunkDraft；Markdown AST Chunker → ChunkDraft；
两者经后续 DocumentChunkWriter 汇入 DocumentChunk。无 Basic ParsedChunk adapter。

停止状态：`AWAITING_PROJECT_OWNER_PHASE11_BASIC_RETIREMENT_REVIEW`。
