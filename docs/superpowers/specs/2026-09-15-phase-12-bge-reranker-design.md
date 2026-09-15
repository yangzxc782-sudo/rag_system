# Phase 12：BAAI/bge-reranker-v2-m3 设计与实施计划 — Design

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

这是 Phase 11 / Graph baseline mismatch，不属于 Phase 12 reranker。**M0 开始前必须由负责人在独立边界解决。** Phase 12 不得借机修改 Graph budget，也不得新增 skip/xfail 或改写断言绕过该问题。Planning Baseline 只记录现状，不运行修复或重新执行测试。

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
7. 长 Markdown 表格。
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
| M0 包含真实 GPU/runtime/coexist probe | 是，尚未执行 |
| 人工 gold、三集合隔离、分类指标 | 是 |
| M0 后负责人冻结 SLO，M5 不改通过线 | 是 |
| M7 final 只验收一次，失败保持关闭 | 是 |
| M0—M7 各有独立 commit 边界 | 见 canonical Implementation Plan |
| 未引入旧 Ollama reranker 设计 | 是 |
| Graph baseline mismatch 在 M0 前独立解决 | 必须 |

## 十一、M0 后证据更新位置

本节在 Planning Baseline 中仅定义更新位置，不包含真实 BGE 结果，不声明 M0 开始。

- Runtime evidence：待负责人授权并完成真实 BGE probe 后更新。
- Benchmark results：待真实 probe 后更新；必须覆盖本文所有矩阵、延迟、共存及失败项。
- SLO owner decisions：待负责人在 M0 Review 中基于实测结果冻结数值后记录。

M0 仅更新既有 canonical docs 的这些证据与决议；不得把两份 Design/Plan 再列为首次创建文件，或将 Planning Baseline 初始提交混入 M0 commit。

当前状态：`PHASE12_BGE_PLAN_ACCEPTED`。

下一授权边界：`AWAITING_PROJECT_OWNER_PHASE12_M0_AUTHORIZATION`。
