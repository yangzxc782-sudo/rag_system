# Phase 11 Design — Source Anchor Graph Retrieval + Markdown Native Ingestion

状态：DRAFT / AWAITING_PROJECT_OWNER_PHASE11_BASIC_RETIREMENT_REVIEW。
审计设计起始日期：2026-09-08；本次修订：2026-09-09。按负责人要求保留原定文件名。
本文件只冻结未来设计，不表示 M1—M7 已实现或验收。

## 1. Git / Phase Gate 与负责人决定

- 项目：`D:\rag_system`；branch：`feature/phase11-graph-retrieval`。
- 原审计/Retirement 起点：`9ddfd182e562e3f58a2153d3559325ed1d824eda`。
- Retirement 独立提交：`396868c2f2f6b149d41318406eea40caea626902`，`chore: retire basic document parser`。
- Retirement 提交后、创建本草案之前，`git status --short` 为空。
- **Basic Parser retired before Phase 11 M1**。
- Phase 10 已完成，依据负责人确认；旧 `docs/phase-10-finished.md` 的 REAL_ROLLOUT_PENDING 仍是未同步历史记录，不在本轮改写。
- 图谱仅辅助定位和实体关系解释；数值、单位、条件以 Canonical Markdown 为准，不修复 importer 属性。
- 只验证 rechunk 身份稳定性，不提供生产 rechunk API。
- 保留当前 RAG API/citations；完整图谱 provenance 仅在内部 typed context 中维护。
- 最终仅有 **MinerU + Markdown Native** 两路。不存在第三种文本 parser 或隐式转换。

Retirement 的完整依赖矩阵、文件路由矩阵、RED/GREEN 与回归证据见
[Basic Parser Retirement](../../phase-11-basic-parser-retirement.md)。后端最终 936 passed / 28 deselected；
本轮未写共享服务、未调用真实模型或 MinerU，未执行 M1。

## 2. Repository Audit：当前实现

下列文件/符号以仓库实际代码为依据。当前路径与未来计划分别标注。
Basic 文件已被删除，不能将旧计划中的 SimpleParser、ParsedDocument 或字符切块作为 read-before-code 入口。

### CURRENT_INGESTION_FLOW_MATRIX

| 阶段 | 真实位置/符号 | 当前行为 |
|---|---|---|
| 上传 | `backend/app/api/v1/documents.py` → `services/documents.py:create_document_from_upload` | 校验扩展名，保存 MinIO raw object，创建 Document；不自动解析 |
| 文件策略 | `ingestion/file_types.py`、`core/config.py:Settings.upload_allowed_extension_set` | 产品能力集合与部署 allowlist 取交集；不能用旧 env 重新开放已退出格式 |
| 解析分派 | `services/document_parsing.py:parse_document` | 查文档、保护已有 chunks；md 503，unsupported 415，支持的非 md 才走 MinerU |
| Markdown 当前 | 同上 | 上传保留；DOCUMENT_PARSER_UNAVAILABLE / 503；不读源文件，不创建 ParseRun/Block/Chunk |
| MinerU | `ingestion/mineru/client.py`、`v4_transport.py` | 签名上传、轮询、结果 ZIP，返回 MinerUParseResult |
| ParseRun | `services/document_parse_runs.py`、`_parse_document_with_mineru` | 真实 run 状态与 active run 管理；DocumentOperationGuard 控制写入 |
| Normalizer | `ingestion/mineru/normalizer.py` | MinerU 结果 → NormalizedDocumentBlock/Asset |
| Block/Asset persistence | `services/document_blocks.py`、`document_assets.py` | 写真实 ORM 并得到 persisted block IDs |
| Block chunking | `ingestion/block_chunker.py:build_block_aware_chunks` | BuiltDocumentChunk + BuiltChunkBlockLink → ChunkBuildResult；没有直接构造 ORM |
| Chunk persistence | `document_parsing.py:_add_mineru_chunks`、`_add_chunk_block_mappings` | service 实例化 ORM；未来才迁入统一 writer |
| Embedding | `services/embeddings.py:generate_document_embeddings` | 按 document_id 取 chunks，只编码 content；显式 API 步骤 |
| Index | `services/search_index.py:rebuild_search_index/build_chunk_index_payload` | normal 文档、完成 embedding 的 chunks → OpenSearch；显式同步 |
| Hybrid | `services/hybrid_search.py` | BM25/kNN → 新鲜 Document 状态过滤 → weighted RRF；不是图查询入口 |
| Vector | `services/vector_search.py` | pgvector 调试检索 JOIN Document，不依赖 ParseRun |
| RAG | `services/rag.py:answer_question`、`rag/context_builder.py`、`rag/prompt.py` | hits → 可选 rerank（当前未正式实现）→ 文本预算 → LLM → citations |

`chunk_index` 在现有 Block Chunker 顺序生成；source_metadata 同样由 Chunker 产生。
目前 `_add_mineru_chunks` 和 mapping helper 位于 service，而非 Chunker。
Retirement 对照起点 AST：存活定义中仅 parse_document 改变，其他 29 个定义相同。

### Markdown current state

```text
CURRENT_MARKDOWN_NATIVE_SUPPORT = NONE
CURRENT_MARKDOWN_UPLOAD = ALLOWED
CURRENT_MARKDOWN_PARSE = DOCUMENT_PARSER_UNAVAILABLE / HTTP 503
```

没有 AST library、Anchor Parser 或 Markdown AST Chunker 的生产实现；未安装新依赖。
`.txt/.csv/.tif/.tiff` 明确拒绝。保留的非 Markdown 扩展名：
`.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.bmp,.webp`。
官方 V4 契约核验与远程逐格式验收是不同证据；本轮没有新远程解析。

### DocumentChunk 与关联模型

实际 DocumentChunk 字段：

- id、document_id、**nullable parse_run_id**、chunk_index。
- content、token_count、page_start/page_end、section_title。
- chunk_type、chunk_method、content_format、nullable source_metadata JSONB。
- embedding VECTOR(1024)、embedding_model、embedding_dim、embedding_status、embedding_error_message、embedding_updated_at。
- created_at、updated_at；关系 document、可空 parse_run、block_mappings、knowledge_item_chunks。

DocumentParseRun 和 DocumentBlock 属于真实 MinerU 中间层；Block 的 parse_run_id 必填，不代表 Chunk 也必填。
DocumentChunkBlock 约束只适用于已有映射行，不要求每个 Chunk 都创建映射。
KnowledgeItemSource / KnowledgeItemChunk 仍有文档、chunk 来源约束；不为本阶段增加重切生产 API。
存量无 ParseRun 的 chunks 保留，不迁移，不清理。

### MARKDOWN_NATIVE_COMPATIBILITY_MATRIX

| component | current assumption | parse_run_id=None? | block_mappings=[]? | required Phase 11 change | risk |
|---|---|---|---|---|---|
| ORM/PostgreSQL | Chunk 必须有 Document，run 可空 | 是 | 是 | 无 migration；真实写入验证 | 低 |
| MinerU Block Chunker | 真实 run 与 blocks | 非此路径 | 非此路径 | 保留算法，仅适配输出 | 中 |
| Embedding | content + document_id + 状态 | 是 | 是 | 控制文本隔离/原生 provenance 测试 | 低 |
| OpenSearch indexing | embedding/normal 文档过滤 | 是 | 是 | metadata 往返测试 | 低 |
| Hybrid/Vector | _source 或 JOIN Document | 是 | 是 | 多 refs/null 保持 | 低 |
| RAG | 文本与 source_metadata | 是 | 是 | 独立 Graph Context | 中 |
| Knowledge extraction | 文档/chunk 文本来源 | 是 | 是 | 来源回归，不改 Knowledge 生产逻辑 | 中 |
| Document detail / chunks | chunks 独立，run/status 列表可空 | 是 | 是 | 空列表和原生 chunks API 测试 | 低 |
| Hard Delete manifest | 独立收集 chunks，run/block/asset 集合可空 | 是 | 是 | 原生 Markdown fixture | 中 |
| Hard Delete finalization | document_id 或 manifest IDs 删除 chunks | 是 | 是 | 回归验证，不修改核心 | 中 |

2026-09-08 PostgreSQL 只读快照：rag_system / codex_ro，transaction_read_only=on，
Alembic 0008_phase10_enforce；11 chunks 均有 ParseRun。nullable 兼容结论来自 schema/调用链，
不是已完成 Markdown E2E。本次不重新查询数据库，也不把旧快照数量称为实时数量。

### PostgreSQL / OpenSearch metadata audit

MinerU source_metadata 实际有 parser_provider、parse_run_id、block_ids/keys/types、asset_keys、
section_path、chunk_method、content_format。历史文本 chunks 可含字符范围与 parser provenance；不再生成。
目前没有 markdown_ast source_range / ast_node_ids / kg_refs。

2026-09-08 已核验 OpenSearch 3.6.0：physical index casting_chunks_v1，alias casting_chunks_current
仅指向该索引，11 entries；根 dynamic=false，source_metadata 为 object / enabled=false，非 flattened。
`build_chunk_index_payload` 原样放入 `chunk.source_metadata or {}`；_id 为 chunk UUID。
metadata 保留在 _source，不参与字段索引解析，见
[OpenSearch enabled 官方说明](https://docs.opensearch.org/latest/mappings/mapping-parameters/enabled/)。

Hybrid 的 _source 明确包含 source_metadata；HybridSearchItem/Pydantic 字典可保留 list/null。
RagContextChunk 已保留 metadata，但当前文本 prompt formatter 不打印它；RagCitationItem 没有 metadata，
retrieval response 仍含搜索 metadata。不能假设所有 page/section/type 顶层字段都已进入 Hybrid/citations。

结论：`DocumentChunk.source_metadata.kg_refs[] → OpenSearch _source → Hybrid hit` 可沿用现有通道。
不增加 PostgreSQL 列，不改 OpenSearch mapping；M4/M7 证明真实数组往返。第一版不按 kg_refs 过滤或聚合索引。

## 3. NEO4J_SCHEMA_AUDIT

本节保存 2026-09-08 的已完成只读审计，2026-09-09 没有重导/写入 Neo4j。
实现 M5 前须复核 schema 未漂移。源为根目录 `import_graph.py`、其图谱 JSON 与实际只读查询；
没有 import/执行 importer 的顶层写入逻辑。backend 当前没有 graph repository。

- Neo4j 2026.07.1 Enterprise；已安装 Python driver 6.2.0，但 backend/pyproject.toml 尚未声明。
- JSON：`C:\Users\32884\Desktop\KG-20260826-001.json`。
- SHA256：1126444fb1ef47ee2aa3a15e94bf4af0278bad1b94e2ff3bcba40cf9dc1a7366。
- 1 graph、1 document、6 tables、138 entities、271 business relationships。

| Node Label | Properties | Identity key |
|---|---|---|
| KnowledgeGraph | graph_id, processed_time, source_type, table_count, entity_count, relationship_count | graph_id unique |
| Document | doc_id, source_pdf, source_type | doc_id unique；不是 PostgreSQL UUID |
| Table | table_id, table_ref, page, table_index | table_id = doc_id + '_' + table_ref；unique |
| Entity | id, name, entity_type, doc_id, table_ref, page | JSON 原样导入 id，全局 unique |

| Relationship | 方向 | 数量 | Properties |
|---|---|---:|---|
| HAS_DOCUMENT | KnowledgeGraph → Document | 1 | 无 |
| HAS_TABLE | Document → Table | 6 | 无 |
| CONTAINS_ENTITY | Table → Entity | 138 | 无 |
| 含有 | Entity → Entity | 62 | graph_id, doc_id |
| 规定 | Entity → Entity | 104 | graph_id, doc_id |
| 适用于 | Entity → Entity | 79 | graph_id, doc_id |
| 约束 | Entity → Entity | 12 | graph_id, doc_id |
| 替代 | Entity → Entity | 1 | graph_id, doc_id |
| 对应 | Entity → Entity | 13 | graph_id, doc_id |

唯一约束及对应 ONLINE indexes：graph_id_unique、document_id_unique、table_id_unique、entity_id_unique；
另有 node/relationship lookup indexes。没有 graph_id + table_ref 复合索引；Table 无 graph_id。
graph_id 位于 KnowledgeGraph 和业务 relationships，不在 Document/Table/Entity。
没有 Chunk/Anchor node、anchor_id property 或 chunk UUID FK。

真实 traversal：

```cypher
(g:KnowledgeGraph {graph_id: $graph_id})
-[:HAS_DOCUMENT]->(d:Document)
-[:HAS_TABLE]->(t:Table {table_ref: $table_ref})
-[:CONTAINS_ENTITY]->(e:Entity)
```

只取同 Table 内的一跳 Entity 业务关系，额外检查 relationship.graph_id/doc_id，
两端 Entity.doc_id/table_ref。标签和关系来自实查，不来自提示词示例。
已验证参数模板从 graph 唯一索引开始；EXPLAIN 含 NodeUniqueIndexSeek。

| table_ref | 候选表 | entities | relationships |
|---|---:|---:|---:|
| T-P8-1 | 1 | 68 | 156 |
| T-P8-2 | 1 | 10 | 8 |
| T-P9-1 | 1 | 21 | 34 |
| T-P13-1 | 1 | 13 | 31 |
| T-P13-2 | 1 | 21 | 36 |
| T-P14-1 | 1 | 5 | 6 |

不存在 table/graph 的参数返回空。scoped CALL 子查询已只读验证；不新增索引，规模变化引起的
性能问题单独审查，不能自行写 schema。

### 属性缺失与隔离限制

JSON 的单位、数值上下限、温度、壁厚等 properties 未被当前 importer 完整保存；不能把 Entity.name 中的
数字当成完整规范事实。负责人选择图谱辅助、文本核实，importer 修复不属于本阶段。
Document/Entity identity 非 graph-versioned；同一 doc 被多 graph 共享时无法保证属性隔离。
第一版拒绝歧义 table，以及由其他 graph 共享的目标 Document 图谱上下文，继续 Text RAG。

审计连接账号不是专用只读账号，但执行语句全部只读。M5 运行必须使用负责人提供的只读账号；
READ_ACCESS 只是 driver 路由模式，不能代替权限。不得从 importer 导入 driver 或复用硬编码凭据。

## 4. Frozen Architecture / Data Contracts

```text
                         Document
                            |
                 +----------+----------+
                 |                     |
                .md            supported non-.md
                 |                     |
          Markdown Native            MinerU
                 |                     |
           Markdown AST         ParseRun / Blocks
                 |                     |
           Anchor Parser        Block-aware Chunker
                 |                     |
            AST Chunker        existing output adapter
                 +----------+----------+
                            |
                        ChunkDraft
                            |
                   DocumentChunkWriter
                            |
                       DocumentChunk
```

两路不共享 Parser 或中间结构，只共享输出契约。MinerU Block Chunker 已无 ORM 构造，不重写算法；
保留 BuiltDocumentChunk / BuiltChunkBlockLink / ChunkBuildResult，先在 service 边界做适配。
Writer 使用现有 Session，创建/flush ORM 和真实 block mappings，不 commit；事务、guard、状态仍由 service 管理。
本轮未创建这些新接口。未来新增接口清单：

| DTO/interface | 契约 |
|---|---|
| KGRef | anchor_id:str，graph_id:str，anchor_type:str，table_ref:str\|None |
| AnchorScope | KGRef + AST 顺序边界 + 覆盖内容 node IDs + debug line range |
| ChunkDraft | chunk_index/content/token_count/page/section/type/method/format/source_metadata；parse_run_id 可空；真实 block refs |
| ChunkBlockRef | persisted block_id + block_order；不携带 ORM |
| DocumentChunkWriter.write(document_id,drafts) | 既有事务内创建 chunks/maps，不生成 Anchor，不自行 commit |
| GraphRetrievalRequest | 单 graph_id 的去重 anchors 与 source chunk/citation provenance |
| GraphRetrievalResult | typed doc/table/entities/relations + per-anchor status/truncation/provenance |

### Canonical Anchor

```markdown
<!-- kg-anchor-start
anchor_id: KG-20260826-001::T-P8-1
graph_id: KG-20260826-001
anchor_type: table
table_ref: T-P8-1
-->
正文
<!-- kg-anchor-end
anchor_id: KG-20260826-001::T-P8-1
-->
```

anchor_id 是 Source 稳定主键，不能由 chunk UUID、chunk index、字符 offset 自动生成。
KGRef 必须为 list。一个 Anchor 可覆盖多 chunks，一个 chunk 可继承多 anchors。
table 类型要求非空 table_ref；其他类型允许缺省/null，保留为 None；M5 第一版仅支持 table template。

AST 依赖拟采用 markdown-it-py==4.2.0，CommonMark + 内置 table rule + SyntaxTreeNode；无额外 YAML framework。
见 [官方 AST/token 文档](https://markdown-it-py.readthedocs.io/en/latest/using.html) 和
[版本信息](https://pypi.org/project/markdown-it-py/)。尚未安装或修改 pyproject。

1. 严格 UTF-8/UTF-8 BOM → AST。
2. 独立 HTML comment 控制节点解析；start/end 必须位于同一 AST 容器。
3. 栈式 scopes，可合法嵌套，不允许 crossing/跨容器配对。
4. 移除控制节点 → clean AST → 内容节点单元 → chunking。
5. line range 可用于保留节点的 Markdown 文本和调试，但 inheritance 只依赖节点身份交集。

Metadata 是每行 key: scalar，按第一个冒号分隔；接受普通字符串/JSON 双引号字符串和 table_ref 的 null/空值。
不支持重复 key、未知控制字段、列表、对象或多行值；不做 YAML 类型推断。end 只接受 anchor_id。
不闭合、孤立 end、mismatch、重复 anchor_id、crossing、坏 metadata、缺必填均 fail closed。
保留控制标记出现在 inline、fence 或其他非控制节点时第一版拒绝输入，确保不会流入 content。
不自动识别/迁移旧 kg-tag；不把旧示例 Cypher 当作 graph schema。空 scope 不补造正文；无 anchor 的 Markdown 正常切块。
错误沿用 BusinessError envelope，M1/M3 拟新增 DOCUMENT_MARKDOWN_INVALID / 400；只报告 reason/line/可选 anchor_id。

### Inheritance / Chunking

`chunk.used_content_node_ids ∩ scope.node_ids != empty` 则必须继承。
内容节点 ID 在同一原文下确定生成，不依赖 chunk config；root/container ID 不作为所有 chunk 的共有继承依据。
长段落拆分与 overlap fragment 保留父内容节点 ID。kg_refs 按 (scope_start_order, anchor_id) 排序并按 anchor_id 去重。
数组顺序必须稳定；JSONB 不保证对象键展示顺序，canonical serialization 用统一 sort_keys 规则测试。

标题维护 section_path 并形成边界；段落可组合，超长段落可拆；表格、代码块、独立公式保持原子性。
超长原子块可超过目标 size，但不破坏行、公式、数值或单位。whole-node overlap 只在预算允许时使用；
fragment overlap 仍带原 AST 身份。不渲染 HTML 后反向提取正文，不创建 fake MinerU block。

原生 Chunk：parse_run_id=None；block_mappings=[]；page 可空；chunk_method=markdown_ast；content_format=markdown。
source_metadata 至少：

```json
{
  "parser_provider": "markdown_native",
  "parser_version": "markdown-it-py/4.2.0",
  "chunk_method": "markdown_ast",
  "content_format": "markdown",
  "section_path": ["章节", "小节"],
  "source_range": {"kind": "markdown_ast", "start_line": 100, "end_line": 112, "ast_node_ids": ["n000042", "n000043"]},
  "kg_refs": [{"anchor_id": "KG-20260826-001::T-P8-1", "graph_id": "KG-20260826-001", "anchor_type": "table", "table_ref": "T-P8-1"}]
}
```

该 JSON 是拟定契约示例，不是已生成数据。line 为 1-based inclusive；无 refs 序列化为空数组。
不新增 KGRef column、关系表、Neo4j chunk node 或持久 chunk↔KG 外键。

### Fixed Graph Retrieval 与 RAG

aggregation/normalization：拟建 services/graph_retrieval.py；resolver：graph/templates.py；
只读查询：graph/repository.py；typed models：graph/models.py；context：rag/graph_context_builder.py。
HybridSearchService 不拼 Cypher，RAG 不直接调用 driver。

唯一模板 table_context_v1：按 graph 分组 UNWIND anchors；从 graph 唯一键走真实路径；最多取两个
候选 doc/table 检测歧义；同表 entities 和一跳关系；关系 allowlist 固定为审计中的六种。
graph/doc/table 参数绑定，用户 query 不进入 Cypher 字符串；无变长遍历。
实体按 id 排序，边按 source.id/type/target.id 排序；返回 map/typed data，不向 prompt 传 driver Node 对象。

默认：开关关闭；max anchors=8；每 anchor max entities=200、edges=400；单 query timeout=2s；
总调度预算=5s；connect/acquisition timeout 各 1s；不自动事务重试；Graph Context max=6000 chars。
可多取一条判定截断；只输出上下文内端点的边；按完整记录裁剪，不截坏 JSON。
预算到期不启动新分组，不声称操作系统层硬抢占。schema/scale 变化超限时单独审查，不先改索引。
driver lazy 创建并在 app lifespan 关闭；Settings 使用 Neo4j URI/database/username/SecretStr password。
缺配置/不可用降级为 text-only，不阻止基础 Text RAG 启动。不开启公共任意 Cypher 接口。

Trigger 位于现有 Text Context 预算筛选之后，仅查询实际保留 chunks 的 refs，确保 citation 可追溯：

```text
Hybrid hits → existing Text Context → KGRef aggregation/dedupe/group
→ fixed GraphRetriever → typed Graph Context → Text + Graph prompt → LLM
```

- 无 refs/无 table refs/feature off：零 Neo4j 调用。
- 同一 anchor 多 chunks 命中仅查一次，保留所有触发来源；冲突 payload 丢弃该 anchor，不能任选版本。
- 多 graph 分组；单组失败保留其他成功结果；超时/歧义/无结果/不可用不破坏 Text RAG。
- 不新增 graph-required intent；原 no_context 仍不调用 LLM；检索和 LLM 的原错误契约保持。
- 文本预算默认 12000 chars 不被 graph 挤占；graph section 并列追加。
- 内部保留 template/version、graph/anchor、Neo4j doc/table、entities/edges、PG document/chunk/citation、source_range、status/truncation。
- 公共 RagCitationItem/RAG response 不变；外部 [n] 仍定位文本 chunk，内部链为 citation→anchor→graph evidence。
- 数值/单位/条件必须由当前可见文本支持；图谱不补造未召回或被截断的参数。
- 不引入持久 graph cache，也不从未命中的文档补取原文绕过删除过滤。

### Hard Delete

finalize_postgresql_deletion 直接按 document_id 或 manifest chunk IDs 删除 chunks，不依赖 run/block join。
manifest 收集 chunks 独立于 runs/assets；原生路径只保留现有 raw object，AST/provenance 在内存/JSONB，
不创建无法归属 ParseRun 的 MinIO 衍生对象。沿用最终 DocumentOperationGuard 写入屏障，不改 saga。
删除文档清除其 chunks/embedding/index/KGRef projection；独立 Source Graph 保持，不加入 Neo4j 删除步骤。

## 5. Dependency Matrix / Acceptance / Stop

| 能力 | 当前 | Phase 11 增量 | Gate |
|---|---|---|---|
| AST | 无 | markdown-it-py、anchor parser | M1 owner review 后 |
| Chunk output | MinerU DTO 已有 | 仅 MinerU output adapter + 原生 ChunkDraft + writer | 不改 Block Chunker 算法 |
| PG | nullable run / JSONB | 无 migration | 专用库 roundtrip |
| OpenSearch | enabled=false metadata | 优先只增加测试 | 专用 index roundtrip |
| Neo4j | 图谱已存在 | neo4j==6.2.0、只读 repository | 只读账号、schema 无漂移 |
| RAG | 内部 metadata 已有 | graph trigger/context/prompt | API/citations 不变 |
| Hard Delete | Phase 10 稳定 | 原生 null-run regression | 原有安全门禁，禁止扩大删除 |
| 评估 | test.md 有一组有效 anchor | 合成 edge cases + 人工审定完整多表 Source/goldens | 不自动生成真实 Anchor |

M0—M7 细节见配套 Implementation Plan。每阶段 TDD RED→GREEN→REGRESSION，独立验收/commit。
本轮 P0 已单独提交；M1 未开始。禁止 unrestricted Text-to-Cypher、LangChain/LangGraph、multi-agent
framework、GraphRAG migration、graph embedding/GNN、自动图谱构建、自动/PDF Anchor、图可视化/Cypher UI。

ARCHITECTURE_CONFLICT_REQUIRING_OWNER_DECISION = NONE（基于本次冻结条件）。若后续必须增加强绑定、
KGRef column、写 Neo4j、修改 Hard Delete/Knowledge、提供生产重切或修改公开 RAG response，停止并单独提交架构冲突。
当前本地 test.md 仅覆盖 T-P8-1，其他旧标注有 kg-tag/错误示例 Cypher；完整真实验收不能伪装为已覆盖。

Self-review：真实路径和符号已区分新增；schema 来自实际审计；无当前原生 MD 假设；metadata 与 nullable
兼容结论分层；table_ref 可空，kg_refs 为 list；AST 控制文本不进入内容；查询 fixed+parameterized；
不破坏 Hard Delete；没有任何未来 Basic production path。
