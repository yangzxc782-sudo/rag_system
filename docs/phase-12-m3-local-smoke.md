# Phase 12 M3 — Real Local BGE Smoke + Failure Recovery

日期：2026-09-15。状态：`AWAITING_PROJECT_OWNER_PHASE12_M3_REVIEW`。

**生产 Provider 的真实 CUDA smoke 已通过本次限定 profile 的全部 M3 Gate。没有修改生产代码，没有选择 M5 生产参数，没有进入 M4，未 commit/push。**

## 1. Git 起点与 Owner 决议

| 项目 | 证据 |
|---|---|
| Branch | `phase12-bge-reranker` |
| HEAD | `40c54e0ae4cd85693dd56fdbf4f6285e7b5e9a28` |
| Planning Baseline | `559087f` — docs: freeze phase 12 bge reranker design |
| Graph baseline 独立修复 | `4fc3412` — test: align graph context budget baseline |
| M0 | `06aa95f` — test: validate phase 12 bge runtime and hardware |
| M1 | `0b327c0` — feat: add local cross-encoder reranker provider |
| M2 | `40c54e0` — feat: integrate fail-open reranking into RAG |
| 初始 status / diff-check | 均为空；clean |

重新读取根 AGENTS、canonical Design/Plan、M0 报告/工具、当前 M1/M2 runtime/service/RAG/Context/lifespan/config、Embedding factory、生命周期与 RAG reranking tests。后端没有额外 AGENTS。当前代码和本次实测是依据。

负责人在 M0 Review 后已冻结 FP16 为主路径；BF16 留作后续质量对照，FP32 不进入当前生产候选优化。已批准 SLO：warm p95 ≤2000 ms、retrieval incremental p95 ≤2200 ms、busy p95 ≤50 ms、timeout 返回 ≤deadline+100 ms、共存设备峰值 ≤6500 MiB。C、batch、max_length、最终 timeout 仍由 M5 选择。

## 2. Gate、模型身份与显式 smoke profile

- 只沿用 `PHASE12_BGE_PROBE_ENABLED=1`。unset、空值、false、0、true 均不启用；没有第二个同义开关。
- 默认 CLI 实测：`{"status": "disabled", "real_load_count": 0}`。导入/关闭 gate 的子进程测试禁止导入 torch/Transformers/SentenceTransformers，仍通过。
- 普通 pytest 只收集 harness 单元测试。真实入口是独立 CLI，不在 pytest discovery 中，不需要增加 marker 或改 pyproject。
- 模型：`BAAI/bge-reranker-v2-m3`；revision：`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`。
- 复核 M0 identity manifest 的全部六文件大小和 SHA-256，均一致；没有下载。现有模型目录仍被精确 ignore。
- 生产 `_load_local_model()` 实际调用 AutoTokenizer / AutoModelForSequenceClassification，local_files_only=True、trust_remote_code=False、safetensors；进程设置 HF/Transformers offline。
- 测试显式使用 Settings 当前的 RERANKER_MODEL_PATH，不修改 `.env` 或示例。

| 测试参数 | 本次值 | 依据 |
|---|---:|---|
| dtype / device | FP16 / cuda | Owner 决议及 M0 已验证路径 |
| candidate count / batch | 8 / 8，整 batch | M0 coexist-float16 C8/batch8 |
| max_length | 512 | M0 已验证；本次没有新增长度档位 |
| 正常请求 deadline | 5 s | M0 已提出的显式实验预算；不是生产默认 |
| timeout 注入 deadline | 50 ms | 仅用于比正常 forward 更短的调用方等待测试 |

输入直接复用 M0 synthetic fixture 的前八个候选，包含短文本、长段落、长 Markdown 表格。M0 对该组合记录 wall p95=132.79 ms、设备边界采样峰值=4669.56 MiB。本次不扩大候选、不自动缩 batch/长度、不切 dtype/device。

## 3. 当前环境与测量方法

| 项目 | 本次实读 |
|---|---|
| Python | 3.13.9 |
| torch / Transformers / sentence-transformers | 2.11.0+cu128 / 4.57.6 / 5.6.0 |
| CUDA build / driver | 12.8 / 592.01 |
| GPU / capability | NVIDIA GeForce RTX 5060 Laptop GPU / 12.0 |
| CUDA total | 8150.56 MiB |
| Tokenizer | XLMRobertaTokenizerFast；声明 model_max_length=8192 |
| 实际模型 dtype | torch.float16 |

1. 真实加载、tokenization、评分、验证、排序、admission、wait、timeout、close 都执行当前生产组件。只在现有 loader 外包计数和无输出修改的模型 hook，没有 mock logits。
2. hook 核验 eval、inference_mode；在首个 encoder layer 入口发出观察事件，此前模型已执行真实 CUDA embedding 操作。没有人为 sleep/barrier 延长 forward。
3. CUDA Event 记录 forward 时长，完成 hook 与生产代码均同步 CUDA。warm wall 从 service 调用前同步到返回，覆盖 tokenize/H2D/forward/D2H/验证/worker；timeout 返回时不追加 GPU 同步，以免把后台完成误算为客户端返回。
4. 各正式分布 n=20，warm 另有三次 warmup。p50/p95 沿用 M0 `(n-1)*p` 线性插值。增量是每对 on−off 的样本分布，不是两个 p95 相减。
5. 显存使用 PyTorch allocated/reserved peak 和目标 10 ms 的设备采样线程。本次 1191 个采样，最大实际间隔 **460.25 ms**（加载/CPU 调度期间）；没有宣称覆盖所有采样间瞬时设备峰值。设备值包括其他进程和驱动，未停止系统服务。
6. 冷请求 1514.10 ms 是首次 admission 内加载+转设备+首个 forward；不含 Python/ML import、先前 Embedding 加载。模型 hash 读取已预热文件缓存，不是冷磁盘延迟分布。
7. 当前 Embedding factory 每次创建新 provider。实验持有一个真实 Qwen3 实例，期间检查同一 `_model`，结束后再次成功编码；不将它描述为生产 RAG 的真实多请求/并发显存行为。没有两模型并发 forward 的验收。

## 4. 真实 score、load once 与稳定性

- 首个 lifespan 启动时 singleton 为空；get 后 provider/thread 仍未创建；第一次 rerank 才加载。
- lifespan #1 全部重复、busy、timeout、恢复过程 **模型 load count=1**。
- 实际 logits shape：矩阵 `[8,1]`；sanity pair `[2,1]`。整个成功请求恰好 N 个 finite 分数，经生产层完整 identity 验证后应用。
- raw logit DESC；没有 sigmoid、threshold、fusion 或覆盖 hybrid_score。
- 20 次固定矩阵重复：**max score drift=0.0，ranking stable=True**。

固定矩阵首轮 raw scores（candidate-00…07）：

```text
[6.2734375, -8.453125, -8.6875, -9.5703125,
 -8.4609375, -9.109375, -11.0390625, -8.359375]
```

排序：`00 → 07 → 01 → 04 → 02 → 05 → 03 → 06`。

| M0 runtime sanity pair | Relevant raw | Irrelevant raw | 方向 |
|---|---:|---:|---|
| WCB impact | 6.2734375 | -10.8671875 | 通过 |
| Riser feeding | 5.9609375 | -10.8125 | 通过 |
| Solution treatment | 8.125 | -10.9921875 | 通过 |
| Sand permeability | 4.64453125 | -9.203125 | 通过 |

四组均与 M0 相关性方向一致。生产 Provider 每次处理同 query，sanity 按两候选成对运行，与 M0 混合 query 的八对 batch padding 不同；没有要求不同 batch 形态逐 bit 一致。这些是 runtime sanity，**不是 M4 Golden Dataset，也不证明正式 ranking 质量提升**。

## 5. SLO 结果

| 指标 | n | p50 ms | p95 ms / 实测峰值 | 已冻结 Gate | 结果 |
|---|---:|---:|---:|---:|---|
| Warm production service | 20 | 111.73 | 113.96 ms | ≤2000 ms | PASS，fail=0 |
| Retrieval off fixture | 20 | 0.059 | 0.109 ms | 对照 | — |
| Retrieval on fixture | 20 | 112.82 | 114.56 ms | 对照 | — |
| Paired incremental | 20 | 112.73 | 114.51 ms | ≤2200 ms | PASS，fail=0 |
| Busy fallback | 20 | 0.215 | 0.337 ms | ≤50 ms | PASS |
| Timeout caller return，deadline=50 ms | 20 | 59.50 | 65.07 ms；max **67.82 ms** | 每次≤150 ms | PASS |
| Timeout A background completion | 20 | 113.28 | 148.24 ms；max 186.28 ms | 与 caller 分列 | 自然完成 |
| Coexist device sampled peak | 1191 samples | — | **4673.56 MiB** | ≤6500 MiB | PASS，见采样限制 |

增量路径使用同一确定性 Hybrid 源候选集合，off 检索 K=2、on 检索 C=8，然后测到 M2 optional selection 完成；不包含 Context、Graph、LLM、真实 OpenSearch/DB/Embedding 检索成本。它验证 Phase 12 的编排增量，不是端到端生产 retrieval 性能。

busy 和 timeout 时间取外部模拟 RAG service 调用返回，包含轻量 Context、关闭状态的 Graph 分支和 fake LLM；因此较直接 worker 返回更保守。没有真实 Graph 查询或 LLM generation。

## 6. Busy / timeout / late result / recovery

### 真实 busy

启动正常 C8 长输入的真实任务，等待首个 encoder layer 进入，在真实 forward 完成之前连续发送 20 个 RAG 请求；每个得到 busy、原 RRF Top K，没有排队。所有样本开始前均确认 forward 未完成。观察到的最大 active forward=1。

### 核心 A → B → C 场景：20/20 通过

```text
A：真实 forward 开始 → 50 ms deadline → timeout，RAG 返回原 RRF
B：立即进入 → busy，RAG 返回原 RRF
A：CUDA 自然完成 → worker 清理 → idle
C：正常请求成功 → 新 budget token → 同一个已加载模型
```

每轮记录并验证：forward start < timeout return ≤ B return < forward end ≤ idle < C return。超时的 task cancelled=True、task.result=None；fallback 结果对象内容未改变；C request identity 正确，budget token 与 A 不同，且成功分数完整。迟到结果没有进入已 fallback 的请求或下一请求。

50 ms deadline 通过 test-only fixture **在 service idle 时替换其不可变 `_config` 的 timeout 字段**注入；A 真正结束后恢复 5 s 测试等待值。没有修改生产配置更新语义、wait/state machine 或 provider scoring；C/batch/length/dtype/device 不变，没有第二模型或重试评分。

### 加载失败与异常边界

- 用不存在的测试路径构造未加载 service：unavailable → M2 原 RRF fallback。没有破坏实际权重。
- 当前生产 load failure latch 保持等待新生命周期，不每请求自动修复/下载。
- 正常真实 smoke **0 OOM、0 CUDA unhealthy、0 推理异常**。没有主动制造 OOM；OOM all-or-nothing、无自动切设备/dtype/batch 继续由 M1 注入回归覆盖。
- 任何真实 SLO/显存/恢复/稳定性失败都会结束该次真实运行，不自动调参继续。

## 7. 显存与生命周期

| 阶段 | allocated MiB | reserved MiB | device sampled MiB |
|---|---:|---:|---:|
| Embedding-only，加载后 | 2280.83 | 2304.00 | 3451.56 |
| Warm 共存，20 次返回后 | **3372.99，逐次相同** | 3516.00 | 4671.56 |
| 整次 smoke 的 peak | **3465.05** | **3518.00** | **4673.56** |

settled allocated growth=**0 bytes**。harness 的 20 样本趋势校验允许 1 MiB 观测容差，实际没有用到容差；不能将短 smoke 等同长期 leak/stress 验收。允许 allocator 保留 reserved，未用 empty_cache 卸载 live 模型或掩盖异常。

- lifecycle #1：shutdown close+clear，旧 service closed；模型 weakref 已释放。Embedding 仍保持同一实例。
- lifecycle #2：取得新 service，仍 lazy；第一请求后累计 load=2（每个 lifespan 各一次）。没有复用已关闭对象。
- 第二个 lifecycle 在真实 forward in-flight 时执行实际 TestClient shutdown：新 admission 返回 closed，调用方释放，shutdown 等真实任务结束后清理。shutdown wall=265.85 ms，当次 forward=101.04 ms，未强杀线程。
- 全部运行观察到 **92 次真实 forward，max_active=1**；包含 sanity、warm、编排、故障恢复与两次 lifecycle，没有把其中故障注入请求算入正常 warm 样本。
- BGE 释放后 Embedding 再次成功输出 1024 维 CUDA embedding；实现/model/device 均未改。

## 8. M2 real-provider wiring 与 public API

真实 BGE + deterministic Hybrid + fake LLM：

- K=2≤C=8：rerank 顺序实际进入最终 retrieval / Citation，首个相关 chunk 从原 RRF 第2位提升到第1位。
- K=9>C=8：Hybrid(K)，0 BGE forward，原因 `public_limit_exceeds_reranker_capacity`；候选不足 K 不补查。
- busy、timeout、unavailable：原 snapshot `[:K]`，每个 RAG 请求断言恰好一次 Hybrid。
- Citation 与最终 Context / retrieval 顺序对应；public RagAskData 和原 Search schema 序列化成功，没有 reranker score/status/latency 字段。
- 本轮不访问真实 Hybrid 存储、Graph 或 LLM，不将该夹具验收称为 M6 真实全链路验收。Delete/Graph/Context 边界继续由已存在 M2 回归锁定，未修改实现。

## 9. Harness TDD、首轮工具错误与回归

初始 helper RED：30 failed（模块缺失）→30 passed。随后 fake LLM result contract 单项 RED→GREEN；真实首轮发现的 fixture UUID contract 单项 RED→GREEN。最终 harness **32 passed**。

首轮真实运行已完成加载/20次 warm/sanity，但 fixture 使用 `candidate-xx` 作 public chunk_id，现有 SearchItem 要求 UUID，序列化发生 ValidationError。保留[首轮原始结果](phase-12-m3-results/attempt-1-fixture-error.json)。旧 CLI 将所有终止统一写为 `PHASE12_M3_SCOPE_CONFLICT`；该次根因已确定为 **test fixture schema 错误**，不是生产 Provider、SLO 或必须修改 public API 的 scope stop。

修复仅给 test-only Hybrid 夹具使用合法、确定性 UUID；文本和 smoke profile 不变，补测试并重新通过 full 后重新运行。**没有修改或放宽 public DTO**，最终运行完整成功；首轮不计为完整 smoke 通过。

| 检查 | 结果 |
|---|---|
| Harness 初次 RED | 30 expected failed |
| 真实首次运行前 full | 1567 passed / 28 deselected / 0 FAIL，15.51 s |
| UUID fixture 修正后、正式真实运行前 full | **1568 passed / 28 deselected / 0 FAIL，15.33 s** |
| 最终 M1/M2 + harness + RAG + lifecycle + Embedding focused | **409 passed**，7.61 s |
| 真实 smoke | **独立 CLI，完整一次成功**；此前一次 fixture error 保留 |

409 的文件集合包含 test_bge_smoke、三个 M1 reranker 文件、test_rag_reranking、RAG context/service/API、LLM startup/provider/API provider、Graph repository、Embedding/document embeddings。Full 覆盖其余安全回归。既有 Starlette/httpx deprecation warning=1；28 个 integration 测试未执行、不计为通过。没有未解释 FAIL，没有真实模型混入普通 pytest 计数。

复现（在 `D:\rag_system\backend`）：

```powershell
# 默认无真实模型调用（前提为环境中未设置既有 real gate）
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_smoke.py
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp <fresh-local-temp> -m "not integration and not phase12_local"

# 仅在负责人授权真实操作的进程中；模型必须已存在且与 M0 指纹一致
$env:PHASE12_BGE_PROBE_ENABLED = '1'
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_smoke.py --output ../docs/phase-12-m3-results/local-smoke.json
```

[最终脱敏原始结果](phase-12-m3-results/local-smoke.json)包含逐次 raw scores/rank、latency、内存、20组 A/B/C 时间线和 token 对照；没有完整 query/chunk、权重路径或凭证。它记录运行时 harness SHA-256：`cf7a35884ebfc810b85e070b561c35d7847423eb3709dcd2d8be527cdf014505`。

## 10. 修改与下一边界

新增两个 test-only Python 文件、本报告、两份脱敏 JSON；更新已有 canonical Design/Plan 的 M3 evidence 和 Owner SLO 状态。没有生产代码 diff，没有 scope conflict 需要扩大实现范围。

最终 `git diff --check` 通过；五个 untracked 文件另经 no-index whitespace 检查。`git status --short` 为两份 canonical 文档 modified、五个新增文件（JSON 在目录行合并显示）；staged files 为空，HEAD 不变。已跟踪 diff stat 为2 files / 87 insertions / 2 deletions，Git 默认 stat 不包含五个 untracked 文件。生产目录、配置示例、pyproject、frontend 的 diff 均为空。

未修改 Embedding、Hybrid 核心、RAG/Context/Citation 生产逻辑、Graph、OpenSearch、public API、数据库、frontend、配置默认或依赖；没有下载模型，没有进入 M4，没有 commit/push。

**AWAITING_PROJECT_OWNER_PHASE12_M3_REVIEW**
