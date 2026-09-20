# Phase 12：BAAI/bge-reranker-v2-m3 设计与实施计划 — Design

> 当前状态：PHASE12_M5_ACCEPTED；最新Owner frozen profile与收口证据见文末 M5 Owner Closure。前文M0–M5状态是历史记录。

## 文档状态与边界

- 状态：`PHASE12_BGE_PLAN_ACCEPTED`。
- 日期：2026-09-15。
- 审计目录：`D:\rag_system`。
- 审计分支：`phase12-bge-reranker`。
- 审计 HEAD：`9fb2d8cf7be05eca8351076e7eced8af3ba5be12`。
- 本文与 [Implementation Plan](../plans/2026-09-15-phase-12-bge-reranker-implementation-plan.md) 构成负责人批准的 Planning Baseline，在 M0 前独立落盘、独立提交。
- Planning Baseline 不代表 M0 已开始，也不授权下载模型、真实推理、benchmark 或生产实现。
- M0 的 canonical 文档职责仅限真实 BGE probe 后，更新已有文档中的 runtime evidence、benchmark results、SLO owner decisions；初始规划提交不属于 M0 commit。

推荐采用直接 Transformers：`AutoTokenizer` + `AutoModelForSequenceClassification`，复用现有依赖、reranker 配置键和 RAG 接入点。

生产参数与数值性能 SLO 尚未冻结：M0 提供实测数据，负责人冻结 SLO，M5 再选择完整参数组合。本设计中的未来组件、参数与行为均为已批准目标，不代表当前 HEAD 已实现。

## 一、Repository Audit 与 Git Gate

### 1. Git Gate

实际工作目录为 `D:\rag_system`；初始提示词中的 `D:\rag\_system` 不存在。Planning Baseline 的提交前 Gate 使用实际检出目录。

| 检查 | 审计结果 |
|---|---|
| Branch | `phase12-bge-reranker`，当前 Phase 12 专用分支 |
| HEAD | `9fb2d8cf7be05eca8351076e7eced8af3ba5be12` |
| `git status --short` | 空 |
| `git diff --check` | 空，通过 |
| 未忽略的 untracked 文件 | 无 |

审计时最近 15 个提交：

```text
9fb2d8c reranker model to bge-reranker-v2-m3
a659ce2 feat: add graph retrieval feature for RAG system
8f20a06 feat: fuse source-anchored graph evidence into RAG
2c38d33 feat: add fixed read-only graph retrieval
44f11df test: verify KGRef propagation through search metadata
da5dca5 feat: add native markdown ingestion with unified chunk persistence
d9a3d16 feat: add source-aware markdown chunk drafts
183440b feat: add markdown AST and source anchor parsing
a4f54ea docs: freeze phase 11 graph retrieval design
396868c chore: retire basic document parser
9ddfd18 test: harden phase 10 rollout resource lifecycle
5a3cc36 test: isolate phase 10 validation and rollout resources
f65255d test: complete phase 10 storage integration coverage
089340b test: unify phase 10 integration run identity
1bdb2c4 test: align phase 10 harness with client contracts
```

当前 HEAD 没有已跟踪的旧 Phase 12 Ollama Provider、probe 源码、测试源码或方案文件。最新提交主要修改 reranker 模型名称和路径，尚未实现推理。旧对话、旧 Ollama 方案与旧 probe 结论不是实现依据。

审计发现两个被 Git 忽略的旧缓存：

```text
D:\rag_system\backend\tests\__pycache__\test_phase12_probe.cpython-313-pytest-8.4.2.pyc
D:\rag_system\backend\tests\phase12_local\__pycache__\probe.cpython-313.pyc
```

负责人已授权限定范围的缓存清理；Planning Baseline 提交不执行该清理，不读取或沿用缓存中的旧方案。后续执行仍须遵守已确认的清理范围，不删除 `.py` 或 fixtures。

### 2. 规则与 Phase 11 baseline

规划阶段重新读取：

- `D:\rag_system\AGENTS.md`。
- `D:\rag_system\frontend\AGENTS.md`。
- `backend/AGENTS.md` 不存在；后端适用根规则。
- `D:\rag_system\docs\superpowers\specs\2026-09-08-phase-11-graph-retrieval-design.md`。
- `D:\rag_system\docs\superpowers\plans\2026-09-08-phase-11-graph-retrieval-implementation-plan.md`。
- `D:\rag_system\docs\phase-11-m65-graph-evidence.md`。
- Embedding、Hybrid、RAG、Hard Delete 的相关现有资料及对应实现、测试。

当前代码包含 Markdown/source anchor、KGRef 传播、固定只读 Graph Retrieval、RAG Graph Context，以及 M6.5 Graph Evidence 响应能力。

历史测试报告和人工验收记录不代替本次代码审计或后续真实验证；没有据此宣布 Phase 11 当前所有最终验收均通过。

### 3. 独立 baseline blocker：M0 前必须解决

规划审计已复现现有测试失败：

```text
tests/test_graph_context.py::test_graph_budget_setting_is_independent
1 failed
```

- `D:\rag_system\backend\app\core\config.py` 当前默认 `rag_graph_context_max_chars = 50000`。
- `D:\rag_system\backend\tests\test_graph_context.py` 的 existing test expected = `6000`。

这是 Planning 时点的 Phase 11 / Graph baseline mismatch，不属于 Phase 12 reranker。**现已在 M0 前由独立提交 `4fc3412` 解决，50000 生产默认不变。** Phase 12 不得借机修改 Graph budget，也不得新增 skip/xfail 绕过问题；以上保留原审计证据，本次 M0 未再次修改该测试。

## 二、CURRENT_RERANKER_PLACEHOLDER_AUDIT

完整审计位置：

- `D:\rag_system\backend\.env.example`。
- `D:\rag_system\backend\app\core\config.py`。
- `D:\rag_system\backend\app\services\rag.py`。
- 根 `D:\rag_system\.env.example` 也存在同组配置。

已对仓库搜索 rerank、reranker、RERANK、RERANKER、top_k、candidate、candidate_limit；不另建已有配置的同义键。

| Symbol | 当前默认值 | 当前 consumer / 是否运行 | Placeholder 判定 | Phase 12 处理 |
|---|---|---|---|---|
| `RERANKER_ENABLED` | `false` | `optional_rerank_chunks()` 实际读取；false 原样返回，true 抛 `RAG_CONFIG_INVALID`，HTTP 400 | 开关接入真实，推理未实现 | 保留原键；替换 enabled 分支为 fail-open 重排 |
| `RERANKER_PROVIDER` | `local_qwen3` | 无实际 Provider consumer | 是；名称与当前 BGE 目标不符 | 保留键，目标值改为 `local_transformers` |
| `RERANKER_MODEL` | `bge-reranker-v2-m3` | 无加载 consumer | 是 | 保留键，规范标识为 `BAAI/bge-reranker-v2-m3` |
| `RERANKER_MODEL_PATH` | `D:/rag_system/models/bge-reranker-v2-m3` | 无加载 consumer | 是 | 保留原键与本地路径语义 |
| `RERANKER_TOP_K` | `8` | 只有 Settings 正整数校验，没有选取候选或最终结果的 consumer | 历史 placeholder | deprecated；运行时 ignored，不参与 K/C 决策 |
| `optional_rerank_chunks()` | disabled 返回原结果 | 每次正常 RAG 检索后调用 | 真实接入钩子，内部仍为 placeholder | 复用，不另建同义 RAG 接入链 |

相关 limit 审计：

| Symbol | 当前事实 |
|---|---|
| `RAG_TOP_K=8` | 服务层 `limit=None` 时使用；HTTP 请求模型本身默认 `limit=8` |
| `HYBRID_KEYWORD_TOP_K=50` | 实际控制 OpenSearch lexical 请求大小 |
| `HYBRID_VECTOR_TOP_K=50` | 实际控制 OpenSearch vector 请求大小与 k |
| `HYBRID_FINAL_LIMIT=10` | 当前没有直接控制 service 默认参数；实际函数与 Search 请求默认值各自为 10 |
| `RERANKER_CANDIDATE_LIMIT` | 当前不存在，可以新增，没有同义现存配置 |

Planning Baseline 不修改这些配置或现有验证逻辑。

## 三、CURRENT_RETRIEVAL_PIPELINE

### 当前真实调用链

```text
POST /api/v1/rag/ask
  → rag_ask_endpoint()
  → answer_question()
  → normalize question / limit
  → retrieve_chunks(K)
  → hybrid_search_chunks()
      → Qwen3 query embedding
      → OpenSearch lexical search
      → OpenSearch vector search
      → PostgreSQL Document.deletion_status 核验
      → 按 chunk_id 合并
      → weighted RRF
      → 排序并截取 K
  → optional_rerank_chunks()
      → disabled: 原样返回
      → enabled: 当前报配置错误
  → build_rag_context()
      → 再按 hybrid_score 排序
      → Text 字符预算
      → 分配 citation_id
  → final Text Context 的 kg_refs
  → optional Graph Retrieval / Graph Context
  → Prompt
  → LLM
  → 从同一个 Text Context 构建 Citation response
```

| 问题 | 当前答案 |
|---|---|
| 1. Query 入口 | `D:\rag_system\backend\app\api\v1\rag.py` 的 `rag_ask_endpoint()`；服务层再次去除首尾空白并验证 |
| 2. lexical 在哪里 | `hybrid_search_chunks()` 构造关键词查询，由 OpenSearch 执行 |
| 3. vector 在哪里 | Qwen3 先编码 query，随后 OpenSearch 执行 k-NN |
| 4. 两路是否都来自 OpenSearch | RAG 使用的 Hybrid 两路均来自同一 OpenSearch alias |
| 5. deletion filter 在哪里 | `_filter_normal_document_hits()`；在两路命中解析之后、RRF 之前 |
| 6. RRF 在哪里 | `fuse_hybrid_results()` / `calculate_weighted_rrf()` |
| 7. Hybrid 何时截 limit | 删除过滤、合并、RRF 和排序之后 |
| 8. public limit 语义 | 返回候选数量上限，不是保证数量；RAG HTTP 默认 8，Hybrid 有效上限 50 |
| 9. Context 是否重新排序 | 是，当前按 `hybrid_score DESC` 再排序 |
| 10. Citation ID 在哪里生成 | `build_rag_context()` 在字符预算筛选过程中，以保留顺序从 1 编号 |
| 11. Graph 何时触发 | Text Context 完成后，仅从其保留 chunks 提取 KGRefs |
| 12. LLM 何时调用 | Graph Context 和 Prompt 完成后；`no_context` 分支不调用 LLM |

另有独立的 PostgreSQL/pgvector vector-search 服务，但它不是上述 RAG Hybrid 的 vector 路径。

## 四、HYBRID_SEARCH_CONTRACT_AUDIT

依据 `D:\rag_system\backend\app\services\hybrid_search.py`、Search schema、OpenSearch client/index schema、索引服务、Embedding 与 Hybrid 测试。

| Contract | 当前实现 |
|---|---|
| lexical top-k | 默认 50 |
| lexical 查询 | `multi_match`，字段 `content^3`、`content_max^2`、`content_smart`，使用 `ik_smart`；没有额外自定义 BM25 相似度公式 |
| vector top-k | 默认 50，OpenSearch k-NN 的 size/k 由配置控制 |
| 两路公共过滤 | embedded 状态、Embedding model、1024 维，以及可选 document_id |
| 删除过滤 | 两路结果统一批量查询 PostgreSQL，只保留存在且 `deletion_status="normal"` 的 Document |
| 异常 document_id | 缺失、非法 UUID 或 Document 不存在均被排除 |
| RRF | `w_keyword/(rrf_k+keyword_rank) + w_vector/(rrf_k+vector_rank)`；不存在的通道贡献 0 |
| 默认参数 | 权重各 0.5，`rrf_k=60`；rank 从 1 开始 |
| 删除后 rank | 对过滤后的通道列表重新编号 |
| 去重 | 按 `chunk_id` 合并两路命中 |
| source 优先级 | 同一 chunk 两路出现时，优先已有的非空 lexical source |
| 同通道重复 | 当前循环会覆盖该通道已有 rank/score；Phase 12 不顺带改变 |
| 排序 | hybrid score DESC → keyword rank ASC → vector rank ASC |
| 完全相同排序键 | Python 稳定排序保持合并字典的插入顺序；没有显式 chunk_id 最终 tie-break |
| 上游稳定性 | OpenSearch 请求没有额外保证同分命中的稳定排序 |
| metadata | `source_metadata` 保留；KGRefs 在其中传播，不供搜索索引查询 |
| 最终 limit | RRF 排序后 `[:limit]` |
| `total` | 返回 items 数量，不是全库命中数或截取前候选总数 |
| 参数边界 | limit 1–50；两路 top-k 必须足够覆盖请求 limit |

### 结果结构

```text
HybridSearchResult
  query
  limit
  total
  items

HybridSearchItem
  chunk_id
  document_id
  original_filename
  chunk_index
  content
  source_metadata
  retrieval_source
  keyword_score
  vector_score
  keyword_rank
  vector_rank
  hybrid_score
  matched_keywords
  embedding_model
  embedding_dim
```

没有 `reranker_score`，也没有可靠的顶层 `section_title`。

虽然索引 payload 包含 section title，但当前 Hybrid 返回字段通路不能保证提供它。因此第一版保持 query + chunk.content，不扩充 metadata 通路。

### 删除隔离的精确边界

删除过滤发生在 RRF 和候选截取前，所以已被核验为非 normal 的内容不会进入 reranker，也不会占最终 C 的位置。

陈旧命中仍可能占用上游 OpenSearch top-50 返回位置；当前没有补查机制，因此有效候选可能少于 C。Phase 12 保持这一行为，不补查、不补齐、不重复 Hybrid。

当前删除状态核验是读取时的状态边界，没有持锁覆盖后续 GPU、Graph 或 LLM 时段。

## 五、RERANK_INSERTION_POINT_AUDIT

### 冻结架构与职责

```text
Query
  → BM25 + Vector
  → deletion-safe hits
  → RRF
  → candidate pool
  → optional BGE Cross-Encoder
  → final Text candidates
  → Context budget + Citation IDs
  → final Text Context 的 kg_refs
  → optional Graph
  → LLM
```

- Hybrid Search = Recall stage。
- BGE Reranker = Precision stage。
- Reranker 只重排 Hybrid 已提供的文本候选，不全库搜索，不替代 BM25、Embedding 或 RRF candidate generation。

### Candidate / public K contract

`K` 是 public final limit。

`C = RERANKER_CANDIDATE_LIMIT`，是经过 benchmark 验证的最大生产 rerank capacity。

| 条件 | 唯一一次 Hybrid 调用 | 后续处理 |
|---|---|---|
| disabled | `Hybrid(K)` | 原 RRF Top K |
| enabled，K <= C | `Hybrid(C)` | Reranker → Top K |
| enabled，K > C | `Hybrid(K)` | 跳过 Reranker，原 RRF Top K |
| reranker 配置未准备好 | `Hybrid(K)` | fail-open，保留原检索错误 contract |
| 已取得 C，但 reranker 失败或 busy | 不再调用 Hybrid | 原始 C candidates `[:K]` |

K > C 必须记录安全内部原因：

```text
public_limit_exceeds_reranker_capacity
```

禁止 `C_effective=max(C,K)`。未来要对 K=50 重排，必须先验证对应容量，再提升 C。生产 C 还必须处于当前 Hybrid 支持范围内，并不超过两路 top-k。

公共请求本身非法时，仍由现有验证链处理，不能被 reranker fallback 掩盖。一个 RAG 请求最多一次 Hybrid：no second Hybrid。

### Context 顺序修正

当前存在确定的覆盖风险：

```text
rerank order → build_rag_context() → hybrid_score 重新排序
```

给现有接口增加仅内部使用、默认关闭的控制：

```text
build_rag_context(..., *, preserve_order=False)
```

- rerank 成功：`preserve_order=True`。
- disabled、skip、fallback：保持默认行为。
- 不用 raw logit 覆盖 `hybrid_score`。

RAG response 的 `retrieval` 仍使用现有结构，封装最终 Top K，`limit=K`、`total=len(items)`，不泄露内部 C 或 diagnostics。Text 字符预算仍可能使实际 Context 少于 K。

### Citation 与 Graph

Citation ID 在最终文本顺序及 Context 预算确定后生成。Prompt 与 Citation response 使用同一个 `RagContext`，不维护第二份编号或顺序。

Graph 继续从最终、经过字符预算的 Text Context 提取 KGRefs：

- 被 reranker 淘汰的 chunk 不触发 Graph。
- 被 Context budget 淘汰的 chunk 不触发 Graph。
- 不改变 Graph Cypher、Repository、预算、Evidence DTO 或前端。
- 不新增第二次 Graph 查询。

## 六、LOCAL_RERANKER_DEPENDENCY_AUDIT

依据 `D:\rag_system\backend\pyproject.toml` 与规划审计时 backend `.venv` 的实际 metadata、导入检查。

| 依赖 | 仓库声明 | 审计时安装 |
|---|---|---|
| Python | `>=3.11` | 3.13.9 |
| torch | `>=2.2,<3.0` | `2.11.0+cu128` |
| transformers | `>=4.51.0,<5.0` | `4.57.6` |
| sentence-transformers | `>=2.7.0,<6.0` | `5.6.0` |
| FlagEmbedding | 未声明 | 未安装 |
| tokenizers | 间接依赖 | `0.22.2` |
| safetensors | 间接依赖 | `0.8.0` |
| huggingface-hub | 间接依赖 | `0.36.2` |
| accelerate | 未声明 | 未安装 |

规划审计只执行了依赖 metadata 查询、模块导入和 `pip check`：

- `AutoTokenizer`、`AutoModelForSequenceClassification` 可导入。
- `CrossEncoder` 可导入。
- `pip check` 返回 `No broken requirements found`。
- 没有调用 `from_pretrained()` 或模型 forward。

### RERANKER_RUNTIME_OPTION_AUDIT

| 维度 | A. 直接 Transformers | B. CrossEncoder | C. FlagReranker |
|---|---|---|---|
| 当前已有依赖 | 是 | 是 | 否 |
| 新增依赖成本 | 无需新增运行依赖 | 无需新增运行依赖 | 会增加 FlagEmbedding 及其依赖集合 |
| score 透明度 | 直接读取 logits | 必须显式使用 Identity，避开默认激活 | `normalize=False` 可取原始分值，但有更多封装行为 |
| batch 控制 | 完全显式 | `predict(batch_size=...)` | 支持 batch，但当前实现有自动缩小 batch 的重试 |
| tokenizer 控制 | 直接控制 | 通过封装参数与 processor 控制 | 内部预处理较多 |
| truncation 控制 | 明确使用 `only_second` | 可配置，但需覆盖默认路径并验证版本行为 | 当前实现会先截 query，不符合本版完整保留 query 的要求 |
| device 控制 | 显式单设备 | 显式 device | 支持 device，但封装层较多 |
| FP16/BF16 | 显式控制并逐项实测 | 通过模型参数控制 | FP16 方便；encoder-only BF16 路径需要额外核验 |
| 生命周期 | 可直接实现项目所需 lazy singleton | 需外包 lazy 生命周期 | 构造时加载，仍需外包 lazy 生命周期 |
| 测试便利性 | loader/tokenizer/forward 注入清晰 | 需同时约束封装层行为 | 隐含截断、batch 重试增加验证范围 |
| GPU memory observability | 可直接围绕加载与 forward 测量 | 同样可用 PyTorch 测量 | 可测，但内部重试影响解释 |

CrossEncoder 的默认 activation 和接口依据其[官方文档](https://www.sbert.net/docs/package_reference/cross_encoder/model.html)及本机安装源码。FlagEmbedding 的截断、batch 重试与新增依赖依据其[官方实现](https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/FlagEmbedding/inference/reranker/encoder_only/base.py)和[依赖声明](https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/setup.py)。

推荐 A：直接 Transformers。当前依赖已满足基础条件；评分、截断、batch、dtype、加载和错误处理都能明确落在本项目代码中。该推荐不包含尚未实测的速度或显存承诺。

### 官方模型 contract

官方 normal-reranker 示例使用 `AutoTokenizer`、`AutoModelForSequenceClassification`、`(query, passage)` batch、`model.eval()`、无梯度 forward 和 `outputs.logits`。

分值越高表示相关性越高，可以直接用于排序。Sigmoid 是可选映射，本版不启用，也不将其解释为严格概率。[官方模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3)

模型配置对应 `XLMRobertaForSequenceClassification`，单标量分类输出；tokenizer 声明 `model_max_length=8192`。这不构成采用 8192 生产输入长度的依据。[模型配置](https://huggingface.co/BAAI/bge-reranker-v2-m3/raw/main/config.json)、[Tokenizer 配置](https://huggingface.co/BAAI/bge-reranker-v2-m3/blob/main/tokenizer_config.json)

## 七、CURRENT_LOCAL_INFERENCE_ENVIRONMENT

以下为规划审计的硬件快照，M0 必须重新读取并记录，不能直接当成 benchmark 结果。

| 项目 | 审计实读结果 |
|---|---|
| Python executable | `D:\rag_system\backend\.venv\Scripts\python.exe` |
| Python | 3.13.9，64-bit |
| PyTorch | `2.11.0+cu128` |
| PyTorch CUDA build | 12.8 |
| `torch.cuda.is_available()` | True |
| CUDA device count | 1 |
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU |
| Compute capability | 12.0 |
| 驱动 | 592.01 |
| 总显存 | 约 8151 MiB |
| 可用显存快照 | 约 6016 MiB，随其他进程变化 |
| BF16 支持查询 | True；尚未进行该模型 BF16 推理验证 |
| 本地 Embedding 目录 | Qwen3-Embedding-0.6B 已存在 |
| 本地 BGE 目录 | 审计时尚不存在 |

规划阶段只检查环境与 CUDA 元数据，没有分配推理输入、加载 BGE 或执行 benchmark。

### 现有 Provider 生命周期

- `LocalQwen3EmbeddingProvider`：实例内 lazy load；`get_embedding_provider(settings)` 当前会创建新实例，不是进程 singleton。
- LLM Provider：已有加锁的进程缓存及关闭接口。
- Graph：应用 lifespan 管理 repository/service；连接仍按现有逻辑建立。
- 当前没有统一 GPU 调度器。

Reranker 采用 LLM 的进程缓存管理风格，加上 Embedding 的 loader 注入方式。不修改 Embedding factory 或生命周期。

M0 必须既测量持有已加载 Embedding 实例时的共存，也记录当前实际调用方式下的检索延迟；不能把注入复用实例的实验结果当成默认 RAG 生命周期表现。

## 八、Provider、生命周期与失败语义

### 1. 内部组件

计划新增：

| 文件 | 职责 |
|---|---|
| `D:\rag_system\backend\app\retrieval\reranker.py` | 内部不可变请求、候选、分数类型与 Provider protocol |
| `D:\rag_system\backend\app\retrieval\local_cross_encoder.py` | 本地 Transformers 加载、成对 tokenizer batch、forward、raw logits |
| `D:\rag_system\backend\app\services\reranking.py` | singleton、单 worker、busy/timeout、完整输出验证、排序、内部诊断 |

复用 `optional_rerank_chunks()` 作为 RAG 接入钩子。

```text
RerankCandidate
  original_rank
  chunk_id
  content

RerankRequest
  request_id
  query
  candidates

RerankScore
  original_rank
  chunk_id
  raw_score

RerankScores
  request_id
  scores
```

模型只接收 query 与 content。ID、rank、request_id 仅用于运行时关联与验证，不进入 tokenizer。结果对象包含最终选中 items、`applied` 和内部 diagnostics；不新增公共响应字段。

### 2. 配置规则

保留现有五个配置键，避免创建同义配置。新增必要内部配置：

```text
RERANKER_CANDIDATE_LIMIT
RERANKER_DEVICE
RERANKER_DTYPE
RERANKER_BATCH_SIZE
RERANKER_MAX_LENGTH
RERANKER_TIMEOUT_SECONDS
```

- `RERANKER_ENABLED=false` 保持默认。
- C、dtype、batch、max_length、timeout 不在 Planning 阶段拍定生产值。
- M5 前，实验参数必须显式提供。
- 生产启用必须使用整体经过验证的参数组合。
- 配置未准备好时 fail-open，不触发模型加载。
- `RERANKER_TOP_K` 保留兼容解析，标记 deprecated，排序与截取不读取它。
- 不新增第二套模型路径或缓存配置。
- 不自动切 CPU、dtype 或 batch 来掩盖失败。

### 3. Lazy loading 与模型下载

单进程只允许一个 reranker runtime、一个模型实例和一个推理 worker。

- Backend startup 不加载模型，不 warmup。
- disabled、K > C、无候选时不创建推理任务。
- 第一次符合条件的请求才加载模型，成功后复用。
- 模型从现有 `RERANKER_MODEL_PATH` 加载，`local_files_only=True`，不执行远程自定义代码。
- v1 不提供自动 startup warmup；冷加载超时允许当前请求 fallback，加载在后台自然完成。
- 配置变更通过重启应用生效，不在已有 worker 工作时创建另一套实例。

Planning 阶段不下载模型。M0 真实执行前必须取得负责人对下载 `BAAI/bge-reranker-v2-m3` 和本地推理的明确授权；使用项目已有模型路径语义或明确的 Hugging Face cache 策略，记录 revision 与内容指纹。下载前精确忽略 BGE 权重目录，模型权重不得提交 Git。

### 4. Batch、文本与 token 预算

真正的 Cross-Encoder batch：

```text
[(query, passage_1), ..., (query, passage_N)]
  → tokenizer batch
  → forward
  → N logits
```

- M0 比较 C=8/16/32、整批与 micro-batch，例如 32 candidates → batch 8 × 4。
- 比较 max_length=512/1024。
- 使用真实 tokenizer token 数及 pair special-token 开销，不使用 `len(text)/4` 作为安全上限。
- 使用 `truncation="only_second"`，完整保留经过现有规范化的 query。[Transformers 官方截断语义](https://huggingface.co/docs/transformers/v4.57.1/en/pad_truncation)
- query 自身超过预算，或无法为 passage 留出有效空间：整次 fallback，原因 `query_too_long`。
- 只截 reranker 临时输入，不修改原 chunk、数据库内容或之后 Context 使用的正文。
- 长 Markdown 表格与长段落必须进入 M0。
- 不拼 section title、KGRefs、Graph Context、Embedding、ID 或 RRF score。

### 5. Score contract

每个 micro-batch 必须输出符合单标量模型 contract 的 logits；不能随意 flatten 多分类结果以凑数量。

整个请求只有同时满足以下条件才应用：

1. request identity 匹配。
2. 候选身份完整、唯一，与原候选一一对应。
3. N 个候选恰好得到 N 个分数。
4. 每个分数为有限数值。
5. 所有 micro-batch 均完成且请求未超时。

```text
raw_score DESC
original_hybrid_rank ASC
chunk_id ASC
```

没有 threshold、RRF fusion、人工校准或 LLM score。FP32/FP16/BF16 的分差、排序一致性和稳定性由 M0 实测；不要求跨 dtype 分数逐 bit 相等。第一版不引入 INT8、4-bit、GPTQ、AWQ；若合理精度均无法满足硬件约束，停止并独立评审。

### 6. 单 worker 与 bounded wait

- 一个进程最多一个 reranker CUDA forward。
- 入场操作不排队；worker busy 时立即返回 RRF fallback。
- 请求等待有上限，覆盖 lazy load、tokenization 和 inference。
- 超时后当前请求立即选择 RRF Top K。
- 运行中的 CUDA forward 不强制取消；完成后丢弃晚到结果。
- 超时不释放 busy 标志；worker 真正结束后才允许下一任务。
- micro-batch 之间检查取消/过期状态，不继续启动过期请求的下一批。
- 后台任务只持有不可变输入，不持有 DB session，也不修改 Context、Citation 或响应对象。

Python Future 的 timeout 不会停止已运行任务，因此不能用每请求 `with ThreadPoolExecutor(...)` 包装并声称获得硬超时。[Python 官方文档](https://docs.python.org/3.13/library/concurrent.futures.html)

关闭时停止入场，正在运行的任务自然结束后释放模型。线程方案不承诺强杀卡死 CUDA；如实测出现无法恢复的阻塞，停止并单独评审进程隔离方案。

### 7. Fail-open / all-or-nothing

| 情况 | 结果 |
|---|---|
| disabled / capacity bypass | 原 RRF Top K |
| worker busy | 原候选 `[:K]` |
| model load failed | 原候选 `[:K]` |
| CUDA OOM | 原候选 `[:K]` |
| timeout | 原候选 `[:K]`，晚到结果作废 |
| score 数量、身份、shape 错误 | 原候选 `[:K]` |
| NaN / Inf / inference exception | 原候选 `[:K]` |
| 任一 micro-batch 失败 | 丢弃整次评分 |

不在失败请求内重新加载、自动缩 batch、使用部分结果或再次 Hybrid。Reranker 失败不得单独导致 RAG HTTP 500。

普通加载失败/OOM 在清理本次临时资源后允许后续请求重新尝试；CUDA 状态无法安全恢复时标记 unavailable，等待应用重启。不得卸载 Embedding 来恢复 reranker。

Hybrid 与 LLM 自身的错误继续使用现有 contract。

### 8. Observability

运行日志允许：

```text
reranker_enabled
model
candidate_count
reranked_count
latency_ms
fallback
fallback_reason
device
batch_size
```

成功时 `reranked_count` 表示通过验证的评分数量；fallback 时为 0。离线报告可记录 query ID、chunk ID、original rank、reranked rank、raw score。

不得在日志记录完整 query、chunk 正文、模型权重路径、credential 或可能包含这些内容的异常字符串。

## 九、Evaluation 与参数选择

### 1. 数据隔离

| 集合 | 用途 |
|---|---|
| Development set | 开发、debug、初步参数探索 |
| Selection validation set | M5 参数选择，应用冻结的质量 Gate 与性能 SLO |
| Final held-out set | M7 最终一次验收；参数冻结后才使用 |

按文档和主题组划分，防止近重复问题跨 selection/final 泄漏。

八类问题：

1. 术语与定义。
2. 同义表达、自然语言改写。
3. 标准编号、牌号等精确标识。
4. 数值、范围与单位约束。
5. 多条件工艺问题。
6. 长段落中的相关证据。
7. 长结构化表格（Structured Long Table）：Markdown Table 与 MinerU HTML Table 均合法，题目必须真正依赖表头/行列/单元格/单位/范围/条件，记录 table_format 与证据定位。
8. 易混淆概念与困难负例。

每个集合每类至少 5 条人工标注问题，形成最低覆盖要求；语料不足以满足独立分组时，M4 停止并提交负责人调整评测设计。无答案查询另列健壮性集合，不混入有相关答案的质量指标分母。

人工 gold 使用相关等级 0/1/2，记录文档、chunk 身份、内容指纹及判定依据，由负责人复核。

### 2. 同候选池比较

每个 query、每个 C：

```text
一次 Hybrid(C)
  ├─ 原始 RRF 排序
  └─ 同一候选快照 → BGE 排序
```

不为 baseline 与 variant 分别取得内容不同的候选池。

- HitRate@1、HitRate@3。
- Recall@8：以冻结人工 gold 的相关 chunks 为分母。
- MRR@8。
- nDCG@8，gain 使用 `2^grade-1`。
- 候选池覆盖率/可达到的 Recall 上限。
- latency、VRAM、fallback。
- 总体与八类 per-category metrics。
- structured_table overall / markdown / html 与 long_paragraph 独立 slice；无该格式样本时count=0、metrics=null，不拆同源文档凑覆盖。

gold 中未进入 C 的相关 chunk 仍计入召回分母。不能只标注进入当前候选池的结果，再将其解释为全库召回。不用 RAG 答案质量替代 ranking 评测。

### 3. 质量 Gate

M5 在 Selection validation 上执行；M7 在 Final held-out 上执行。

Primary quality gates：

```text
nDCG@8 > 同候选池 Hybrid baseline
MRR@8 > 同候选池 Hybrid baseline
```

Non-regression gates：

```text
HitRate@1 >= baseline
HitRate@3 >= baseline
Recall@8 >= baseline
```

所有类别均输出独立结果。任何类别指标下降都标记审查，明显退化必须由负责人明确处理，不能用总体均值覆盖。

### 4. 性能 SLO

Planning 阶段不设毫秒值。M0 必须报告：

- cold model load time。
- warm rerank p50/p95。
- C=8/16/32 的 latency。
- max_length=512/1024 的 latency。
- 可行 dtype、batch/micro-batch 表现。
- retrieval baseline 与 reranker-on p50/p95。
- incremental retrieval latency。
- busy fallback latency。
- timeout fallback latency。
- reranker-only GPU peak。
- embedding-only GPU peak。
- embedding + reranker coexist GPU peak。
- OOM 与 fallback。

负责人在 M0 Review 后冻结：

1. warm rerank p95 SLO。
2. retrieval p95 incremental SLO。
3. fallback tail latency SLO。
4. GPU/显存安全边界。

只有负责人冻结数值 SLO 后，M5 才能选参。M5 不得根据 benchmark 结果重新定义通过线。

### 5. M5 选择规则与 M7 最终验收

只从同时通过质量、类别审查和性能 Gate 的组合中选择。

优先剔除被其他组合在延迟与共存显存上同时优于的方案；剩余组合依次优先：

1. 更低的 incremental retrieval p95。
2. 更低的共存显存峰值。
3. 更低的 warm rerank p95。
4. 成本相同时更小的 C。

冻结完整参数包：模型 revision、runtime 版本、device、dtype、C、batch、max_length、timeout、数据及配置指纹。

M7 使用该参数包只运行一次 Final held-out。失败后保持 `RERANKER_ENABLED=false`，进入新设计/参数评审周期；不能针对原 final set 调参重测。任一质量或性能 Gate 未通过，不得宣布 Phase 12 完成或默认启用 reranker。

M5 当前候选范围由2026-09-16 Owner Decision B修订：dtype=[FP16,BF16]，FP32仅numerical reference；max_length=[1024,2048,4096]，C=[8,16,32]。8192为 `EXCLUDED_FROM_PHASE12_V1`，tokenizer历史声明上限不是候选值。至少六个dtype×length组合先做hardware/SLO feasibility screening，再进入Selection质量比较。M4只做token长度与预期truncation审计，不选最终profile。

## 十、Non-goals 与评审确认

- 不修改 Qwen3-Embedding-0.6B、1024 维向量或 Embedding 生命周期。
- 不修改 OpenSearch mapping、DB schema、migrations。
- 不修改 Search API、RAG response schema、Citation API、Graph Evidence API。
- 不优化 Graph，不修改 Cypher、Repository、预算或前端展示。
- 不实现全库 reranker 搜索、score threshold、RRF score fusion、LLM score 或量化。
- 不执行第二次 Hybrid，不把部分评分用于结果。
- 不通过卸载 Embedding 使 BGE 共存测试通过。

| Self-review | 结果 |
|---|---|
| 模型明确为 BAAI/bge-reranker-v2-m3 | 是 |
| runtime 来自实际依赖审计和官方 contract | 是，推荐直接 Transformers |
| public K 与 validated capacity C 分离 | 是 |
| K > C 请求级 RRF fallback | 是，原因字符串已冻结 |
| deletion filter、RRF 均在 reranker 前 | 是 |
| Context 在 reranker 后并保留成功顺序 | 是 |
| Citation 使用最终 Context 顺序 | 是 |
| Graph 只由最终 Text Context 触发 | 是 |
| fail-open、all-or-nothing、no second Hybrid | 是 |
| Embedding、1024 维、mapping、DB schema、public API 不变 | 是 |
| M0 包含真实 GPU/runtime/coexist probe | 实测见第十一节，含 FP32 限制，待 Owner Review |
| 人工 gold、三集合隔离、分类指标 | 是 |
| M0 后负责人冻结 SLO，M5 不改通过线 | 是 |
| M7 final 只验收一次，失败保持关闭 | 是 |
| M0—M7 各有独立 commit 边界 | 见 canonical Implementation Plan |
| 未引入旧 Ollama reranker 设计 | 是 |
| Graph baseline mismatch 在 M0 前独立解决 | 已由 `4fc3412` 独立解决，M0 未改 Graph budget |

## 十一、M0 后证据更新位置

### 1. 2026-09-15 实测依据

- M0 branch/HEAD：`phase12-bge-reranker` / `4fc3412f7a20efbac21032b44a8964debed12fd2`；开始前 clean。
- Planning Baseline 已独立提交：`559087f42816859a2c255750d1b6613bc228d845`。
- Graph baseline 已由独立提交 `4fc3412` 对齐测试至当前默认 50000；M0 未修改 Graph budget、算法或测试。
- Baseline：1335 passed / 28 deselected / 0 FAIL；最终 full：1386 passed / 28 deselected / 0 FAIL。Probe unit 51 passed，Search/Embedding 171 passed；各普通回归与真实推理分开计数。
- 完整方法、限制、原始数据：[M0 Probe Report](../../phase-12-m0-bge-probe.md)。

### 2. Runtime / model evidence

| 项目 | 本次证据 |
|---|---|
| 模型 | BAAI/bge-reranker-v2-m3 |
| 固定 revision | `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |
| 文件身份 | [model-identity.json](../../phase-12-m0-results/model-identity.json)，六文件大小/hash；每进程加载前复核 |
| Runtime | Python 3.13.9；torch 2.11.0+cu128；Transformers 4.57.6；sentence-transformers 5.6.0；CUDA 12.8 |
| GPU | RTX 5060 Laptop，capability 12.0，BF16 supported=True，CUDA total 8150.56 MiB |
| Direct Transformers | 加载成功；local_files_only=True，trust_remote_code=False；eval + inference_mode；无需新依赖 |
| Tokenizer / output | XLMRobertaTokenizerFast，8192 声明上限、pair 开销 4；实际 logits 严格 [N,1]，raw score DESC |
| 输入长度 | 仅测 512/1024；真实 token IDs 校验 query 完整，仅临时截 passage；长段落/表格均有有限分数 |

当前实际下载路径为 `D:\rag_system\models\bge-reranker-v2-m3`，只新增该目录的精确 ignore；没有权重进入 Git。仅下载这一模型，未安装依赖。

### 3. Benchmark results 与限制

- Reranker-only：三种 dtype × 两长度 × 六个 C/batch 组合全部完成，共 36 配置。
- FP16/BF16 共存：24 配置全部完成；完整配置的最慢 warm p95 分别 1163.39 / 1098.38 ms，allocated peak 均 4133.37 MiB、reserved peak 4538 MiB、设备边界快照峰值 5685.56 MiB。
- FP32 共存：7 个不同配置完成（补测产生 2 个重复配置运行）；C=32/L=512/batch=32 超过 300 s 仍无完整配置结果，在显存压力下手动中止；4 个较大 batch 配置未执行。没有把未知部分样本用于 p50/p95，没有将中止计为通过。
- FP32 batch=8 共存补测覆盖 C8/16/32、L512/1024；C=32/L=1024 p95=3916.91 ms，但 device 快照仍曾 free=0，不据此承诺安全。
- 总计 67 个不同完整配置、69 次完整配置运行、1380 个 warm 样本；CUDA OOM exception=0，不等于所有配置都安全。详细 allocated/reserved/device 数据及中止记录在报告中。
- 每完整配置 warm n=20；同配置最大重复 drift=0，排序稳定。FP16/BF16 相对 FP32 最大分差约 0.00703/0.07966；FP16 独立运行 Top8 为 12/12 一致，BF16 为 9/12。Runtime sanity 不是 M4 gold，不据此冻结 dtype。
- Embedding-only allocated peak=2289.28 MiB，warm query p50/p95=40.83/47.52 ms。共存始终持有一个实际 provider 实例，不卸载 Embedding。真实 factory 每次创建新实例，因此不能外推为生产 RAG 并发显存结论。
- Cold 是新进程模型加载，不含 import，文件 hash 读取已预热 OS cache；设备峰值是边界采样，非连续监控。温度/其他 GPU workload 未隔离。

负责人本轮明确收窄 M0 为候选模拟：**没有运行 Hybrid/RAG、没有实现 worker/admission/Future timeout/late-result**。因此本文早先列出的真实 retrieval incremental、busy、timeout latency 本轮均未测，留待相应实现里验证；不把 forward 数据当作这些路径的实测。

### 4. SLO owner decisions：仍待确认

报告提出的待审预算为：warm p95 ≤2000 ms；retrieval incremental ≤2200 ms（200 ms 编排余量是假设）；busy ≤50 ms（未测工程目标）；候选 timeout=5000 ms、返回≤deadline+100 ms（未实现/未测）；device 采样使用量≤6500 MiB（仍需更完整资源测量）。**均未冻结，不是已通过的生产 SLO。**

M1 优先工程验证 FP16，BF16 对照、FP32 数值参考；C8/16/32、L512/1024、micro-batch8/16 为实验范围。生产 C/dtype/batch/max_length 仍由后续独立人工 gold 与 M5 Gate 决定。K>C RRF fallback、no second Hybrid、fail-open 及 Delete/Context/Citation/Graph 边界不变。

未出现“所有合理共存配置均不可运行”或必须引入新依赖/量化/生产修改的 scope conflict；FP32 压力边界及未执行项需负责人明确 Review。M1 未授权。

M0 只更新已存在 canonical docs 的证据和待决事项；不首次创建 Design/Plan，不混入 Planning Baseline commit。本轮未 commit/push。

当前状态：`AWAITING_PROJECT_OWNER_PHASE12_M0_REVIEW`。

## 十二、M3 真实 production-provider evidence 与当前 Owner 决议

### 1. 当前基线与已批准约束

Planning `559087f`、Graph baseline `4fc3412`、M0 `06aa95f`、M1 `0b327c0`、M2 `40c54e0` 均为独立提交。M3 从 `phase12-bge-reranker` / `40c54e0ae4cd85693dd56fdbf4f6285e7b5e9a28` 的 clean 工作区开始。

负责人已接受 M0，并冻结 **FP16 主路径**；BF16 保留后续质量对照，FP32 不进入当前生产候选优化。已冻结 SLO：warm p95≤2000 ms、retrieval incremental p95≤2200 ms、busy p95≤50 ms、timeout 返回≤deadline+100 ms、GPU coexist device peak≤6500 MiB。前文“尚待 Owner 确认”的措辞是 M0 报告时点记录，已被本节的明确决议更新。

**C、batch、max_length 和最终 timeout 仍未冻结为生产默认；M5 才选择完整 profile。** M1 当前运行 contract 为 RerankResult；生产 model load failure latch 至新的应用生命周期，不执行每请求重试。M2 已接入 close+clear cache、K/C 分支与 preserve_order；实际源码为准。

### 2. M3 实测摘要

[M3 完整报告](../../phase-12-m3-local-smoke.md)与[脱敏原始数据](../../phase-12-m3-results/local-smoke.json)。本轮仅使用已有模型，六文件大小/hash 与 M0 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` 一致；runtime 仍为 Python 3.13.9 / torch 2.11.0+cu128 / Transformers 4.57.6 / sentence-transformers 5.6.0 / CUDA 12.8。

显式 **M3 smoke profile**：FP16、cuda、C8、batch8、max_length512，来自 M0 已验证配置；正常测试 deadline=5 s，故障注入 deadline=50 ms。没有修改配置默认、Embedding 或 production runtime。

| 项目 | 本次真实结果 |
|---|---|
| Lazy / model load | startup=0；lifespan #1 加载一次并复用；close+clear 后 lifespan #2 新实例再次 lazy load |
| Score contract | 实际 [8,1]、sanity [2,1]；N finite raw logits，完整 identity 验证；20 次 drift=0、ranking stable |
| Warm p50 / p95 | 111.73 / 113.96 ms，n=20，fail=0；PASS |
| Retrieval incremental p50 / p95 | 112.73 / 114.51 ms，n=20；同 deterministic Hybrid fixture，计时排除 Graph/LLM；PASS |
| Busy p50 / p95 | 0.215 / 0.337 ms，n=20；真实 forward in-flight，原 RRF fallback；PASS |
| Timeout | deadline50 ms，返回 p95=65.07 ms、max=67.82 ms；全部≤150 ms；PASS |
| 后台完成 | A completion p95=148.24 ms，与调用方返回时间分列；20/20 B-after-timeout busy、C recovery success |
| Late result / concurrency | cancelled task result 为空、fallback 不变、新 token/request identity 正确；max_active_forward=1 |
| Coexist peak | allocated=3465.05 MiB、reserved=3518.00 MiB、device sampled=4673.56 MiB；PASS |
| 显存增长 | 20 次返回后 allocated 恒为3372.99 MiB，growth=0 bytes |
| Shutdown in-flight | 停止 admission，等待真实 forward 自然结束后清理，模型 weakrefs 释放；没有第二模型 |
| RAG wiring | K≤C 真实顺序生效；K>C 零 forward；busy/timeout/unavailable 精确原 RRF；一次 Hybrid；public schema 不变 |

这是单个显式持有 Embedding 的 smoke，不是默认 Embedding factory 的生产并发模型，也不是 M6 真实存储/Graph/LLM 全链路。设备目标 10 ms 周期采样，1191 个样本，最大实际间隔460.25 ms；不保证采样间未观测设备瞬时峰值。没有主动制造 OOM，普通异常/OOM 继续由 M1 注入回归覆盖。

### 3. 测试、限制与下一边界

Harness 先 RED→GREEN，最终32个单元测试；M1/M2/RAG/lifecycle/Embedding 合并 focused=409 passed；真实完整运行前 Backend full=1568 passed / 28 deselected / 0 FAIL。真实 CLI 与普通 pytest 分开报告。

首轮真实运行在 public DTO 序列化处因 synthetic chunk ID 非 UUID 终止；保留原始结果，补 RED 测试，仅修正 test fixture 为合法 UUID。现有 public API 未修改。随后 full 与完整真实 smoke 通过；没有通过改生产代码或调参规避 Gate。

本次无生产代码 diff，无需扩大 scope；未进入 M4，未创建 Golden Dataset，未安装依赖/下载模型，未 commit/push。三集合隔离、M5 选参、Delete/Citation/Graph/Public API 边界继续不变。

当前状态（M3记录时点）：`AWAITING_PROJECT_OWNER_PHASE12_M3_REVIEW`。

## 十三、M4 Owner decisions 与待审证据

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

## M5 现有证据 — 2026-09-16 Owner停止剩余实验

M4已独立提交`dbce60a07f6166c47b5f5a4e9e00ba274178381a`，提交后clean再开始M5。M5状态为**OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW**，未提交、未启用、未进入M6。

Owner授权的2048/4096最小validator扩展实际位于`app/retrieval/reranker.py:RerankerConfig`；512保留兼容，8192继续拒绝。示例配置只改注释，C/batch/length/timeout没有写为生产默认。

Selection40题Hybrid8/16/32的ID/顺序/score/rank前缀40/40一致；冻结C32并派生C8/C16，snapshot fingerprint=`f5e7830cf93a1f492e57aef8bf98794dd9f0ad0c1dd418ba1140813ee993f3b7`。Corpus/Golden沿用上节冻结身份，实验结束只读核验仍一致。

Stage A固定54个profile：25 feasible、29 rejected；FP16 12/27，BF16 13/27。0 OOM，12项4096/C32 timeout，5秒deadline未扩大。Stage B现有24份处理记录：22份完整40题×2请求结果、2份实际Selection GPU超限拒绝。Owner要求停止BF16剩余2048/4096实验，调度停止且已启动子进程自然结束；BF16/4096/C8/B1未启动，不能当失败或补造数据。

14个profile总体Gate通过但paraphrase退化，8个C16项Recall8 .9125→.8875而淘汰；暂无无需Owner类别审查即可接受的profile，不启动成本winner宣告。推荐profile=null。HTML5题与long paragraph5题分别报告，Markdown0/null；Selection robustness未执行，Final retrieval/score/metrics均0。

Selection完整记录1760请求，重复score drift0、ranking稳定；跨batch存在1个query排序差异，明确记录。实际BF16/4096/C8/B8 device peak8150.56MiB被拒绝；FP16对应full batch也超6500但初始工具未持久化精确峰值/分数，保留失败而不重跑。合成Stage A通过不能覆盖真实Selection显存失败。

Production仅长度validator一行；没有修改Hybrid/RAG/Context/Citation/Embedding/Graph/OpenSearch/DB/API。Backend full1725 passed、28 deselected、0 FAIL。设备采样不能保证捕获瞬时峰值；实验只持有一个Embedding实例，不外推生产多请求内存。

完整指标、Gate、限制、未执行项与原始证据索引见[M5报告](../../phase-12-m5-parameter-selection.md)、[硬件](../../phase-12-m5-hardware-screen.md)、[质量切片](../../phase-12-m5-quality-results.md)。不修改frozen Gold、质量Gate、性能SLO，不把部分矩阵标记为M5 acceptance完成。

## M5 Owner Closure — 2026-09-20

**PHASE12_M5_ACCEPTED**。本节覆盖历史的 OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW；原始实验记录、自动Gate及失败证据完整保留，未重新计算Selection质量或执行模型实验。

Owner-selected frozen profile：`bf16-L1024-C32-B8`。model=`BAAI/bge-reranker-v2-m3`，revision=`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，provider=`local_transformers`，runtime=Transformers AutoTokenizer + AutoModelForSequenceClassification，local_files_only=true、trust_remote_code=false，dtype=BF16、device=CUDA、max_length=1024、C=32、batch=8、timeout=5.0s。5秒是fail-open safety deadline，不是正常请求延迟SLO。

selection policy = **owner quality-first decision**；不是自动成本排序winner。paraphrase category degradation = **accepted known Phase 12 v1 risk**，必须持续披露；不修改Gold、query、qrel、质量Gate或性能SLO。`bf16-L4096-C8-B1=OWNER_STOPPED_NOT_RUN`，不补跑任何失败、缺失或停止profile，不重跑Selection，不刷新snapshot。Final run count=0，production RERANKER_ENABLED=false，actual .env保持原样。

冻结记录：`backend/tests/fixtures/phase12/selected_profile.json`；test-only helper：`backend/tests/phase12_local/selected_profile.py`。复用既有`corpus_audit.fingerprint()`的sorted-key紧凑UTF-8 JSON/SHA-256格式，identity绑定全部运行参数、runtime/provider、model/revision和M0六个模型/tokenizer文件大小及SHA。确定性生成的profile fingerprint：

`3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7`

原始结果索引/decision仍保留当时自动Gate状态，不覆盖或改写原始证据；本Owner决议与selected_profile为后续M6/M7依据。现有Settings默认值不据此自动启用；integration harness显式构造完整profile并验证指纹。

M5 closure补充15项漂移/旧env隔离测试：15 RED（helper不存在）→GREEN。M5/config/Provider/runtime/RAG及M4 integrity组合321 passed。完整安全回归结果见下方收口验证记录。只提交M5与上下文交接文档，不含M6测试；独立commit后clean才进入M6。M7 Gate与SLO不变。

收口验证：Backend full1740 passed/28 deselected/0 FAIL；actual .env、Golden、Selection、原始82份artifact及生产代码边界SHA不变，git diff --check通过。详细结果见M5报告。M6尚未开始。
