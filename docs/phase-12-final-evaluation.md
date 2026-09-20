# Phase 12 / M7 Final Evaluation

**Status: PHASE12_M7_ACCEPTED / PHASE12_COMPLETE**。唯一一次正式 Final 已完整执行，总体 Primary / Non-regression 和性能 SLO 通过。Owner 已接受 `multi_condition` nDCG@8 从 1.000000 降至 0.992788（delta=-0.007211913337）的已知风险，允许最终收口。

## Owner category review closure

Owner category review: **ACCEPTED**。Owner decision: **CATEGORY_REGRESSION_ACCEPTED_AS_KNOWN_RISK**。Risk status: **ACCEPTED_KNOWN_RISK_FOR_PHASE12_V1**。

Owner accepts this small category-level regression for Phase 12 v1. 唯一类别均值下降仍存在；`p12-fin-025` baseline nDCG=1.000000、BGE nDCG=0.963940、delta=-0.036060 保持原记录。Owner 接受风险，不改变评测事实，不重新判定该题为无退化，不修改 Golden/qrels。

**Final was NOT rerun. Final run count remains 1.** 原始 preflight、final-run-lock、evaluation、regression、final-audit 全部保持原字节，包含当时的 OWNER_REVIEW_REQUIRED / 待审状态。最新决议独立记录在 `phase-12-m7-results/owner-review-acceptance.json`，不覆盖正式 Final evidence。

Completion is subject to recorded known risks and deferred manual deletion acceptance.

## Git boundary and immutable identity

- Branch: `phase12-bge-reranker`。
- M5 commit: `f6c1c01200ad859cd7555846a96da4b886333205`。
- M6 commit / M7 start HEAD: `df38c7bd8507601e63c5d9c7db515a4de1823a0b`。M6 独立提交后 clean，再开始 M7。
- M7 期间 Owner 确认主动删除 `manual_test_llm_api.py` / `manual_test_project_llm_provider.py`，并明确指示不暂停、继续运行完 M7。保留删除；preflight 将这两个非评测诊断脚本的删除作为精确限定的 Owner exception 绑定身份，其余 tracked 文件必须与 M6 一致。
- 新增仅 test-only M7 helper/tests 和本阶段文档、脱敏证据；生产代码无变更。
- Frozen profile: `bf16-L1024-C32-B8`；Owner quality-first decision，不重新选择参数。
- Model: `BAAI/bge-reranker-v2-m3`；revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`。
- BF16 / CUDA / max_length=1024 / C=32 / batch=8 / timeout=5.0s；direct local Transformers AutoTokenizer + AutoModelForSequenceClassification，local_files_only=true，trust_remote_code=false。5s 是 fail-open deadline。
- Profile fingerprint: `3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7`。
- Corpus fingerprint: `c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1`。
- Frozen Golden fingerprint: `cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a`。
- Final manifest fingerprint: `aa8ad954940b6ad4b3443b663fc5e1a238db1e72fae9b0ec29e46ca1e0fc37e5`。
- Run identity fingerprint: `7528fd5f8a7b21e2cf594df7adaa8863cc0fd4235b8551f8e4582ecd176de089`。绑定 M5/M6、所有生产 Python 和评测 helper/test SHA、六个模型/tokenizer文件、冻结参数、数据、原 M3/M6 evidence SHA、Owner waiver、原 Gate/SLO。
- Preflight PASS；模型、8 documents / 423 chunks / 423 lexical+vector entries 在运行前后均通过只读身份核验。
- `Final run count: 0 -> 1`；40 quality + 1 independent robustness。固定 `final-run-lock.json` 以 exclusive create + fsync 在首次 Final Hybrid 前消耗许可。任何失败或中断均不自动返还；禁止重跑。

## One-shot method

每条 Final query 仅一次真实 Hybrid(32)，同一 deletion-safe snapshot 同时作为 RRF baseline 与 BGE variant。复用正式 `rag.optional_rerank_chunks`、RerankingService、local_cross_encoder 及已有 evaluate/metrics，不复制检索算法。记录 ID、原 rank、RRF score、content hash 和 raw BGE scores，不保存题面、正文、完整 prompt 或 credentials。

41/41 请求成功，Hybrid 调用41次，每池32项；Final BGE请求41次、实际batch forwards164次。另有消耗Final许可之前的1次synthetic readiness forward，共165 forwards，仅加载模型1次。Embedding持有同一模型直至请求结束，未卸载以降低显存。本阶段不调用远程LLM；真实生成/Citation/Graph证据复用M6，不把ranking指标解释为生成答案质量。

## Overall quality

| Metric | Same-pool RRF baseline | Frozen BGE | Delta |
|---|---:|---:|---:|
| HitRate@1 | 0.875000 | 0.975000 | +0.100000 |
| HitRate@3 | 0.975000 | 1.000000 | +0.025000 |
| Recall@8 | 0.975000 | 1.000000 | +0.025000 |
| MRR@8 | 0.918750 | 0.983333 | +0.064583 |
| nDCG@8 | 0.927328 | 0.982307 | +0.054979 |
| Candidate coverage | 1.000000 | 1.000000 | +0.000000 |
| Achievable Recall@8 ceiling | 1.000000 | 1.000000 | +0.000000 |

nDCG@8、MRR@8 严格提升 PASS；HR1、HR3、Recall8 非退化 PASS。Gold 中未进入池的相关项仍计入分母；本次候选覆盖及可达 Recall ceiling 均为1.0。robustness 1题不进入40题质量分母，其质量指标均为null。

## Eight categories and slices

每类5题。下表为 baseline -> BGE；每类 candidate coverage / Recall ceiling 均为 1.000000 -> 1.000000。

| Category | HR1 | HR3 | Recall8 | MRR8 | nDCG8 |
|---|---:|---:|---:|---:|---:|
| definition | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 |
| paraphrase | 0.800000 -> 1.000000 | 1.000000 -> 1.000000 | 0.900000 -> 1.000000 | 0.866667 -> 1.000000 | 0.865247 -> 0.982623 |
| exact_identifier | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 |
| numeric | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 0.900000 -> 1.000000 | 1.000000 -> 1.000000 | 0.965247 -> 0.988970 |
| multi_condition | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 0.992788 |
| long_paragraph | 0.800000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 0.866667 -> 1.000000 | 0.898709 -> 1.000000 |
| structured_table | 0.400000 -> 0.800000 | 0.800000 -> 1.000000 | 1.000000 -> 1.000000 | 0.616667 -> 0.866667 | 0.712321 -> 0.900000 |
| hard_negative | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 1.000000 -> 1.000000 | 0.977100 -> 0.994073 |

**唯一类别均值下降：multi_condition nDCG@8 -0.007211913337。Owner review: ACCEPTED，作为 Phase 12 v1 已知风险保留。** 其余四个类别指标均不降。本次 Final 类别决议独立于 M5 Selection paraphrase 风险的历史接受决议。

Final paraphrase 五项不退化，nDCG由0.865247升至0.982623；Selection已知paraphrase风险继续保留，不能用Final结果抹去历史限制。long_paragraph五项BGE均为1.0。structured_table五项均不退化；全部5题来自HTML，HTML slice与structured_table相同。Markdown slice count=0、所有metrics=null；没有拆分或增造样本。

保留各题 nDCG 下降证据，避免均值掩盖单题行为：

| Query ID | Category | Baseline | BGE | Delta |
|---|---|---:|---:|---:|
| p12-fin-025 | multi_condition | 1.000000 | 0.963940 | -0.036060 |
| p12-fin-032 | structured_table | 1.000000 | 0.500000 | -0.500000 |
| p12-fin-036 | hard_negative | 1.000000 | 0.983218 | -0.016782 |

## Frozen performance SLO

| Observation | Result | Gate |
|---|---|---|
| Warm rerank, n=41 | p50 712.74ms / p95 908.43ms | <=2000ms PASS |
| Retrieval incremental, n=41 | p50 713.01ms / p95 908.70ms | <=2200ms PASS |
| Busy fallback, n=20 | p95 0.003595ms | <=50ms PASS |
| Timeout return, n=1 | 5014.01ms | <=5100ms PASS |
| GPU coexist device sampled peak | 4953.56MiB | <=6500MiB PASS |
| Retrieval baseline / on p95 | 144.36 / 1053.06ms | observation |
| Cold load | 1461.14ms | observation |

Warm/incremental来自41条不同Final请求，各只执行一次，不是重放同一问题。增量用同一次Hybrid快照的rerank-off/on差值；不含LLM。包含1条robustness请求的性能，但不包含其质量指标。

GPU为2444个device-wide采样，最大间隙570.70ms；采样峰值不能证明未采到的瞬时峰值。各请求PyTorch allocator peak另存JSON。没有卸载Embedding、OOM或fallback。

Busy/timeout本次采用冻结配置的真实RerankingService + fault-injected mock provider（1次blocked调用、20次busy）；不是真实CUDA hang测试。M3已有真实GPU timeout/busy/late-discard/recovery证据继续保留，其历史FP16/C8/L512/50ms注入配置不能冒充当前BF16/C32/5s真实hang证据。不重复制造高风险CUDA超时。

## Regression and acceptance evidence

新增M7 harness使用synthetic数据RED -> GREEN；Final未用于debug。补充最终cleanup/env/开关/GPU峰值完整性和Owner删除边界测试后focused33 passed。Backend full在正式Final前完成，之后没有修改执行代码；只生成报告，因此没有无意义重复全量。以下为同一次完整JUnit运行的分组统计，存在重叠，不能相加。

| Group | Passed |
|---|---:|
| M7 focused | 33 |
| Final evaluation harness / metrics | 62 |
| Provider/runtime | 52 |
| RAG/reranking | 114 |
| Context/Citation | 9 |
| Hybrid/Search | 73 |
| Graph | 181 |
| Deletion safe/mock | 283 |
| Lifecycle named checks | 26 |
| M4/M5 fingerprint/integrity/evaluation | 162 |
| M6 focused/integrity/recovery | 35 |

**Backend full safe: 1808 passed / 28 deselected / 0 FAIL**；一条既有Starlette/httpx deprecation warning。普通测试PHASE12_BGE_PROBE_ENABLED=0；真实CLI显式=1。

获授权真实集成分列：M7一次Final含41个真实PG/OpenSearch/Embedding/BGE请求；M6已提交11个完整真实RAG和40个mock-LLM性能请求，本轮未重跑。M6 Prompt/Citation、Graph on/off/provenance、K<C/K=C/K>C、51/51 once-Hybrid、K>C zero BGE、fallback/no-second-Hybrid均沿用原始证据。

原M6首次LLM_UNAVAILABLE和恢复证据保持原字节与SHA，分类为TRANSIENT_REAL_LLM_CONNECTION_FAILURE_RECOVERED；不宣称已证明底层原因。

## Waiver, limits and remaining decision

M6: PHASE12_M6_ACCEPTED_WITH_OWNER_WAIVER。Real destructive deletion: **NOT_RUN / OWNER_WAIVED_FOR_PHASE12_M6 / MANUAL_ACCEPTANCE_DEFERRED**。历史AUTHORIZATION_BLOCKED及mocked deletion-before-model PASS保留，不执行真实删除，不写为PASS。

**Project Owner must later execute/inspect the dedicated real destructive deletion acceptance.**

Final multi_condition 类别退化已由 Owner 明确接受，不再阻塞 M7 acceptance。保持 Final run count=1，不重新运行该 Final，不修改参数/Golden/qrels/Gate/SLO/Selection。M5 Selection paraphrase 历史风险与 M7 Final multi_condition 已接受风险同时保留。

本次文档收口补充合成 M7/selected-profile 单元回归 **48 passed**（33+15），real gate=0，不访问真实 Final。执行代码未变，已有 Backend full **1808 passed / 28 deselected / 0 FAIL** 继续有效，未重复全量。最终提交同时保存此前 Owner 主动删除的两个诊断脚本；这是已绑定的 M7 边界事项。

实际.env SHA前后一致，RERANKER_ENABLED=false，worker已关闭，模型/代码/Corpus/Golden身份均PASS。Public API、Hybrid/Embedding/Graph/Deletion算法及生产文件无改动。未push。两份canonical文档仅追加本次证据与状态，不改变Gate。最终静态边界核验见 `phase-12-m7-results/final-audit.json`。

## Evidence files

- `phase-12-m7-results/preflight.json`: 不可变preflight与完整运行身份。
- `phase-12-m7-results/final-run-lock.json`: 已消耗的唯一Final许可；不可删除/覆盖。
- `phase-12-m7-results/evaluation.json`: 脱敏逐请求snapshot/raw scores、质量/性能、cleanup/integrity。
- `phase-12-m7-results/regression.json`: 全量及分组计数、JUnit SHA。
- `phase-12-m7-results/final-audit.json`: env/Golden/Selection/生产/历史证据边界及artifact SHA。
- `phase-12-m7-results/owner-review-acceptance.json`: 最新 Owner 决议、已接受风险、收口检查及原始证据 SHA；优先于历史待审状态，不改写历史结果。
