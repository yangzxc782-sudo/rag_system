# Phase 11 Implementation Plan — MinerU + Markdown Native + Source Anchor Graph Retrieval

状态：DRAFT / AWAITING_PROJECT_OWNER_PHASE11_BASIC_RETIREMENT_REVIEW。
原定日期：2026-09-08；本次修订：2026-09-09，保留负责人指定文件名。
执行本计划需要负责人后续明确授权；**本轮不得开始 M1**。

设计依据：[Phase 11 Design](../specs/2026-09-08-phase-11-graph-retrieval-design.md)。
退出证据：[Basic Parser Retirement](../../phase-11-basic-parser-retirement.md)。
本计划的 repository-relative paths 均相对于 `D:\rag_system`；“新建”表示当前尚不存在。

## 1. P0 已完成：Basic Retirement checkpoint

**Basic Parser retired before Phase 11 M1**。

- Branch：feature/phase11-graph-retrieval。
- 起点：9ddfd182e562e3f58a2153d3559325ed1d824eda。
- 独立提交：396868c2f2f6b149d41318406eea40caea626902，`chore: retire basic document parser`。
- 提交后先核验工作区为空，再创建本 Plan 和 Design；两份草案不混入 P0 commit。
- Baseline：focused 190 passed；full 923 passed / 28 deselected。
- RED：15 failed / 11 passed；GREEN：26 passed。
- 最终 focused 203；MinerU 116；Embedding/Search/RAG 159；Hard Delete/guard/Phase10 helpers 306；full 936 passed / 28 deselected。
- 原始 full baseline 曾因旧 pytest 临时目录权限出现 6 setup errors；使用新独立目录恢复，不改测试、不清理原目录。
- 前端两个修改文件 ESLint 通过；全量 TS2345 位于 KnowledgeItemsPanel.tsx:270，经 HEAD 内容复现为既有错误，不在 P0 修复。
- 未改 Hard Delete 核心、MinerU 算法、migration、真实 .env；未写服务、未 push。

已删除的旧实现与 DTO 不得在未来任务中被作为现有接口引用或重新引入。
当前 .md upload 保留，但 parse 明确 503 unavailable；.txt/.csv/.tif/.tiff 拒绝。
当前 provider 配置仅接受 mineru_api；Markdown 的未来路由独立于 provider。

## 2. Frozen execution contracts

```text
Document
├─ .md → Markdown Native → AST → Anchor Parser → AST Chunker ─┐
└─ supported non-.md → MinerU → Blocks → Block Chunker → adapter ─┤
                                                            ChunkDraft
                                                                ↓
                                                       DocumentChunkWriter
                                                                ↓
                                                           DocumentChunk
```

- 只适配 **MinerU existing output → ChunkDraft**；Markdown AST Chunker 直接输出 ChunkDraft。
- 保留 BuiltDocumentChunk / BuiltChunkBlockLink / ChunkBuildResult，初期不改变 Block Chunker 公共签名或算法。
- Writer 不 commit；原 service 持有 Session、事务、DocumentOperationGuard 和状态转换。
- 原生 Markdown run=None、maps=[]，不创建 fake ParseRun/Block/Asset，不新增衍生 MinIO 对象。
- KGRef 为多值 list；anchor_id 稳定，table_ref 可空（table 类型要求非空）；放 source_metadata。
- start/end 配对、合法嵌套、异常 fail closed；控制信息在 AST 阶段移除。
- AST 内容节点交集决定继承；排序 `(scope_start_order, anchor_id)`，canonical JSON 验证稳定序列化。
- 真实 schema 唯一 table 模板；fixed parameterized Cypher；按 graph 分组、按 anchor 去重。
- 数值/单位/条件必须由当前可见 Markdown 文本支持。图谱不替代 Text Context。
- 现有 RAG API/citations 不变；不新增 graph-required intent；失败降级 Text RAG。
- 不新增列/关系表/graph nodes，不改 mapping，不重写 RAG，不提供生产 rechunk，不改 importer。

### 依赖矩阵

| Milestone | 前置 | 新依赖/增量 | 必须保持 |
|---|---|---|---|
| M0 | P0 commit + owner review | 审计文档 | 无生产实现 |
| M1 | M0 冻结 | markdown-it-py==4.2.0、AST/Anchor DTO | 无 ORM/driver |
| M2 | M1 | ChunkDraft + AST chunker | 现有 MinerU 算法 |
| M3 | M2 | MinerU adapter + Writer + native routing | guard/事务/Hard Delete |
| M4 | M3 | metadata contract tests，生产预计零改动 | PG/OS schema |
| M5 | M1 KGRef + schema audit | neo4j==6.2.0、read-only repository | 无 graph writes |
| M6 | M4 + M5 | Graph Context + RAG fusion | public API/citations |
| M7 | M1—M6 | 专用资源验收/人工 goldens | 原 Phase 10 安全门禁 |

代码按 M0→M7 顺序实施，每个 milestone 完成 RED→GREEN→REGRESSION→Acceptance 后独立提交。
M5 的逻辑依赖允许独立单元测试，但不得绕过 M0/负责人 gate 自动开始。

### 测试命令与环境规则

工作目录 `D:\rag_system\backend`；pytest 为 `.\.venv\Scripts\pytest.exe`。
所有默认测试使用 fake/local transport，不调用真实 MinerU、Ollama 或远程 LLM。
如使用独立 --basetemp，必须生成唯一不存在的路径，避免 pytest 清理既有目录；不改测试选择。

| 回归组 | 测试选择 |
|---|---|
| R-Ingestion | test_basic_parser_retirement.py（当前 P0 断言）、test_document_parsing.py、test_document_parsing_mineru.py、test_documents.py、test_documents_parse_api.py、test_document_parse_models.py、test_document_blocks_assets.py、test_document_chunk_blocks.py、test_block_chunker.py、test_mineru*.py |
| R-Search | test_document_embeddings.py、test_embeddings.py、test_search_engine.py、test_search_index.py、test_hybrid_search.py、test_vector_search.py |
| R-RAG | test_rag_context.py、test_rag_service.py、test_rag_api.py |
| R-Delete | test_document_deletion*.py、test_document_knowledge_deletion.py、test_document_operation_guard.py、test_phase10*.py |
| R-All | `pytest -q -m "not integration and not phase11_integration"`，使用上述 venv 可执行文件 |
| R-Phase10-Real | 原 tests/integration 的 PostgreSQL/concurrency/storage/recovery gates，保持已有专用目标/备份/授权规则 |

现有 5 项 phase10_knowledge_deferred 单独列册，不删除、不 xfail、不修改测试来隐藏失败，不能计为通过。
本轮 P0 的 28 deselected 是完整 integration 集合，并非只包含这 5 项。
后续真实验收缺少环境/授权时明确“未执行”，不能用 skip 代替 required gate 通过。

## 3. M0 — Audit / Design Freeze

| 要求 | 执行规格 |
|---|---|
| 1 Goal | 复核 P0 commit，保存当前两路架构、真实 schema 和全部审计矩阵 |
| 2 Non-goals | 不实现生产功能，不改 Phase 10 完成记录，不重导图谱 |
| 3 Read-before-code | AGENTS.md、README.md、docs/phase-10-finished.md、docs/superpowers/specs/2026-08-27-phase-10-document-hard-delete-design.md、对应 Phase 10 implementation plan；本 Design 与 Retirement 报告 |
| 4 Files expected to create | 本 Design 与 Plan 已作为待审草案创建；后续不得另造 competing canonical plan |
| 5 Files expected to modify | 仅这两份草案，补 owner 审查结论/必要证据 |
| 6 Interfaces | 冻结 Design 的 DTO、metadata、inheritance、graph template 和 error contract |
| 7 RED tests | 文档缺失项/不一致项先明确标红；没有生产代码，不伪造 pytest RED |
| 8 Minimal steps | Git gate → 核对 P0 → 核对四项审计矩阵 → 标明当前/拟新建 → 核对 Neo4j 路径 → 自审 |
| 9 Regression suite | git diff --check、路径/符号/边界核验 |
| 10 Acceptance criteria | CURRENT_INGESTION_FLOW_MATRIX、MARKDOWN_NATIVE_COMPATIBILITY_MATRIX、NEO4J_SCHEMA_AUDIT、metadata/dependency matrix 齐全；无未来第三路 parser |
| 11 Scope stop | dirty/HEAD 漂移、未解释 owner 决策冲突或 schema 缺证据时停止 |
| 12 Git commit boundary | 后续获准时独立 docs commit；本轮草案不提交 |

## 4. M1 — Markdown AST + KG Anchor Parser

| 要求 | 执行规格 |
|---|---|
| 1 Goal | Canonical Markdown → Clean AST + AnchorScopes |
| 2 Non-goals | 无 API/persistence/embedding/graph 调用，不生成 Anchor、不提供旧标签转换 |
| 3 Read-before-code | backend/app/ingestion/file_types.py、backend/app/ingestion/mineru/models.py（只参考 DTO 风格）、backend/app/core/errors.py、backend/pyproject.toml、Design 的 Anchor contract |
| 4 Files expected to create | backend/app/graph/{__init__,models}.py；backend/app/ingestion/markdown/{__init__,models,parser,anchors}.py；backend/tests/test_markdown_ast.py、test_kg_anchor_parser.py；必要的明确测试 fixtures |
| 5 Files expected to modify | backend/pyproject.toml（仅 AST 依赖）；backend/app/core/errors.py（Markdown 错误常量，API 接线仍在 M3） |
| 6 Interfaces | KGRef、AnchorScope、CleanMarkdownDocument；parse_markdown(source)、parse_anchors(ast)；无 ORM 或 Neo4j driver import |
| 7 RED tests | 有效 pair；缺 start/end；mismatch；duplicate；nested；crossing；跨容器；坏/重复 metadata；缺必填；非 table 的 null；table 缺 ref；无 anchor；空 scope；控制文本非标准位置；清理后无控制节点 |
| 8 Minimal steps | 引入 AST 依赖 → token/tree 适配 → 稳定内容 node IDs → 栈式配对 → scalar metadata 验证 → 移除控制节点 → 有序 scope 输出 |
| 9 Regression suite | 新 AST/Anchor tests + R-Ingestion；R-All |
| 10 Acceptance criteria | 所有异常在 chunking 前 fail closed；nested 支持；不依赖字符 offset；重复 parse 同一 source 输出确定；null 语义保持 |
| 11 Scope stop | 必须生成 ID、用正则字符范围作为继承核心、fake blocks 或安装大框架才能继续时停止 |
| 12 Git commit boundary | feat: add markdown AST and source anchor parsing |

M1 接受严格 UTF-8/BOM。控制节点必须是独立 HTML comment，同容器配对；YAML-like scalar 子集按 Design 固定。
source lines 仅 provenance；不把 inline/fenced 控制标记当正文泄漏到下游。非 table anchor 暂不提供 graph template。

## 5. M2 — Structure-aware Markdown Chunker

| 要求 | 执行规格 |
|---|---|
| 1 Goal | Clean AST + AnchorScopes → ChunkDraft[] |
| 2 Non-goals | 不写 DB，不接 ingestion API，不改 MinerU chunk 算法，不增加生产 rechunk |
| 3 Read-before-code | M1 模块；backend/app/ingestion/block_chunker.py、backend/app/models/document_chunk.py、backend/app/models/document_chunk_block.py |
| 4 Files expected to create | backend/app/ingestion/chunk_drafts.py；backend/app/ingestion/markdown/chunker.py；backend/tests/test_markdown_chunker.py |
| 5 Files expected to modify | M1 models（仅暴露节点/来源信息，不引入 ORM） |
| 6 Interfaces | ChunkDraft、ChunkBlockRef；build_markdown_chunks(clean_document, config) → list[ChunkDraft] |
| 7 RED tests | heading/paragraph/list/table/code/formula；超长原子块不拆；长段落 fragment；1 anchor→N chunks；N anchors→1 chunk；partial intersection；nested inheritance；overlap；deterministic refs；无控制文本；空输入 |
| 8 Minimal steps | 内容节点单元 → 按结构/预算组合 → 长段落片段仍携带 node ID → node-set 交集继承 → 去重排序 → source_range/section_path metadata |
| 9 Regression suite | M1/M2 tests + test_block_chunker.py + R-All |
| 10 Acceptance criteria | 相同 source 两种 size/overlap，chunk 数量/边界可变而 Anchor identity 不变；KGRef list 稳定；表格/单位/公式表达保持；无 ORM |
| 11 Scope stop | 需要修改 Source identity、Neo4j、Block Chunker 输出算法或引入生产重切接口时停止 |
| 12 Git commit boundary | feat: add source-aware markdown chunk drafts |

每个 draft 使用从零开始的连续 chunk_index。原生 run=None、block refs 为空；source_metadata 包含 Design 固定字段。
禁止把覆盖整个 root 的 node ID 当作所有 chunks 的继承依据。fragment/overlap 的节点集合必须反映实际使用内容。

## 6. M3 — MinerU Adapter + Unified Persistence + Native Routing

| 要求 | 执行规格 |
|---|---|
| 1 Goal | 现有 MinerU 输出与 Markdown ChunkDraft 汇入同一 Writer；把 P0 md 503 接通为原生解析 |
| 2 Non-goals | 不重写 ingestion、不创建 fake 中间层、不改 Hard Delete/Knowledge，不提供 rechunk |
| 3 Read-before-code | backend/app/services/document_parsing.py、document_operation_guard.py；backend/app/ingestion/block_chunker.py、file_types.py；Document/Chunk/Block/mapping models；document_deletion_manifest.py；frontend/AGENTS.md 与两个 P0 文案组件 |
| 4 Files expected to create | backend/app/ingestion/chunk_adapters.py（仅 MinerU）；backend/app/services/document_chunk_writer.py；backend/tests/test_document_chunk_writer.py、test_markdown_ingestion.py |
| 5 Files expected to modify | document_parsing.py；必要的 errors.py；test_document_parsing*.py/test_basic_parser_retirement.py；frontend/components/DocumentUploadForm.tsx、DocumentParseButton.tsx 仅移除 P0 未开放提示 |
| 6 Interfaces | adapt_mineru_chunks(ChunkBuildResult) → ChunkDraft[]；DocumentChunkWriter(db).write(document_id,drafts) → list[DocumentChunk]；返回现有 DocumentParseResult |
| 7 RED tests | MinerU adapter 输出等价；writer 无 commit；chunk/map index 对应；失效 persisted block ID 拒绝；md 不调用 MinerU；null run/maps=[]；无 fake rows；错误回滚；初始/最终删除 guard；已有 chunks 409；失败可重试；旧 chunks 读取 |
| 8 Minimal steps | 先 adapter/Writer 等价替换 _add_mineru_chunks 和 mapping 写入 → 保留事务/guard 位置 → md 调用 AST/chunker → final lock 后再次核验未解析并持久化 → 状态/错误处理 → 文案同步 |
| 9 Regression suite | M1—M3 + R-Ingestion + R-Delete + R-All；前端改动文件 ESLint，全量类型检查报告既有基线差异 |
| 10 Acceptance criteria | .md 成功生成 chunks；run=None/maps=[]；零新 ParseRun/Block/Asset；零衍生 MinIO 对象；PDF/DOCX 字段/关系/状态语义等价；没有 migration |
| 11 Scope stop | 必须改 deletion saga、Knowledge、Block Chunker 算法、创建无 run 衍生对象或重构 UI 才能继续时停止 |
| 12 Git commit boundary | feat: add native markdown ingestion with unified chunk persistence |

P0 test_markdown_parse_is_unavailable... 必须在 M3 明确改成 native success contract，而不是删除 routing test。
继续保留“无旧 parser 实现、md never MinerU、unsupported upload 拒绝”等 Retirement 验收。
Writer 只 add/flush；它不接管 service 事务和锁，不生成 embeddings、不操作 OpenSearch。
切块配置只改变新解析结果；已有 chunks 的保护行为保留。此次对“重切”的验证仅发生在纯函数/隔离 fixtures。

## 7. M4 — KGRef Persistence / Search Metadata

| 要求 | 执行规格 |
|---|---|
| 1 Goal | 证明 KGRef list 从 source_metadata 到 OpenSearch/Hybrid 完整保持 |
| 2 Non-goals | 不新增 PG 列、不改 mapping，不按 KGRef 搜索/聚合 |
| 3 Read-before-code | backend/app/services/search_index.py、hybrid_search.py、embeddings.py、vector_search.py；backend/app/search_engine/index_schema.py；backend/app/schemas/search.py |
| 4 Files expected to create | backend/tests/test_kg_refs_propagation.py |
| 5 Files expected to modify | test_search_index.py、test_hybrid_search.py、test_document_embeddings.py；预计无生产变更，必要修补须有失败证据 |
| 6 Interfaces | 沿用 source_metadata 字典与现有 Search API；canonical KGRef 序列化，不额外定义新公开字段 |
| 7 RED tests | 空/单/多 refs；null table_ref；JSONB-compatible payload；index serialization；_source roundtrip；Pydantic output；list 顺序；不原地修改 metadata；embedding input 无控制标记 |
| 8 Minimal steps | 编写跨层 fixture → 各层 serialization/deserialization 测试 → 仅修证实的最小丢失点 → 真实 PG/OS 往返放 M7 |
| 9 Regression suite | R-Search + M1—M3 + R-All |
| 10 Acceptance criteria | semantic values/array ordering 完整保持；无 mapping/model/migration diff；历史与空 metadata 可读 |
| 11 Scope stop | 必须改变 source_metadata mapping 或新增 kg_refs column/关系表时提出 architecture conflict，不能自行实施 |
| 12 Git commit boundary | test: verify KGRef propagation through search metadata（仅存在真实修复时改为 fix） |

当前 enabled=false 会保留 _source；不能把“不索引 metadata”误判为“不存 metadata”。
本阶段以行为契约测试为成果，不为了产生生产 diff 重构已有 serialization。

## 8. M5 — Read-only Neo4j Repository / Fixed Graph Retriever

| 要求 | 执行规格 |
|---|---|
| 1 Goal | 根据真实 schema 实现唯一 table_context_v1 和 typed results |
| 2 Non-goals | 不写 Neo4j、不修 importer、不补数值属性、不创建 indexes、不提供 Text-to-Cypher |
| 3 Read-before-code | import_graph.py（只读，不 import 执行）；Design NEO4J_SCHEMA_AUDIT；backend/app/core/config.py；backend/app/main.py；M1 graph/models.py |
| 4 Files expected to create | backend/app/graph/repository.py、templates.py；backend/app/services/graph_retrieval.py；backend/tests/test_graph_repository.py、test_graph_retrieval.py |
| 5 Files expected to modify | graph/models.py、config.py、main.py、backend/pyproject.toml（neo4j==6.2.0）、backend/.env.example（占位配置）；真实 .env 不改 |
| 6 Interfaces | normalize/aggregate requests → resolver → fixed repository query → typed GraphRetrievalResult；lazy driver/close lifecycle |
| 7 RED tests | no refs 零调用；dedupe；冲突 refs；多 graph 参数隔离；非 table/null ref 不查询；missing graph/table；歧义；shared Document；relationship allowlist；cross graph/table 排除；timeout/budget/partial failure；bounded result；close |
| 8 Minimal steps | 复核 schema → 声明依赖/只读配置 → 固定参数 Cypher → typed mapping → 限额/排序/错误状态 → 关闭资源 → 现有图谱只读 smoke |
| 9 Regression suite | 新 graph tests + R-All；六表 schema/count/path 只读复核 |
| 10 Acceptance criteria | 六表 audit 数据一致；missing 返回空；参数不混 graph；query text 不受自然语言控制；没有 raw driver objects 进入 prompt；关闭可测 |
| 11 Scope stop | schema 漂移、只读账号缺失、需写 schema/data、需修 importer 或无法证明 identity isolation 时停止对应真实 gate |
| 12 Git commit boundary | feat: add fixed read-only graph retrieval |

模板实现约束：UNWIND 同组 anchors，MATCH graph 唯一键；CALL (g,ref) 子查询最多取两个候选 doc/table。
非唯一则不返回实体；目标 Document 若被其他 graph 共享，也不返回。
entities 仅从选定 table CONTAINS_ENTITY 获取且检查 doc_id/table_ref。
业务 edges 仅同表一跳，检查 r.graph_id/r.doc_id 与两端所属；内部 allowlist 为含有/规定/适用于/约束/替代/对应。
所有 query values 绑定参数；不插值用户 question、label、relationship type。
返回 anchor_id、graph_id、candidate/status、doc/table properties、typed entities/relations；结果排序见 Design。

默认 feature off；anchors 8；entities 200/anchor、edges 400/anchor；Query timeout 2s；调度预算 5s；
connect/acquisition 各 1s；不自动事务重试；lookahead 一条判截断。预算耗尽不启动下一组。
配置/网络/空结果不应使基础 Text RAG 失败。只读运行账号由负责人配置，不创建账号、不复用 importer 硬编码密码。

## 9. M6 — Hybrid Hits → Graph Trigger → RAG Fusion

| 要求 | 执行规格 |
|---|---|
| 1 Goal | 保留现有 Text Context，追加有引用来源的 Graph Context |
| 2 Non-goals | 不改 Hybrid 职责，不直接从 RAG 调 driver，不改公开 RAG/citations，不新增 graph-required intent |
| 3 Read-before-code | backend/app/services/rag.py；backend/app/rag/context_builder.py、prompt.py、citations.py；backend/app/schemas/rag.py；M5 graph service |
| 4 Files expected to create | backend/app/rag/graph_context_builder.py；backend/tests/test_graph_context.py、test_rag_graph_fusion.py |
| 5 Files expected to modify | services/rag.py、rag/prompt.py；必要内部 DTO；test_rag_context.py/test_rag_service.py/test_rag_api.py |
| 6 Interfaces | 从已保留的 RagContext chunks 生成请求；build_graph_context → typed evidence；build prompt 接受 optional GraphContext |
| 7 RED tests | feature off/no refs 零查询；重复一次；多 refs/graphs；被文本预算丢弃的 chunk 不触发；部分失败；超时降级；text retained；graph included；no_context 不调 LLM；public JSON shape/citation IDs 不变；数值必须文本支持 |
| 8 Minimal steps | existing context 后聚合 → GraphRetriever → 独立预算的 typed context → prompt 并列 evidence → 既有 citations 映射 → 保持原错误分支 |
| 9 Regression suite | R-RAG + R-Search + graph tests + R-All |
| 10 Acceptance criteria | graph off/failure 与 Text-only 基线一致；不挤占原文本；6000 chars 图谱预算按完整记录裁剪；内部 provenance 完整，公开 API 无新字段 |
| 11 Scope stop | 需要重写 RAG、绕过 deletion filter、补取未命中文本、增加 graph_citations/public API 或 graph-required classifier 时停止 |
| 12 Git commit boundary | feat: fuse source-anchored graph evidence into RAG |

同一 anchor 来自多个 chunks 时一次查询、多个 provenance；conflicting payload 不任选版本。
内部 evidence 包含 template/version、graph/anchor、Neo4j doc/table/entity/edge identity、触发 PG document/chunk/citation、source_range/status/truncation。
引用仍使用已有 [n] 文本定位；图谱辅助关系说明，数值/单位/条件只能来自当前可见 Markdown。

## 10. M7 — Evaluation / Acceptance / Hard Delete Regression

| 要求 | 执行规格 |
|---|---|
| 1 Goal | 验证真实原生入库、metadata、图检索、回答与 Phase 10 删除兼容 |
| 2 Non-goals | 不写/重导图谱，不修共享环境，不自动生成真实 Anchor，不修改 deferred tests，不修无关 Knowledge 缺陷 |
| 3 Read-before-code | M0—M6；backend/tests/integration/conftest.py、phase10_support.py、phase10_run_context.py；现有 deletion PostgreSQL/concurrency/storage/recovery tests；Design 的限制与快照 |
| 4 Files expected to create | backend/tests/phase11_integration/conftest.py、test_markdown_pipeline.py、test_graph_readonly.py、test_markdown_hard_delete.py、test_rag_graph_acceptance.py；backend/tests/fixtures/phase11/ 审定数据 |
| 5 Files expected to modify | backend/pyproject.toml 注册 phase11_integration marker；本 Design/Plan 补实际验收证据 |
| 6 Interfaces | 独立资源 gate、canonical source、golden question/answer/evidence records、结构化测试结果 |
| 7 RED tests | PG/OS refs roundtrip；md null run/empty mappings；完整删除；删除期间禁止重写；六表读查询；rechunk 稳定；fallback；text-only/text+graph 对比 |
| 8 Minimal steps | 专用资源/备份/授权检查 → 合成契约 → 专用 pipeline → 只读图谱 → 审定问答 → 原 Phase 10 gate → 记录真实结果 |
| 9 Regression suite | R-All + Phase11 real suite + 原有 Phase10 real gates；required skips 不计通过；5 项既有 deferred 单独列明 |
| 10 Acceptance criteria | 下表全部达到；所有外部前置完成；无 unsupported claims、无跨 graph、无删除残留 |
| 11 Scope stop | 非预期失败、共享资源命中、需要写 Neo4j/改 Phase10/Knowledge、缺 canonical source 或未获准真实模型调用时停止相应 gate |
| 12 Git commit boundary | test: complete Phase 11 acceptance and hard delete regression；只有真实完成后才写完成结论 |

专用 fixture 放 `tests/phase11_integration/`，不能误继承 `tests/integration/conftest.py` 的 Phase10 autouse gate，
也不能为此削弱旧门禁。PG/MinIO/OS 资源须新鲜明确命名、备份/恢复与目标确认，不使用当前共享服务作验收目标。
不自动删库/删卷/清 bucket；Graph 只读使用既有审定数据，多 graph 冲突场景默认 fake fixtures。

| 维度 | 验收标准 |
|---|---|
| Anchor parsing | 合法/非法 golden 100% 对应冻结异常契约 |
| Inheritance | AST 节点交集 oracle 100% 一致；排序/JSON 确定 |
| 控制文本隔离 | Chunk/embedding input/OS content/RAG prompt 零控制标记 |
| Native provenance | parse_run_id=None；maps=[]；零 fake run/block/asset |
| Metadata | PG→OS→Hybrid 空/多值/null 完整往返 |
| Trigger | 无 refs 零调用、duplicate 一次、多 graph 不串参 |
| Graph correctness | 六表结果匹配审定 snapshot，无 cross graph/table 泄漏 |
| Rechunk | 同一 canonical source 至少两套配置；Anchor 身份不变；不重建图谱 |
| Text baseline | feature off/graph failure 保持现有 Text RAG |
| 问答质量 | 至少 20 个审定问题，数值/化学成分/工艺参数/标准表格/实体关系各 4 个；至少 18/20 正确且不低于同配置 Text-only；零无依据数值，引用可追溯 |
| Hard Delete | 原生 raw/chunks/embedding/index 完整清除；无 run/block 也可 finalization；Neo4j 不变 |
| Phase10 | 原授权范围 gates 通过，已有 deferred 单独报告，不伪称全部通过 |

真实样本前置：本地 test.md 仅有 T-P8-1 的合法 pair，不能代表六表/所有问题覆盖。
完整 Canonical Markdown 及 goldens 需负责人确认；不自动转换旧 kg-tag、旧 chunk_id 或错误示例 Cypher。
缺材料不妨碍合成测试，但完整真实问答 gate 保持未完成。

## 11. Final self-review / Owner gate

- 所有现有文件以 Retirement 后仓库为准；未来新文件明确标为新建。
- 不引用已删除 DTO 作为未来适配来源；只有 MinerU adapter 与 Markdown AST Chunker。
- 所有 Neo4j label/property/path 源于审计；M5 启用前复核 drift，不猜测 schema。
- 不假设原生 Markdown 或真实 null-run E2E 已完成；P0 503 明确。
- source_metadata 透传优先复用；不提前新增字段/mapping。
- table_ref 可空，kg_refs 为 list；Anchor 控制文本不进入 embedding/context。
- 不改 Hard Delete 核心，不重新引入 chunk↔KG，不引入 LangChain/LangGraph。
- 本轮未实现 M1；后续不得把“计划已写入”当作“允许开始实现”。

停止：**AWAITING_PROJECT_OWNER_PHASE11_BASIC_RETIREMENT_REVIEW**。
