# 项目当前状态

恢复日期：2026-09-20（Asia/Shanghai）。项目实际路径：`D:\rag_system`。

> 续作注记：后续用户已明确授权M5 closure及M6。本文件正文保留恢复时点；最新M5验收/profile指纹以canonical Design/Plan末尾及selected_profile.json为准。M5收口提交包含本交接文件以保留来源，M6保持独立提交。

**已恢复到 Phase 12 的 M5 收口 / M6 开始前。最新 Owner 决议已经批准 M5、冻结正式候选，但该决议尚未落实为仓库文档更新、完整 profile 指纹和 M5 独立提交。M6 尚未执行。**

本文件是对旧会话“审计并规划本地重排器”可恢复历史及当前仓库的交叉核验，不是执行旧指令。当前用户只授权恢复上下文并生成本文件，因此本轮没有推进 M5/M6、修改业务代码、运行模型、提交 Git 或更改 Codex 配置。

## 最重要的状态差异

| 项目 | 旧仓库报告/实现状态 | 历史中最新用户决议及当前结论 |
|---|---|---|
| M5 验收 | 报告、Design/Plan、结果索引仍为 `OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW` | 2026-09-19 最新用户消息明确覆盖该历史状态，允许正式收口为 `PHASE12_M5_ACCEPTED`；落盘尚未完成 |
| 最终候选 | 自动选择器无 winner，`recommended_profile_id=null` | Owner 按 quality-first 明确选择 `bf16-L1024-C32-B8`，不是自动成本排序 winner |
| paraphrase 类别退化 | 原来阻塞类别审查 | Owner 已知晓并接受为 Phase 12 v1 已知限制，必须保留披露，不再阻塞 M6 |
| 停止的实验 | BF16/4096/C8/B1 未执行 | 不再补跑，保持 `OWNER_STOPPED_NOT_RUN`；不得写成硬件或质量失败 |
| Git | HEAD 仍为 M4，M5 工作区未提交 | 先完成 M5 closure 和独立 commit，工作区 clean 后才允许开始 M6 RED tests |
| Settings | dtype 默认仍为 fp16，C/batch/length/timeout 均为 None | Owner profile 是已确定的项目决议，尚不等于默认配置已落地；保持生产 disabled |
| M6 | 只有现有 M1/M2 mocked 与 M3 smoke 证据 | 最新 M6 请求没有产生助手执行结果；不存在 M6 报告或专用验收文件 |

不能仅凭 canonical 文档开头或末尾仍写“待审查”，要求用户再次选参；也不能把 Owner 接受 M5 推断成 M5 已提交、M6 已验收。

项目已远超初始骨架阶段：FastAPI、同步 SQLAlchemy/Pydantic、Next.js App Router/TypeScript，已有上传、MinerU/Markdown 解析、分块、Qwen3 Embedding、OpenSearch Hybrid、RAG/Citation、Knowledge Items、删除隔离及 Graph 能力。根 AGENTS.md 的“第一阶段”描述是初始定位，当前阶段以真实代码、阶段文档和后续用户决议为准。

# 当前分支与 Git 状态

本轮实查：

- 分支：`phase12-bge-reranker`，没有切换分支。
- HEAD：`dbce60a07f6166c47b5f5a4e9e00ba274178381a`。
- 最新提交：`test: freeze phase 12 reranker evaluation dataset`，2026-09-16 20:43:52 +08:00。
- 写入本文件前：8 个 tracked 文件修改，91 个 untracked 文件，暂存区为空；与旧会话最后完成报告一致。
- `git diff --check` 退出码 0；Git 提示部分 LF/CRLF 转换警告，不是 diff-check 失败。
- tracked diff 为 58 行新增、11 行删除。91 个新增文件包含测试工具、测试、Selection 快照、M5 报告及原始证据。
- 本文件是本轮唯一仓库新增文件，写入后应增加 1 个 untracked；已有 M5 改动必须保留。
- 当前分支未显示 upstream；本轮未 fetch/push，不能据此断言远端最新状态。历史明确要求不 push。

已确认的独立提交边界：

| 阶段 | Commit | 内容 |
|---|---|---|
| Phase 11 baseline | `a659ce2` | Graph Retrieval 功能 baseline |
| BGE 目标切换 | `9fb2d8c` | reranker 模型占位改为 BGE |
| Planning | `559087f` | canonical Design/Plan 冻结 |
| 独立 Graph baseline 修复 | `4fc3412` | 测试默认期望 6000 对齐生产 50000，不改预算算法 |
| M0 | `06aa95f` | BGE runtime/hardware probe |
| M1 | `0b327c0` | local Cross-Encoder Provider |
| M2 | `40c54e0` | fail-open RAG 集成 |
| M3 | `97bf603` | 真实 BGE runtime smoke |
| M4 | `dbce60a` | 冻结 Golden 与 evaluation dataset |
| M5 | 未提交 | 实验与报告在工作区，Owner 最终决议待收口 |
| M6/M7 | 无提交 | 尚未执行 |

# 已完成工作

## Planning 与 M0-M4

1. 审计真实 Retrieval、Hybrid、Context、Citation、Graph、Provider、GPU、配置占位和依赖；比较直接 Transformers、CrossEncoder、FlagReranker，选择直接 Transformers。
2. 两份 canonical 文档独立提交，M0-M7 各包含 Goal、Non-goals、Read-before-code、文件、接口、RED、实现、回归、验收、停止条件、commit boundary 等 12 项。
3. 独立修复 Graph 测试过时期望，正式生产 `rag_graph_context_max_chars=50000` 保持不变。
4. M0 下载并核验指定 BGE revision，完成 FP32/FP16/BF16、C8/16/32、512/1024、batch、真实 tokenizer、显存/延迟及 Embedding 共存 probe；FP32 压力配置未通过完整执行，未冒充成功。
5. M1 实现 DTO/Protocol、local-only lazy loader、paired tokenizer、raw logits、单 worker、busy、bounded wait、late-result discard、失败锁存与 close。
6. M2 接入 K/C 编排、一次 Hybrid、all-or-nothing RRF fallback、Context preserve_order、Citation/Graph provenance 与 lifespan close+clear。
7. M3 真实 CUDA/生产 Provider smoke，通过重复请求、busy/timeout/recovery、shutdown、cache 生命周期；确定性 Hybrid 和 fake LLM 部分必须与全真实链分开理解。
8. M4 初次因仅 2 文档/71 chunks、单一 Markdown 表格来源不支持 split 隔离而停止。用户补充真实铸造语料，并正式允许 HTML/Markdown Structured Long Table，阻塞随后解除。
9. M4 最终完成 8 文档/423 chunks 的 corpus、120 道质量题（每 split 40，每个八类别各 5）及 3 道独立 robustness、149 条 qrels、指标/快照/泄漏/Final 门禁。
10. 用户原样批准全部 Golden 后，仅更新审核元数据并冻结，完成 Development smoke 与 M4 独立提交；不把获批 qrels 描述成全库穷尽真值。

八类别：definition、paraphrase、exact_identifier、numeric、multi_condition、long_paragraph、structured_table、hard_negative。Development 的表格题为 Markdown；Selection/Final 的表格题为 HTML；空格式切片报告 0/null，不能据此跨来源拆分。

## M5 已实际完成的工作

- Stage A 54 个预声明 profile 全部筛选：25 feasible、29 rejected、0 OOM；12 个 4096/C32 profile 发生 deadline timeout。
- Selection 首次只读 Hybrid 采集 40 题 × C8/16/32 共 120 次；40/40 前缀一致，冻结 40 份 C32 快照，C8/C16 派生，不为每个 profile 重新检索。
- 24 份处理记录：22 份完整质量报告，40 题各重复两次，共 1760 条完整请求记录；另 2 份实际 Selection 显存拒绝记录。
- 14 个总体质量通过但 paraphrase 退化；8 个 C16 profile 因 Recall@8 从 0.9125 降至 0.8875 被拒绝。
- 两个 4096/C8/batch8 profile 在真实变长 Selection 中显存超限；BF16 peak=8150.56 MiB，FP16 精确峰值与分数未落盘，只有超限/控制流证据，不能补造数值。
- 初版实验工具缺口已补测试和安全续跑：每请求检查历史 GPU peak、实际 Selection 拒绝覆盖 Stage A 可行、续跑验证既存证据 hash。仅实验工具修正。
- 用户叫停后停止调度，让在途 BF16/4096/C8/B2 自然结束；BF16/4096/C8/B1 没有启动。历史记录确认调度器和子进程退出，本轮未重查进程。
- 生产改动仅 `RerankerConfig` 长度合法集合由 512/1024 扩至 512/1024/2048/4096；512 兼容保留、8192 拒绝。两份 `.env.example` 只改允许值注释。
- 最后一个完成的实际工作是 **M5 停止后的结果汇总、证据核验与文档整理**。最后一个完成且已提交的 milestone 是 **M4**。

# 关键技术决策

## Owner 已冻结的唯一后续 Profile

| 字段 | 值 |
|---|---|
| profile_id | `bf16-L1024-C32-B8` |
| model | `BAAI/bge-reranker-v2-m3` |
| revision | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |
| runtime/provider | 直接 Transformers，`AutoTokenizer` + `AutoModelForSequenceClassification`，`local_transformers` |
| device/dtype | CUDA / BF16 |
| max_length | 1024 |
| candidate capacity C | 32 |
| batch_size | 8 |
| timeout | 5.0 秒，fail-open safety deadline |
| local_files_only | true，禁止下载和 remote code |
| production enabled | false |
| selection policy | Owner quality-first decision |

现有 matrix/index/snapshot/worktree hash 不等于完整 selected-profile fingerprint。本轮没有发现已经落地、绑定 model/revision/dtype/device/length/C/batch/timeout/provider 的最终 profile 指纹；需要 M5 closure 复用现有 `corpus_audit.fingerprint()` 及 identity 约定确定性生成/确认，不能人工编造。

## 检索与生命周期 Contract

```text
BM25 + Vector (OpenSearch)
  -> PostgreSQL Document normal 状态核验
  -> 去重 + weighted RRF
  -> deletion-safe candidates
  -> optional BGE reranker
  -> public Top K
  -> Context 字符预算 + Citation ID
  -> final Text Context 的 kg_refs
  -> optional Graph
  -> LLM
```

- Hybrid 两路默认 top-k=50，权重各 0.5、rrf_k=60；按 chunk_id 合并。另有 pgvector vector-search，但它不是这条 RAG Hybrid 的向量通路。
- K 是 public final limit，C 是已验证最大 rerank capacity。enabled 且 K<=C：Hybrid(C) -> rerank -> Top K；K>C：Hybrid(K) -> skip BGE -> RRF，原因 `public_limit_exceeds_reranker_capacity`。disabled/配置无效也按既有分支处理。
- 每请求最多一次 Hybrid。候选不足 C 不补齐；BGE 自身异常、busy、timeout、非法输出等精确回退原候选 `[:K]`，不能掩盖 Hybrid/LLM 自身错误。
- 删除过滤在 RRF/候选裁剪及 tokenizer/model 前；deleting、delete_failed、missing/非法 Document 等不进入 BGE。上游 stale hit 仍可能占 OpenSearch top-k，这一版本没有补查机制。
- BGE 仅输入 query+chunk 正文；paired tokenizer、`truncation="only_second"`，保留 query，只截临时 passage，不改持久化 chunk；超长 query 安全失败。
- 严格 `[N,1]` raw logits、exactly N finite scores 和 ID/rank 全量验证；按 raw score DESC、original rank ASC、chunk ID ASC 排序。原 Hybrid item/metadata/score 不改写。
- `preserve_order=True` 仅用于完整重排成功；fallback 保持 RRF 行为。Context、Prompt 与 Citation 来源一致，Graph 只取预算后真正保留的 chunks。
- 单进程缓存、lazy load、单 worker/单槽位、不排队；timeout 返回后实际 forward 未完成前仍 busy；迟到结果丢弃。close 等待在途任务自然结束，然后清引用/缓存。
- 当前生产 model load failure 锁存到新生命周期；canonical 早期“加载失败每请求重试”叙述已由 M1/M3 后续决议覆盖，以实际实现为准。
- Qwen3-Embedding-0.6B、1024 维、OpenSearch mapping、Graph Cypher/预算/Repository、public API/DTO、DB schema 保持既有边界。

## 固定质量与性能门槛

- 同一 frozen candidate pool 比 RRF/BGE；HitRate@1、HitRate@3、Recall@8、MRR@8、nDCG@8。Recall 分母保留所有冻结相关 Gold，包括不在 C 内的相关项；grade>0 作为二值相关用于对应指标，nDCG 保留既有 graded gain。
- nDCG@8 和 MRR@8 各自严格提升，其余三项不退化；分类退化单独审查。此次 paraphrase 风险是 Owner 显式接受，不是修改质量 Gate 或隐藏退化。
- warm rerank p95<=2000ms；retrieval incremental p95<=2200ms；busy p95<=50ms；timeout 返回<=deadline+100ms；Embedding 共存 device peak<=6500MiB。
- 三集合严格隔离：Development 用于开发/harness，Selection 用于 M5，Final 只留 M7 一次冻结验收。当前 Final run count=0。
- GPU 实验只显式持有一个 Embedding；生产 Embedding factory 非 singleton，不能把这些结果外推为生产并发 RAG 显存通过。

## 已否决或停止的方案

旧 Ollama/Qwen reranker 方案不作为 Phase 12 依据；不选 FlagEmbedding 作为默认捷径；不使用 sigmoid 排序、score fusion、threshold、人工校准或 LLM 打分。禁止自动扩大 C、缩 batch/length、降 dtype、转 CPU、量化或卸 Embedding 救结果。FP32 不进入当前生产候选，8192 排除。旧“只接受 Markdown 长表格”要求被正式替换为 HTML/Markdown Structured Long Table。C8 的低延迟优势不再触发选参讨论，Owner 已选 C32。停止/失败/缺失 profile 与 Selection 均不得补跑。

# 关键文件

以下路径均相对 `D:\rag_system`，保留既有文件职责。

| 文件/目录 | 职责与状态 |
|---|---|
| `AGENTS.md` | 项目规则、目录、存储/Git 安全、MCP 只读约束 |
| `docs/superpowers/specs/2026-09-15-phase-12-bge-reranker-design.md` | canonical Design；尚未反映最新 M5 Owner closure |
| `docs/superpowers/plans/2026-09-15-phase-12-bge-reranker-implementation-plan.md` | M0-M7 计划及历史证据；需同步最新决议 |
| `backend/app/core/config.py` | Settings，默认 disabled，尚无完整生产默认 profile |
| `backend/app/retrieval/reranker.py` | DTO、配置与结果校验；M5 唯一生产 diff 为允许长度扩展 |
| `backend/app/retrieval/local_cross_encoder.py` | BGE local runtime/tokenizer/forward |
| `backend/app/services/reranking.py` | 单 worker、失败/timeout/排序、cache 生命周期 |
| `backend/app/services/rag.py`、`backend/app/rag/context_builder.py` | K/C 与 fallback、preserve_order、最终 Context/Graph 来源 |
| `backend/app/rag/citations.py`、`graph_response.py` | Citation 和既有 Graph Evidence 映射 |
| `backend/app/services/hybrid_search.py` | OpenSearch 双路、PG 删除过滤、RRF |
| `backend/app/main.py` | lifespan cleanup 接线 |
| `backend/tests/phase12_local/` | M0 probe、M3 smoke、M4 corpus/metrics/snapshot/finalization、M5 hardware/selection 工具 |
| `backend/tests/fixtures/phase12/corpus_manifest.json`、`golden_manifest.json` | M4 冻结语料和人工获批 Gold；不得修改内容 |
| `backend/tests/fixtures/phase12/selection_candidate_snapshots.json` | M5 冻结快照，未提交，禁止刷新 |
| `backend/tests/fixtures/phase12/m5_profile_results.json` | 原始证据路径/SHA 索引；历史待审状态仍在 |
| `docs/phase-12-m0-bge-probe.md`、`phase-12-m3-local-smoke.md` | 真实模型/生命周期证据与局限 |
| `docs/phase-12-m4-corpus-audit.md`、`phase-12-m4-golden-dataset.md`、`phase-12-evaluation-protocol.md` | corpus、审核/冻结、评测协议 |
| `docs/phase-12-m5-parameter-selection.md`、`phase-12-m5-hardware-screen.md`、`phase-12-m5-quality-results.md` | 未提交 M5 汇总/完整矩阵/类别结果 |
| `docs/phase-12-m5-results/` | hardware/selection 原始 JSON，必须保留 |
| `backend/tests/integration/phase10_support.py`、`phase10_run_context.py`、`conftest.py` | 专用删除目标、授权、备份/隔离机制 |
| `docs/phase-10-finished.md` | 实际状态 `REAL_ROLLOUT_PENDING`，不是完整真实删除验收通过 |
| `docs/phase-11-m65-graph-evidence.md` | Graph Evidence API/前端历史 contract，不等于 Phase 12 M6 |

8 个已有 tracked 修改为：根 `.env.example`、`backend/.env.example`、`backend/app/retrieval/reranker.py`、`backend/tests/phase12_local/candidate_snapshots.py`、`evaluation.py`、`backend/tests/test_reranker_config.py`、上述 canonical Design/Plan。

关键新增测试为 `backend/tests/test_phase12_m5_hardware.py` 和 `test_reranker_parameter_selection.py`；M5 工具为 `backend/tests/phase12_local/m5_hardware_screen.py` 和 `parameter_selection.py`。

预期 M6 文件 `backend/tests/phase12_integration/test_rag_reranker_acceptance.py`、`docs/phase-12-m6-real-regression.md` 本轮确认均不存在。

# 已验证证据

## 本轮只读实查

- 通过 Python `sqlite3.connect("file:C:/Users/32884/.codex/state_5.sqlite?mode=ro", uri=True)` 查询 threads，递归核验 rollout 引用和祖先文件；没有写 Codex 数据库或线程。
- 实读 Git 分支、HEAD、log、status、diff、cached diff、diff check 及关键源码；没有把旧日志中代码副本当成当前源码。
- M5 结果索引的 **82 个原始 artifact SHA-256 全部匹配**，索引自身 deterministic fingerprint 匹配。
- Selection snapshot 的 deterministic fingerprint 匹配；Golden/Corpus 文件与当前 M4 HEAD 内容一致（规范化换行比较）。仅核验 manifest 元数据/完整性，不展示或用 Final 题面/qrels 设计测试。
- 本地 BGE 六文件 SHA 与 M0 `model-identity.json` 全部匹配：config、model.safetensors、sentencepiece、special_tokens_map、tokenizer、tokenizer_config。没有加载模型。
- 根 `.env` 与 `backend/.env` 的 `RERANKER_ENABLED` 均为 false，但两者仍含旧 Qwen provider/model 名称。只提取了非敏感字段，没有输出连接串或密钥；没有修改实际 `.env`。后续 harness 必须显式构造冻结配置。
- 未查询当前 PG/OpenSearch/Neo4j 的实时状态，没有重跑 pytest、真实模型或数据库集成。因此旧的 8 docs/423 chunks、服务可达性、当前 GPU 可用显存及运行进程状态均只能作为最近历史证据，进入实施时需按边界重新验证。

关键冻结指纹（业务证据 hash，不是认证信息）：

```text
Corpus:   c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1
Golden:   cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a
Selection:f5e7830cf93a1f492e57aef8bf98794dd9f0ad0c1dd418ba1140813ee993f3b7
M5 index: b97ea67a6cb1f7fe2f6443fd498976d5d81657b70ac56709b6ad90ef13da8ade
```

## 历史测试结果（本轮没有重跑）

| 阶段 | 重点结果 | Backend full 安全回归 |
|---|---|---|
| Graph baseline | focused108，RAG/Graph176 | 1335 passed / 28 deselected |
| M0 | probe51，Search/Embedding171 | 1386 passed / 28 deselected |
| M1 | focused84，Embedding/LLM lifecycle/config213 | 1470 passed / 28 deselected |
| M2 | focused178，RAG58、Graph106、Deletion/guard203 | 1536 passed / 28 deselected |
| M3 | harness32，focused409，独立真实 smoke | 1568 passed / 28 deselected |
| M4 草案 | focused96，相关回归403 | 1664 passed / 28 deselected |
| M4 收口 | focused102，相关回归410+140，Development41请求 | 1670 passed / 28 deselected |
| M5 最新 | 初始focused84+后补3，Selection focused31，M4回归102，组合回归518 | 1725 passed / 28 deselected / 0 FAIL |

存在既有 Starlette/httpx 弃用 warning；28 deselected 不能算作真实存储集成通过。真实 GPU 结果另计，不能加到 unit tests 数量中。

M3：warm p95=113.96ms，incremental p95=114.51ms，busy p95=0.337ms；50ms 注入 deadline 下 timeout 最大返回67.82ms；20/20 timeout->busy->自然完成->恢复；device sampled peak4673.56MiB。此为 FP16/C8/B8/L512 smoke，不是后续 BF16/C32 全真实 RAG 的验收。

Owner 已选 profile 的现存 Selection 结果（直接读取已有 JSON，未重新计算质量）：

| 指标 | 同池 RRF baseline | BF16/L1024/C32/B8 |
|---|---:|---:|
| HR@1 | 0.725 | 0.825 |
| HR@3 | 0.875 | 0.925 |
| Recall@8 | 0.9125 | 0.9375 |
| MRR@8 | 0.80625 | 0.8729166667 |
| nDCG@8 | 0.8286245621 | 0.8865904670 |

该 profile 的 paraphrase 5题：baseline 五指标全为1；variant HR1/HR3=0.6、Recall8=0.8、MRR8=0.65、nDCG8=0.6861353116。风险被 Owner 接受，数据本身没有变。

Stage A warm/incremental p95约1017.34/1012.42ms，device peak4819.5625MiB；Selection80请求 p50/p95=754.78/944.98ms，0 fail、device peak4835.5625MiB。设备采样会漏过采样间瞬时峰值，不能解释为绝对连续峰值保证。

## 执行过的重要命令/入口

以下是历史命令摘要，不是本轮执行清单，也不是要求重跑实验。

- Git Gate：`git branch --show-current`、`git rev-parse HEAD`、`git status --short`、`git diff --check`、`git log -15 --oneline`。
- 后端历史安全测试使用 `backend\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp <该次专用临时目录>`，由 backend 目录运行。
- M5 focused 目标：`tests/test_phase12_m5_hardware.py tests/test_reranker_parameter_selection.py tests/test_reranker_config.py`。
- 最新安全 full 历史选择表达式：`-m 'not integration and not phase12_local'`，真实 BGE gate 关闭。未来核对实际 markers，不能机械复制旧命令替代检查。
- 真实工具唯一 gate：`PHASE12_BGE_PROBE_ENABLED=1`；M0/M3 分别使用 `tests.phase12_local.bge_probe`、`bge_smoke`。
- M5 首次采集 `parameter_selection.capture_selection(...)`；Stage A `m5_hardware_screen.run_matrix(...)`；Selection `parameter_selection.run_feasible_selection(...)`。用户已禁止再次调用这些选参/采集入口重跑。
- 历史 M4 独立提交：`git commit -m "test: freeze phase 12 reranker evaluation dataset"`；不是本轮提交。
- 历史只读存储核验确认 `rag_system`、`codex_ro`、read-only transaction，核对文档/分块/embedding 和 OpenSearch alias/UUID/mapping/lexical/vector；不记录含认证参数的命令。
- 停止实验时仅终止对应 M5 CPU 调度器，等待在途 GPU 子进程自然退出；没有清库、删卷或清对象存储。

# 未完成工作

1. **M5 工程收口未完成**：最新 Owner 决议未写入 M5 汇总、canonical Design/Plan；完整 selected-profile fingerprint 待确认/生成；closure 回归及独立 commit 尚未执行。
2. **M6 未执行**：不存在专用 regression 文件、真实环境报告、验收结论或 commit；M2 mocked/M3 smoke 不能替代 M6。
3. **M7 未执行**：Final held-out 尚未运行，Phase 12 finished/handoff/final-evaluation 尚未形成，不得宣称 Phase 12 完成或开启生产 reranker。
4. Selection robustness 在实验停止时未执行；保留为未测事实，不自动补跑，不把它算进40题质量分母。
5. Phase 10 真实 rollout 文档仍 pending；真实破坏性删除需要既有 dedicated targets、双 gate、确认及 backup/restore，不得因 M6 大目标而扩大到共享语料。
6. Knowledge Item Library 重构相关 deferred marker 已在 pytest 配置中存在；若 M6 碰到既有 Knowledge blocker，单独报告，不能顺手扩大范围修复。

## 当前阻塞项与已解除事项

- M6 的直接前置阻塞：缺少 M5 独立 commit 和 clean 起点；不是等待用户重新选择 profile。
- 类别审查阻塞已由最新 Owner 决议解除；paraphrase 退化转为持续披露的已知风险。
- M4 早期语料不足与 Graph budget baseline mismatch 均已解决，不能当成当前 blocker。
- 旧线程最后一次请求以 API 调用错误结束，未产生助手回复或项目执行结果；此为旧会话历史运行中断，不作为修改当前 provider/auth 配置的理由。
- 当前真实服务可达性和删除 dedicated target/gates 未复验；若不满足，真实 destructive 子项应 `AUTHORIZATION_BLOCKED`，其余安全 contract 工作可独立推进；是否阻塞整体验收按 canonical 标准判定，不能自行降低。

# 当前任务

本轮任务仅为上下文恢复与交接，已核对历史中最新 M6 请求和当前仓库之间的差异。**当前不执行旧请求中的 M5 closure、commit 或 M6 开发。**

后续继续开发的第一项实际任务是：在当前 M4 HEAD 上审计并收口既有 M5 diff，把 Owner 已接受的 `bf16-L1024-C32-B8` 决议与已知 paraphrase 风险落盘，生成/确认完整 profile 指纹，运行规定安全回归后形成独立 M5 commit。不要直接创建 M6 RED tests，更不要继续参数搜索。

# 下一步执行计划

1. 重新 Git Gate，读取本文件、AGENTS、canonical Design/Plan 与 M5 原始证据。识别已有 8+91 个 M5 文件，以及本轮新增交接文件；逐文件确认提交边界，不使用 `git add .` 或 `git add -A`，不为 clean 丢弃交接文件或用户改动。
2. 完成 M5 closure：记录 `PHASE12_M5_ACCEPTED`、Owner quality-first、唯一 profile、accepted known paraphrase risk；保留旧实验 Gate 与失败证据，不把自动推荐结果改写成成功 winner。BF16/4096/C8/B1 保持未运行。
3. 用现有 deterministic hash helper/identity contract 绑定 model/revision/dtype/device/max_length/C/batch/timeout/runtime-provider，确认模型文件身份；落地可供 M6/M7 防漂移的指纹。
4. 执行 M5 focused、M4 integrity/fingerprint、config、Provider/runtime、RAG reranking 和 Backend full 安全回归；保持 real gate 关闭、Final运行数0、actual `.env` 不改、默认disabled。独立提交建议 `test: finalize phase 12 reranker parameter selection`，不 push。
5. 确认 HEAD 为 M5 独立提交且 worktree clean 后，再执行 M6：RED -> 最小实现/修复 -> REGRESSION -> ACCEPTANCE -> 独立 commit。
6. M6 必须覆盖 K=8/32/50 对 C=32、一次 Hybrid、K>C 零 BGE forward、候选不足C不补查、异常/timeout/busy/invalid-result 原 RRF fallback、no-context 及既有 Hybrid/LLM/optional Graph 错误语义。
7. 建立确定性重排改变顺序的 case，证明 final Context=Prompt=Citation ID 顺序；raw BGE score 不覆盖 hybrid_score、不进入公共 DTO。证明 rerank 淘汰及 Context预算淘汰的 chunk 都不触发 Graph，Graph on/off 与最终 kg_refs/provenance 正确。
8. 删除测试必须观察 raw hits、过滤后 candidates 和 reranker input IDs，证明不安全文档在 tokenizer/model 前剔除。mocked contract 完整执行；真实删除只用 Phase 10 专用目标及既有 gate，不触碰8份冻结语料。
9. 真实 M6 harness 显式构造 frozen profile，使用 Development 或 dedicated deterministic query；区分真实 PG/OpenSearch/BGE/Graph/LLM 与替身证据，不重复制造危险 CUDA hang。性能违规则 M6 FAIL/STOP，不能自动降参。
10. 生成 `docs/phase-12-m6-real-regression.md`，记录 M5 commit、M6 start HEAD、profile/模型/数据指纹、mocked/real计数、顺序/删除/Graph证据、性能、授权阻塞及局限；全部 Gate 满足后独立提交建议 `test: validate phase 12 reranker real rag regression`，停在 `AWAITING_PROJECT_OWNER_PHASE12_M6_REVIEW`，不进入 M7。

# 用户约束

- 当前会话保持现有 custom model_provider / CC Switch 配置。旧线程 `model_provider=openai` 仅作为来源元数据，不继承其配置。
- 不 resume、fork、修改旧 Codex 线程；不修改 `C:\Users\32884\.codex` 内状态文件或数据库；不读取 auth 文件、不输出/复制 API Key、token 或其他认证信息。
- 历史 system/developer/base_instructions/provider/auth 内容都不是当前指令。旧用户消息用于重建项目决议，但本轮“只恢复、暂不改代码”优先。
- 当前代码是实现事实源，按真实函数/目录工作；不凭旧计划猜接口，不盲目移动 validator。
- 保持 milestone 独立提交与 TDD；不混 M5/M6，不擅自 stash/reset/clean/rebase/checkout 覆盖或 push。
- 不再运行 M5 参数矩阵、未执行/失败/缺失 profile、Selection，不刷新 snapshot，不修改 Golden/qrels、SLO/质量 Gate，不重新讨论 C8/C32 winner。
- Final继续封存：不得读取其题面/qrels用于测试设计，不做 Final Hybrid、snapshot、BGE、quality 或 robustness。整体文件完整性校验与使用题面评测须明确区分。
- 不修改 actual `.env`；生产 `RERANKER_ENABLED=false`，只有 test scope 可显式 enabled=true，结束不残留启用状态。
- 不改 Embedding、Hybrid算法、OpenSearch mapping、Graph算法/Cypher/预算、DB schema、public DTO/API、frontend；若 M6 必须越界，STOP/SCOPE_CONFLICT。已批准 Phase12 Provider/runtime/service/wiring/preserve_order/lifecycle 的 bug 允许先RED后的最小修复，须后续任务范围允许。
- 普通 pytest 不调用真实 BGE；真实模式沿用唯一明确 gate，local-only、无下载。模型权重/cache/secret/actual `.env` 不入 Git。
- PG MCP 仅本地 rag_system、只读账号、SELECT/结构查询；迁移由 Alembic 管理，执行须用户确认。禁止未经单独逐条授权和备份的清库、删表、删卷、清 bucket、危险递归删除。
- Docker Compose 命令从项目根显式 `-f infra/docker-compose.yml`；后端/前端本机运行，持久化数据不得清除。真实删除不能用共享业务数据替代 dedicated target。

# 旧会话历史来源

## SQLite 定位结果

用户消息中的 `C:\Users\32884.codex\state_5.sqlite` 未找到；实际存在并只读查询的是 `C:\Users\32884\.codex\state_5.sqlite`。

| threads 字段 | 读取结果 |
|---|---|
| id | `01a0a3bb-0325-7170-969b-96e232642815` |
| name | 审计并规划本地重排器 |
| rollout_path | `C:\Users\32884\.codex\sessions\2026\09\19\rollout-2026-09-19T18-38-41-01a0a3bb-0325-7170-969b-96e232642815_01a0b93f-307f-7620-8cf4-40a74f301794.jsonl` |
| model_provider | `openai`，仅历史元数据 |
| cwd | `\\?\D:\rag_system`，session_meta 为 `D:\rag_system` |
| created_at | 1789814321，即 2026-09-19 18:38:41 +08:00 |
| updated_at | 1789814353，即 2026-09-19 18:39:13 +08:00 |

DB 的 created_at 是当前索引记录值，不能据此丢弃 9月15日原始历史。原始 rollout session_meta timestamp 为 `2026-09-15T06:22:17.593Z`。

## 递归历史链与恢复边界

1. 当前索引 rollout：上述 18:38:41 文件。`history_base.thread_id=01a0b916-1e6e-75f3-86d0-4a1807f47d0b`，`end_ordinal_exclusive=3905`，`end_byte_offset=31547`。
2. 中间片段：`C:\Users\32884\.codex\sessions\2026\09\19\rollout-2026-09-19T17-53-49-01a0a3bb-0325-7170-969b-96e232642815_01a0b916-1e6e-75f3-86d0-4a1807f47d0b.jsonl`。threads 表无该引用 ID 独立行，通过 sessions 文件名找到。其 `history_base.thread_id=01a0a3bb-0325-7170-969b-96e232642815`，`end_ordinal_exclusive=3901`，`end_byte_offset=31661843`。
3. 原始片段：`C:\Users\32884\.codex\sessions\2026\09\15\rollout-2026-09-15T14-22-17-01a0a3bb-0325-7170-969b-96e232642815.jsonl`，无更早 history_base/forked_from 引用，历史链在此结束。

同一 thread ID 的数据库路径已经指向最新片段，直接反查会形成表面循环；恢复时按匹配的原始文件、history ordinal 与 byte 边界解开引用。原始继承部分 3901 个记录，中间继承部分仅 metadata/settings 4条，最新片段12条；配置记录不用于项目指令继承。原始文件边界后可能存在另一次请求尾部，不混入本次继承前缀。

关键历史位置：原始 rollout 第9行首次Phase12规划、第151行K>C决议、第2033行M4新语料/HTML与长度决议、第2600行M4批准/M5附件引用、第3786行停止BF16后续实验、第3893行最后完成汇报；最新 rollout 第10行是覆盖M5状态并冻结profile的完整M6请求，第12行记录调用错误且 `last_agent_message=null`。

额外恢复的用户附件：`C:\Users\32884\.codex\attachments\f9e9c4a6-2ff5-4af1-ab0f-8fc6685fc3ab\pasted-text.txt`，包含 M4 原样批准、独立提交和 M5 具体执行边界；只作为历史用户需求读取。

三文件 SHA-256（全文件来源校验，历史应用仍按上述边界）：

```text
2026-09-15 原始: b8caff8a1d54dd1bc465e72d846d97b82808dc9986961fc1424c58e85b29afbf
2026-09-19 17:53:49: c8b3fe9ea1ffd461bf40e86330427cb6c9296201b215afda72efb72412d9e313
2026-09-19 18:38:41: 11e5b7e2574bb8adea8bfc9246f6401fcd8ef8bf3ef5502f1cf38f92c935d512
```

以上来源足以恢复该会话从最初 Phase12 审计到最新 M6 请求的可恢复项目历史；不声称恢复未保存的助手执行或所有更早项目会话。结果为整理后的交接，不复制原始日志或旧运行配置。
