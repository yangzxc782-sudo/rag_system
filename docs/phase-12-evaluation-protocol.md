# Phase 12 ranking evaluation protocol

状态：PHASE12_M4_ACCEPTED；123题已由项目负责人原样审核并冻结，Development真实smoke通过。
模型身份仍为 BAAI/bge-reranker-v2-m3。所有路径相对于项目根目录。

## 1. 事实源与使用顺序

- `backend/tests/fixtures/phase12/corpus_manifest.json`：只读语料身份目录；不含正文或向量。
- `golden_manifest.json`：唯一 query/qrel/group 事实源。报告里的题目清单仅供审核。
- `candidate_snapshots_development.json`：Development 前缀核验的原始 C8/C16/C32 快照。没有 BGE score。
- 原文从获批准的只读 PostgreSQL DocumentChunk 或同身份语料解析，不从 snapshot 重建/编造。
- 先审原文、标注相关 chunk，再审核冻结，最后评测。不得用 BGE 排名或分数挑选 gold。

当前review_status/qrel_completeness_status及groups均approved；reviewer_role=project_owner，review_date=2026-09-16，review_reference=Phase12 M4 owner approval。
保留assistant_evidence_draft作为标注起源，不将助手整理当成人工审核本身。
Owner批准当前qrels作为Phase 12 frozen evaluation gold，不宣称穷尽全知识库相关证据；未列项按评测grade0处理，但不是人工确认不相关。
冻结所有grade>0仍是Recall分母，包括候选池外Gold。
FROZEN_GOLDEN_FINGERPRINT：`cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a`。

## 2. Schema、来源和隔离

每题：query_id/query/category/split/group_id/qrels/annotation_status/review_status。
每条 qrel：真实 document_id/chunk_id、grade、SHA256 UTF-8 content_fingerprint、original_filename、source_position（chunk index、页码、section、source_metadata）、简短 annotation_reason。
grade 0=无关；1=相关但非直接完整；2=主要直接证据。HR/Recall/MRR 一律 grade>0。

Development / Selection / Final 各八类各至少 5 题。独立 robustness 不用于补足 40。
第7类 `structured_table` 包括 Markdown/HTML；必须有 table_format 和 table_evidence（依赖类型、chunk、行列定位）。
tag 存在不等于题目依赖表格。HTML 的 rowspan/colspan 必须在原文中核对。
全局必须包含两种格式；格式来源只有一份时保持在同一 split，空 slice 输出 count=0、metrics=null。

group 按原始标准/手册来源和逻辑主题建立，不以 query hash 或导出格式充当来源。
自动拒绝：跨 split 文档/source_id/topic 交集、相同源文件 hash、相同正相关内容 hash、共享近重复 group、规范化 query 相似度>=0.85。
规范化用 NFKC、casefold、去标点、数字占位。此检查不能证明语义完全独立；同源不同格式、OCR 近重复、主题关联仍需人工复核。
Development 也使用相同跨 split 检查。

## 3. Corpus/read-only 与 stale

语料必须 normal、持久化、有完整 Qwen3-Embedding-0.6B 1024 维向量，且 OpenSearch lexical/vector 同时可查。
Graph/KGRef 不作门槛。PostgreSQL 审计用 codex_ro MCP SELECT；实际 Hybrid 删除过滤使用项目 SessionLocal，显式只读事务并验证 transaction_read_only=on。
不执行 UPDATE、DDL、reindex、refresh 或任何存储清理。

corpus hash = SHA256(canonical JSON)，固定排序和 UTF-8，包含文档身份、chunk 身份/正文 hash/版本与 embedding metadata、OpenSearch alias解析结果、concrete index UUID/version/mapping hash、代码 commit、schema version。
`corpus_audit.corpus_identity()` 实现规范序列化。源码/向量正文不进入目录。
每次正式 M5/M7 前从只读源重新核验；旧 JSON 自己不能证明实时数据没变。
missing/deleting/delete_failed/content hash变化/embedding或index identity变化：dataset stale，停止；不得静默补丁式跳过 gold。
工具会重新计算提供的 corpus 目录 hash，拒绝在旧 digest 下篡改 metadata。当前版本输入必须与冻结 corpus 完全一致；漂移应建立新审核边界。

## 4. Candidate snapshot 公平性

每个 query/C 只取一个候选池，baseline 与 variant 共用 IDs。
baseline 严格保存原列表顺序，不重新按 RRF float 排序。
snapshot 保留 query_id/query fingerprint、run ID、C、chunk/document ID、1-based rank、hybrid_score、keyword/vector rank、content hash；不存完整 chunk。
BGE输出必须 exactly N、无 missing/extra/duplicate/unknown identity，original_rank匹配，score有限。
变体排序：raw_score DESC、original_rank ASC、chunk_id ASC。无 sigmoid/threshold/fusion。
任何无效输出整项拒绝，不计算部分指标。每个 C 的 query 覆盖必须完整；不同 C 不合并均值。

当前 40 道 Development 草案实测 Hybrid8/16/32，120 次生产 Hybrid 调用，前缀严格一致（IDs、RRF值和两路 rank 均一致）。
这是前缀协议验证，不是 baseline/variant 双重检索，更不是质量评测。
后续相同语料/配置可一次 Hybrid32 派生8/16/32；索引、参数或实现变化后重新验证。
若任何样本不一致：各 C 保存独立 snapshot；仍在每个 C 内共用一次快照比较两种排序。
现有保存三份审计快照是证据，未来单32采集不需重复它们。
Golden 被 owner 修改 query 后，旧 query fingerprint 快照作废。

## 5. 手算指标

对一个有答案 query，R={所有冻结 grade>0 的 chunk}，排序前8为 T8：

- HitRate@K = 1[TopK 与 R 有交集]，K=1、3。
- Recall@8 = |T8∩R| / |R|。C 以外的相关 gold 仍在分母。
- MRR@8 = Top8首个相关位置 r 的 1/r；无相关为0。
- DCG@8 = Σ(i=1..min(8,N)) (2^grade_i−1)/log2(i+1)。
- IDCG@8 = 全部冻结 qrels 按 grade DESC 的理想前8 DCG。
- nDCG@8 = DCG/IDCG。grade0/1/2的gain为0/1/3。
- candidate_coverage = |C∩R|/|R|。
- recall_at_8_ceiling = min(8, |C∩R|)/|R|。

query 等权平均，不按 chunk 数量加权。空结果且有 gold 得0；不足8自然按实际长度计算。
无答案（R为空/IDCG=0）进入独立 robustness；质量指标返回 null，不进入 HR/Recall/MRR/nDCG/coverage 平均。
重复 candidate 使 snapshot invalid，不能重复计算命中。
结果分 C 输出 overall、八类、structured_table合并/markdown/html、long_paragraph，以及独立 robustness count。

## 6. 审核、冻结、Final 门禁

1. Owner 审核每题 query、category、qrels/grade、冻结评测集合、表格坐标和source/topic分组。
2. 只有明确批准后，将该题 review_status 与 qrel_completeness_status 设 approved，填写实际 owner_review_reference；groups 也须 approved。
3. 所有审核完成后才将 status 设 frozen，并计算整个manifest（排除frozen_manifest_fingerprint自身）的规范JSON SHA256，写入该字段。2026-09-16已按Owner授权完成；hash见前文及canonical manifest。
4. 冻结后才运行一次真实 Development harness smoke。不是质量 Gate，不据此选参数。
5. Selection 仅 M5；Final 仅 M7 且有独立 Owner 授权。

普通 CLI `python -m tests.phase12_local.evaluation --split final ...` 在读文件/打分前直接拒绝。
CLI 没有 --include-final 或同义快捷开关，也不允许 Selection。
M7 显式API需 phase=M7 和 owner_authorization 对象（phase、owner_reference、准确的 frozen manifest hash）；还会再次验证人工审核和语料身份。
授权对象应由负责人实际授权记录构造，不是机器自动产生；普通CLI不能构造。
只有当次授权后才允许 Final scoring。M7失败不得反复用原Final调参。
Core不导入Transformers/CUDA/BGE loader。variants只接受已经完成的严格identity/raw score记录。
M4真实adapter复用production service及既有PHASE12_BGE_PROBE_ENABLED gate，默认零load。

Development CLI（审核后才能使用）接受 `--manifest --corpus --snapshots --variants --output`。
snapshots为该split的list，variants为 `query_id:C8` 等键到 [{chunk_id,original_rank,raw_score}]。
实际 prefix report 的 snapshots 字段可提取使用；正式 smoke还必须纳入独立robustness快照。已补充Development robustness并完成41题smoke，详见phase-12-m4-development-smoke.json。

## 7. Token-only audit 与后续候选

本地BGE tokenizer，local_files_only=True、trust_remote_code=False，不实例化分类模型，不执行forward。
长度统计使用真实token IDs，不用len/4。独立输出普通文本、>=500字符非表格长块、Markdown、HTML、overall。
>=500字符是描述性审计分组，不是模型token安全预算；部分块为前言/图注，Gold类别需人工确认语义。
分位数：排序后位置(n−1)×p作线性插值；p50/p75/p90/p95/p99。
统计未加query的passage token。pair特殊token=4，所以 passage预算=max_length−真实query tokens−4。
记录1024/2048/4096 chunk-only覆盖率，只是乐观覆盖证据，不能声称1024可放1024正文token。
不会截写数据库正文。M5保留query，仅截passage，报告真实pair truncation。

M5候选dtype=[FP16,BF16]；FP32只作数值参考。
M5_MAX_LENGTH_CANDIDATES=[1024,2048,4096]；8192=EXCLUDED_FROM_PHASE12_V1。
C候选8/16/32。至少六个dtype×length组合先经过hardware/SLO feasibility筛查，再做Selection质量对比。M4未运行该矩阵、未选C/batch/timeout/max_length。
当前生产 RerankerConfig 的支持长度仍是512/1024；扩展2048/4096须在后续明确授权的范围完成校验/硬件验证，M4不改生产validator。
