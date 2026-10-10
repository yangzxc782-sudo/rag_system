# PDF/KG 分阶段实施：M0 / M1 / M2 / M3 / M4 / M5

## 结构感知切分恢复 S4：API、前端与旧入口退役（2026-10-08）

当前 PDF 唯一算法是 `pdf-block-aware-codepoints-v1`，仍按冻结来源 → 图谱 → 结构感知切分
→ 向量 → 索引验证/封存 → CAS 发布执行。本节替代下方历史阶段中的旧切分入口说明。
本轮仅完成 S4 及直接依赖；S5 综合验收及真实服务验收尚未执行。

- 默认值只在后端 `BlockChunkerConfig` 定义：
  `{"max_chunk_chars":1800,"min_chunk_chars":200,"overlap_chars":0,"max_table_chars":4000,"keep_table_intact":true,"keep_formula_with_context":true}`。
  两个现有列表 API（processing-jobs、chunk-sets）下发 segmentation_defaults/segmentation_version。
  前端验证后才启用提交，不硬编码备用默认值，不在获取失败时 fallback。
- process/rechunk 创建允许省略 config 或合法部分六字段，由同一模型补齐、严格验证。
  null、旧 chunk_size/overlap/boundary、未知字段及非法类型拒绝；持久化仍要求全部六字段。
  resume 省略 config 复用冻结配置（未冻结才取默认）；null 拒绝，显式不同配置仍冲突。
  retry/单步 advance 不接受新配置，完整配置与指纹沿用 S3。
- 共用六字段表单明确说明：正文目标不是表格上限；整表保护开启可超过两个阈值，关闭时
  按表格阈值分物理行，正常片段也可超过正文目标。关闭公式合并也不拆断独立公式。
  小尾块阈值不是每片最低长度；overlap 按剩余容量与安全边界尽量添加，可以为 0。
- 待确认请求缓存 UUID、完整配置和切分版本。旧格式/损坏/未知版本须用户显式丢弃，
  没有自动转换或轮询自动重发。合法缓存刷新后保持原 UUID/配置，显式提交前先查任务。
- PDF 未冻结时不展示旧独立 embedding/索引操作。切片列表显示真实类型、显示标题、完整
  章节路径、格式、方法、页码、区间、块和资产。直接依赖增加 source_metadata.table_fragmented：
  依据已验证冻结目录判断 chunk 是否仅包含表格的一部分，随 metadata 进入 manifest/SQL/index
  严格校验；不改正文、区间、kg_refs 或图谱身份。无此字段时显示状态未记录，不猜测完整性。
- 退役 sequential_chunker.py、chunk_adapters.py 及旧 sequential 测试。共享 overlaps 位于
  source_intervals.py，锚点映射位于 chunk_anchors.py；中性 chunk_drafts.py 保留，writer 测试直接
  使用中性 Draft。结构/来源/映射由 block/ChunkSet 测试覆盖。不保留第二种 PDF 算法。
  无消费者的旧 Settings/示例 CHUNK_SIZE_CHARS、CHUNK_OVERLAP_CHARS 已移除，不改真实 .env。

### S4 定向前端验证（不等同 S5）

复用 Playwright；SSR 和浏览器只访问 loopback fake API，未知路由 404 且不转发。
独立构建目录 .next-pdf-structure 不覆盖开发构建；两端口占用时拒绝复用服务器。
在 frontend 目录执行（仅命令环境变量，不写 .env.local）：

```powershell
$env:PDF_STRUCTURE_E2E_MOCK="1"
$env:NEXT_PUBLIC_API_BASE_URL="http://127.0.0.1:19081"
$env:PDF_MOCK_BROWSER_CHANNEL="chrome"
$env:NEXT_TELEMETRY_DISABLED="1"
npm run build
node node_modules/@playwright/test/cli.js test --project=pdf-structure-mock
```

使用本机已安装 Chrome；也可省略 channel 使用预先安装的 Playwright Chromium，测试不自动
下载浏览器。fake 无真实模型或存储客户端；SQLite/Fake/浏览器通过不代表真实服务验收。

### S4 实际验证记录

在 backend 目录执行以下两组互不重叠的定向测试（不执行 S5 全量回归）：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/test_document_processing_api.py tests/test_chunk_set_api_context.py `
  tests/test_document_processing.py tests/test_chunk_set_builds.py `
  tests/test_document_chunk_writer.py tests/test_document_parsing.py `
  tests/test_document_parsing_mineru.py tests/test_pdf_cleaning_pipeline.py
# 152 passed, 0 failed, 0 skipped

.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/test_block_chunker.py tests/test_frozen_source.py `
  tests/test_graph_sources_v2.py tests/test_graph_snapshot_history_v2.py
# 194 passed, 0 failed, 0 skipped
```

前述 Playwright 最终 22 passed、0 failed、0 skipped；初次因未安装配套 Chromium 无法启动，
随后使用已有 Chrome 验证通过。覆盖后端默认值/无备用常量、配置失败阻断与恢复、旧缓存拒绝、
合法缓存 reload、完整/重切分请求、retry/resume/单步使用冻结配置和表格片段展示。
typecheck、lint、独立构建、mock JS 语法和本轮修改 Python 文件 py_compile 均通过。
构建在受限沙箱内遇到 Windows 路径访问错误，使用相同 fake 环境在授权执行环境中完成。

标准 git diff --check 仍报告不是 work tree；未修 Git 配置。分别完成 diff-files --check
（已跟踪未暂存）、diff-index --cached --check HEAD（暂存）及五个未跟踪源码的独立空白检查。
本轮没有真实模型/资产写入、migration、真实 .env 修改或 commit；真实与隔离 PostgreSQL
验收均未执行。停止在 S4，后续 S5 需另行批准。

## 结构感知切分恢复 S3：ChunkSet 契约集成（2026-10-08）

PDF ChunkSet 主路径已改为调用 S2 `build_block_aware_chunks`，只消费 S1 冻结 source-map v2。
S3 当时未退役 sequential 模块、未改前端表单或旧缓存；这些工作现已在上节 S4 完成。
没有新增 migration、真实数据操作、解析/模型服务调用或真实发布验收。

- `BlockChunkerConfig` 是唯一六字段定义，默认 1800/200/0/4000/true/true；
  S3 补齐 `0 < max_chunk_chars <= 60000`。请求省略字段按此模型补齐，禁止旧字段。
  **持久化**配置必须包含全部六字段，读取时不允许补缺省值；同时核验版本和配置 SHA-256。
- `segmentation_version=pdf-block-aware-codepoints-v1`。配置 JSON 使用既有
  `ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False` 后 UTF-8/SHA-256。
  process pipeline 输入和 rechunk 输入含完整配置、版本与配置指纹；独立 M2 job 交接到 M3 时，
  保留原 KG 输入身份，在 checkpoint 中另行冻结 `chunk_input` 及其指纹。
- resume 不传配置时使用已冻结配置（没有冻结配置才用默认）；显式传不同配置拒绝，
  包括显式空配置对象所表达的默认配置。retry 不接收新配置，不回到旧 Settings 的 1000/100。
- **manifest v3**：绑定 document/parse/source/build/set 身份、canonical_sha256、
  source_map_sha256（对应 SourceDocumentVersion.block_map_sha256）、切分版本、完整配置和指纹、
  chunk_count；每项含确定性 UUID/序号、区间/content_sha256、完整结构 metadata 和有序 block links。
- draft、manifest、SQL 和索引保留 chunk_type、section_title、content_format、chunk_method、
  token_count、parse_run_id、页码、section_path/block_ids/block_keys/block_types/asset_keys。
  `source_metadata` 同时保留结构字段和 kg_refs，进入检索及 citation/snapshot，不以 kg_refs 替代完整校验。
  index 协议仍为 v2，manifest v3 通过原 manifest_sha256 和精确 payload readback 绑定，不改 mapping。
- chunk 写入后、embedding/indexing 前、CAS 发布前重新核对完整内容和有序 block links。
  检索请求显式读取结构字段，并与权威 SQL 比较；图谱来源校验同时检查结构字段与 metadata 一致。
- kg_refs 由同一 document/source/build 的严格半开区间相交生成；去重、冲突拒绝及排序保持。
  KG units、GraphBuild/provider 指纹、graph_id/anchor_id 均不变，完整覆盖仍由 M4 判断。
- 保留 PDF_KG_MAX_CHUNKS、租约/fencing、IO reconciliation、create-only 索引写入、封存和 CAS。
  表格/公式遵循 S2 各自长度规则，没有全局正文上限断言。现有 section_title 列限长 255：
  Draft 统一使用最后一级章节标题的前 255 个 Python Unicode code points 作为显示投影，
  不按 UTF-8 字节截取、不添加省略号；完整 section_path 保存在 metadata，正文和区间不变。
  manifest/SQL/index 共享该投影，仍执行字段长度和完整内容一致性校验，不扩展 schema。

离线验收覆盖 A/B/C 完整六字段配置（1800/200/0、1200/200/120、3000/200/100，
其余 4000/true/true）、同 set 重算、JSON 对象键重排、真实结构 metadata 全链路、历史引用、
配置冲突、manifest/SQL/index 损坏、重试和 CAS。使用 SQLite/FakeAssets/FakeProvider/FakeIndex，
不等同真实 PostgreSQL JSONB/触发器、OpenSearch 或 embedding 服务验收。

## 结构感知切分恢复 S2：六字段配置与连续区间算法

S2 在 ingestion/block_chunker.py 内替换旧的正文重拼实现，不新增第二个结构切分器。
算法只接收 S1 FrozenSource、document/parse 身份及来源目录，先验证 v2 字节、哈希和
目录，再输出连续区间和纯内存 chunk/block 引用。不再接收可变 DocumentBlock 或 renderer。
版本为 pdf-block-aware-codepoints-v1，未知/旧版本直接拒绝，没有版本分派或 fallback。

**S2 当时的阶段边界（S3 进展见上节）：** 未修改 ChunkSet/manifest/任务/API/前端，主流程仍使用原入口，
切换留待 S3/S4；暂不部署或运行真实 PDF。旧 sequential_chunker.py 尚未退役，仅迁出其
共享 overlaps 到 source_intervals.py，图谱来源和证据校验改用相同的严格相交纯函数。

### 配置

BlockChunkerConfig 是严格、不可变、禁止未知字段的 Pydantic 六字段模型：

| 字段 | 类型 | 默认值 | 校验与用途 |
|---|---|---|---|
| max_chunk_chars | int | 1800 | >0，S3 补齐 <=60000；普通内容合并和拆分目标 |
| min_chunk_chars | int | 200 | 0..max_chunk_chars；同一长正文最后两个基础片段的合并阈值 |
| overlap_chars | int | 0 | 0<=值<max_chunk_chars；长正文的尽力重叠 |
| max_table_chars | int | 4000 | >0；关闭整表保护时的物理行分组目标 |
| keep_table_intact | bool | true | 整表保护；false 支持完整物理行分组 |
| keep_formula_with_context | bool | true | 短公式与相邻上下文合并；false 保持独立 |

不强制类型转换、不 clamp、不读取旧 Settings，也不增加环境变量；只指定小于 200 的
max_chunk_chars 时必须同时提供合法 min_chunk_chars。旧 chunk_size/overlap/boundary 和
include_headers_footers 被该模型拒绝。默认补齐后的全部六字段以现有确定性 JSON/UTF-8/
SHA-256 计算配置 fingerprint，未在 S2 接入任务或持久化指纹。

### 区间规则与长度

- section_path 变化必断；相邻标题、正文、列表等在同章节且实际区间容量允许时合并。
  无重新继承章节、远处标题复制或正文反向匹配。
- 表前普通正文先结束；小表只可与紧邻、同章节且容量允许的纯标题组合并。表后必结束。
  keep_table_intact=true 时 6000 字符表格仍完整；false 且表格不超过 4000 时也完整，
  因此 2500 字符表格可以超过正文的 1800。超过表格阈值时按完整 LF 物理行贪心分组，
  正常 3500 字符片段合法；单行超过 4000 也不截断。不复制表头或声称关闭保护后仍整表完整。
- 独立公式从不内部拆分；短公式按开关、章节和容量合并，超长公式独立输出。
  false 的含义是独立保留，不是允许拆公式。
- 长普通块优先选择段落、空白边界，再退至 code-point 边界。小尾块仅在同一正文最后
  两个基础片段连续且合并后不超过正文目标时合并，不跨章节、表格、公式或其他 block。
  0 禁用此尾块合并；短结构块无需凑足 200。
- overlap 只向前扩展同一长 text/list/footnote/unknown block 内后片段的 start，end 不变。
  同时受请求值、剩余容量、前一基础片段和空白边界限制，可缩小或为 0。1800/100 下两个
  满 1800 的片段不强行重叠；不插分隔符，不重叠表格或独立公式，不跨 block/章节。
- block 间已有分隔符归前块；拆分边界后的连续空白也归前片段。长起始空白跟随第一个
  非空白字符；不生成纯空白 chunk。所有字符由 canonical 切片得到，长度按实际区间计算。
  普通正文、表格各用各的目标；受保护整表、超长物理行、独立公式及所述空白有对应例外，
  不存在适用于全部 chunk 的 len(content)<=max_chunk_chars 断言。

每个结果带 source_version、source_start/end、content_sha256、结构类型、章节/页码、
格式、资产和有序 block 引用，满足 content == canonical_text[start:end]，Python
Unicode code-point 左闭右开；不引入多区间、strip 后重拼或查找重复文本来反推坐标。
基础片段无遗漏，重叠后仍严格前进；公式及开启保护的表格，任何相交 chunk 均完整包含它。
KG units/anchors/提取/writer 均不改变，kg_refs 与持久化集成留在 S3。

仅保护已识别的 table/formula block。不新增行内公式保护、跨页逻辑表合并或 HTML 行语义
修复；超大结构的 embedding/RAG 下游输入上限仍需后续独立验收。

## 结构感知切分恢复 S1：冻结 source-map v2（2026-10-08）

S1 只补齐不可变来源目录；本节保留 S1 当时的实施边界，S2 进展见上节。
不应在中间阶段部署或开始真实 PDF 处理。下方 M0–M5 记录保留当时的验收事实。

`source-map.json` 的唯一受支持格式为 `schema_version=2`。保留 document/parse_run/
source_version 身份、canonical SHA-256、字符数、坐标与规范化约定；每个 block 保留
ID/index/key/type、页码和 `[source_start, source_end)`，新增三个必需字段：

- `section_path: list[str]`：冻结时优先取 block 的显式章节路径，再取 metadata 中的
  路径；无路径的标题以当前渲染标题建立路径，其他块继承最近有效路径。
  这是原结构块准备规则，已有 cleaner 提取的多层章节不再解析；空块、页眉页脚不更新路径。
- `content_format: str`：直接保存既有 `render_cleaned_block()` 返回的格式，不根据
  block 类型或扩展名猜测。验证允许 `plain_text`、`markdown`、`mixed`。
- `asset_keys: list[str]`：保存当前块引用，稳定去重、保持列表顺序；无引用时为 `[]`。
  冻结时必须属于本次 normalized parse asset 清单和同一 output prefix，拒绝越界路径。
  清单表示已登记引用，不能单凭该字段宣称外部对象已经存在。

章节和资产列表复制到目录中，不原位修改结构块。正文 renderer、cleaner、LF/NFC、
UTF-8、固定 `\n\n` 分隔和 code-point 区间规则保持不变；新增目录字段只改变目录哈希。
目录仍使用确定性 JSON 编码，不依赖对象键插入顺序，也不重排 block 或有序列表。

解析上传后的回读和 M2/M3 共用的 `_source_text()` 都验证 v2：两份资产哈希、来源身份、
字段及类型、block 唯一性/次序、页码、连续区间和资产前缀。canonical 与目录必须位于
同一来源目录。旧/未知版本、缺失结构字段或不合法引用明确拒绝；没有从可变 DocumentBlock
补取字段、兼容旧目录或退回字符切分的分支。

M2 继续使用原有 KG 单元、锚点和 extraction piece 规则；不以 `section_path` 替代 KG
标题识别。S1 不修改数据库 schema、Neo4j schema、抽取模板或任何预算。

## M5 实施契约与验收（2026-10-07）

本轮仅实施持久处理任务、完整处理 API、页面及新版资产删除生命周期。基线仍为
`phase13-Multi-turn-RAG` / `b3605feb8ce5963fbed7b9f627f0af8bd58aecb9`，保留已有 M0–M4 未提交改动。
任务复用 M0 的五类版本/任务记录，没有新增数据库表或迁移，没有修改历史迁移、历史评测或锁文件。
完整契约、文件清单、恢复限制和验收结果见 [M5 生命周期说明](pdf-kg-m5-lifecycle.md)。
下列 M0–M4 章节保留各阶段当时的事实与验收边界；例如早期冻结文档禁止删除，已由 M5 的版本化删除替代。

## M4 实施契约与验收（2026-10-07）

本轮仅实现新版在线图谱查询、最终文本证据约束、API/UI 与 Phase 13 快照恢复。
未进入后续里程碑，未正式启用新准入，未处理存量 PDF。基线分支
`phase13-Multi-turn-RAG`，HEAD `b3605feb8ce5963fbed7b9f627f0af8bd58aecb9`；
保留所有既有 M0–M3 未提交修改。未修改 .env、依赖或锁文件，未提交 commit。
本轮以已验收 M2 的 kg_protocol、固定模板及 kg_writer 实际属性核对查询，没有假称重新读取参考 ZIP。

### 实际链路与证据策略

Hybrid → 原 weighted RRF → 可选 BGE → 最终预算文本 → SQL 来源验证 →
固定 Neo4j 只读查询 → SQL 准入复核 → 图谱证据预算 → prompt/LLM。
没有改动 RRF/BGE 算法、分数或模型参数，也没有恢复纯向量接口。

业务来源仍只有 source_version/source_start/source_end，文档、构图、chunk set 身份独立。
RagContextChunk 新增 effective_start/effective_end 两个标量，仅表示本次保留的正文前缀。
坐标为 Python Unicode code point 左闭右开区间，end=start+实际保留原文字符数；
省略标记、提示词引用标签、标题头不计入原文字符数。没有区间列表或局部投影框架。

GraphSourceAuthority 通过短 SQL 读取联合验证文档正常/PDF、冻结来源、parse_run、
sealed/ready GraphBuild、模板哈希、图谱写入收据、indexed/sealed ChunkSet、索引验证收据、
当前发布指针及 chunk 内容哈希/区间/元数据。完整 chunk 的 refs 必须与权威单元索引一致，
有效前缀再与单元按严格相交筛选；端点相接不映射。控制字段不进入正文、embedding、
关键词、BGE 或文本 prompt。每个 chunk 的 provenance 单独保留，查询身份去重不覆盖冲突。

“完整连续 KG 单元被当前保留文本覆盖”是本期保守的**事实使用策略**，不是映射算法。
部分长条款/长表可以映射成功、查询成功，但实体及关系不进入 prompt。多个保留 chunk
可共同覆盖同一单元；按起点排序逐个检查覆盖，遇到缺口拒绝，不使用 min/max 包络。
不引入逐三元组位置、自动补召、摘要改写或远处标题/表头复制。
图谱预算只准入完整证据单元，不截断数值/单位/适用条件对象。
facts_used 表示该证据被放入最终回答 prompt，不宣称能够观测 LLM 是否在自然语言中实际采用每条事实。

### 查询及证据格式

- 唯一运行模板为 anchor_context_v2；旧 table_context_v1 与 Document/Table/Entity 运行模型已退役。
- 同时支持 M2 的 table/clause discriminated union，不要求 clause.table_ref。
- 匹配 KnowledgeGraph 的 document_id/source_version/graph_build_id/graph_id/source_path、
  payload_sha256、schema=2、built 状态及模板版本；经 HAS_ENTITY 定位同锚点 MaterialEntity 集合。
- 只读 RELATES_TO，业务类型读取 relation_type，关系与两端都核验 graph/build/anchor，
  两端都必须属于对应 HAS_ENTITY 集合，实体类型及关系 from/to 规则来自 M2 固定模板。
- 不查询 HAS_TRIPLE，避免重复计数；不查询 writer 未写入的 clause_ref/table_no/heading_ref 顶层属性。
  展示锚点字段来自已验证 SQL 单元索引，没有伪造 Neo4j Document 或 Table。
- 固定查询、参数绑定、稳定排序、实体/关系/锚点数量上限、单查询超时及调度总预算。
  READ session 禁止自动重试；无任意 Cypher 接口、全库相似扫描或写入。
- properties_json 严格解码并验证身份，拒绝重复键、非有限数、超长属性与身份冲突；
  保留完整业务值/条件/来源对象，投影掉图谱控制身份。跨图或跨锚点返回拒绝为可用证据。

API graph 为 schema_version=2，证据为 anchor（table/clause）+ source（真实 SQL 来源）+
entities/relationships/source_citations。诊断分别给出 mapped、query_status、
full_unit_covered、facts_used、use_status；查询成功但未使用为 not_used。
Neo4j 暂不可用保留文本回答，诊断 timeout/unavailable；不会触发构图或旧查询 fallback。
图谱开关启用但新版来源准入未启用时，只给 v2_admission_disabled 诊断，不执行旧图谱查询。

### 持久化与恢复

复用既有 QA snapshot/artifact，无新增表、无新增 Alembic 迁移。
GraphPayload 显式携带 schema_version=2，保存 verified binding 与各引用有效区间。
EvidenceDetails 保存图谱证据版本及诊断；旧记录缺失版本保持缺失，不伪造为新版。

恢复前同时检查快照来源集合、当前引用的有效区间、完整单元覆盖、SQL 不可变来源绑定，
再重建 prompt 并比较原指纹。恢复只读取历史原 chunk set，允许已被新 set 替换的历史
indexed set，但仍要求文档正常、原来源/构图/封存/收据均有效；不会重查 Neo4j或调用抽取模型。
新检索仍只准入当前 set。相同内容的新 chunk set 不能冒充旧 citation。

不支持的旧图谱/证据版本返回 QA_GRAPH_EVIDENCE_VERSION_UNSUPPORTED（409）；
失败释放本轮占用，不允许同请求恢复旧执行，提示发起新一轮检索。
已发布历史回答正文和引用记录保留，旧图谱快照标为 unsupported_version，不按新版反序列化，
不更改快照 payload，不关闭 prompt 指纹或来源检查。
删除/证据失效仍隐藏相关图谱；M1–M3 的冻结文档删除守卫未放宽。

### 本轮文件级变更

| 操作 | 文件（相对根目录） | 职责 |
|---|---|---|
| MODIFY | backend/app/graph/models.py、templates.py、repository.py | 替换旧四字段/Document/Table DTO 与查询；新来源绑定、固定查询、严格解码和预算 |
| ADD | backend/app/services/graph_sources.py | 只读权威索引、已发布来源校验、有效前缀及历史绑定验证 |
| MODIFY | backend/app/services/graph_retrieval.py、rag.py | 经验证绑定分组查询，前后准入检查，退役 M3 临时图谱禁用分流 |
| MODIFY | backend/app/rag/context_builder.py、graph_context_builder.py、graph_response.py | 前缀有效区间、完整单元覆盖、完整证据预算、独立诊断/API 投影 |
| MODIFY | backend/app/services/chat_evidence.py、conversation_history.py、conversation_recovery.py | 多轮最终预算、历史读取、不支持版本的明确终止与新检索提示 |
| MODIFY | backend/app/rag/conversation_nodes.py、schemas/conversation_rag.py、schemas/rag.py、schemas/conversations.py | V2 快照、恢复来源校验、原 prompt 指纹、真实 source 类型 |
| MODIFY | frontend/components/GraphEvidencePanel.tsx、RagAnswerPanel.tsx；frontend/lib/rag-evidence.ts、qa-sessions.ts | 条款/表格、来源版本与区间、映射/查询/事实使用、旧证据提示 |
| ADD | backend/tests/graph_v2_support.py、test_graph_sources_v2.py、test_graph_snapshot_history_v2.py | M2 writer 形状的 fixture、SQLite 来源/历史/rechunk、版本拒绝与只读历史 |
| ADD | backend/tests/test_graph_v2_postgresql.py | 独立授权门禁的 0013 PostgreSQL/Checkpoint 验收，当前仅收集 |
| MODIFY | backend/tests/test_graph_{repository,retrieval,context}.py、test_rag_graph_{fusion,response}.py | 用新版协议替换纯旧图谱用例，保留隔离/预算/失败/来源保护 |
| MODIFY | backend/tests/test_rag_reranking.py、test_rag_reranker_contracts.py、reranker_observation.py、test_conversation_m3_prompt.py、test_chunk_set_api_context.py | 换新版 fixture 和查询观察契约，保留 RRF/BGE/引用断言 |
| MODIFY | backend/tests/phase13_integration/test_conversation_m{3,4}_postgresql.py | 移出旧图谱专用用例；新版恢复/重切分/失效测试移至单独门禁套件 |
| MODIFY | backend/tests/test_document_deletion_api.py、test_rag_service.py | 补齐旧 SimpleNamespace fixture 的既有 process_status/启动配置；不修改业务守卫 |
| MODIFY | frontend/tests/chat.spec.ts、README.md、本文件 | 新 UI fixture、当前运行约定和验收边界 |
| KEEP | M0 ORM/0013、M1 冻结、M2 构图/writer、M3 切分/向量/索引、全部真实数据及冻结评测结果 | 无真实迁移、模型或业务存储写入；无数据回收 |

### 验收记录与限制

专项与完整离线回归结果在本节末尾记录。测试包含纯函数/driver double、SQLite 临时数据库
服务集成、单轮与 Phase13 恢复及现有 RRF/BGE/引用/清洗回归。它们不能替代真实
Neo4j Cypher 执行、PDF/模型语义验收或 PostgreSQL 触发器/锁/Checkpoint 联调。

- 新版 PostgreSQL 套件须另行授权：PDF_KG_M4_TEST_MIGRATIONS=0013_pdf_kg_versions，
  并满足既有 PHASE13_TEST_* 独立实例校验。它会创建并保留隔离测试库，不可指向业务库。
- 真实 PostgreSQL 迁移/用例、真实 Neo4j 查询与写入、模型/embedding 调用、PDF 全链路、
  MinIO/OpenSearch 写入、浏览器交互验收：本轮均未执行。
- 首次上线顺序仍是代码开发 → 隔离验收 → 单独授权准备新版 PDF/source/KG/chunk/index 资产 →
  核对可检索数量 → 单独授权正式启用准入。新准入排除未升级 PDF/chunk；没有旧图谱 fallback。
- 回滚应用不等于降级解释新版快照；旧/新图谱快照不可互译。应保留源与版本资产，
  暂停新准入/新任务后用已验证版本恢复；不得清库或回退发布指针来掩盖失败。
- 残留旧符号分类：当前 backend/app 与 frontend 消费者没有旧运行符号；
  test_graph_repository.py 仅在退役的负断言里保留旧名字。历史文档/验收记录不改写。
  被 Git 忽略的 backend/build/lib 旧构建缓存仍含旧代码，未删除或更新；已核实本次
  Python 导入路径是 backend/app/graph/repository.py。正式打包/部署须用新构建产物，
  不能发布该旧缓存。本轮没有重启或改动正在运行的业务服务。

### M4 实际验证命令与结果

以下均在本机离线执行；测试 doubles/SQLite 通过不代表真实服务验收。
最终总回归一次 **1747 passed in 54.64s**；之后补充历史独立引用诊断并复验受影响范围，
**130 passed in 5.71s**。两组有重叠，不相加为通过总数。早先全量的 5 个 fixture 失败
已经修复并在上述全量中通过；过长参数测试 ID 导致的 Windows pytest 环境变量错误
也已用短参数 ID 修复，没有放宽数据校验。

backend 工作目录，全量离线命令：

```powershell
$m4Temp = Join-Path (Get-Location) ('.test-artifacts/m4-' + [guid]::NewGuid().ToString('N'))
$m4Files = Get-ChildItem tests -Filter 'test_*.py' -File | Where-Object { $_.Name -notmatch 'postgresql|test_casting_engine.py' } | ForEach-Object { 'tests/' + $_.Name }
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $m4Temp --tb=short @m4Files
```

最终受影响范围复验：

```powershell
$m4Temp = Join-Path (Get-Location) ('.test-artifacts/m4-' + [guid]::NewGuid().ToString('N'))
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $m4Temp --tb=short tests/test_graph_repository.py tests/test_graph_retrieval.py tests/test_graph_context.py tests/test_graph_sources_v2.py tests/test_graph_snapshot_history_v2.py tests/test_rag_graph_fusion.py tests/test_rag_graph_response.py tests/test_conversation_m3_prompt.py tests/test_conversation_m3_contracts.py tests/test_conversations_schema.py tests/test_conversations_api.py
```

- 单元/合成编排：新查询属性、绑定、关系两端/类型、冲突、超时/预算、条款与表格、前缀覆盖、
  API 事实使用状态、prompt 降级、快照版本/原文指纹和旧历史只读处理均通过。
- SQLite 集成：test_graph_sources_v2.py 单独 **18 passed in 4.24s**，涵盖 M0–M3 真实 ORM
  的来源归属、ready_empty、真实 substring、连续区间缺口、三套切分、零重构图及历史恢复。
  不证明 PostgreSQL 专用 FK/触发器/锁或真实 Checkpoint。
- PostgreSQL/Checkpoint：下列命令仅 **71 tests collected**，包括迁移后的 M4 独立套件。
  没有设置授权环境变量，没有运行 migration/DDL/业务写入。

```powershell
./.venv/Scripts/python.exe -B -m pytest --collect-only -q -p no:cacheprovider tests/test_graph_v2_postgresql.py tests/phase13_integration/test_conversation_m3_postgresql.py tests/phase13_integration/test_conversation_m4_postgresql.py
```

frontend 工作目录，两项最终均 exit 0：

```powershell
node node_modules/typescript/bin/tsc --noEmit --incremental false
node node_modules/eslint/bin/eslint.js components/GraphEvidencePanel.tsx components/RagAnswerPanel.tsx lib/rag-evidence.ts lib/qa-sessions.ts tests/chat.spec.ts
```

项目根目录 `git -c core.safecrlf=false diff-files --check` 通过。
浏览器测试已更新 clause fixture 与可访问性断言，但未启动浏览器/Next 构建或业务前后端服务。
构建缓存和新生成的离线测试临时目录未作为交付部署产物。
模型、Neo4j/MinIO/OpenSearch 真实写入：0；真实数据库迁移：0；
M4 新增迁移文件：0（M0 的 0013 仍保留，未在本轮执行）。
待授权的数据工作仍是隔离真实联调、准备存量 PDF 的新版资产、正式切换准入和未来回收，
不包含自动清库、旧快照改写或无确认启用。

## M3 实施契约与验收（2026-10-07）

本轮只接续构图后：顺序切分→映射→向量→独立索引验证→SQL 发布。
在线图谱只增加版本隔离，未更换图谱证据/多轮 schema；不进入 M4。
起始分支 `phase13-Multi-turn-RAG`，HEAD `b3605feb8ce5963fbed7b9f627f0af8bd58aecb9`。
已有 M0–M2 大量未提交改动全部保留；本轮没有提交 commit、安装依赖或修改真实配置。

复用 ChunkSet、DocumentChunk、DocumentProcessingJob 及 Document.current_chunk_set_id/
publication_revision，无新增表或迁移。每个 chunk 严格等于 canonical[start:end]，字符坐标
为 Unicode code point，连续半开区间，可 overlap；不拼接标题/表头、不改写或摘要。
KG refs 只由同 document/source/build 的严格区间相交产生，字段仍为新业务锚点。

首次切分继续 M2 的 process job（沿用其 request_id）；rechunk 创建独立 request/job/set，
复用当前发布的 source/build；首次发布失败且存在同一来源/构图的失败切片任务时，也允许
显式新建 rechunk 请求，修正切分配置，不重绑或覆写原任务。两种路径均不解析清洗、不抽取、
不生成图 ID、不调用 Neo4j。没有先前切片任务时不能借 rechunk 绕过 M2 的 process 交接。
所有旧 chunk、知识条目关联、历史引用保持原样。向量仅在文本与完整 embedding 指纹相同
时复用，否则计算新向量。模型 revision 需由配置显式声明，变更模型须变更指纹。

每个 set 使用一个独立物理索引，不修改旧 alias，不按文档删索引记录。索引 _meta 绑定
document/source/build/set、manifest 与 embedding 指纹；bulk create 可恢复，封禁写入并
回读逐条核验后才提交 SQL indexed 和 Document 发布指针。SQL revision CAS 失败、
租约过期、模型或索引失败均不切换当前版本。保留未发布资产供审计，不自动回收。

新准入通过独立开关显式启用，默认关闭以允许先准备资产。启用后按 SQL 当前已发布 set
的精确索引集合查询，并在 RRF 前复核所有权/当前指针/来源元数据；旧 Markdown、未升级
PDF、旧 chunk set、未完成版本不参与。开关开启时空集或失败不 fallback；RRF/BGE 算法、
分数语义与模型参数不变。正式启用与存量 PDF 处理仍待单独授权。

### M3 文件级变更

| 操作 | 文件（相对项目根目录） | 实际职责 |
|---|---|---|
| ADD | backend/app/ingestion/sequential_chunker.py | characters/line/paragraph 三种连续边界，严格相交，真实 block/page 辅助来源，数量上限 |
| ADD | backend/app/services/document_chunk_sets.py | process 交接、独立 rechunk、manifest、逐批向量、租约/fence、发布 CAS、状态/列表 |
| ADD | backend/app/services/embedding_contract.py | 显式模型 revision 指纹与有限 float32 向量契约 |
| ADD | backend/app/search_engine/versioned_index.py | 无 alias 的独立索引、create-only 重试、写封禁、全量回读与身份/内容/向量校验 |
| ADD | backend/app/services/retrieval_admission.py | 查询前选当前版本精确索引，RRF 前以一次 SQL 联查重新验证当前版本与来源 |
| ADD | backend/app/services/versioned_document_guard.py | 阻止旧向量/索引写入口修改冻结流水线文档 |
| ADD/MODIFY | backend/app/schemas/document_chunk_set.py、api/v1/documents.py | 显式创建、列表、状态、advance/retry；读取 chunks 可指定版本 |
| MODIFY | backend/app/services/{document_parsing,document_deletion,embeddings,search_index}.py | 扩展冻结资产守卫；默认 chunk/embedding 统计仅当前版本；旧索引统计/重建排除冻结资产 |
| MODIFY | backend/app/services/hybrid_search.py、search_engine/client.py、schemas/search.py | 双路查询共用已发布索引集合，携带独立版本字段；不改 weighted RRF |
| MODIFY | backend/app/rag/context_builder.py、services/rag.py | 版本字段随 BGE 后候选进入上下文/Phase13 payload；新版上下文显式禁用旧图谱消费者 |
| ADD/MODIFY | frontend/components/DocumentChunkSetPanel.tsx、lib/chunk-sets.ts、app/documents/[id]/page.tsx、components/DocumentParseButton.tsx、lib/{documents,search}.ts | 状态刷新、规则选择、显式单步/重试/重切分；冻结文档展示版本化入口及类型 |
| MODIFY | backend/app/core/config.py、backend/.env.example、README.md、本文件 | 分离资产准备/检索准入开关，模型 revision，占位配置和上线边界 |
| ADD | backend/tests/test_{sequential_chunks,chunk_set_builds,versioned_index,chunk_set_api_context,chunk_set_postgresql}.py | 纯区间、HTTP/载荷、SQLite 服务编排、索引协议 double、独立 gated PostgreSQL 用例 |
| MODIFY | backend/tests/test_{documents,document_embeddings,kg_refs_propagation}.py | 补齐旧 fixture 文档状态、新增参数和明确的版本字段断言 |
| KEEP | M0 ORM 与 0013 迁移、M1 canonical/MinerU/清洗、M2 单元/锚点/writer、RRF/BGE 参数、历史评测与锁文件 | 无新增迁移、无修改冻结历史结果；本轮无源码 DELETE |

### 来源、映射与向量

`source_start/source_end` 是冻结正文的 Python Unicode code point 左闭右开坐标，UTF-8 内容
哈希独立计算。边界优先段落/换行时只选择原文位置，超长段落退到字符上限；不复制标题或
表头、拼接不连续文本、生成摘要、引入局部投影。真实源范围可跨相邻 block/page。

同文档/source/build 的 chunk C 和 anchor A 只有 `max(C.start,A.start)<min(C.end,A.end)`
才关联。保持一对多、多对一和 overlap；去重身份冲突拒绝。`source_metadata` 只保存业务
`kg_refs`；版本/区间在独立列及 ChunkSet 记录。没有区间列表契约。

manifest 保存确定性 chunk UUID、序号、内容哈希、单一区间、kg_refs 与 block/page 来源。
每一阶段重读验证冻结正文/来源目录/AnchorIndex/manifest；SQL chunk 必须重新与确定性结果相等。
控制注释不得进入 canonical 或切片，embedding、关键词三个正文域、BGE 及 prompt 均使用无标记正文。

向量 fingerprint 包含 provider/model/dimension/模型路径/归一化/query instruction、显式
`PDF_KG_EMBEDDING_REVISION` 与 float32 契约版本。复用只来自同文档、同 source、同指纹、
已发布 set 的完全相同正文；新文本才编码，成功批次在显式重试时保留。仍需重算变化文本，
没有声称重切分完全不计算 embedding。模型身份中途改变会拒绝继续，不将混合向量写进同一 set。

### API、状态与恢复

- `POST /api/v1/documents/{id}/chunk-sets`：`source_version/graph_build_id/request_id/operation/config`。
  `operation=process` 沿用 M2 request，`operation=rechunk` 使用新 request；同 request/config 幂等，冲突拒绝。
- `GET /api/v1/documents/{id}/chunk-sets?limit=20&offset=0`：当前版本、近期版本、M2 可交接 process；只读。
- `GET /api/v1/documents/{id}/chunk-sets/{set_id}`：阶段、数量、向量统计、发布状态、错误码和租约。
- `POST .../{set_id}/advance`：`{"retry":false}`，一次只执行一个阶段或一个现有 batch-size 的向量批次。
  失败/过期任务须 `retry=true`；有效租约不可重复执行，计数/fence 不回退。
- `GET /api/v1/documents/{id}/chunks?chunk_set_id=...`：显式读取某个所属版本；默认只读当前已发布
  版本；尚无发布版本时返回空，不混入历史 legacy chunk。旧引用仍按原 chunk UUID 读取。

| 步骤 | Job / ChunkSet | 发布可见性 |
|---|---|---|
| 准备 | queued/kg_ready，set pending | 无新 chunk |
| 顺序切分及映射 | running/chunking → queued/chunks_ready，seal manifest | 仅管理读取可见，不进入搜索 |
| 逐批向量 | running/embedding → queued/embedding 或 indexing | 原版本继续服务 |
| 索引 | running/indexing，独立索引暂存、写封禁、全量验证 | 原版本继续服务 |
| SQL 发布 | set indexed，Document pointer/revision +1，job succeeded/indexed | 新版本可被新版准入选中 |
| 错误 | set/job failed，保留 stage、成功批次、错误码 | 不切换指针；明确重试或显式新建 set |

SQL 锁顺序为 Document → job → set → chunk。存储、模型和 OpenSearch 调用前均释放 SQL
事务；返回后再次校验文档正常、token/fence/租约及图谱 ready。M0 的 seal/ownership/引用约束
仍作为数据库边界。`KG_READY_EMPTY` 是成功输入，可生成空 kg_refs；图谱失败不准入 M3。

索引名固定 `pdf-kg-chunks-v2-{chunk_set_uuid_hex}`。不使用旧 alias、不执行 delete_by_query、
不覆盖 bulk 409。重试验证相同 _meta，回读校验全部 ID/正文/元数据/向量/count，拒绝超时/失败分片。
不存在半张索引被指针发布的路径；SQL 与 OpenSearch 无分布式事务，可能遗留未发布但完整或部分
写入的独立资产，保留审计而不自动删除。永久冲突拒绝覆写，可显式新建独立 set。

旧失败请求在较新版本发布后重试会被 revision CAS 拒绝。代码不提供自动回退发布指针、解封
索引、删除旧 set 或回收历史对象的操作。故障恢复默认保留当前已发布版本，修复配置/服务后
显式重试；需要改变冻结来源、模型身份或运行数据清理时按相应边界另行批准。

### 首次启用与本期边界

1. **代码及离线验收（本轮）**：两个开关保持默认 false，未运行迁移，无真实服务写入。
2. **隔离验收（待授权）**：独立 PostgreSQL schema 的 0013/触发器、真实 PDF、模型、MinIO、
   OpenSearch 的 analyzer/kNN/写封禁/恢复，以及并发事务联调；不能以 doubles 代替。
3. **准备新版资产（待授权）**：先确认存量 PDF 清单、身份冲突和处理策略，再授权模型/图谱/
   向量/索引操作；同名 slug 冲突依旧拒绝覆盖。保留 legacy 记录及当前检索服务。
4. **启用新准入（待授权）**：审核已发布 PDF 清单及文档覆盖率，确认足量可检索资产后才切换
   `PDF_KG_SEARCH_ENABLED=true`。旧 Markdown、未升级 PDF、旧 schema/旧 set 和未完成 set 被排除；
   新版查询空集/出错不回退 legacy。后续暂停可关闭资产执行开关，保持已发布新版查询；
   不能把切回旧准入当作默认回滚，否则会重新暴露隔离数据，须单独授权。

每个 set 一个物理索引；查询当前索引总数超过 `PDF_KG_MAX_SEARCH_INDICES`（默认 512）明确
拒绝，不截断文档集合或回退。索引/旧向量保留会消耗存储；回收与规模扩展另行设计、授权。

M3 保留 Hybrid→RRF→可选 BGE→文本上下文与引用及 Phase13 payload 的版本字段。
新版候选不会进入旧 Document/Table 图谱查询：当前明确返回 graph disabled。
**新版在线查询、最终前缀截断的有效区间重算、映射/查询/事实使用三种状态、保守全单元覆盖
策略以及新图谱 GraphPayload/旧图谱快照拒绝恢复，仍属于 M4，未实施。**
未声明整个最终 RAG 图谱链路已经接通；未执行存量 PDF 升级、清库、删除旧会话或任何业务回收。

### M3 验证记录

新增离线验收 30 项：纯顺序/集合交集契约 6 项，HTTP/BGE/Phase13 序列化 7 项，
OpenSearch 协议 double 3 项，SQLite 服务集成 14 项。SQLite 用例不验证 PostgreSQL 触发器或真实行锁。
覆盖三套规则、同 block 多锚点、跨页、端点相接、多对多、Unicode、标记拒绝、控制字段不进入
模型正文；构图/解析入口 spies 为零、graph/unit/anchor 身份不变、完全相同正文复用向量、
失败批次/过期租约/删除守卫/首次失败新建任务/发布竞争、旧知识条目引用及历史 chunk 保留。

本轮还执行了原 M0–M2、PDF、embedding/index、Hybrid/RRF/BGE、引用和 Phase13 相关回归。
初次回归发现 16 个 fixture/API 字段断言不匹配，补全 fixture 并更新新增版本字段断言后通过；
没有放宽业务守卫、关闭指纹检查、修改评测集或冻结结果。最终命令/结果见下面记录。
最终完整回归：**709 passed in 14.80s**（包含新增 M3 30 项及原相关回归 679 项）。
前端 TypeScript、相关 ESLint 和 `git -c core.safecrlf=false diff-files --check` 均 exit 0。

```powershell
# cwd: D:\rag_system\backend；临时目录位于已忽略 .test-artifacts 下。
$testM3Temp = Join-Path (Get-Location) ('.test-artifacts/m3-' + [guid]::NewGuid().ToString('N'))
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $testM3Temp --tb=short `
  tests/test_sequential_chunks.py tests/test_chunk_set_builds.py tests/test_versioned_index.py tests/test_chunk_set_api_context.py `
  tests/test_kg_v2_protocol_units.py tests/test_kg_v2_builds.py tests/test_kg_v2_writer.py tests/test_kg_v2_api.py `
  tests/test_frozen_source.py tests/test_pdf_source_admission.py tests/test_basic_parser_retirement.py `
  tests/test_document_chunk_writer.py tests/test_document_parsing.py tests/test_document_parsing_mineru.py `
  tests/test_pdf_cleaning_pipeline.py tests/test_documents.py tests/test_kg_refs_propagation.py tests/test_pdf_cleaner.py `
  tests/test_document_deletion_service.py tests/test_document_operation_guard.py tests/test_pdf_kg_persistence_contracts.py `
  tests/test_document_parse_models.py tests/test_document_blocks_assets.py tests/test_document_embeddings.py `
  tests/test_hybrid_search.py tests/test_search_index.py tests/test_rag_graph_response.py tests/test_rag_graph_fusion.py `
  tests/test_rag_context.py tests/test_rag_reranking.py tests/test_conversation_persistence.py `
  tests/test_conversation_m3_retrieval.py tests/test_conversation_m3_prompt.py tests/test_mineru_normalizer.py `
  tests/test_mineru_v4_transport.py tests/test_mineru_archive_reader.py tests/test_block_chunker.py

& ./.venv/Scripts/python.exe -B -m pytest --collect-only -q -p no:cacheprovider tests/test_chunk_set_postgresql.py
# 1 test collected；未执行。需要独立授权与专用测试库确认变量。

# cwd: D:\rag_system\frontend
node node_modules/typescript/bin/tsc --noEmit --incremental false
node node_modules/eslint/bin/eslint.js components/DocumentChunkSetPanel.tsx components/DocumentParseButton.tsx lib/chunk-sets.ts lib/documents.ts lib/search.ts 'app/documents/[id]/page.tsx'
# 两项 exit 0。
```

真实 PostgreSQL、真实 PDF/模型/Neo4j/MinIO/OpenSearch、前端浏览器交互验收均 **未执行**。
M3 没有新增或运行 Alembic 迁移；0013 保持 M0 原样。没有真实抽取/embedding 模型调用，
没有真实 Neo4j、MinIO 或索引写入。相关授权不由本次代码实施推定。

## M2 数据与执行契约（2026-10-07）

只实现新版锚点、连续 KG 单元、构图与可恢复检查点；不产生 chunk、不向量化、
不发布搜索索引，不切换在线旧图谱消费者。真实服务调用与迁移另行授权。

复用 M0 三张表，不新增表或修改 0013：GraphBuild 固定图身份和输入指纹，
KGExtractionUnit 固定 canonical 中的单一区间、预分配锚点、输入哈希和结果，
DocumentProcessingJob 保存分步租约/栅栏。AnchorIndex 是已封存 build 下成功且
含合格关系的 unit 视图。空成功仍保留 unit 身份，但不暴露业务锚点。

准备请求绑定 document/source/request；按新包日期及文件名规则分配 graph_id，
同日同 slug（包括纯中文回落 DOC）冲突返回错误，不能覆写或悄悄另起标识。
source_path 使用 document/source/build 专属路径。准备时保存全部单元及锚点。
单元由 canonical 扫描位置直接产生；HTML、管道表格和条款只取连续区间。
标题上下文、表格内部分次调用仅用于模型输入，不改来源坐标或锚点。

每次 advance 最多调用一次模型，或执行最终写图；成功 piece 存内容寻址对象及
哈希检查点，显式 retry 只补未成功部分。SQL 文档锁→job→build→unit；网络调用
前提交释放事务，之后核验 token/fence/租约及 deletion guard。过期抢占后旧执行
不能提交。完成停在 job queued/kg_ready（等待后续阶段），不能假称整个 job 已完成。

Neo4j 使用包内 KnowledgeGraph/HAS_ENTITY/MaterialEntity/RELATES_TO 结构。
单次有界事务写入整张图，事务内校验数量与归属；既有图只允许完全相同 owner、
payload hash 的幂等确认。不自动建约束、不删图、不覆盖旧图；唯一约束须另行授权
部署。图保存 build/source/document/hash 与 built 状态，后续查询必须同时通过 SQL
ready/sealed 准入。Neo4j 成功而 SQL 提交失败时可复核重试；未封存图不可用于检索。
本期只写 RELATES_TO，不复制可重复计数的 HAS_TRIPLE/Triple 派生路径。

构图配置默认关闭，使用项目 LLMProvider 和独立 Neo4j writer 凭据；不复用只读账号。
`KG_LLM_MAX_TOKENS` 是 M2 PDF KG extraction 专用 completion/output token budget，
默认 8192，必须为正整数。它独立于通用 `LLM_MAX_TOKENS`，不覆盖问答、rewrite、
conversation 或 knowledge-items API 的预算。M2 显式传入该值，GraphBuild provider
fingerprint 的 `max_tokens` 也使用该有效值；修改它会改变构图执行配置身份，旧指纹
的未完成 build 会被现有一致性检查拒绝，不能直接续跑。只改变通用 `LLM_MAX_TOKENS`
不会改变 KG 指纹。同一冻结来源目前不提供替代 build 的重建入口，不能通过修改历史
指纹或检查点恢复；需要另行授权版本重建方案。真实 `.env` 和历史构图资产不自动修改。
失败、部分失败、写入失败分别持久化，全部成功无合格关系才允许 ready_empty。
默认禁止部分发布。所有源/构图资产保留；M2 冻结来源文档继续拒绝旧删除流程。

### M2 参考包核验与字段对应

本轮实际读取指定 ZIP 的 `extract/segment_md.py`、`kg.py`、`write_neo4j.py`、
`prompt.py`、`paths.py` 及当前模板。没有执行包内脚本、导入其 config 或重复集成清洗器。
`prompt.py` 实际读取无扩展名的 `配置/材料知识提取模板（不应用本体）`（4.0.0），
不是同目录旧 `材料知识提取模板.json`（3.0.0）。已单独内置当前模板，保留内容，
只统一 JSON 排版；源字节哈希为 `fefa92ef1b7f80ac1fe524f6031e6ad6d1f0d45f21688b6f1fb173346e431613`。
持久化 template_sha256 使用排序/紧凑 UTF-8 JSON 的语义哈希
`5a0e37a28809e099a8d355cdad5819fe8dcf9430da9bec1f0f485c7d9696727b`，避免 Git 换行转换误判。
真实 table/clause 标注字段已取自包内 20260929 QJ3185 标注产物，附源哈希存为测试 fixture。

| 数据位置 | 字段/处理 |
| --- | --- |
| start 业务锚点 | anchor_id、graph_id、anchor_type 必需；table 必需 table_ref，clause 必需 clause_ref |
| start 可选字段 | table_no 只用于 table；heading_ref、heading 有值时保留；空值、未知类型、冲突身份拒绝 |
| end 注释 | 只保存 anchor_id；不强制补齐 start 字段 |
| KGExtractionUnit | source_version/start/end；unit_index、状态、allocated_anchor_*、input/result 哈希和 piece_checkpoints；无 chunk 依赖 |
| KnowledgeGraph | graph_id/source_path、source_type/mode/template、entity/relationship/unit_count；增加 document/source/build/schema/hash/built 门槛 |
| MaterialEntity | graph_id/entity_id/entity_type/name/source_path、heading/tag/anchor_id/local_ref/anchor_type/table_ref、properties_json；增加 graph_build_id |
| RELATES_TO | rel_id、relation_type、graph_id、anchor_id、properties_json；增加 graph_build_id；两端必须同图、同单元 |
| clause_ref/table_no/heading_ref 展示 | 来自已校验 AnchorIndex；不假设包 writer 将其写成实体顶层属性；clause 不制造 table_ref/Table 节点 |

模型输入用项目 LLMProvider，要求当前模板的 entities/relationships JSON；格式破损、
重复冲突 ID、模型携带控制身份均报错。白名单/from-to 不合格关系不发布，剔除计数留在
piece 结果中。合法但无合格关系为成功空结果，不把模型异常当空成功。
数值/单位/provenance 属性原样保存；语义正确性仍需真实模型与 PDF 验收。

标题从 Markdown 结构或冻结 block 目录的明确区间读取，无 `#` 的标题块可用编号深度
或一级默认值，不以首次 find 绑定。连续单元可跨相邻 block/page；辅助溯源保留在冻结目录。
保留 HTML 原文，避免包中 `_strip_html` 去掉公式。简单长 HTML 行容器按行分次调用且
不跨 rowspan，复杂包装保持整表；管道表格按 40 行分次。重复表头只存在于 KG 模型
输入，仍是同一个 unit/anchor。超出预算明确阻断，不摘要、截断或静默丢单元。
可选忠实标注导出只插入完整注释；去注释与 canonical UTF-8 字节相等。未使用包的
重组标注稿作为权威来源，也未引入通用投影或非连续来源模型。

### M2 接口、恢复与启用顺序

- `POST /api/v1/documents/{document_id}/graph-builds`：请求 `source_version`、UUID `request_id`；
  验证 PDF/来源/模板/预算，在一次短事务预分配 build、units、job。不调用模型或 Neo4j。
  同 request/source 重试返回原 build；相同 request 绑定其他来源返回冲突。
  本期一个来源只开放首建与同身份恢复；不提供覆盖式重新构图。规则/模板/provider
  指纹改变会拒绝旧 build 重试，需要另行设计并授权显式版本重建操作。
- `GET /api/v1/documents/{document_id}/graph-builds/{build_id}`：返回构图状态、单元分类计数、
  job 阶段、尝试次数、租约到期时间、错误码及 can_advance；不返回凭据或模型异常文本。
- `POST /api/v1/documents/{document_id}/graph-builds/{build_id}/advance`：请求 `{"retry":false}`；
  最多一次模型调用，或一次有界完整图事务。失败/过期租约须 `retry:true`；有效租约不能抢占。
  ready/ready_empty 重入只返回状态，模型和 writer 调用均为零。
- 任务只通过这些显式请求推进；本轮不启动后台 worker、不自动下一步，不提供任意 Cypher。
  失联后先 GET，再决定是否显式 retry。首次 prepare 的 request_id 由客户端保留用于幂等恢复。
- 每次租约获取递增 fence/attempt；最大次数为初始 piece 总数 + 1 个写图步骤 + 3 次额外恢复。
  达到上限不自动重置；需要另行审阅。成功 piece 不再调用模型；在响应取得后、检查点提交前
  崩溃仍可能重做该次模型调用，不承诺外部模型 exactly-once。未引用的内容寻址资产不自动删除。
- 写图成功、SQL 封存失败：保留完全相同的 payload 与身份，重试核验 Neo4j 归属/哈希/计数，
  再完成 SQL；失败期间没有 AnchorIndex 准入。缺唯一约束、旧图冲突、半成品/跨图关系均阻断。
- 保留默认 `KG_BUILD_ENABLED=false`。先完成代码/离线验收，再经授权执行隔离 PG/Neo4j/模型验收；
  后续经授权迁移并准备存量 PDF 的新版资产，确认可检索资产齐备后才能正式切换新准入。
  M2 不切换检索准入，不恢复旧图谱 fallback，也不删除/改写存量数据。

### M2 文件级变更与保留边界

| 分类 | 文件 | 作用 |
| --- | --- | --- |
| ADD | app/extraction/kg_protocol.py、kg_units.py | 新 start/end 协议、连续 KG 单元、稳定预分配、忠实标注导出 |
| ADD | app/extraction/kg_extract.py、kg_template_v4.json | 当前模板、项目 provider 调用、限定与冲突校验、piece 合并 |
| ADD | app/extraction/kg_writer.py | 固定参数化写语句、约束预检、整图事务与幂等回执 |
| ADD | app/services/document_graph_builds.py | prepare/advance/status、来源校验、租约与恢复、AnchorIndex 视图 |
| ADD/MODIFY | schemas/document_graph_build.py、api/v1/documents.py | 有界构图管理 API；没有新增在线 RAG 查询接口 |
| MODIFY | core/config.py、backend/.env.example、pyproject.toml | 默认关闭、独立写账号、预算/超时、模板打包；不改真实 .env/锁文件 |
| MODIFY | services/document_parsing.py、document_deletion.py、frontend/DocumentParseButton.tsx | M2 状态继续保护冻结来源，拒绝旧重解析/删除 |
| ADD | test_kg_v2_{protocol_units,builds,writer,api,postgresql}.py、fixtures/kg_v2_package_anchors.json | 纯函数、离线服务/事务 double、API、独立 gated PG 用例 |
| MODIFY | tests/test_rag_graph_response.py | 原“无任何 graph 路径”断言收窄为只允许构图管理路径；保留单次 LLM/证据一致性断言 |
| ADD/MODIFY | docs/kg-v2-constraints.cypher、本说明、database-schema.md、README.md | 待授权 Neo4j 约束脚本、当前边界与验收记录 |
| KEEP | M0 模型/0013、M1 清洗/冻结、legacy 数据、Hybrid/RRF/BGE、Phase 13 | 本轮无结构迁移、无历史记录改写；旧图谱在线消费者待后续获批阶段替换 |

M2 没有 DELETE 源文件。此前 Git 中 Markdown Native 删除项属于已验收的 M1，未重复扩展删除。
不宣称检索新查询、三套重切分配置、最终截断后的图谱事实约束或新协议多轮恢复已完成；
这些属于后续里程碑。M2 结束点为构图封存，尚无完整索引流水线。

### M2 验收记录（2026-10-07）

- 本地单元、离线服务/API 与相关回归：最终 **679 passed**（其中 M2 新增 91 项）。
  范围包括真实包 table/clause 样例、当前 4.0.0 模板、身份冲突、连续 Unicode 区间、
  同标题/重复正文、原始 HTML/公式/rowspan 保留、表格 pieces、忠实标注、合格三元组准入、
  空成功/失败/部分失败、写图回滚 double、约束缺失、跨图/跨文档拒绝、成功检查点复用、
  存储回读哈希、租约抢占/过期恢复、SQL 提交失败后同 payload 重试、无 SQL 锁跨网络、
  零 chunk 与 M2 删除守卫。现有 Hybrid/RRF/BGE、引用和 Phase 13 相关回归保持通过。
- 首次完整回归曾为 677 passed / 1 failed：旧测试断言所有 API 路径都不能含 graph；
  已按上表收窄到只允许新的构图管理路径，并保留单次 LLM 和在线证据检查。未改冻结评测集、
  历史结果或锁文件。之后补充资产回读测试，最终总数为 679。
- 前端 TypeScript 与受影响组件 ESLint：退出码 0。未执行浏览器手动验收。
- `git diff-files --check`、M2 Python AST、模板 JSON/打包声明与禁用范围静态检查：通过。
- 真实 PostgreSQL 服务用例：**仅收集 1 项，未执行**；需要独立
  `PDF_KG_M2_TEST_MIGRATIONS=0013_pdf_kg_versions` 和 verified_engine 专用数据库保护。
  SQLite/事务 double 不代表 PG 触发器、真实锁竞争或 Neo4j 事务语法验收。
- 真实 PDF/MinerU/模型/MinIO/Neo4j/OpenSearch 集成：**均未执行**。未启动应用/后台任务，
  未执行数据库或图约束迁移，未写业务图谱/索引、未回填/删除数据，未修改 .env，未提交 commit。
  M0 的 0013 仍仅为已编写且此前离线验收的迁移，M2 没有新增 Alembic 迁移。

后端最终命令（`D:\rag_system\backend`，11.29 秒）：

```powershell
$testM2Temp = Join-Path (Get-Location) ('.test-artifacts/m2-' + [guid]::NewGuid().ToString('N'))
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $testM2Temp --tb=short `
  tests/test_kg_v2_protocol_units.py tests/test_kg_v2_builds.py tests/test_kg_v2_writer.py tests/test_kg_v2_api.py `
  tests/test_frozen_source.py tests/test_pdf_source_admission.py tests/test_basic_parser_retirement.py `
  tests/test_document_chunk_writer.py tests/test_document_parsing.py tests/test_document_parsing_mineru.py `
  tests/test_pdf_cleaning_pipeline.py tests/test_documents.py tests/test_kg_refs_propagation.py tests/test_pdf_cleaner.py `
  tests/test_document_deletion_service.py tests/test_document_operation_guard.py tests/test_pdf_kg_persistence_contracts.py `
  tests/test_document_parse_models.py tests/test_document_blocks_assets.py tests/test_document_embeddings.py `
  tests/test_hybrid_search.py tests/test_search_index.py tests/test_rag_graph_response.py tests/test_rag_graph_fusion.py `
  tests/test_rag_context.py tests/test_rag_reranking.py tests/test_conversation_persistence.py `
  tests/test_conversation_m3_retrieval.py tests/test_conversation_m3_prompt.py tests/test_mineru_normalizer.py `
  tests/test_mineru_v4_transport.py tests/test_mineru_archive_reader.py tests/test_block_chunker.py

& ./.venv/Scripts/python.exe -B -m pytest --collect-only -q -p no:cacheprovider tests/test_kg_v2_postgresql.py
```

前端实际命令（`D:\rag_system\frontend`）：

```powershell
node node_modules/typescript/bin/tsc --noEmit --incremental false
node node_modules/eslint/bin/eslint.js components/DocumentParseButton.tsx
```

待单独授权的真实验收应包括：在独立 PG 应用 0013 并验证租约竞争/触发器；在隔离 Neo4j
确认唯一约束、参数化语句、超时整事务回滚及不影响旧图；用批准的 PDF/模型验证 table、
clause、HTML/公式/单位的真实抽取质量与空/失败区分。之后才能准备存量 PDF 资产并考虑启用。
回滚可关闭构图入口并保留全部 M0/M1/M2 记录与资产，但必须继续保留冻结来源删除保护；
禁止回退到会清除这些资产的旧删除流程。失败孤立资产、重试上限恢复、版本重建与真实数据
回收均不作为源码变更的附带清理。本轮已停止于 M2，不进入 M3。

以下 M1/M0 记录为历史验收事实。

## M1 当前实施边界

本轮仅实施 PDF-only、退役 Markdown Native、清洗并冻结连续来源；未进入 M2。
M0 已有未提交修改完整保留，HEAD 仍为 `b3605feb8ce5963fbed7b9f627f0af8bd58aecb9`。
M0 真实迁移/数据库约束验收仍未执行；没有回填、清理或升级业务数据。

### 当前接口与状态

- `POST /api/v1/documents`：只允许 `.pdf`（大小写不敏感），同时检查 `%PDF-` 文件头。
  MIME 仍为辅助元数据；文件头准入检查不等于完整 PDF 语法验收，解析由 MinerU 负责。
  配置与上传服务各自有 PDF 上界，旧 `.env` 不能重新开放非 PDF。
- `POST /documents/{id}/parse`：上传 → parsing → cleaned_source_ready；异常为 parse_failed。
  重入 parsing 返回 409；已有冻结来源或旧 chunk 的文档拒绝重复解析，不覆盖存量。
  数据库缺少来源模型时在外部 IO 前返回 503，不自动执行迁移或走旧解析分支。
- 清洗复用 `pdf_cleaner.py`，默认开启且本阶段必需；false 返回配置错误。保留
  profile/backfill 参数与已有保守规则，没有第二套清洗器。
- 成功响应保留 document_id/parser 信息，新增 parse_run_id、source_version、
  canonical_sha256、character_count、block_count；chunk_count 固定为 0。
  parse_run 的 block/asset 统计与 source_version 摘要保留，供页面刷新后查看。
- 切片列表继续读取历史数据；没有新 chunk 时不能向量化。Hybrid/RRF/BGE、图谱查询、
  QA 快照与存量索引准入均未切换，不在 M1 宣称旧图谱协议已整体退役。

### 冻结正文与连续坐标

`frozen_source.py` 在既有 renderer 渲染每个有内容的来源 block 时同步记录
block ID/index/key/type、PDF 页码和一个 `[source_start,source_end)` 区间。
块目录是 PDF 辅助溯源；不会按标题/首次 find 猜权威位置，也不产生 KG 单元或 chunk。
块间仅使用固定 `\n\n` 渲染分隔符，后续顺序切分可跨相邻 block/page。

冻结前完成 LF/NFC（仅起始 BOM 移除），字符单位为 Python Unicode code point；
正文 SHA-256 使用无 BOM UTF-8 字节。保留表格 HTML、公式、图片相对路径及代码缩进。
完整移除保留的 kg-anchor start/end 控制注释；未闭合控制注释拒绝冻结。
不解析旧四字段协议、不产生锚点、不设计多区间字段或 chunk-local 投影。

每次新的 parse_run 使用独立目录：`parsed-assets/{document_id}/{parse_run_id}/`。
原 `output.md/output.json` 与图片资产保持原样；新增 `cleaned.md`、`source-map.json`。
本期每个 parse_run 只冻结一次；以后若从同一解析重清洗，应分配新的资产定位及
source_version，禁止覆盖本期冻结对象。本期未开放该操作。

正文与目录上传后回读，核对两份哈希、来源身份、规范化约定、字符数、块次序及边界。
确认通过后，在同一短 SQL 事务保存来源块/资产、SourceDocumentVersion、parse_run
成功状态和 cleaned_source_ready。对象存储与 SQL 不是分布式事务：失败可能留下
隔离在失败 parse_run 前缀下的对象，但不提交可用来源；不自动回收这些对象。

### 互斥、删除与恢复边界

解析准入、写前检查、最终提交均使用已有 Document deletion guard；网络解析、上传、
回读不持 SQL 事务或行锁。删除准入使用同一文档锁，并在构造 manifest 前拒绝
`parsing` 与 `cleaned_source_ready`，避免晚到上传和旧删除流程部分删除新资产。
正常 legacy 文档的原删除路径不变；没有实现新的删除执行器或源版本回收。

预期异常记录 parse_failed，重试使用新的 parse_run 与资产目录。进程硬崩溃后若停在
parsing，本期不会猜测租约失效或自动抢占；需另行审阅后恢复状态。完整租约执行器、
任务恢复和版本化删除属于后续阶段，不用清库或隐藏 fallback 代替。
一旦正式产生冻结来源，回退应用也必须保留此删除保护；不能直接部署不识别冻结
来源的旧删除流程。数据库与对象资产保持原样，回收或恢复操作仍需单独批准。

### 文件范围

- MODIFY：file_types/config/documents、document_parsing、document_sources（新增）、
  parse 响应 schema、parse-run 摘要；现有解析已移除 chunk 调用。
- ADD：`ingestion/frozen_source.py`；纯内存渲染/验证和本地合成存储回归测试。
- DELETE：`ingestion/markdown/` 五个源码文件与四个独占旧功能测试；移除仅被它们
  使用的后端 markdown-it-py 依赖声明，不改历史锁文件或当前已安装依赖。
- MODIFY：共享 writer/metadata 传播测试改用独立 PDF fixture，保留传播、引用、
  rollback、MinerU 资产/公式/表格保护；历史评测记录与锁定结果不修改。
- MODIFY：PDF 上传 accept、解析按钮/响应类型、无切片提示、文档页、当前 README
  与 backend/.env.example；不修改真实 .env。
- KEEP：既有清洗器、MinerU、通用 chunk writer、旧图谱消费者、Hybrid/RRF/BGE 与
  Phase 13；后者的协议替换及最终截断来源约束留在后续阶段。
- 残留分类：历史文档/测试保护中的旧名称不当成入口；ignored `backend/build/lib`
  是旧构建产物，不是当前 `backend/app` 运行源码。本轮未清理该目录；部署时不得
  复用它打包，应在隔离构建目录从当前源码构建。

### M1 验证记录（2026-10-06）

最终本地单元与合成服务回归 **588 passed**。覆盖 PDF-only/伪扩展名拒绝、冻结
字符坐标/哈希/重复正文归属、标记移除、存储回读篡改拒绝、无 SQL 事务跨外部 IO、
零 chunk、重复解析拒绝、删除前置保护，以及现有清洗/表格/公式/图片、共享 writer、
Hybrid/RRF、BGE、引用/旧图谱响应与 Phase 13 相关契约。

本地回归使用内存 SQLite、合成 PDF 与明确的模型/存储 double；未把它们当作真实
PostgreSQL 触发器、并发锁、业务 PDF、MinerU、Neo4j、模型或索引联调验收。
真实数据库迁移、资产写入、存量升级、正式准入切换均**未执行**。
没有新增或执行迁移，M0 的 `0013_pdf_kg_versions` 保持原样；没有提交 commit。

后端实际最终命令（工作目录 `D:\rag_system\backend`）：

```powershell
$testM1Temp = Join-Path (Get-Location) ('.test-artifacts/m1-' + [guid]::NewGuid().ToString('N'))
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $testM1Temp --tb=short `
  tests/test_frozen_source.py tests/test_pdf_source_admission.py tests/test_basic_parser_retirement.py `
  tests/test_document_chunk_writer.py tests/test_document_parsing.py tests/test_document_parsing_mineru.py `
  tests/test_pdf_cleaning_pipeline.py tests/test_documents.py tests/test_kg_refs_propagation.py `
  tests/test_pdf_cleaner.py tests/test_document_deletion_service.py tests/test_document_operation_guard.py `
  tests/test_pdf_kg_persistence_contracts.py tests/test_document_parse_models.py tests/test_document_blocks_assets.py `
  tests/test_document_embeddings.py tests/test_hybrid_search.py tests/test_search_index.py `
  tests/test_rag_graph_response.py tests/test_rag_graph_fusion.py tests/test_rag_context.py `
  tests/test_rag_reranking.py tests/test_conversation_persistence.py tests/test_conversation_m3_retrieval.py `
  tests/test_conversation_m3_prompt.py tests/test_mineru_normalizer.py tests/test_mineru_v4_transport.py `
  tests/test_mineru_archive_reader.py tests/test_block_chunker.py
```

前端实际命令（工作目录 `D:\rag_system\frontend`），均退出码 0：

```powershell
node node_modules/typescript/bin/tsc --noEmit --incremental false
node node_modules/eslint/bin/eslint.js components/DocumentUploadForm.tsx components/DocumentParseButton.tsx components/DocumentChunkList.tsx components/DocumentEmbeddingPanel.tsx lib/documents.ts 'app/documents/[id]/page.tsx'
```

没有安装依赖、启动服务或执行浏览器/业务数据验收。工作区差异检查通过。

## M0 持久化契约与验收记录（保留）

本文件先定义 M0 的持久化契约。M0 只新增模型、增量迁移和测试，不接通入库流水线。
PDF-only、Markdown Native 退役、来源冻结实现、抽取、切片、embedding、索引发布、
任务执行器和新版 RAG 消费者属于后续阶段，本阶段均不实施。

## 迁移与部署边界

- revision `0013_pdf_kg_versions`，父版本 `0012_casting_answers`。
- 新增五张空表；既有 documents/document_chunks 增加字段，旧引用及原文不改写。
- document_parse_runs 只增加 `(id, document_id)` 唯一约束，为来源归属复合外键提供目标。
- 不执行数据回填、模型调用、Neo4j/MinIO/OpenSearch 写入，不修改真实 .env。
- 迁移文件编写与真实迁移执行是两项权限；使用新版字段/表前须单独批准并执行迁移。
  新增既有表字段采用 deferred/server default，普通 legacy ORM 查询、写入不提前引用
  这些字段；这不是新版协议 fallback，也不表示新字段可在旧 schema 上使用。
  M0 不自动迁移，也不将未升级业务库当作测试库。
- downgrade 明确拒绝；恢复方式为回退应用并保留扩展 schema/data。
  清理或备份恢复必须另行审阅授权，不提供自动 DROP/DELETE 回滚。
  此应用回退边界适用于 M0 尚未产生新版资产的阶段；以后已有新版 chunk 时，
  不能直接切回不识别版本准入的旧检索应用，需另审兼容版本或暂停检索的恢复方案。

## 来源坐标与真实性

业务来源只使用 `source_version/source_start/source_end`，单位是 Python Unicode
code point，区间为左闭右开 `[start,end)`。冻结前统一 LF/NFC，内容哈希是无 BOM
UTF-8 字节的 SHA-256；之后正文不可静默变换。

每个新版 chunk 必须满足 `content == canonical_text[source_start:source_end]`。
数据库约束负责非空区间、来源上界、正文字符数、归属与哈希格式；canonical 位于
对象存储，正文等式、实际 SHA-256、LF/NFC 和对象内容不可变性必须由后续冻结服务、
writer、发布校验验证，M0 不声称数据库可验证外部正文。

每个 KG 单元绑定一个连续原文区间；HTML 转换/补充标题只影响后续抽取输入。
block/page 辅助索引存为来源资产，是顺序边界目录，不是正文重组或局部投影框架。

## 五个必要模型

| 表 | 最小职责及关键字段 | 不能仅复用已有记录的原因 |
|---|---|---|
| document_source_versions | source_version、document/parse_run、bucket、canonical/block-map key/hash、字符数、cleaner/renderer 版本、配置指纹、frozen_at | parse_run 是执行尝试；冻结正文有独立、不变的身份，同一次解析可以产生不同来源版本 |
| document_graph_builds | id、document/source、graph_id、identity_day、独占 source_path、协议/模板/规则/provider/输入指纹、状态、结果 manifest、计数、检查点、sealed_at | 标识可重试、可封存的构图结果，区别成功空、抽取失败和写图失败 |
| kg_extraction_units | id、document/build/source、start/end、kind、unit_index、首次分配的 anchor 身份/元数据、状态、合格三元组资格、输入/结果 hash、piece 检查点 | 即使成功空或失败也保留单元与原始区间；不依赖检索 chunk |
| document_chunk_sets | id、document/source/build、切分器/配置/embedding 指纹、状态、封存 manifest、chunk_count、索引凭据 | 待发布、当前及历史切片必须共存，re-chunk 不产生新的 parse_run |
| document_processing_jobs | id、document、operation/request_id/input hash、版本引用、stage/status、租约/fencing、检查点、错误 | process/rechunk 跨阶段幂等与恢复不适合混入 parse_run 或删除任务 |

模型只保存不含密钥的配置指纹；JSON 检查点必须是对象且序列化大小不超过 64 KiB。
完整模型输入/响应属于对象资产，不应塞进检查点。切分配置不超过 16 KiB。
graph/unit/chunk-set 的对象 key 均在所属 SourceDocumentVersion.bucket_name 下解析；
后续 writer 不得以可变环境默认 bucket 重新解释历史定位。

SourceVersion 行一经插入即表示已冻结，不创建第二个冻结状态机；任何 UPDATE 被拒绝。
GraphBuild 和 Unit 的来源/身份/规则字段、ChunkSet 的来源/切分配置字段一经创建不可变。
构图和抽取成功后结果不可改写；ChunkSet 封存后不能增删 chunk 或改正文/坐标/kg_refs，
但允许后续向量计算和索引状态推进。这些数据库守卫不替代后续任务/删除服务。

## 复用结构

- **AnchorIndex 不建表**：后续查询接口仅返回 ready GraphBuild 中有合格三元组的 Unit。
  allocated_anchor_id/metadata 表示首次分配的内部候选身份，不能仅凭非空就当作发布锚点。
- **不建 ChunkAnchor 表**：后续按同文档/source/build 的连续区间相交生成 kg_refs，发布时核验。
- **不建 Publication 表**：Document.current_chunk_set_id 是当前指针，publication_revision 用于 CAS。
  source/build 从 set 推导。未发布与 legacy 的指针为空。
- DocumentChunk 新增 chunk_set_id、source_version、source_start/end、content_sha256。
  legacy 的五个新增字段全部为空；新版全部非空并关联真实 parse_run。
  `(chunk_set_id,chunk_index)` 唯一；SQL NULL 允许历史 chunk_index 原样保留。
- parse_run/assets/blocks、DocumentChunkBlock、QA snapshots/sources、knowledge-item/chunk
  关联继续复用；本阶段不修改历史证据格式，不删除历史 chunk。

## 数据库约束与后续服务边界

- 复合外键保证 parse/source/build/set/job/chunk 和当前指针不会跨文档/来源串接。
- 新版 unit/chunk 的上界通过触发器校验冻结正文字符数；不使用全文搜索猜位置。
- graph_id/source_path 全局唯一；unit 顺序和候选锚点在 build 内唯一。
- 同一来源和相同配置允许多个 ChunkSet，幂等由 job 请求键负责。
- 同文档最多一个 queued/running/retry_wait 活动任务；running 必须持完整租约字段。
  fencing_token 为递增整数；实际领取、续租、失租处理留给后续执行器。
- 版本身份和终态结果不可原位重写，失败状态与结果检查点可以恢复；同请求键不同
  input_fingerprint 必须由未来服务在冲突后核对并拒绝，不假装唯一约束会比较输入。
- 当前指针只能指向同文档 indexed set，其 GraphBuild 必须 ready/ready_empty。
  指针变更必须将 revision 恰好加一，且非空发布要求文档 normal；实际 CAS 和任务
  lease/fencing 校验由未来发布服务在短事务内完成。
- 子项写入通过父 build/set 行锁与封存互斥；后续编排统一采用 Document →
  GraphBuild/ChunkSet → 子记录的短事务锁序。构图结果的真实完整性、子项计数一致性、
  发布时租约仍有效等尚未实现，不能只把状态置为 ready/indexed 就视为验收通过。
- 不使用 ON DELETE CASCADE 清除新增版本。正式文档删除必须先撤销发布、取消任务，
  扩展精确 manifest 后按依赖收尾；M0 不实现删除新资产的运行流程。
- 完整对象哈希、锚点 union/schema、合格三元组、图完整性、索引真实性，以及
  “构图完成才切片”等跨服务业务门槛仍由后续阶段实现，不由状态字符串自动证明。

## 历史与首次启用

旧数据不回填来源、图谱或切片身份，不因添加字段自动获得新版准入。
新检索要求当前指针；历史恢复使用保存的合法版本，不要求它仍为当前 set。
未来必须依次完成代码、隔离验收、经授权准备新版资产、正式切换，不能先排除旧 PDF
再等待重建。本阶段不启用任何准入过滤，也不改变正在使用的检索接口。

## 验证分层

M0 的本地测试覆盖 ORM 注册、迁移离线 SQL、DDL 与模型一致性、legacy 空字段形态、
归属/唯一/区间/状态约束及恢复边界。离线 SQL 生成不连接数据库，不等于真实迁移通过。
专用 PostgreSQL 约束/legacy 升级测试必须通过显式隔离目标门禁；未执行时单独报告。

## 本轮验证记录（2026-10-06）

基线：`phase13-Multi-turn-RAG`，HEAD `b3605feb8ce5963fbed7b9f627f0af8bd58aecb9`，
开始时工作区干净。只修改 M0 模型、迁移、说明和相关测试，没有修改 API、服务、
前端、真实配置或历史迁移。以下命令在 `D:\rag_system\backend` 执行：

```powershell
New-Item -ItemType Directory -Force -Path .test-artifacts | Out-Null
$testM0Temp = Join-Path (Get-Location) ('.test-artifacts/m0-' + [guid]::NewGuid().ToString('N'))
& ./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $testM0Temp `
  tests/test_pdf_kg_persistence_contracts.py tests/test_document_parse_models.py `
  tests/test_document_deletion_models.py tests/test_knowledge_item_models.py `
  tests/test_document_chunk_writer.py tests/test_pdf_cleaner.py tests/test_pdf_cleaning_pipeline.py `
  tests/test_document_parsing.py tests/test_document_parsing_mineru.py `
  tests/test_document_operation_guard.py tests/test_document_qa_evidence_deletion.py `
  tests/test_conversation_persistence.py tests/test_conversation_m3_contracts.py `
  tests/test_conversation_m3_retrieval.py

& ./.venv/Scripts/python.exe -B -m pytest --collect-only -q -p no:cacheprovider `
  tests/test_pdf_kg_persistence_postgresql.py
```

- 离线/单元及既有本地回归：170 passed；SQLAlchemy mapper 注册、模型/冻结 DDL
  一致性及旧 schema ORM 读写通过。SQLite 仅验证旧 writer 的事务/内容兼容，
  fixture 不模拟 PostgreSQL 新约束或锁行为。
- PostgreSQL 专用用例：17 项收集通过，**未执行**。包括 legacy 升级原值比较、
  跨文档/来源拒绝、Unicode 区间、身份冲突、封存、发布 revision、租约和删除约束。
- 真实迁移、业务数据库读写、真实 PDF/模型/Neo4j/OpenSearch/MinIO 联调：**未执行**。
  未执行前端检查；本轮未改前端。未提交 commit。

执行 PostgreSQL 用例需要另行授权；门禁同时要求
`PDF_KG_M0_TEST_MIGRATIONS=0013_pdf_kg_versions` 和现有 Phase 13 专用实例
URL/cluster/数据库名确认，校验固定专用用户、非业务端口及实际实例身份。
用例不会加载应用 `.env`，会创建并保留合成测试 schema；不自动创建服务、扩展或清库。
本轮只完成用例编写与收集，不能据此宣称真实数据库迁移或并发验收通过。
