# Phase 12 M6 — Real RAG / Citation / Graph / Deletion Regression

> 当前状态：PHASE12_M6_ACCEPTED_WITH_OWNER_WAIVER；真实destructive deletion NOT_RUN / OWNER_WAIVED_FOR_PHASE12_M6 / MANUAL_ACCEPTANCE_DEFERRED，不再阻塞M7。此前blocker记录保留为历史；最新决议见文末。

## Status and Git boundary

**PHASE12_M6_BLOCKED / STOP_REAL_CHAIN_UNAVAILABLE**。本轮不能标记 `PHASE12_M6_ACCEPTED`。

真实请求到达已配置的 API LLM 时返回 `LLM_UNAVAILABLE`。依 canonical Plan M6 第 11 项「真实链不可用」停止真实验收；没有换 LLM、修改实际 `.env`、降低 reranker 参数或绕过该失败。安全单元回归与停止后的只读完整性核验已完成。

- Branch：`phase12-bge-reranker`。
- M4 commit：`dbce60a07f6166c47b5f5a4e9e00ba274178381a`。
- M5 commit / M6 start HEAD：`f6c1c01200ad859cd7555846a96da4b886333205`，`test: finalize phase 12 reranker parameter selection`。
- M6 开始前已核验 branch、HEAD、status、diff、diff --check；M5 commit 后 worktree clean。
- M6 commit：**未创建**。用户要求 acceptance 后才独立提交；当前测试、报告和证据留在工作区供审查。不把未验收 M6 宣称为完成，也不与 M5 混交。不 push，不进入 M7。

## M5 Closure and frozen identity

M5 状态为 **PHASE12_M5_ACCEPTED**。本轮没有新增 M5 参数实验、重跑 Selection、刷新 snapshot 或读取 Final 题面设计测试。历史 pending/category-review 状态由 Owner 决议覆盖；原始证据保留。

| 字段 | 冻结值 |
|---|---|
| Profile | `bf16-L1024-C32-B8` |
| Model | `BAAI/bge-reranker-v2-m3` |
| Revision | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |
| Provider / runtime | `local_transformers` / Transformers AutoTokenizer + AutoModelForSequenceClassification |
| Local / remote code | `local_files_only=true` / `trust_remote_code=false` |
| dtype / device | BF16 / CUDA |
| max_length / candidate C / batch | 1024 / 32 / 8 |
| Timeout | 5.0 秒，fail-open safety deadline，非正常延迟 SLO |
| Selection policy | **owner quality-first decision**；Owner-selected frozen profile，不是 M5 自动 winner |
| Production | `RERANKER_ENABLED=false` |

Profile fingerprint：`3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7`。

复用 `tests/phase12_local/selected_profile.py` 和既有 deterministic identity contract，绑定参数、runtime/provider 与六个本地模型/tokenizer 文件的大小/SHA。执行前、停止后均核验实际模型文件。实际加载参数 dtype 为 `torch.bfloat16`，tokenizer 为 `XLMRobertaTokenizerFast`；tokenizer 自身 model_max_length=8192 不覆盖本次明确传入的 `max_length=1024`。

Known paraphrase risk：**accepted known Phase 12 v1 risk**，继续披露同义改写类别退化；没有改 Gold/query/qrel、fusion、threshold、calibration、质量 Gate 或 SLO 来救结果。`bf16-L4096-C8-B1=OWNER_STOPPED_NOT_RUN`，不是 hardware/quality failed。

M5 closure regression：focused 321 passed；Backend full 1740 passed / 28 deselected / 0 FAIL。M5 独立提交后才开始以下 M6 测试。

## Corpus, Golden and isolation

| 身份 | 值 / 核验 |
|---|---|
| Corpus fingerprint | `c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1` |
| Golden fingerprint | `cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a` |
| Golden 文件 SHA-256 | `dadaceb59b3437d35f8356399f0dc70c6f60ad52e51433d989a42e418c4f6289` |
| Selection snapshot fingerprint | `f5e7830cf93a1f492e57aef8bf98794dd9f0ad0c1dd418ba1140813ee993f3b7`，未刷新 |
| 实时 PG / OpenSearch | 执行前及停止后均为 8 docs / 423 chunks / 423 lexical+vector，身份一致 |
| PostgreSQL | 复用 `read_only_session`，REPEATABLE READ / read-only transaction 验证通过 |
| Final run count | **0**；无 Final retrieval、candidate snapshot、BGE score、quality/robustness metrics |
| 保护边界 | 200 个文件 SHA 无变化，包括 actual `.env`、全部生产 Python、Golden、Corpus、Selection 与原 M5 evidence |

新 real harness 不 JSON-parse Golden，只校验文件字节哈希；使用源码中单独编写的六类 wiring query。既有 M4/M5 单元回归的 manifest integrity 校验按原 contract 执行，没有把 Final query/qrel 用于测试设计或真实运行。

Integration settings 用 `frozen_settings(get_settings())` 显式覆盖旧 reranker provider/model/参数，只在测试进程中 enabled=true。未写 actual `.env`，生产默认和读取到的生产配置均为 false。Codex provider / CC Switch / 旧任务 / Codex 状态未被修改。

## RED → GREEN → REGRESSION

新增测试先于测试 helper：初次 collection 因缺少 observation helper RED；补齐后发现独立目录无法导入既有顶层测试 helper，添加局部 conftest 路径适配后 23 GREEN。随后 5 个样本数/SLO 门禁测试 RED → GREEN。首次真实失败后增加 1 个失败报告状态测试 RED → GREEN，确保后续记录不遗留 RUNNING 请求状态。最终 focused **29 passed**。

这些 RED 属于验收工具/观测缺口，**没有宣称发现生产 bug**。生产代码修改为 0。

复用关系：M2 的 `items/harness/FakeRerankingService`、Hybrid fake storage helpers、Graph service/repository helpers、runtime fake tokenizer/model；真实链复用 M0 probe gate、M3 `Observation/DeviceSampler/SmokeLLM`、M4 live corpus/read-only session、M5 selected profile。新增 `RequestTrace` 的原因是旧 helper 没有把同一真实请求的 raw hits、deletion filtering、RAG、Context、Prompt 和 Graph provenance 串成可保存观测。它只包装原调用，不实现第二套算法。未新增 marker 或第二套真实执行 gate。

## Mocked evidence — all PASS

| Contract | 证据 |
|---|---|
| K<C | K=8，Hybrid(32) 一次，32 candidates 进入 service，rerank 后 Top8；公开 limit=8 |
| K=C | K=32，Hybrid(32) 一次，rerank 后 Top32 |
| K>C | K=50，Hybrid(50) 一次，原 RRF Top50；service getter 未调用，model loader 未调用，BGE forward=0；reason=`public_limit_exceeds_reranker_capacity` |
| 短候选 | 实际 1/21 candidates 直接 rerank；请求容量仍32，不 refill、不第二次 Hybrid |
| 空候选 | Hybrid 一次，no_context、原 no-context message，无 BGE/Graph/LLM 调用 |
| Reranker fail-open | exception/timeout/busy/invalid_output，以及不完整 scores，均严格 original C snapshot[:8]，无第二次 Hybrid |
| Disabled | Hybrid(K)，无 reranker，原 RRF 顺序和公开 K 语义 |
| Public API | 顶层仍 question/answer/context_status/citations/retrieval/llm/graph，item 字段集合不变；无 C/raw BGE score/rerank 字段 |
| hybrid_score | 原 item 对象、所有原字段及 Hybrid/RRF score 保持，不写入 raw logit |
| Hybrid error | 原 BusinessError 对象继续抛出；不进入 reranker，不被 fail-open 掩盖 |
| LLM error | 原 BusinessError 对象继续抛出 |
| Optional Graph error | Graph 异常仍继续 text/LLM，调用一次 |

### Prompt / Citation ordering and Context budget

独立 deterministic fixture：A/B/C/D 是 `items(4)` 的四个不同 chunk；score 特意形成非简单逆序。

| 阶段 | 足够预算 | 400-char budget |
|---|---|---|
| Hybrid order | A,B,C,D | A,B,C,D |
| Reranker ranking | C,A,D,B | C,A,D,B |
| Public TopK (K=3) | C,A,D | C,A,D |
| Final Context order | C,A,D | C（截断） |
| Prompt text order | C,A,D | C |
| Citation order | [1]=C,[2]=A,[3]=D | [1]=C |
| Graph enabled input | C/A/D 的 kg_refs 和 provenance | 只有 C 的 kg_refs 和 provenance |

B 被 reranker 淘汰，在所有分支均不触发 Graph。400-char case 中 A/D 虽在 TopK 内，因 Context budget 未进入 final Text Context，也不触发 Graph。Graph enabled 的 service/repository 各一次；fixture 单 graph group。Graph disabled 时两者均零调用，Text/Citation 保持。没有修改 Graph Cypher、预算或 repository contract。

`RequestTrace.verify()` 对比真实调用产生的 Context/Prompt/Citation 和 Graph refs/provenance；另有故意反转 Prompt 观测顺序的测试证明验收会拒绝错序。

### Deletion before tokenizer/model

使用**真实 Hybrid、过滤函数、RRF、reranking service、provider/runtime**，PG/OpenSearch/Embedding 和 tokenizer/model 为替身。六个 raw candidates：normal、deleting、delete_failed、missing Document、unexpected status、invalid document_id。

- raw IDs = 六个 distinct chunks。
- post-deletion IDs = 仅 normal chunk。
- reranker input IDs = 同一个 normal chunk；tokenizer passages 也只有它，fake model forward=1。
- 切换该唯一 normal 为 deleting 后：post-deletion/reranker/tokenizer input 均空，forward=0，Graph/LLM=0。
- 观测顺序为 deletion_filter → hybrid_return → reranker_input，断言 `reranker_input ⊆ deletion_safe_candidates ⊆ raw_candidates`。

这证明移除发生在 tokenizer/model 前，不只是从 response 隐藏。

## Real environment evidence — partial, not acceptance

[原始真实证据](phase-12-m6-results/real-read-only-20260920-01.json) 的顶层状态为 STOP，失败阶段 `real_end_to_end`，业务码 `LLM_UNAVAILABLE`。**1 个真实端到端请求尝试，0 个完成，1 STOP**。未启动后续真实 query、K=32/50、Graph off 或性能 paired samples。

已观察到的真实子阶段：

| 项目 | 实测 |
|---|---|
| 实际模型加载 | 1 次，本地六文件身份匹配；BF16/CUDA，无下载 |
| Embedding | 真实 Qwen3-Embedding-0.6B，单实例与 BGE 同时驻留 |
| Hybrid call count / requested limit | 1 / 32 |
| Raw unique hits / deletion-safe hits | 76 / 76；该 corpus 全部 normal，不是假造真实 unsafe 文档 |
| BGE input / forwards | 32 candidates / 4 batch forwards |
| Rerank applied / fallback | true / null |
| Final TopK / final Context | 8 / 6（真实 Context budget 丢弃尾部两项） |
| 顺序 | final Top8 相对 Hybrid 前8发生变化；Context 与已构建 Prompt 的6个 chunk ID顺序完全相同 |
| Graph | Graph enabled，但该最终 Context 没有可用 kg_refs；Graph/repository calls=0。**未证明真实 Graph success** |
| LLM | 已配置 API provider 返回 `LLM_UNAVAILABLE`；未产生成功 RAG answer/Citation response |
| 清理 | worker_closed=true；repo/LLM/client 关闭；生产配置未启用 |

失败的根本原因尚未诊断，不能据此断言为密钥、网络、模型或服务端故障。没有复制 endpoint、认证信息、请求文本、prompt、answer 或异常原文。

原始请求级 `status` 因首次记录器未在异常退出时改写，仍为 RUNNING；以**顶层 STOP + LLM_UNAVAILABLE**为准，绝不能将其计作成功。该原始文件保持不变。已补上 `record_failure` 单元回归，当前 harness 的变更仅修正后续失败元数据；没有重新执行真实链，也没有替换旧失败证据。原始 JSON 保留执行时源码 SHA，当前 runner SHA 因此与该执行记录不同。

### Latency / GPU observations

| 指标 | 观察值与限制 |
|---|---|
| 冷加载（含移动到GPU/同步） | 1362.48 ms，单独记录，不计 warm SLO |
| 真实 Hybrid | 324.15 ms，一次 |
| 真实 rerank | 992.57 ms，一次；**没有足够样本计算 p50/p95** |
| 请求到 LLM 错误返回 | 2207.17 ms，不是成功请求时延 |
| Retrieval incremental | **未测**，paired samples=0 |
| Device sampled peak | 4899.56 MiB；639 samples，最大采样间隔490.61ms，不代表捕获了瞬时最大值 |
| busy/timeout fallback | M6 mocked wiring PASS；真实 lifecycle 沿用 M3 既有证据，本轮不制造 CUDA hang |

Owner SLO 原样：warm rerank p95≤2000ms、retrieval incremental p95≤2200ms、busy p95≤50ms、timeout≤deadline+100ms、coexist device peak≤6500MiB。M6 **未完成性能验收**，不以单样本、M5旧结果或采样峰值替代。未更换冻结配置。

真实 harness 显式持有一个 Embedding provider，并注入现有 Hybrid 的参数入口；没有卸载 Embedding 换取显存，也没有证明生产多请求 Embedding 生命周期。计划的 paired latency 隔离使用既有 SmokeLLM 替身并关闭 Graph；这一阶段未执行，不能计入真实 LLM/Graph evidence。

## Regression counts

所有普通 pytest 均使用 `PHASE12_BGE_PROBE_ENABLED=0`、`python -B -m pytest -q -p no:cacheprovider`，各自独立 basetemp。真实 CLI 与单元测试分开计数。

| 要求组 | 结果 |
|---|---:|
| M6 focused | 29 passed |
| Reranker Provider/runtime + config + M0/M3 safe tests | 177 passed |
| RAG reranking | 65 passed |
| Context Builder | 9 passed |
| Citation focused | 6 passed / 52 deselected |
| Hybrid/Search | 73 passed |
| Graph fusion/repository safe tests | 181 passed |
| Deletion / Phase10 authorization helpers | 306 passed |
| Lifecycle/startup/close | 18 passed / 78 deselected |
| M4/M5 fingerprint/integrity | 162 passed |
| Backend full safe (`-m 'not integration'`) | **1769 passed / 28 deselected / 0 FAIL** |
| Real integration | **0 completed / 1 STOP**；另有真实 BGE 子阶段一次成功 |

组之间有重叠，不应相加为 distinct test 数。仅既有 Starlette/httpx deprecation warning。完整命令 targets、结果计数和 JUnit 哈希见 [regression JSON](phase-12-m6-results/regression-20260920.json)。JUnit 本地日志保留于 ignored `backend/M6-verification-20260920.tmp/`，不提交包含宿主路径的原始输出。

真实命令（backend 目录；本轮已停止，未经 Owner review 不继续）：

```powershell
$env:PHASE12_BGE_PROBE_ENABLED='1'
.\.venv\Scripts\python.exe -B -m tests.phase12_integration.real_acceptance --output ..\docs\phase-12-m6-results\real-read-only-20260920-01.json
```

## Authorization blockers and scope

真实 destructive deletion：**AUTHORIZATION_BLOCKED**。`PHASE10_INTEGRATION_ENABLED` 与 `PHASE10_M7_ROLLOUT_AUTHORIZED` 未开启；现有 `load_phase10_integration_settings` 返回 None。没有自建 destructive workflow、随机选业务文档、修改8份语料或进行任何 deletion write。完整 mocked deletion-before-model 已通过。

canonical M6 同时要求核验必要授权、使用 dedicated 测试目标，以及「获授权的真实存储回归」；未授权删除本身是停止条件。当前不能把该子项当作 PASS，也不自行降低 acceptance 标准。Owner 需确认既有 dedicated-target / backup / restore / cleanup 前提和授权范围；它与真实 LLM 不可用一起留作验收阻塞项，不能凭 mocked 结果放行整个 M6。

Production files modified：**无**。本轮仅新增 `backend/tests/phase12_integration/` 下4个测试/观测文件、此报告与 sanitized evidence，并在 canonical Design/Plan 记录实际状态。Hybrid/Embedding/Graph/Deletion semantics、schema、mapping、public DTO、frontend、production 默认均未改。

Scope conflicts：未实施范围外变更；触发的是既有真实链不可用停止条件。如果恢复需要修改不在已批准 Phase12 范围内的组件，必须先交 Owner Review，不能借 M6 修复。

## Acceptance and next action

Mocked contract gates 全部 PASS；Profile/Model/Corpus/Golden identity、safe regressions、生产关闭和 `.env` 不变均 PASS；Final run count=0。实际执行了 `git diff --check`，PASS。

**尚未满足**：真实 RAG/LLM 成功、真实 Graph provenance/on-off、真实 K=32/50、足量性能观察，以及获授权的 dedicated storage deletion 回归。故不写 PHASE12_M6_ACCEPTED、不创建 M6 acceptance commit、不进入 M7。

下一步第一项：Owner Review `LLM_UNAVAILABLE`，在保持当前冻结配置、实际 `.env` 和 Codex provider 边界的前提下，确认真实 LLM 可用性及允许的恢复方式；同时明确 Phase10 dedicated destructive 子项的授权前提。获确认后从已保留 M6 测试和新输出文件继续真实验收，不重做 M5、不访问 Final。全部 Gate 满足后才单独提交 M6。

Final state：**AWAITING_PROJECT_OWNER_PHASE12_M6_REVIEW**。

## Owner-authorized recovery — 2026-09-20

### Current outcome

LLM preflight **PASS**，原真实 RAG case 的唯一恢复性 retry **PASS**。原失败关联状态为 **TRANSIENT_REAL_LLM_CONNECTION_FAILURE_RECOVERED**。后续获授权的10次真实 RAG请求也全部通过；没有第三次恢复重试，没有修改 model/provider/timeout/prompt，没有增加 SDK retry 或 cross-provider fallback。

本次只读真实链验收结果为 **REAL_READ_ONLY_PASS**，但整个 M6 仍为 **PHASE12_M6_BLOCKED / AUTHORIZATION_BLOCKED**。按 canonical Plan 第8项 dedicated测试目标、第9项获授权真实存储回归及现有删除验收边界，未完成的 dedicated destructive deletion 不被豁免，也不记为PASS。本次Owner明确没有新增该授权，因此它是当前**唯一剩余 acceptance blocker**。M6 acceptance commit 未创建；不push、不进入M7。

### Authorization, preflight and environment

恢复前只读核验 branch=`phase12-bge-reranker`、HEAD=`f6c1c01200ad859cd7555846a96da4b886333205`；保留所有未提交工作。原三份M6 JSON、实际.env与Owner新增的两个手工诊断脚本均建立哈希保护。

首次网络执行申请被自动审批拒绝，发生在进程启动前，**没有远程请求或retry消耗**。随后Owner明确授权将M6检索语料、问题、RAG prompt、可用Graph证据发送到当前远程目的地，范围为1次最小preflight、1次恢复retry及成功后的10次真实RAG；重新申请获准后执行。此前审批阻塞已解除。[恢复前审计](phase-12-m6-results/recovery-preflight-audit-20260920.json)保留当时待授权状态，未覆盖重写。

| 检查 | 结果 |
|---|---|
| Python executable | `D:\rag_system\backend\.venv\Scripts\python.exe` |
| cwd | `D:\rag_system\backend` |
| Active provider / model | `api` / `gpt-4o-mini` |
| Remote base URL host / path | `api.openai-proxy.org` / `/v1`，与当前Settings完全一致 |
| Remote API key | 仅记录 **SET**；没有输出、复制或持久化值 |
| Timeout | 60秒，与现有配置一致；preflight未设置request级覆盖 |
| Provider / transport | `APILLMProvider` / `OpenAIChatTransport` |
| SDK max_retries | **0**，首次preflight后实际client核验 |
| Proxy environment | HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY及小写对应名均未出现；不记录环境变量值 |
| Settings lifecycle | 新进程读取cached base，frozen copy只改reranker；全部LLM字段相等，无需重新读配置，`get_settings.cache_clear()`调用0次 |
| Provider lifecycle | runner内构造前`clear_llm_provider_cache()`一次；显式build；preflight与RAG使用同一实例；退出关闭；生产每请求路径无变更 |
| Execution environment | 使用本次获准的可访问远程网络执行环境；此前失败未记录此层诊断，不能证明根因是随机远程服务波动 |
| 最小Provider preflight | 1次generate成功、非空响应、api/gpt-4o-mini，**2049.01ms**；不保存响应文本 |
| Original RAG recovery | 相同原query、K=8、冻结profile与原prompt逻辑；**2308.01ms**完成、Citation通过 |

“transient recovered”是Owner指定的**恢复验收分类**：同一项目配置在此次授权重试成功。它不等价于已证明首次失败的底层根因。没有以修改生产网络配置或远程设置救调用。

### Evidence preservation and counts

- 原始失败 [real-read-only-20260920-01.json](phase-12-m6-results/real-read-only-20260920-01.json) 未改，SHA=`1e6a34e0e4d1921b505c518a3750255971476017228b531fdac0cf36dcb7d037`。
- 新真实恢复证据 [real-recovery-20260920-02.json](phase-12-m6-results/real-recovery-20260920-02.json)，SHA=`ee1449be182d69bedd96e15d93ee84846acd30546296439aefa17558436293e8`。
- 1次Provider preflight + **11次真实RAG生成** = 12次远程generate；没有超出Owner明确授权范围。
- **40次**performance RAG调用使用现有`SmokeLLM`替身，真实Embedding/Hybrid/PG/OpenSearch/BGE；Graph关闭以隔离增量时延。它们不是40次远程LLM调用，不计入11次真实端到端。
- 共51个RAG请求均PASS；30个使用真实BGE、120个真实batch forwards。K>C及reranker-disabled性能基线均不执行BGE。
- 8 docs / 423 chunks的实时corpus执行前后fingerprint一致；六个模型文件、profile、Golden字节哈希匹配。Final run count=0。
- [恢复后完整性核验](phase-12-m6-results/recovery-integrity-20260920.json)：205个受保护文件无变化，actual.env、原M5/M6 evidence与Owner脚本均未改变。无生产代码修改。

### Real contract results

| Case | Hybrid调用/limit | BGE input/forward | Public K / final Context | Graph service/repository | 结果 |
|---|---|---|---|---|---|
| 原text恢复，K<C | 1 / 32 | 32 / 4 | 8 / 6 | 0 / 0（无有效refs） | PASS |
| table，K<C | 1 / 32 | 32 / 4 | 8 / 8 | 1 / 1 | PASS，2 anchors success |
| K=C | 1 / 32 | 32 / 4 | 32 / 18 | 1 / 1 | PASS |
| K>C | 1 / 50 | 0 / 0 | 50 / 20 | 1 / 1 | PASS，原RRF Top50 |
| Graph off | 1 / 32 | 32 / 4 | 8 / 8 | 0 / 0 | PASS |
| dedicated Graph on | 1 / 32 | 32 / 4 | 8 / 8 | 1 / 1 | PASS，3 anchors success |
| dedicated Graph off | 1 / 32 | 32 / 4 | 8 / 8 | 0 / 0 | PASS |

其他numeric、long paragraph、exact identifier、paraphrase观察query均成功。它们仅是wiring输入，不是质量指标；没有重算Selection或消除已知paraphrase风险。专用Graph query由已有语料的T-P9-1表格内容/metadata设计，没有查看Final query/qrel。

**Citation / Prompt**：51/51请求满足`final_context_order = prompt_text_order = citation_order`；Context仍是最终TopK前缀，citation来自成功RAG response。dedicated Graph on/off的final TopK和Citation IDs完全相同。

**Graph provenance**：真实Graph调用均来自final Context的refs/provenance，最多一次Graph service调用；这4个实际触发Graph的case均为一个graph group，因此各一次repository query。table case中2个带kg_refs的candidate被rerank淘汰、dedicated Graph on中6个带refs的candidate被淘汰，均无Graph provenance。K=C和K>C中各2个带refs的TopK chunk被Context budget淘汰，也没有Graph provenance。精确chunk IDs与集合核验保存在恢复后integrity JSON。

**once Hybrid / fallback**：51/51均只调用一次Hybrid。真实K>C fallback reason严格为`public_limit_exceeds_reranker_capacity`，BGE forward=0，无补检索。exception/timeout/busy/invalid-output故障路径继续复用原M6 mocked PASS，本次没有人为制造真实GPU故障，也不把mocked故障记录宣称为新增真实故障实验。

**Deletion-before-model**：真实只读请求观测的reranker input是post-deletion安全集合的子集；8文档当前全部normal，因此真实unsafe-status写入/删除未执行。原mocked deleting/delete_failed/missing/invalid ID/其他status完整验证仍PASS。

**Public API / score / errors**：真实调用执行原DTO验证、公开K及hybrid_score保持检查。原Hybrid/LLM error semantics、optional Graph failure由安全回归覆盖，不改生产代码。

### Performance observations under unchanged SLO

| 指标 | 本次恢复实测 |
|---|---|
| Warm rerank（20 samples） | p50 **626.35ms** / p95 **817.74ms**，PASS ≤2000ms |
| Retrieval incremental（20 paired samples） | p50 **628.02ms** / p95 **816.24ms**，PASS ≤2200ms |
| 11次真实完整RAG request latency | 1641.95–5409.34ms，包含远程LLM及可选Graph；不混作rerank latency |
| BGE cold load | 1424.08ms，单独记录 |
| Device sampled peak | **4905.56MiB**，PASS ≤6500MiB；3754 samples，最大采样间隔537.56ms |
| Busy/timeout | 冻结SLO不变；M3真实生命周期证据 + M6 mocked wiring，无新增危险timeout试验 |

同一Qwen Embedding实例全程驻留，没有卸载模型换取显存；BGE模型只加载一次。设备采样可能漏掉间隔内瞬时峰值，PyTorch每请求allocated/reserved/peak同时保存在JSON。结果沿用既有采样方法，不宣称超过证据范围的显存上界。未调整C/batch/length/dtype/timeout/SLO。

### Recovery regression and remaining work

测试先行：新增6项recovery control/metadata/error-redaction测试先RED，再GREEN；相关M6 focused **35 passed**（原29 + 新6）。因为runner有变更，执行Backend full safe **1775 passed / 28 deselected / 0 FAIL**，probe gate=0，普通pytest没有真实BGE。此前各组独立回归结果仍保留；本次full覆盖它们，没有无理由再逐组重复。见 [recovery regression](phase-12-m6-results/recovery-regression-20260920.json)。

`git diff --check` PASS；新增文件空白检查PASS。真实请求完成后只增加文档/证据，没有再次改测试代码。新增`test_llm_recovery.py`，修改仅限test-only runner；actual.env、生产默认和冻结profile均未变。

**唯一剩余blocker**：真实dedicated destructive deletion仍为 **AUTHORIZATION_BLOCKED**，Owner本次明确未授权；没有执行任何存储删除、状态写入、DDL或迁移。下一步只能由Owner处理现有Phase10 dedicated-target、备份/恢复/清理和逐项授权前提；不得在当前会话自行豁免该Gate。满足后才能继续该子项并评估M6 acceptance，不重做已通过的M5或访问Final。

M6 commit：**无**。工作区保留所有测试、历史失败、恢复证据及报告。Final run count=0，`RERANKER_ENABLED=false`。

Final state：**AWAITING_PROJECT_OWNER_PHASE12_M6_REVIEW**。


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
