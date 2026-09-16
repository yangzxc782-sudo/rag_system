# Phase 12 M4 — Corpus readiness and token audit

日期：2026-09-16。目录：D:\rag_system。分支：phase12-bge-reranker。
HEAD：`97bf6031292853c334d68fe926270ddd344410c1`。
Git Gate 起点clean，diff --check通过。Planning/M0/M1/M2/M3各自提交：
`559087f` / `06aa95f` / `0b327c0` / `40c54e0` / `97bf603`。
Graph baseline 独立修复 `4fc3412`。

## CURRENT_PHASE12_EVALUATION_CORPUS

重新从实际 PostgreSQL 与 OpenSearch读取，未采用旧“2文档/71chunks”结论。

| document_id | original_filename / source内容信号 | normal / embedded Qwen1024 | chunks | Markdown | HTML | structured | 长非表格块 | 数值单位信号块 |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 065d11b4-6dfb-4bbb-af15-a2b64fd5fdf3 | KG-20260826-001(2)(1).md<br>Markdown原文frontmatter指向GB/T12229-2025阀门碳钢标准；含化学成分/拉伸/冲击及焊补/探伤 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 60 | 6 | 0 | 6 | 1 | 8 |
| 1d415bed-57c0-4632-85d7-77dcb5d31cee | GB╱T 6414-2017 铸件尺寸公差、几何公差与机械加工余量.pdf<br>PDF/MinerU；GB/T6414-2017尺寸、几何公差、机械加工余量及一般基准 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 72 | 0 | 13 | 13 | 5 | 9 |
| 27c077f1-baa6-4b43-995f-6cb6e6b234b4 | GBT 9438-2013+铝合金铸件.pdf<br>PDF/MinerU；GB/T9438-2013铝铸件分类、孔洞、力学与检验 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 47 | 0 | 5 | 5 | 2 | 9 |
| 49d7f761-886c-412f-b583-6638ee68ab5f | 铸造工艺手册（节选）.pdf<br>PDF/MinerU；浇道截面积比、水模拟、临界高度与氧化膜紊流的手册节选 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 11 | 0 | 2 | 2 | 4 | 3 |
| 93a4387e-0166-4b84-bd83-4483e87f4ac2 | JB T 5106-2025 铸件模样型芯头 基本尺寸.pdf<br>PDF/MinerU；正文JB/T5106—XXXX征求意见稿、替代1991；水平/垂直芯头 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 30 | 0 | 9 | 9 | 0 | 3 |
| 9449d035-dc30-4789-9072-aa88bfffc47d | GBT+47236-2026铸造机械 低压铸造机及其他金属型铸造设备 安全技术规范.pdf<br>PDF/MinerU；GB/T47236-2026金属型设备安全、联锁、危险与验证方法 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 101 | 0 | 7 | 7 | 5 | 2 |
| d9163784-495a-4cc4-a297-0eced8162715 | GBT31204-2014熔模铸造碳钢件.pdf<br>PDF/MinerU；GB/T31204-2014熔模碳钢件材料、检验与复验 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 66 | 0 | 16 | 16 | 2 | 7 |
| f01c9d80-3e44-49ad-8329-41e118726b93 | GB T 32238-2015 低温承压通用铸钢件.pdf<br>PDF/MinerU；GB/T32238-2015低温承压铸钢、牌号、试验与重大焊补 | normal / embedded / Qwen3-Embedding-0.6B / 1024 | 36 | 0 | 4 | 4 | 2 | 2 |


- 全库8文档、423chunks；**8文档全部可评测**，无删除/缺向量/缺任一索引通道排除项。
- 所有423条embedding_status=embedded；model=Qwen3-Embedding-0.6B；metadata维度与pgvector实际维度均1024；非空。
- ordinary文本340块；长非表格21块；Markdown6块；HTML56块；structured总62块。
- “长非表格”定义为>=500字符且不是结构化表格，包含某些前言/图注，不能直接推断为合格长段落题。
- 数值单位信号仅为正文中数字紧邻mm/cm/MPa/dB/%/℃等的保守识别，可能漏掉LaTeX隔开的单位；不作为自动相关标签。
- 内容信号来自实际chunk阅读，未根据文件名判断技术内容。
- Graph anchors/KGRefs存在与否均不参与资格判定。
- KG Markdown与其frontmatter原始标准是同一source。JB5106文件名2025不能覆盖正文XXXX征求意见稿身份，使用范围需Owner审核。

## 只读核验方式与结果

PostgreSQL：项目codex_ro MCP，数据库rag_system，transaction_read_only=on；只执行SELECT。
取文档状态、chunks正文/metadata用于本地审阅，hash使用原始UTF-8正文（保留换行）。
原文暂存于已有*.tmp忽略规则覆盖的本地审计文件；没有正文/向量进入Git版本化manifest。

OpenSearch：项目现有client，只调用get_alias/get_mapping/get_settings/count/search。
对全部423条逐项比对document/chunk ID、正文hash、content_max/content_smart、embedding model/status/dim和1024维有限向量：**missing=0、extra=0、mismatch=0**。
每份文档分别使用现有keyword/vector query builder做doc限定read/query；vector使用其已持久化向量，全部两路非空。
这一步没有重新生成文档embedding。
后续Development实际生产Hybrid查询快照覆盖了全部8个document，未发现仅存在PG而不可检索的文档。

| 文档简称 | lexical探针hits | vector探针hits |
|---|---:|---:|
| KG12229 |32|50|
| 6414 |4|50|
| 9438 |1|47|
| 手册 |10|11|
| 5106草案 |1|30|
| 47236 |7|50|
| 31204 |1|50|
| 32238 |1|36|

探针计数不代表检索质量、完整召回率或gold。
Prefix阶段用真实生产Hybrid和当前Qwen query编码，删除过滤通过项目SessionLocal显式只读事务；验证transaction_read_only=on。
开始/结束均复核文档状态、所有chunk内容与embedding身份、index解析/UUID/mapping/count，无变化。
实验显式持有一个Embedding provider，不声称生产factory已是singleton。

## OpenSearch identity / CORPUS_FINGERPRINT

- alias：casting_chunks_current。
- concrete index：casting_chunks_v1。
- index UUID：I6z2NPZwRPCAQetOXk2c7w。
- version.created：137277827；creation_date：1783053998677；knn=true。
- mapping SHA256：ca3156f6515f3a5b864f997997da14bd855a24b9ce0ac631010c9a36afdd4e8a。
- corpus schema：phase12-golden-v1。
- corpus fingerprint：`c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1`。
- 字段级目录：[corpus_manifest.json](../backend/tests/fixtures/phase12/corpus_manifest.json)。
- 此为**语料身份hash**；人工Gold冻结hash尚未生成。

## Structured table来源与readiness

| 格式 | 原始source/document数 | chunk数 | 拟用topic group数 |
|---|---:|---:|---:|
| Markdown |1|6|1|
| HTML |7|56|5|
| union |8|62|5|

同一文档的多张表没有计成多个source。
三split草案采用Development4文档、Selection2文档、Final2文档；每套8类各5题。
Markdown全部来自单一12229来源，仅留Development；Selection与Final分别从独立HTML来源选题，各有5道真正依赖表格行列的题。
**CORPUS_READINESS = PASS（可建立可追溯草案）**。不存在原来的数量不足停止条件。
这不等于人工题目质量、qrel完整性或语义隔离已获批准；参见[逐题审核](phase-12-m4-golden-dataset.md)。

## TOKEN_LENGTH_CORPUS_AUDIT

只加载已下载的本地BGE tokenizer，local_files_only=True、trust_remote_code=False、use_fast=True。
未下载、未实例化BGE分类模型、model forward=0、BGE CUDA inference=0。
模型revision：953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e。
Tokenizer：XLMRobertaTokenizerFast；pair special tokens=**4**。
tokenizer_config SHA256：7e4c1cc848840aeccdd763458c18dd525eb0f795c992e00ebe9c28554e7db2d4。
tokenizer.json SHA256：69564b696052886ed0ac63fa393e928384e0f8caada38c1f4864a9bfbf379c15。

| Slice | count | min | median | P75 | P90 | P95 | P99 | max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 普通文本 |340|1|43|101.5|174.4|233.15|309.66|332|
| 长非表格块 |21|293|415|566|631|670|675.6|677|
| Markdown表格 |6|159|220.5|283.75|426.5|492.25|544.85|558|
| HTML表格 |56|91|648.5|848|2276|2772.75|3922.2|4096|
| overall |423|1|62|183|536.4|731.5|2540.76|4096|

这里按真实token IDs计数，未用字符估算token。分位数规则见[协议](phase-12-evaluation-protocol.md)。

### 1024/2048/4096 chunk-only coverage evidence

每格为 fully_covered / truncated / truncated_ratio。truncated也即 >相应阈值的数量。

| Slice | 1024 | 2048 | 4096 |
|---|---|---|---|
| 普通文本 |340 / 0 / 0%|340 / 0 / 0%|340 / 0 / 0%|
| 长非表格块 |21 / 0 / 0%|21 / 0 / 0%|21 / 0 / 0%|
| Markdown |6 / 0 / 0%|6 / 0 / 0%|6 / 0 / 0%|
| HTML |43 / 13 / 23.2143%|49 / 7 / 12.5%|56 / 0 / 0%|
| overall |410 / 13 / 3.0733%|416 / 7 / 1.6548%|423 / 0 / 0%|

**不等于pair实际覆盖率**：passage预算=max_length−query真实token数−4。
最大HTML正文恰有4096 tokens，即便最短query，4096 pair也无法完整容纳它。M5需测真实pair截断、质量、latency与VRAM。
Markdown最长558 tokens；本轮题目依赖表格结构，但不能将其描述为>1024的长输入压力样本。长输入压力主要来自HTML，slice需分别报告。
数值JSON与逐chunk token计数：[token audit](phase-12-m4-token-audit.json)。
8192已排除Phase12 v1；未测试。未从1024/2048/4096选择最终值，未比较FP16/BF16质量。
