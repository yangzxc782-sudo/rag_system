# Phase 12：BAAI/bge-reranker-v2-m3 设计与实施计划 — Implementation Plan

> 当前状态：PHASE12_M6_ACCEPTED_WITH_OWNER_WAIVER；真实destructive deletion NOT_RUN / OWNER_WAIVED_FOR_PHASE12_M6 / MANUAL_ACCEPTANCE_DEFERRED，不再阻塞M7。此前blocker记录保留为历史；最新决议见文末。

## 一、Planning Baseline 与授权边界

- 状态：`PHASE12_BGE_PLAN_ACCEPTED`。
- 日期：2026-09-15。
- 项目目录：`D:\rag_system`。
- Planning Baseline 提交前分支：`phase12-bge-reranker`。
- Planning Baseline 提交前 HEAD：`9fb2d8cf7be05eca8351076e7eced8af3ba5be12`。
- Canonical Design：[2026-09-15-phase-12-bge-reranker-design.md](../specs/2026-09-15-phase-12-bge-reranker-design.md)。
- Canonical Plan：本文。

本文与 Design 在 M0 前独立存在，Planning Baseline 使用独立提交：

```text
docs: freeze phase 12 bge reranker design
```

初始 Design/Plan 的创建与提交不属于 M0。M0 不再首次创建两份 canonical docs；其 canonical 文档职责仅为真实 BGE probe 后，更新已有文档中的 runtime evidence、benchmark results、SLO owner decisions。M0 commit 不得混入初始规划提交。

本次只落盘并提交 Planning Baseline。以下 milestone 的文件清单、模型操作、测试和提交均为未来计划，不构成本次执行授权。不得开始 M0、下载模型、调用 `from_pretrained()`、运行 BGE、执行 benchmark、修改生产代码或 tests；不得 push。

## 二、M0 前独立 baseline blocker

Planning 时点记录了以下 Phase 11 / Graph baseline mismatch；现已在 M0 前由独立提交 `4fc3412` 解决，以下保留原审计事实：

```text
backend/app/core/config.py:
  rag_graph_context_max_chars = 50000

backend/tests/test_graph_context.py:
  existing test expected = 6000

test_graph_budget_setting_is_independent:
  规划审计已复现 1 failed
```

该问题不属于 Phase 12 reranker。**M0 前独立解决条件已满足。** Phase 12 不得借机修改 Graph budget；本轮 M0 未修改该测试，也未新增 skip/xfail。

旧 Phase 12 `.pyc` 缓存的限定清理已有负责人授权，但本次 Planning Baseline 不执行清理；后续执行仅可在已批准范围内处理，不读取、复用旧 probe 结论，不删除 `.py` 或 fixtures。

## 三、全程冻结的技术 contract

### 1. 模型、runtime 与职责

- 模型：`BAAI/bge-reranker-v2-m3`。
- 推荐 runtime：直接 Transformers，`AutoTokenizer` + `AutoModelForSequenceClassification`。
- runtime 选择依据当前仓库依赖、安装版本和官方接口审计；具体证据见 canonical Design。
- Hybrid Search = Recall stage；BGE Reranker = Precision stage。
- 不使用旧 Ollama reranker 设计，不以 chat/LLM score 代替 Cross-Encoder logits。
- 不新增重量依赖作为默认方案；需要变更依赖时停止并独立评审。

### 2. Candidate capacity 与 public K

`K` 是 public final limit，`C = RERANKER_CANDIDATE_LIMIT` 是经过 benchmark 验证的最大生产 rerank capacity。

```text
disabled:
  Hybrid(K) → 原 RRF Top K

enabled, K <= C:
  Hybrid(C) → BGE Reranker → Top K

enabled, K > C:
  Hybrid(K) → skip Reranker → 原 RRF Top K
  fallback_reason = public_limit_exceeds_reranker_capacity
```

- 禁止 `C_effective=max(C,K)`。
- 将来 K=50 要使用 reranker，必须先在 M5 验证相应容量，再提升 C。
- 已取得 Hybrid(C) 后发生失败：使用原始 C candidates `[:K]`。
- no second Hybrid：不得再调用 Hybrid(K)；一个 RAG 请求最多一次 Hybrid。
- `RERANKER_TOP_K` 标记 deprecated / ignored，运行时不参与最终 K 或 C 选择。
- Planning 阶段不冻结 8、16、24、32 等数值为生产 C 默认。

### 3. 输入、评分、生命周期与失败

- 模型输入仅 query 与 chunk.content；不拼 Graph、KGRefs、ID、Embedding、RRF score 或 section title。
- raw relevance logit DESC，tie-break 为 original hybrid rank ASC、chunk_id ASC。
- 不使用 Sigmoid 排序、不设 threshold、不做 RRF fusion 或分数校准。
- tokenizer 实际计数，完整保留 query，只截 passage；不修改数据库 chunk 正文。
- 单进程一个 lazy reranker 实例、一个 worker；startup/disabled 不加载模型。
- 单进程最多一个 reranker CUDA forward；busy 立即 fallback，不设无限队列。
- bounded wait 覆盖加载、tokenization、inference；timeout 后当前请求 fallback，运行中 forward 自然结束，晚到结果丢弃，结束前保持 busy。
- N input candidates 必须恰好得到 N 个有限且身份匹配的分数，才应用 rerank。
- load failure、OOM、timeout、exception、NaN/Inf、shape/count/identity mismatch 均 all-or-nothing fail-open。
- 不通过卸载 Embedding、自动切设备、自动缩 batch 或采用部分结果掩盖失败。
- Hybrid 与 LLM 自身错误保留现有 contract。

### 4. Delete / Context / Citation / Graph / public API

```text
BM25 + Vector
  → deletion-safe hits
  → RRF
  → Candidate Pool
  → Reranker
  → final Text candidates
  → Context budget + Citation IDs
  → final Text Context 的 kg_refs
  → optional Graph
  → LLM
```

- 当前 PostgreSQL deletion-status filtering 在 RRF 与 reranker 前；非 normal、缺失或非法 document_id 的候选不进入模型。
- 删除过滤后的实际候选可少于 C，不补查、不补齐。
- Context Builder 增加内部 `preserve_order=False`；rerank 成功才传 True；fallback/disabled 保留原行为。
- 不把 reranker score 写进 `hybrid_score`。
- Citation 与 Prompt 使用同一最终 Text Context，编号跟随最终保留顺序。
- 被 reranker 或 Context budget 淘汰的 chunk 不触发 Graph。
- 不修改 Graph Cypher、Repository、budget、Evidence API 或前端；不增加第二次 Graph 查询。
- Qwen3-Embedding-0.6B、1024 维向量、OpenSearch mapping、DB schema 不变。
- Search API、RAG response schema、Citation API、Graph Evidence API 不变；diagnostics 仅内部使用。

### 5. Evaluation 与 Gate

三个隔离集合：

| 集合 | 用途 |
|---|---|
| Development set | 开发、debug、初步参数探索 |
| Selection validation set | M5 参数选择，应用质量 Gate 与已冻结性能 SLO |
| Final held-out set | M7 冻结全部参数后最终一次验收 |

禁止同文档/主题近重复问题跨 selection/final 泄漏。八类问题、人工 gold 0/1/2、每集合每类至少 5 条及指标定义沿用 canonical Design。无答案查询另列健壮性集合，不混入有答案质量指标的分母。

Baseline 为同 candidate pool 的 Hybrid RRF；variant 对该同一候选快照进行 BGE 重排，不用 RAG 答案质量替代 ranking 评测。

Primary quality gates：

```text
nDCG@8 > baseline
MRR@8 > baseline
```

Non-regression gates：

```text
HitRate@1 >= baseline
HitRate@3 >= baseline
Recall@8 >= baseline
```

总体与八类 per-category metrics 均必须输出。任何类别指标下降标记审查，明显退化必须提交负责人，不得用总体均值覆盖。

Planning 阶段不设毫秒级 SLO。M0 实测后由负责人冻结 warm rerank p95、retrieval p95 incremental、fallback tail latency、GPU/显存安全边界。只有数值 SLO 经负责人冻结后，M5 才允许选参；M5 不得改通过线。

M5 只从质量、类别审查、性能 Gate 同时通过的组合中选择，优先资源和延迟成本更低者；具体成本排序按 canonical Design。M7 使用冻结模型、C、batch、max_length、dtype、device、timeout、runtime 与数据指纹只验收一次。

任一最终质量或性能 Gate 失败，保持 `RERANKER_ENABLED=false`，不得宣布 Phase 12 完成。M7 失败后进入新的设计/参数评审周期，不得针对同一 Final held-out 调参重测。

## 四、共同执行与回归规则

所有实现 milestone：

```text
RED → GREEN → REGRESSION → Acceptance → 独立 commit
```

M0：

```text
独立 Graph baseline blocker 已解决
  → 负责人授权开始 M0
  → 探针工具 RED/GREEN
  → 负责人明确授权模型下载与真实推理
  → 实测
  → 更新既有 canonical docs 的证据与 SLO 决议
  → Review
  → M0 独立 commit
```

本次不执行上述 milestone 或其提交。Planning Baseline commit 与 M0 commit 分离。

回归集合按当前实际测试组织：

- **Search**：Embedding、document embedding、OpenSearch、index、Hybrid、vector search、KGRef propagation。
- **RAG**：service、context、API、graph fusion、graph response。
- **Lifecycle**：LLM Provider/startup、Graph Repository，以及新 reranker 生命周期测试。
- **Deletion**：现有 deletion、operation guard 单元测试；真实存储测试继续使用独立授权 Gate。
- **Full**：完整安全单元回归；真实集成与负责人已确认的 deferred 项分开计数，不能把未运行项计为通过。

真实存储写入、清理和删除继续遵守项目已有逐项授权与备份要求。已有 deferred 项保持原审批边界，不通过修改 skip/xfail 宣称全量通过。

以下 M0—M7 每个 milestone 均包含 12 项。

## M0 — Repository Audit + BGE Runtime/Hardware Probe

| 必需项 | 计划 |
|---|---|
| 1. Goal | 验证本机直接 Transformers 的模型 contract、性能、显存与 Embedding 共存，提供负责人冻结 SLO 的数据 |
| 2. Non-goals | 不实现生产 Provider，不修改检索算法，不选定未经质量验证的生产 C；不首次创建或提交 Planning Baseline Design/Plan |
| 3. Read-before-code | 根规则；已独立存在的 canonical Design/Plan；当前 Embedding、Hybrid、config、pyproject；官方模型卡、tokenizer/config；确认独立 Graph baseline blocker 已解决 |
| 4. Files expected to create | `D:\rag_system\backend\tests\phase12_local\bge_probe.py`；`D:\rag_system\backend\tests\test_bge_probe.py`；`D:\rag_system\backend\tests\fixtures\phase12\bge_probe_cases.json`；不包含任何首次创建的 Design/Plan |
| 5. Files expected to modify | `D:\rag_system\.gitignore`：精确忽略 BGE 权重及生成报告目录；`D:\rag_system\backend\pyproject.toml`：仅注册必要测试 marker，不安装依赖；真实 probe 后仅更新已有 canonical Design/Plan 中的 runtime evidence、benchmark results、SLO owner decisions |
| 6. Interfaces | test-only CLI，默认 synthetic；真实模型模式须显式启用，模型路径和实验参数显式传入；输出脱敏 JSON 报告 |
| 7. RED tests | 默认不下载/不加载；输出 shape、数量、身份和 finite 验证；token budget；只截 passage；计时/显存统计；busy/timeout；脱敏 |
| 8. Minimal implementation steps | baseline 独立解决并获 M0 授权后，完成已授权缓存清理并重验 Git；先实现测试工具；下载前确认精确路径和忽略规则；获得模型下载/推理授权；记录模型 revision/指纹；执行 FP32/FP16/可行 BF16 × C8/16/32 × 长度512/1024 × full/micro-batch 矩阵；将真实证据和负责人 SLO 决议更新到既有 canonical docs |
| 9. Regression suite | 探针单元测试、Search 相关安全回归；不运行未经授权的真实存储测试 |
| 10. Acceptance criteria | 完整报告 shape、raw logits、ranking sanity、分数与排序稳定性；包含长段落/长表格/超长 query；报告全部要求的延迟和 GPU peak；Embedding 共存实测；负责人完成 SLO Review |
| 11. Scope stop conditions | baseline 未独立解决；M0 或下载/推理未授权；模型或依赖 contract 不符；需要卸载/修改 Embedding 才能运行；需要量化；所有合理配置均 OOM；真实 retrieval 数据不足 |
| 12. Git commit boundary | 单独提交 probe 工具、测试、必要测试配置与真实 probe 后对既有 canonical docs 的证据/SLO 更新；不得包含生产 Provider、权重或初始 Design/Plan 创建提交 |

### M0 测量方法与必须报告的内容

- 冷加载在独立进程重复测量，并说明文件缓存状态。
- 可行矩阵先 warmup，再进行固定次数重复测量，报告 p50/p95、样本数和失败数。
- CUDA 计时明确同步边界；记录 allocated/reserved peak 及设备全局快照。
- 分别测 Embedding-only、reranker-only、同进程共存。
- 共存测量期间持续持有已加载 Embedding，不通过卸载它降低显存。
- 真实检索基线与 reranker-on 采用相同查询、语料和环境，分别计量 Hybrid 与重排增量；每次模拟请求最多一次 Hybrid。
- 不调用 LLM，也不将 synthetic 检索计时报告为真实 retrieval 性能。

M0 完整报告必须包含：

1. dependency 与模型 revision/指纹，重新读取的 GPU/runtime evidence。
2. model load、score shape、raw logits、ranking sanity。
3. repeated-score 稳定性与跨 FP32/FP16/可行 BF16 的分差、排序一致性。
4. cold model load time。
5. warm rerank p50/p95。
6. C=8/16/32 latency。
7. max_length=512/1024 latency，以及长段落、长 Markdown 表格、超长 query 行为。
8. 实际可行 dtype、full batch 与 micro-batch 表现。
9. retrieval baseline 与 reranker-on p50/p95。
10. incremental retrieval latency。
11. busy fallback latency。
12. timeout fallback latency。
13. reranker-only GPU peak。
14. embedding-only GPU peak。
15. embedding + reranker coexist GPU peak。
16. OOM/fallback 情况，区分真实发生与故障注入。

负责人据此冻结四类数值门槛：warm rerank p95 SLO、retrieval p95 incremental SLO、fallback tail latency SLO、GPU/显存安全边界。SLO owner decisions 记录在既有 canonical docs，不创建新的初始规划文档，不将 baseline commit 并入 M0。

## M1 — Local Cross-Encoder Provider

| 必需项 | 计划 |
|---|---|
| 1. Goal | 实现本地评分、lazy singleton、单 worker、bounded wait 和完整输出校验 |
| 2. Non-goals | 不接入 public API，不修改 Hybrid、Embedding 或 Graph |
| 3. Read-before-code | 既有 canonical docs 中经批准的 M0 evidence/benchmark/SLO 决议；当前 `D:\rag_system\backend\app\retrieval\embeddings.py`、`D:\rag_system\backend\app\llm\provider.py`、`D:\rag_system\backend\app\core\config.py` |
| 4. Files expected to create | `D:\rag_system\backend\app\retrieval\reranker.py`；`D:\rag_system\backend\app\retrieval\local_cross_encoder.py`；`D:\rag_system\backend\app\services\reranking.py`；`D:\rag_system\backend\tests\test_reranker_provider.py`；`D:\rag_system\backend\tests\test_reranker_runtime.py`；`D:\rag_system\backend\tests\test_reranker_config.py` |
| 5. Files expected to modify | `D:\rag_system\backend\app\core\config.py`；`D:\rag_system\backend\.env.example`；`D:\rag_system\.env.example`，复用原键并说明 deprecated TOP_K |
| 6. Interfaces | 不可变 request/candidate/score；可注入 tokenizer/model loader；评分 protocol；进程缓存、非排队 admission、关闭接口 |
| 7. RED tests | disabled 不 import/load；并发首次请求只加载一次；N=0/1；raw logits；非法 shape/NaN/Inf/身份；query 保留；micro-batch 全败回退；busy、timeout、晚到结果、关闭 |
| 8. Minimal implementation steps | 定义轻量类型；实现 lazy local-only loader；明确 eval/无梯度；paired tokenizer；CPU finite scores；实现单 worker 与状态机；限定重试和关闭行为 |
| 9. Regression suite | 新 Provider/runtime/config 测试；Embedding 与 LLM 生命周期回归 |
| 10. Acceptance criteria | 模型只加载一次；单进程最多一个 reranker forward；无无限队列；timeout 后不会使用晚到结果；不需要真实模型的测试全部通过 |
| 11. Scope stop conditions | 需要新重量依赖、独立推理进程、量化、Embedding 生命周期修改，或需放宽 M0 已批准约束 |
| 12. Git commit boundary | 单独提交 Provider/runtime/config 与测试；不包含 RAG 接入 |

## M2 — Candidate Pool + RAG Integration

| 必需项 | 计划 |
|---|---|
| 1. Goal | 接入一次 Hybrid 的 K/C 分支，保留成功重排顺序及现有公共结构 |
| 2. Non-goals | 不修改 Hybrid 算法、公共 DTO、Graph 实现或数据库 |
| 3. Read-before-code | 当前 `D:\rag_system\backend\app\services\rag.py`、`D:\rag_system\backend\app\rag\context_builder.py`、RAG schema/citation/Graph fusion 测试 |
| 4. Files expected to create | `D:\rag_system\backend\tests\test_rag_reranking.py` |
| 5. Files expected to modify | `D:\rag_system\backend\app\services\rag.py`；`D:\rag_system\backend\app\rag\context_builder.py`；`D:\rag_system\backend\app\main.py`；现有 RAG service/context/API/startup 测试 |
| 6. Interfaces | 复用 `optional_rerank_chunks()`；内部返回 applied/selected items；`preserve_order=False`；RAG 测试注入 reranking service；lifespan 关闭缓存 |
| 7. RED tests | K<C、K=C、K>C；精确 capacity 原因；各分支最多一次 Hybrid；C 失败回退原对象排序；Context 不覆盖新顺序；public retrieval 不泄漏 C/score；no_context 不加载 |
| 8. Minimal implementation steps | 在检索前选择 K/C；取得候选后调用内部重排；构造最终 K 结果；成功时 preserve order；沿原链生成 Context/Citation/Graph/LLM；接入关闭逻辑 |
| 9. Regression suite | Search、RAG、Lifecycle、Deletion 单元回归 |
| 10. Acceptance criteria | 一次 Hybrid；K>C 无模型调用；score 不覆盖 hybrid_score；Citation 与 Prompt chunk 顺序一致；淘汰 chunk 不触发 Graph |
| 11. Scope stop conditions | 需要第二次 Hybrid；需要改公共 schema、Graph budget、mapping 或删除策略；出现非本范围 baseline 失败 |
| 12. Git commit boundary | 单独提交 RAG 接入、Context 顺序控制及对应回归 |

## M3 — Real Local Smoke + Failure Recovery

| 必需项 | 计划 |
|---|---|
| 1. Goal | 用真实 BGE 验证生产组件加载、重复调用、超时、busy 与恢复行为 |
| 2. Non-goals | 不进行最终质量选参；不把 mocked Hybrid/LLM 视为真实 RAG 验收 |
| 3. Read-before-code | canonical docs 中的 M0 证据与 M1/M2 结果、单 worker 状态机及现有启动/关闭行为 |
| 4. Files expected to create | `D:\rag_system\backend\tests\phase12_local\bge_smoke.py`；`D:\rag_system\backend\tests\test_bge_smoke.py`；`D:\rag_system\docs\phase-12-m3-local-smoke.md` |
| 5. Files expected to modify | M1/M2 新增的 reranker 组件及其测试，仅限本 milestone 暴露的缺陷 |
| 6. Interfaces | 显式 real-model smoke；固定参数；运行标识与脱敏结果；故障注入独立标记 |
| 7. RED tests | load failure 后恢复；busy 不排队；timeout 后状态保持 busy；晚到结果作废；下一正常请求恢复；关闭时无第二 worker |
| 8. Minimal implementation steps | 跑真实连续评分；验证加载次数与显存；制造受控 deadline/busy；验证恢复；对 OOM/异常分别注明真实发生或注入；持续保留 Embedding 共存条件 |
| 9. Regression suite | 新 smoke 工具单元测试、Provider/runtime、RAG、Lifecycle |
| 10. Acceptance criteria | 真实分数符合 contract；默认进程生命周期无重复加载；超时/busy 能继续 RRF；恢复后请求不使用旧结果；无逐请求 GPU 内存持续增长 |
| 11. Scope stop conditions | CUDA 卡死、无法恢复的资源泄漏、需要卸载 Embedding，或线程方案无法满足已冻结 SLO |
| 12. Git commit boundary | 单独提交 smoke 工具、证据及必要的最小修复 |

## M4 — Golden Dataset + Evaluation Harness

| 必需项 | 计划 |
|---|---|
| 1. Goal | 新corpus readiness审计，>=120个人工Gold，三split各8类×5，公平ranking harness及token coverage证据 |
| 2. Non-goals | 0生产修改；不选C/dtype/batch/max_length/timeout；不运行Selection参数比较或Final；不以模型输出决定Gold |
| 3. Read-before-code | 当前AGENTS、Design/Plan、M0/M3、Hybrid/RAG/reranking、Document/Chunk、Embedding/index、Markdown/MinerU、metadata/KGRef、真实只读语料与fixtures |
| 4. Files expected to create | tests/phase12_local下corpus_audit.py、token_length_audit.py、candidate_snapshots.py、metrics.py、evaluation.py及对应unit；fixtures/phase12下corpus/golden/snapshot manifest；docs下M4 corpus、token、Golden审核及evaluation protocol |
| 5. Files expected to modify | 仅canonical Design/Plan记录Owner A/B和实际证据；不改backend/app、配置、依赖或存储 |
| 6. Interfaces | 三级qrels、真实source/位置/hash、人工review状态、table_format/table_evidence、三split groups、corpus/index身份、原顺序snapshot、raw-score identity输入、分类与格式slice |
| 7. RED tests | 手算指标/C外gold/无答案/空结果；stale与source/topic/内容泄漏；Markdown/HTML结构；local-only tokenizer及1024/2048/4096；同候选池/score身份；Final gate、人工freeze gate |
| 8. Minimal implementation steps | 先readiness，PASS后token-only及原文选题草案；工具RED→GREEN；Development真实prefix核验；Owner逐项review；批准后冻结manifest/hash并运行一次Development quality smoke；最后regression/acceptance |
| 9. Regression suite | M4 focused、M1/M2/M3非真实unit、Hybrid/RAG reranking/Embedding/lifecycle、Backend full not integration and not phase12_local；普通pytest零BGE模型加载 |
| 10. Acceptance criteria | 真实PG+Qwen1024+lexical/vector；三split各>=40且各类>=5；两种表格格式均纳入；无泄漏；手算与公平性通过；corpus及token审计完成；Owner批准Gold；Final未运行；0生产diff |
| 11. Scope stop conditions | 语料/隔离仍不足、Gold无法追溯、需改生产、需看模型决定Gold或运行Final调试；无人工审批则停在AWAITING_PROJECT_OWNER_PHASE12_GOLDEN_REVIEW，不宣称M4完成 |
| 12. Git commit boundary | M4独立边界，不混M3；本轮不得commit/push。仅未来明确授权后提交test/docs/脱敏manifest，不含原文权重或缓存 |

## M5 — Parameter Selection

| 必需项 | 计划 |
|---|---|
| 1. Goal | 在 Selection validation 上选出同时通过质量与已冻结性能 SLO 的完整生产参数组合 |
| 2. Non-goals | 不访问 Final held-out；不修改 SLO；不默认启用 |
| 3. Read-before-code | canonical docs 中的 M0 负责人 SLO 决议、M3 smoke、M4 manifest 与质量 Gate |
| 4. Files expected to create | `D:\rag_system\backend\tests\phase12_local\parameter_selection.py`；`D:\rag_system\backend\tests\test_reranker_parameter_selection.py`；`D:\rag_system\docs\phase-12-m5-parameter-selection.md` |
| 5. Files expected to modify | Owner已授权必要validator扩展；实际为 `backend/app/retrieval/reranker.py:RerankerConfig` 一处允许2048/4096，保留512/1024；两份 `.env.example` 只更新允许值注释，不写最终默认；enabled仍false |
| 6. Interfaces | 输入冻结 SLO、validation manifest、候选参数矩阵；输出每项 Gate、类别审查状态、成本排序及选中 profile 指纹 |
| 7. RED tests | 任一主指标不提升则淘汰；任一非退化指标下降则淘汰；SLO 未冻结拒绝选参；final 输入拒绝；类别风险不可隐藏；无可行组合保持关闭 |
| 8. Minimal implementation steps | Frozen Gold/实时语料核验→Selection40题8/16/32前缀并冻结snapshot→固定54个FP16/BF16 × 1024/2048/4096 × C8/16/32显式batch/microbatch实验→Stage A共存hardware/SLO→冻结feasible list→Stage B同池质量→类别审查→原成本规则。2048/4096 validator经Owner授权TDD扩展；FP32/8192不进入生产候选 |
| 9. Regression suite | 参数选择/metrics 测试、Provider/runtime、RAG；选中参数的真实本地复测 |
| 10. Acceptance criteria | 全部Stage A与feasible profile Selection证据完整、Gate不变、Final run=0；有合格者按原成本顺序提出推荐，类别退化必须Owner review；无合格者报告PHASE12_M5_NO_ACCEPTABLE_PROFILE，不宣称自动失败 |
| 11. Scope stop conditions | Final泄漏、Gold/corpus drift、CUDA不健康、需扩大固定矩阵或修改Embedding/Hybrid/Graph/API；不提高SLO、timeout或修改Gold救结果 |
| 12. Git commit boundary | M4已独立commit且clean才开始M5；本轮M5不得commit/push、不得启用或写最终default，等Owner Review |

## M6 — Real RAG / Citation / Graph / Deletion Regression

| 必需项 | 计划 |
|---|---|
| 1. Goal | 验证真实 RAG 链中重排、Citation、Graph 与删除隔离兼容 |
| 2. Non-goals | 不修改 Graph Retrieval、UI、mapping、数据库结构或既有删除实现 |
| 3. Read-before-code | 当前 RAG/Graph/Deletion 实现及测试；Phase 10 dedicated-target 授权 Gate；M5 冻结 profile |
| 4. Files expected to create | `D:\rag_system\backend\tests\phase12_integration\test_rag_reranker_acceptance.py`；`D:\rag_system\docs\phase-12-m6-real-regression.md` |
| 5. Files expected to modify | 必要的新测试辅助文件与 marker；生产修复仅限已批准 Phase 12 组件，其他范围停止评审 |
| 6. Interfaces | 显式真实环境模式；资源标识、授权与 profile 指纹；区分真实 OpenSearch/DB/Graph/LLM 和替身 |
| 7. RED tests | rerank 后 Prompt/Citation 顺序；淘汰 chunk 不触发 Graph；字符预算淘汰同样不触发；stale deleting/delete_failed/missing Document 在模型前排除；全路径一次 Hybrid |
| 8. Minimal implementation steps | 先通过 mocked contract；核验真实环境与必要授权；使用 dedicated 测试目标；跑 Graph on/off、K<=C/K>C、fallback、无 Context、删除场景；保留请求级证据 |
| 9. Regression suite | Search、RAG、Lifecycle、Deletion；获授权的真实存储回归与真实 RAG 验收 |
| 10. Acceptance criteria | 删除隔离、Citation 顺序和 Graph provenance 均通过；公共 schema 无新增字段；真实与 mocked 证据分列；原 Hybrid/LLM 错误语义保留 |
| 11. Scope stop conditions | 需要共享资源写入或未授权删除；出现现有 Knowledge blocker；需要改 Graph/DB/mapping；真实链不可用 |
| 12. Git commit boundary | 单独提交集成测试与验收报告；不混入范围外修复 |

## M7 — Final Evaluation / Documentation

| 必需项 | 计划 |
|---|---|
| 1. Goal | 使用冻结参数完成 Final held-out 最终一次验收并形成完整交接 |
| 2. Non-goals | 不调参，不反复运行原 final set，不在失败时宣布完成 |
| 3. Read-before-code | M0 SLO 决议、M5 参数指纹、M6 验收、封存 final manifest、全部停止条件 |
| 4. Files expected to create | `D:\rag_system\docs\phase-12-finished.md`；`D:\rag_system\docs\phase-12-handoff.md`；`D:\rag_system\docs\phase-12-final-evaluation.md` |
| 5. Files expected to modify | 两份 Phase 12 canonical Design/Plan 的最终状态；必要的运行说明；不修改参数或质量通过线 |
| 6. Interfaces | 参数、模型、代码与数据指纹必须匹配；最终一次运行输出总体/分类质量 Gate 与性能 Gate |
| 7. RED tests | 参数漂移拒绝验收；final 重复调参运行被阻止；任一 Gate 失败不能生成 completed 状态；缺少真实验收证据不能通过 |
| 8. Minimal implementation steps | 先检查全部指纹与 M6；锁定 final run；执行一次评测；审查类别退化；汇总真实/模拟/未完成项；形成启用、关闭和恢复说明 |
| 9. Regression suite | Full 安全单元回归、已授权集成回归；冻结参数下最终性能与 Final held-out 验收 |
| 10. Acceptance criteria | final nDCG@8、MRR@8 均严格提升；其余三项不退化；类别审查通过；所有性能 Gate 通过；证据完整 |
| 11. Scope stop conditions | 任一质量/性能 Gate 失败、参数漂移、final 泄漏、需要针对同一 final set 调参。保持 enabled=false，进入新评审周期 |
| 12. Git commit boundary | 单独提交最终评测、完成记录和交接；只有全部 Gate 通过才能标记 Phase 12 完成 |

## 五、Planning Baseline Self-review

| 检查 | 冻结结果 |
|---|---|
| 模型为 BAAI/bge-reranker-v2-m3 | 是 |
| runtime 推荐为直接 Transformers | 是；真实依赖与官方 contract 依据在 canonical Design |
| M0—M7 完整且每个包含 12 项 | 是 |
| public K 与 validated capacity C 分离 | 是 |
| K > C 使用 RRF fallback 与指定原因 | 是 |
| no second Hybrid、all-or-nothing、fail-open | 是 |
| Context 顺序、Citation、Graph、Delete 边界 | 是 |
| Embedding、mapping、DB schema、public API 不变 | 是 |
| Development / Selection validation / Final held-out 隔离 | 是 |
| M5 不改 SLO，M7 final 不反复调参 | 是 |
| 各 milestone 独立 commit | 是 |
| M0 不再首次创建 Design/Plan | 是 |
| M0 文档更新仅限真实 probe 后的证据、结果和 SLO 决议 | 是 |
| Planning Baseline commit 不与 M0 commit 混合 | 是 |
| 独立 Graph baseline mismatch 在 M0 前解决 | 已由 `4fc3412` 独立解决 |
| 未带入旧 Ollama reranker 设计 | 是 |

## 六、M0 后 canonical 更新位置

### 1. M0 本次执行边界与 baseline

- Planning Baseline：`559087f42816859a2c255750d1b6613bc228d845`；Graph 测试 baseline 独立修复：`4fc3412f7a20efbac21032b44a8964debed12fd2`，生产默认 50000 不变。
- M0 从 `phase12-bge-reranker` / `4fc3412` 的 clean 工作区开始；没有改 Graph budget。
- 本轮负责人授权模型下载/真实 CUDA/benchmark/单 Embedding 持有者共存，允许 test-only report 和脱敏 JSON。
- 本次授权优先于前文旧 M0 测量条目：只模拟 C8/16/32，**不执行真实 Hybrid/RAG、不实现 worker/busy/timeout**；不因缺少这些本轮禁止的实测而擅自进入 M1/M2。
- test-only CLI 默认 disabled，精确 `PHASE12_BGE_PROBE_ENABLED=1` 才运行；普通 unit tests 与真实模式隔离。本次不需要 pytest marker 或依赖变化，pyproject 未修改。
- 创建测试工具、unit tests、synthetic fixture、[M0 Report](../../phase-12-m0-bge-probe.md)和脱敏结果；Design/Plan 是已有文件的 evidence 更新，不是 M0 首次创建文件。

### 2. Runtime evidence / benchmark results

| 项目 | 当前结果 |
|---|---|
| 模型 revision | BAAI/bge-reranker-v2-m3 / `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |
| 模型身份 | [model-identity.json](../../phase-12-m0-results/model-identity.json)：六文件大小/hash；下载路径按当前 Settings，精确忽略模型目录 |
| Runtime | Python 3.13.9、torch 2.11.0+cu128、Transformers 4.57.6、sentence-transformers 5.6.0、CUDA 12.8 |
| Hardware | RTX 5060 Laptop、capability 12.0、BF16 支持、CUDA total 8150.56 MiB |
| Direct runtime | 三 dtype 均加载；local-only，无 remote code、新依赖或量化；logits=[N,1] |
| 输入 | paired tokenizer、only_second；query 完整；512/1024；长段落/Markdown 表格均有有限分数 |
| Reranker-only | 三 dtype 共 36 配置全部完成 |
| FP16/BF16 coexist | 24 配置全部完成；各场景最慢完整 warm p95=1163.39/1098.38 ms |
| 半精度共存显存 | allocated peak=4133.37 MiB，reserved peak=4538 MiB，device 边界快照 peak=5685.56 MiB |
| FP32 coexist | 7 个不同配置完成；1 个整 batch 配置在显存压力下中止、4 个未执行；补测保持 Embedding loaded、显式 batch=8 |
| 总计 | 67 个不同完整配置、69 次完整配置运行、1380 warm 样本；不完整项不计为通过 |
| 稳定性 | 同配置 repeated drift=0、排序稳定；跨 dtype 有真实分差/顺序变化，不是正式 gold 质量验收 |
| Lifecycle | 实验持续持有一个 Embedding；实际 factory 非 singleton，不能外推生产多请求显存 |

FP32 C32/L512/batch32 在上一完成检查点后超过 300 s 仍无完整配置报告，被手动中止。这个下界不是单次 forward 或 p95。没有捕获 CUDA OOM exception；显存压力和 incomplete 必须单独列出。FP32 batch8/L1024/C32 补测 p95=3916.91 ms，device 快照仍曾 free=0；不作为 M1 优先档位。

全部 cold 分段、warm p50/p95、逐配置 peak、truncation、sanity raw scores、跨 dtype 比较、失败边界和复现命令见 [M0 Report](../../phase-12-m0-bge-probe.md)。真实 retrieval、busy fallback、timeout fallback **未测**；没有实现生产 Provider。

### 3. Regression 与 acceptance 状态

- Probe helper 先 RED→GREEN；batch 筛选补测接口也先 5 RED→GREEN。最终 51 passed。
- 模型调用前 Backend baseline：1335 passed、28 deselected、0 FAIL。
- 模型调用后 Search/Embedding：171 passed；Backend full：1386 passed、28 deselected、0 FAIL。
- 既有 Starlette/httpx warning 保留，不安装依赖消除；真实 BGE 数据与 pytest 数量分开统计。
- 未出现必须新依赖/remote code/量化/卸载或修改 Embedding 才能得到合理运行配置的 scope conflict。FP32 压力配置和未执行项仍需 Owner Review。

### 4. SLO owner decisions / 下一边界

待审建议：warm p95≤2000 ms；retrieval incremental≤2200 ms（编排余量是假设）；busy≤50 ms（未测目标）；timeout 候选 5000 ms、返回≤deadline+100 ms（未实现/未测）；device 采样预算≤6500 MiB（需更完整资源监测）。**这些不是已冻结 SLO。**

推荐 M1 工程验证以 FP16 优先、BF16 对照、FP32 参考，C8/16/32、L512/1024、micro-batch8/16。不得自动设为生产默认；C、dtype、batch、max_length 最终仍由三集合隔离的 M4/M5 决定。

负责人需要明确接受 FP32 未完成边界、SLO/未测预算和资源约束，再授权 M1。生产 K>C 请求级 RRF fallback、no second Hybrid、all-or-nothing/fail-open、Delete/Context/Citation/Graph 边界均未改变。

M0 commit 继续与 Planning Baseline 分离，但**本轮未 commit/push**。M1—M7 没有执行。

当前状态：`AWAITING_PROJECT_OWNER_PHASE12_M0_REVIEW`。

## 七、M3 执行证据与下一授权边界

### 1. 当前 HEAD 与已冻结 Owner 决议

- Branch：`phase12-bge-reranker`；M3 开始 HEAD：`40c54e0ae4cd85693dd56fdbf4f6285e7b5e9a28`，clean。
- Planning `559087f`、Graph baseline `4fc3412`、M0 `06aa95f`、M1 `0b327c0`、M2 `40c54e0` 均独立提交；M0/M1/M2 已获负责人验收。
- M0 Review 已冻结 FP16 主路径；BF16 后续质量对照，FP32 不进入当前生产候选优化。
- 已冻结 SLO：warm p95≤2000 ms、retrieval incremental p95≤2200 ms、busy p95≤50 ms、timeout 返回≤deadline+100 ms、coexist device peak≤6500 MiB。
- **C/batch/max_length/最终 timeout 未选为生产默认**，仍属于 M5；前文 M0“待审”是历史时点，以上是当前明确 Owner 决议。

### 2. M3 结果

详见[M3 Report](../../phase-12-m3-local-smoke.md)及[原始数据](../../phase-12-m3-results/local-smoke.json)。保持 M3 表中原有12项边界，只新增 test-only harness/report，未修改生产组件。

固定模型 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，六文件 hash 已复核，未下载。既有 `PHASE12_BGE_PROBE_ENABLED=1` 为唯一 gate；默认 CLI 零 load，普通 pytest 不执行真实模型。

显式 smoke profile 为 M0 已验证的 FP16/C8/batch8/512；5 s 正常实验 deadline、50 ms timeout 注入。32个 harness unit 全通过，先有对应 RED；最终 focused=409 passed；完整真实运行前 full=1568 passed / 28 deselected / 0 FAIL。

| Acceptance | 实测 |
|---|---|
| Production loader / raw score | local-only Transformers、eval/inference_mode、[N,1]、N finite；四组 sanity 方向通过 |
| Load once / repeated stability | 第一 lifespan load=1；固定20轮 drift=0、ranking stable |
| Warm / incremental p95 | 113.96 / 114.51 ms，均 n=20，PASS |
| Busy p95 | 0.337 ms，n=20，真实 forward 尚在执行，PASS |
| Timeout / recovery | 50 ms deadline，返回max67.82 ms；20/20 timeout→B busy→A结束→C成功；迟到结果未污染 |
| Worker | 最大1个active forward，未排队、未在timeout时提前idle |
| GPU / memory | device sampled peak4673.56 MiB；allocated peak3465.05、reserved peak3518.00 MiB；20轮settled allocated增量0 |
| Lifecycle | 实际TestClient close+clear；第二lifespan新对象lazy加载；shutdown等待真实in-flight自然结束 |
| M2 wiring | deterministic Hybrid + real BGE + fake LLM；K>C零forward；fallback原RRF；每请求一次Hybrid；public API不变 |

所有上述性能 Gate 在本次 smoke profile 下通过。设备采样最大间隔460.25 ms，报告明确其漏采瞬时峰值的限制；手工持有一个 Embedding 与生产非singleton factory边界分开，不外推真实RAG并发显存或完整服务SLO。真实OOM未制造，继续保留M1注入测试。

### 3. 保留问题与后续停止点

首轮真实运行因测试夹具 chunk ID 不满足既有 UUID schema 而终止；原始记录保留。仅将 test-only Hybrid identity 对齐，新增RED→GREEN并重新通过full；没有改 public DTO，最终真实完整运行通过。没有发现需要修改生产实现或扩大范围的 scope conflict。

本轮已更新 existing canonical docs 的真实production-provider/busy/timeout/recovery/SLO证据；没有创建M4数据、访问selection/final集合或选择生产参数。未修改Embedding、Hybrid核心、Graph、OpenSearch、数据库、frontend或生产配置。未commit/push。

M3 需负责人 Review；不得由本轮结果自动进入 M4 或启用生产 reranker。M4/M5/M7三集合隔离与Gate保持原计划。

当前状态（M3记录时点）：`AWAITING_PROJECT_OWNER_PHASE12_M3_REVIEW`。

## 八、M4 Owner decisions 与执行状态

### 当前Owner A/B合同（覆盖先前评测类别与M5候选范围）

- 第7类为Structured Long Table，Markdown与HTML均合法；逐题记录table_format与实际表格证据依赖。整体包含两种格式，单一source不跨Selection/Final；空格式slice报告0/null。
- M5_MAX_LENGTH_CANDIDATES=[1024,2048,4096]；8192=EXCLUDED_FROM_PHASE12_V1。M0/M3历史512/1024数据保留，不代表新的生产候选矩阵。
- M5 dtype=[FP16,BF16]；FP32仅numerical reference。六个dtype×length组合先筛hardware/SLO，再做Selection质量比较。C仍8/16/32，batch与最终timeout未选。
- M0 Owner SLO不变：warm p95≤2000ms、retrieval incremental≤2200ms、busy≤50ms、timeout≤deadline+100ms、coexist device peak≤6500MiB。
- M4只允许真实local-only tokenizer审计，不运行BGE forward或选择profile。当前生产长度validator仍512/1024，2048/4096扩展是未来授权/可行性验证事项，M4未修改它。

### 2026-09-16 M4 draft evidence（历史，收口状态见末节）

Git Gate：phase12-bge-reranker / 97bf6031292853c334d68fe926270ddd344410c1，起点clean。
Planning559087f、M006aa95f、M10b327c0、M240c54e0、M397bf603均独立提交；用户已验收M3。

重新盘点8真实文档423chunks，全部normal、Qwen3-Embedding-0.6B/1024已完成且lexical/vector可查。索引逐chunk身份核验零差异。
Markdown1source/6chunks；HTML7sources/56chunks。CORPUS_READINESS=PASS（草案可行，不等于人工Gold批准）。
corpus fingerprint：c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1。

Tokenizer revision保持953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e，pair开销4，BGE forward=0。
overall token p50=62、p95=731.5、max4096；HTML p50=648.5、p95=2772.75、max4096。
1024/2048/4096的overall chunk-only覆盖410/416/423，HTML43/49/56；不含query+4special开销，不可直接视为pair覆盖。
没有尝试8192，没有质量对比或参数冻结。

120质量题草案（每split40，每类5）+3robustness；149 qrels（0/1/2分别11/12/126）。
所有题目和qrel完整性都是OWNER_REVIEW_REQUIRED，approved=0。语料hash已经生成，Golden冻结hash未生成。
40道Development草案真实Hybrid8/16/32前缀全部一致，120次只读调用，无BGE评分。
工具支持独立C报告、五项质量指标、candidate coverage/Recall上限、table格式/long paragraph切片、stale/leakage与Final/人工审批门禁。

最终M4 focused=96 passed；M1–M3/Search/RAG/Embedding/lifecycle回归403 passed；Backend full=1664 passed / 28 deselected / 0 FAIL。
Development quality smoke尚未运行：按本次Owner指令，必须逐题审核后才能冻结并运行。Selection/Final均未运行。
没有production diff、存储写入、依赖/权重下载、commit或push；没有进入M5。

完整证据与逐题审核：[Corpus audit](../../phase-12-m4-corpus-audit.md)、[Golden review](../../phase-12-m4-golden-dataset.md)、[Evaluation protocol](../../phase-12-evaluation-protocol.md)。
无当前M4实现scope conflict；人工Review是尚未完成的必要Gate，不得宣布M4 acceptance通过。

当前状态：**AWAITING_PROJECT_OWNER_PHASE12_GOLDEN_REVIEW**。


## M4 Finalization — 2026-09-16 Owner Approval

**PHASE12_M4_ACCEPTED**。本节覆盖上面的M4草案待审状态（保留历史审计时间点）。Owner按当前canonical manifest原样批准120质量题+3robustness、全部query/split/category/group/qrel/grade/source/table format。
仅更新审核metadata与冻结状态；实质字段完全不变。qrel_completeness_status=approved表示Phase12 frozen evaluation gold获批，不表示全知识库所有潜在相关证据已穷尽。
CORPUS_FINGERPRINT=`c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1`；FROZEN_GOLDEN_FINGERPRINT=`cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a`。
冻结前及smoke后实时只读核验8docs/423chunks/embedding/OpenSearch alias+UUID+mapping+逐chunk lexical/vector全部一致；identity/leakage/stale Gate PASS。
Development真实smoke FP16/C8/batch8/1024/5s：41/41成功，load1；quality40+独立robustness1；HR1 .825→.95、HR3 .975→1、Recall8 .9875→.9875、MRR8 .8925→.970833、nDCG8 .912238→.973929。仅验证harness，不参与选参或宣称M5质量通过。
Finalization补充TDD6项；M4 focused102、相关回归410+140、Backend full1670 passed /28 deselected /0 FAIL。详细限制与原始数据见[收口报告](../../phase-12-m4-golden-dataset.md)。
M4 production diff0；独立commit边界 `test: freeze phase 12 reranker evaluation dataset`，提交且clean后才开始已授权M5。Selection/Final尚未运行，未选生产参数、未启用reranker、未push。
M5维持FP16/BF16 × 1024/2048/4096 × C8/16/32，8192排除；先hardware/SLO再Selection，Final封存。完整候选profile必须Owner复核后才能写默认值。

## M5 Execution Record — Owner停止后汇总（2026-09-16）

M4独立commit=`dbce60a07f6166c47b5f5a4e9e00ba274178381a`，clean Gate通过后开始M5。仅按Owner第14节扩展实际`RerankerConfig`长度validator与tests/env注释；512兼容、8192拒绝，未设生产默认。

Stage A已冻结54项，25可行（FP16 12、BF16 13）、29拒绝；C8/16/32与1024/2048/4096均按已声明矩阵测试，无FP32生产、量化或自动降参。全部硬件证据先于Selection评分固定。

Selection prefix40/40通过，快照`f5e7830cf93a1f492e57aef8bf98794dd9f0ad0c1dd418ba1140813ee993f3b7`不刷新。已有22份完整质量结果、2份实际GPU拒绝；Owner要求不再执行剩余BF16 2048/4096。调度已停止，最后在途子进程自然结束；BF16/4096/C8/B1未启动，不作淘汰结论。

14项QUALITY_PASS_WITH_CATEGORY_REVIEW，8项QUALITY_REJECTED（Recall下降），2项SELECTION_RUNTIME_REJECTED。无自动推荐，成本选择尚无符合全部边界的输入。状态**OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW**，不宣称M5完整acceptance通过，不进入M6。

Final run count0，Gold/Corpus保持M4冻结hash，最后只读核验PASS。M5 focused84项通过后新增3项失败记录/安全续跑RED→GREEN；相关Selection focused31，M4回归102，M1–M3/检索/RAG/生命周期组合518，最终full1725 passed/28 deselected/0 FAIL。普通pytest没有真实BGE调用。

真实样本、八类别/table/long paragraph、模型版本、worktree hash、失败证据限制和未执行项见[M5收口报告](../../phase-12-m5-parameter-selection.md)。保留Owner待决事项：类别退化是否接受、是否继续未执行项；未经新指令不得继续实验、冻结生产profile、启用reranker或提交M5。

## M5 Owner Closure — 2026-09-20

**PHASE12_M5_ACCEPTED**。本节覆盖历史的 OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW；原始实验记录、自动Gate及失败证据完整保留，未重新计算Selection质量或执行模型实验。

Owner-selected frozen profile：`bf16-L1024-C32-B8`。model=`BAAI/bge-reranker-v2-m3`，revision=`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，provider=`local_transformers`，runtime=Transformers AutoTokenizer + AutoModelForSequenceClassification，local_files_only=true、trust_remote_code=false，dtype=BF16、device=CUDA、max_length=1024、C=32、batch=8、timeout=5.0s。5秒是fail-open safety deadline，不是正常请求延迟SLO。

selection policy = **owner quality-first decision**；不是自动成本排序winner。paraphrase category degradation = **accepted known Phase 12 v1 risk**，必须持续披露；不修改Gold、query、qrel、质量Gate或性能SLO。`bf16-L4096-C8-B1=OWNER_STOPPED_NOT_RUN`，不补跑任何失败、缺失或停止profile，不重跑Selection，不刷新snapshot。Final run count=0，production RERANKER_ENABLED=false，actual .env保持原样。

冻结记录：`backend/tests/fixtures/phase12/selected_profile.json`；test-only helper：`backend/tests/phase12_local/selected_profile.py`。复用既有`corpus_audit.fingerprint()`的sorted-key紧凑UTF-8 JSON/SHA-256格式，identity绑定全部运行参数、runtime/provider、model/revision和M0六个模型/tokenizer文件大小及SHA。确定性生成的profile fingerprint：

`3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7`

原始结果索引/decision仍保留当时自动Gate状态，不覆盖或改写原始证据；本Owner决议与selected_profile为后续M6/M7依据。现有Settings默认值不据此自动启用；integration harness显式构造完整profile并验证指纹。

M5 closure补充15项漂移/旧env隔离测试：15 RED（helper不存在）→GREEN。M5/config/Provider/runtime/RAG及M4 integrity组合321 passed。完整安全回归结果见下方收口验证记录。只提交M5与上下文交接文档，不含M6测试；独立commit后clean才进入M6。M7 Gate与SLO不变。

收口验证：Backend full1740 passed/28 deselected/0 FAIL；actual .env、Golden、Selection、原始82份artifact及生产代码边界SHA不变，git diff --check通过。详细结果见M5报告。M6尚未开始。

## M6 实际验收记录 — 2026-09-20

M5 已独立提交 `f6c1c01200ad859cd7555846a96da4b886333205`，commit 后 clean，亦为 M6 start HEAD。M5 Owner profile 继续冻结 `bf16-L1024-C32-B8`，profile fingerprint=`3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7`；owner quality-first decision 与 accepted known paraphrase risk 不变。

M6 当前 **PHASE12_M6_BLOCKED / STOP_REAL_CHAIN_UNAVAILABLE**。29 focused tests 及 Backend full 1769 passed / 28 deselected / 0 FAIL。Mocked K8/32/50、once-Hybrid、fail-open、deletion-before-tokenizer/model、Prompt/Citation order、Context budget 和 Graph final-context provenance/on-off 均通过，生产修改0。

1 次真实 RAG 尝试中，Hybrid(32) 一次、冻结 BGE 四个 batch 完成，rerank 992.57ms；在已配置 API LLM 处返回 `LLM_UNAVAILABLE`，0 个端到端完成。依 M6 真实链不可用条件停止；没有换配置救结果。此请求 final Context 无有效 kg_refs，真实 Graph success 尚未覆盖；未执行后续 K32/K50/on-off、paired performance，也不从单次观察宣称性能验收。

Phase10 真实 destructive deletion 门禁未开启，明确 **AUTHORIZATION_BLOCKED**。不能把未执行项写成 PASS 或自行降低 M6 acceptance。只读实时 corpus 前后均为8文档/423chunks，200个受保护文件无变化，实际 .env 与生产 enabled=false不变，Final run count=0。

完整证据与阻塞项见 [M6 report](../../phase-12-m6-real-regression.md)。M6 尚无 acceptance commit；测试/证据保留工作区。下一步先 Owner Review 真实 LLM 可用性与 dedicated deletion 授权前提，再继续 M6。M7 Gate、质量 Gate、SLO 未修改；未进入 M7、未 push。

### M6 Owner 授权恢复结果 — 2026-09-20

Owner完成独立LLM诊断后授权一次恢复，并补充明确授权向当前api.openai-proxy.org/v1发送本地检索内容。相同api/gpt-4o-mini/60s配置、SDK retries=0，同一Provider实例的最小preflight PASS（2049.01ms）及原RAG retry PASS（2308.01ms）。首次LLM_UNAVAILABLE按Owner要求归类 `TRANSIENT_REAL_LLM_CONNECTION_FAILURE_RECOVERED`；原失败JSON及SHA保留，没有证明底层故障根因。

11次真实RAG生成与40次使用LLM替身的真实性能请求全部PASS；51/51 once-Hybrid、Context/Prompt/Citation一致。真实K8/32/50通过，K50 BGE forward=0；真实Graph on/off与final Context provenance通过，含rerank淘汰和Context budget淘汰的带refs chunk不触发Graph。20 warm样本rerank p95=817.74ms、20 paired增量p95=816.24ms，device sampled peak=4905.56MiB，冻结SLO通过。冻结profile/指纹、质量Gate与已知paraphrase风险不变。

恢复focused35 passed（新增6项RED→GREEN），Backend full1775 passed/28 deselected/0 FAIL，生产修改0。205个受保护文件无变化，8文档/423chunks实时身份一致，Final run count0，actual.env不变，RERANKER_ENABLED=false。

整体M6仍 **PHASE12_M6_BLOCKED / AUTHORIZATION_BLOCKED**，唯一剩余acceptance blocker为Owner未授权且未执行的dedicated真实destructive deletion；不能记PASS或豁免现行Gate。M6无commit，工作区保留历史与恢复证据，不push、不进入M7。详见[M6恢复报告](../../phase-12-m6-real-regression.md)及新增recovery JSON。下一步仅处理该子项既有dedicated target与授权前提。**AWAITING_PROJECT_OWNER_PHASE12_M6_REVIEW**。


## M6 Owner waiver and closure — 2026-09-20

最新Owner决议覆盖此前真实删除授权blocker：**PHASE12_M6_ACCEPTED_WITH_OWNER_WAIVER**。真实destructive deletion状态为 **NOT_RUN / OWNER_WAIVED_FOR_PHASE12_M6 / MANUAL_ACCEPTANCE_DEFERRED**。历史AUTHORIZATION_BLOCKED证据和mocked deletion-before-model PASS完整保留；没有实际执行删除，没有把未执行项改写为PASS。

```yaml
owner_waiver:
  scope: real_destructive_deletion
  reason: owner elected to defer manual acceptance
  test_result: NOT_RUN
  prior_status: AUTHORIZATION_BLOCKED
  follow_up: MANUAL_ACCEPTANCE_DEFERRED
```

Manual follow-up: Project Owner must later execute/inspect the dedicated real destructive deletion acceptance. 此项不再阻塞进入M7，仅限M6这一明确豁免，不改变M7质量Gate、SLO、冻结profile或Final一次性约束。

M5 commit为f6c1c01200ad859cd7555846a96da4b886333205；M6使用bf16-L1024-C32-B8、profile fingerprint 3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7。LLM/Citation/Graph/K与真实51次请求证据沿用恢复run，无生产修改，不重复高成本真实链。独立M6 commit并确认clean后才进入M7；M7前Final run count=0、actual.env不变、RERANKER_ENABLED=false。

结构化Owner waiver、原始evidence SHA见docs/phase-12-m6-results/owner-waiver-closure-20260920.json。两个Owner手工LLM诊断脚本保持原样归档入M6提交（未执行、未改写）；扫描没有真实凭据。

M6 waiver closure验证：focused/regression组合167 passed（含M6 focused35），Backend full1775 passed/28 deselected/0 FAIL；实际env、原evidence、生产代码及冻结模型身份保持一致。git diff --check PASS。
