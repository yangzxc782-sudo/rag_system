# Phase 12 M0 — BGE Runtime / Hardware Probe

日期：2026-09-15。状态：`AWAITING_PROJECT_OWNER_PHASE12_M0_REVIEW`。实测和回归已结束，SLO/参数尚待负责人确认；存在明确列出的 FP32 共存中止/未执行配置。

## 1. Git Gate、baseline 与授权

- Branch：`phase12-bge-reranker`。
- HEAD：`4fc3412f7a20efbac21032b44a8964debed12fd2`。
- Planning Baseline：`559087f42816859a2c255750d1b6613bc228d845`，`docs: freeze phase 12 bge reranker design`。
- 独立 Graph baseline 修复：`4fc3412`，`test: align graph context budget baseline`。生产默认仍为 50000；本次未修改 Graph 或其测试。
- 开始前 branch/status/diff-check/log Gate 通过，工作区 clean。
- 真实模型操作前 Backend baseline：**1335 passed，28 deselected，1 warning，13.84 s，0 FAIL**。
- 当前授权覆盖指定 BGE 下载、真实 CUDA、benchmark、Embedding 共存、test-only 工具/fixture/report；不包括生产接线、依赖安装、量化、M1 或 commit/push。
- 当前授权明确 M0 只模拟候选，不运行 Hybrid/RAG，不实现 worker/busy/timeout。它优先于 Planning 文本中早先的 M0 retrieval/worker 测量要求；这些项没有实测结果。

## 2. 当前环境与模型身份

| 项目 | 当前实测 |
|---|---|
| Python | 3.13.9 |
| torch | 2.11.0+cu128 |
| transformers | 4.57.6 |
| sentence-transformers | 5.6.0 |
| tokenizers / safetensors | 0.22.2 / 0.8.0 |
| huggingface-hub | 0.36.2 |
| CUDA build / driver | 12.8 / 592.01 |
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU |
| CUDA capability / BF16 supported | 12.0 / True |
| CUDA device total | 8546484224 bytes，8150.56 MiB |

`backend/pyproject.toml` 已声明 torch、transformers、sentence-transformers；使用现有安装版本。未安装 FlagEmbedding、accelerate、hf_xet 或其他依赖。Hugging Face 提示可安装 hf_xet 时，使用现有普通 HTTP 下载 fallback。

实际 Settings 指定下载目录为 `D:\rag_system\models\bge-reranker-v2-m3`。下载前只在 `.gitignore` 新增 `/models/bge-reranker-v2-m3/`，权重与该目录内 cache 均被忽略。此路径按负责人要求在本文说明，机器可读结果不保存模型绝对路径。

- model ID：`BAAI/bge-reranker-v2-m3`。
- revision：`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`。
- config SHA-256：`13dcd6c31d9fec9d1d8e158702072f62d7fa7d312a64b9fe057bec9a08cfe41a`。
- tokenizer config SHA-256：`7e4c1cc848840aeccdd763458c18dd525eb0f795c992e00ebe9c28554e7db2d4`。
- 唯一权重文件：`model.safetensors`，2271071852 bytes；SHA-256：`d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286`。
- [完整模型身份清单](phase-12-m0-results/model-identity.json)保存六个文件的大小/hash、下载时间、固定 revision。每个 benchmark 进程加载前校验所有文件的大小/hash。M5/M7 应核对同一身份。

## 3. 模型、tokenizer 与 score contract

官方模型卡给出的直接 Transformers 接口在本机加载成功：[BAAI 官方模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3)。所有正式 benchmark 使用 `local_files_only=True`、`trust_remote_code=False` 及进程级 HF/Transformers offline 开关。

- `AutoTokenizer` + `AutoModelForSequenceClassification`；没有 remote code、device_map 自动 offload 或量化。
- 本地 config：`XLMRobertaForSequenceClassification`、单标签、hidden_size=1024、24 层、16 heads、max_position_embeddings=8194。历史 `_name_or_path=BAAI/bge-m3` 不作为模型身份依据。
- 实际为 `XLMRobertaTokenizerFast`，model_max_length=8192，pair 特殊 token 开销=4。8192 没有被设为生产长度；仅 benchmark 512/1024。
- `model.eval()` + `torch.inference_mode()`；实际参数 dtype 由每份报告记录，attention implementation 为 SDPA。
- 每个 micro-batch logits 严格为 `[batch_N,1]`，整 batch 为 `[8,1]`、`[16,1]`、`[32,1]`。校验 shape/count/identity/finite 后取每行唯一元素，等价于 `logits[:,0]`；没有 flatten 凑数。
- 仅 raw relevance logit DESC，同分按原候选位置、candidate ID；没有 Sigmoid、threshold、fusion 或分数校准。
- paired query/passage，`truncation="only_second"`，见 [Transformers 官方截断说明](https://huggingface.co/docs/transformers/v4.57.1/en/pad_truncation)。query 无法完整容纳时在 forward 前拒绝，绝不截 query。

## 4. 测量方法与边界

1. 四组人工编写的领域正负 sanity 对，不引用数据库真实 chunks，不虚构材料验收值；只用于 runtime sanity，**不是 M4 Golden Dataset**。
2. 同一个 query 的 32 个 passage 组成候选矩阵，含短文本、长段落、240 行 Markdown 表格；C=8/16/32 使用同一池的前缀，原始输入不被修改。
3. 每长度的 C/batch 组合为 8/8、16/16、16/8、32/32、32/16、32/8。每组 3 次 warmup，20 次正式 warm 样本。
4. CUDA Event 与 `torch.cuda.synchronize()` 用于 forward 计时。wall 包含成对 tokenization、query 校验、H2D、全部 micro-batch forward、D2H、分数校验和显存边界采样；不含报告落盘。
5. 全长 token 计数预扫描在正式计时之外；计时内 tokenizer 仍处理原 passage 并临时截断。没有使用字符数估算 token。
6. p50/p95 使用 `(n-1)*p` 线性插值；20 个样本是 M0 最低规模，不能替代真实生产输入、长期压力或并发验证。
7. allocated/reserved peak 来自 PyTorch peak API；device snapshot peak 是 CUDA mem_get_info 在 micro-batch 边界采样的最大值，**不是连续设备总峰值保证**。设备快照含驱动/其他进程，不等同于 PyTorch allocated，也不保证与 nvidia-smi 字段口径一致。
8. 独立进程串行顺序：FP32-only → Embedding-only → FP16-only → BF16-only → FP16 coexist → BF16 coexist → FP32 coexist。未停止其他服务；未人为清空桌面 GPU 使用或文件缓存。
9. Cold 指新进程中的 tokenizer/from_pretrained/to(device)/首次 forward；文件加载前已做 hash 读取，**不是冷磁盘 I/O**。不含 Python 启动/import；每场景/dtype 一次，不能将六个不同场景的数值解释成同场景 cold 分布。
10. 每配置开始的 `empty_cache()` 只释放未使用缓存块，不卸载 live 模型。micro-batch 是预先列出的独立配置，没有失败后自动缩 batch。
11. **Embedding 生命周期边界**：当前 factory 每次创建新 provider，模型在该实例内 lazy 缓存。本实验显式持有一个已加载实例；每配置后检查同一 `_model` 对象并再次成功 encode_query。不能据此宣称生产 RAG 多请求的显存行为已验证。
12. 共存是单 Embedding 持有者与 BGE 串行 forward；没有两个模型同时 forward、生产多请求并发或生产 fallback/worker/timeout。

## 5. TDD 与默认关闭 Gate

先 RED：44 failed / 1 passed，缺少 helper 的预期失败；再 GREEN。单 query 候选矩阵用例也先单独 RED；实现后 46 passed。FP32 共存出现显存压力后，显式 batch 筛选工具先新增 5 个 RED tests，再实现；当前总计 **51 passed**。

覆盖默认 gate 零真实操作、import 不加载 ML runtime、shape/count/identity/NaN/Inf、样本不足、p50/p95、GPU 字段缺失、query 保留、passage 临时截断和报告脱敏。fake/unit tests 无需启用环境变量。

只有 `PHASE12_BGE_PROBE_ENABLED=1` 才允许真实操作；unset/false/其他值默认关闭。未开启 gate 的 download CLI 实测返回 `disabled`、`real_load_count=0`。普通 pytest 不执行真实 BGE inference。

```powershell
# 从 D:\rag_system\backend 执行；默认零真实调用
Remove-Item Env:PHASE12_BGE_PROBE_ENABLED -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_probe.py
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_bge_probe.py

# 仅在负责人已授权时执行；模型已有，无需再次下载
$env:PHASE12_BGE_PROBE_ENABLED = '1'
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_probe.py --mode reranker --dtype float16 --output ../docs/phase-12-m0-results/reranker-float16.json
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_probe.py --mode embedding --output ../docs/phase-12-m0-results/embedding-only.json
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_probe.py --mode coexist --dtype float16 --output ../docs/phase-12-m0-results/coexist-float16.json
# FP32 共存只补测显式 micro-batch=8，不自动扩大候选容量
.\.venv\Scripts\python.exe -B tests/phase12_local/bge_probe.py --mode coexist --dtype float32 --batch-sizes 8 --output ../docs/phase-12-m0-results/coexist-float32-microbatch8.json
```

另外两个 dtype 为 `float32` / `bfloat16`，应分别在新进程串行执行。原始报告保存 probe/fixture SHA、HEAD、runtime、revision，结果只含脱敏标识/数值，不含完整 query/chunk 或权重路径。

### FP32 共存压力与中止记录

FP32 coexist 的前三个配置完成后，C=32、L=512、batch=32 未能在上一完成检查点后超过 300 s 内产生完整结果。C=16 整 batch 的 CUDA device 快照曾报告 free=0；中止前一次 nvidia-smi 快照为 used=7447 MiB、free=453 MiB。两种 API 口径不同，不能把这两个读数当作同一连续曲线；分页/驻留机制没有被单独分析。

仅向本轮探针 session 发出中断，终止该实验进程；未停止其他服务。没有捕获到 CUDA OOM exception。**300 s 是整个未完成配置的观察下界，不是单次 forward 时长，也不是 p95。** 未保存的部分样本数量未知，不计算该配置 p50/p95，不计为通过。

保留 [中止报告与前三组原始数据](phase-12-m0-results/coexist-float32.json)。未执行的 FP32 coexist 配置为 L=512/C=32/batch=16，以及 L=1024 的 C=16/batch=16、C=32/batch=32、C=32/batch=16；没有用推测值补齐。

随后以新进程重新加载并持续持有 Embedding，再加载 BGE，显式补测 batch=8；没有通过先卸载 Embedding 再让 BGE 单独运行来通过共存 Gate。测试工具仅增加 CLI batch 选择，评分/计时实现未改；因此早期与补测报告记录的 probe SHA 不同，fixture/model 身份相同。

该压力配置与其他通过配置分别报告；它不代表所有 8GB 合理配置都不可运行。FP16/BF16 的完整矩阵已经通过。是否接受该 FP32 实验边界及后续资源门槛，留给负责人 M0 Review。

## 6. Sanity、重复稳定性与跨 dtype

| dtype（reranker-only） | WCB 正/负 | 冒口 正/负 | 固溶 正/负 | 型砂 正/负 | 胜出 | 5 次 drift |
|---|---:|---:|---:|---:|---:|---:|
| float32 | 6.272568 / -10.870256 | 5.960584 / -10.816666 | 8.126191 / -10.992961 | 4.644268 / -9.202494 | 4/4 | 0.0 |
| float16 | 6.273438 / -10.875000 | 5.960938 / -10.820312 | 8.125000 / -10.992188 | 4.644531 / -9.203125 | 4/4 | 0.0 |
| bfloat16 | 6.281250 / -10.875000 | 5.968750 / -10.812500 | 8.125000 / -11.000000 | 4.656250 / -9.187500 | 4/4 | 0.0 |

完成的 67 个不同配置（69 次配置运行）各 20 次重复：max drift=0.0，ranking stable=True。共存三种 dtype 的 sanity 也均为 4/4。只代表同配置重复稳定。

| 比较 | sanity 最大分差 | matrix 最大分差 | 全排序一致 / 实际可比配置 | Top8 一致 / 实际可比配置 |
|---|---:|---:|---:|---:|
| reranker-float32 -> reranker-float16 | 0.00474358 | 0.00702858 | 6/12 | 12/12 |
| reranker-float32 -> reranker-bfloat16 | 0.01499367 | 0.07965565 | 3/12 | 9/12 |
| coexist-float32 -> coexist-float16 | 0.00474358 | 0.00702858 | 5/7 | 7/7 |
| coexist-float32 -> coexist-bfloat16 | 0.01499367 | 0.07965565 | 2/7 | 4/7 |
| reranker-float32 -> coexist-float32 | 0.00000000 | 0.00000000 | 7/7 | 7/7 |
| reranker-float16 -> coexist-float16 | 0.00000000 | 0.00000000 | 12/12 | 12/12 |
| reranker-bfloat16 -> coexist-bfloat16 | 0.00000000 | 0.00000000 | 12/12 | 12/12 |

FP16 的具体例子：C=32/L=512 的 candidate-30/12 在 FP32 中为 -11.035269/-11.036484，FP16 都为 -11.0390625；同分按原顺序，末尾排名交换。没有加 epsilon 或校准隐藏变化。BF16 分差更大；三集合人工 gold 质量仍待 M4/M5，不能凭 sanity 宣称检索质量提高。

## 7. Cold、warm 与显存摘要

| profile | tokenizer ms | from_pretrained ms | to CUDA ms | 首次 GPU / wall ms | profile elapsed s |
|---|---:|---:|---:|---:|---:|
| reranker-float32 | 710.92 | 1048.32 | 1391.68 | 429.05 / 438.02 | 531.77 |
| reranker-float16 | 654.08 | 1711.38 | 231.67 | 355.51 / 361.97 | 165.21 |
| reranker-bfloat16 | 583.81 | 1712.82 | 232.69 | 305.23 / 308.83 | 155.86 |
| coexist-float16 | 652.49 | 1600.01 | 317.16 | 391.32 / 399.88 | 169.94 |
| coexist-bfloat16 | 499.56 | 1833.83 | 256.61 | 257.75 / 270.26 | 159.93 |
| coexist-float32 | 689.69 | 142.65 | 1509.41 | 142.01 / 145.64 | 未完成 |
| coexist-float32-microbatch8 | 683.28 | 112.15 | 1504.72 | 158.96 / 163.48 | 237.97 |

| profile | allocated peak MiB | reserved peak MiB | device snapshot peak MiB | 最大 warm wall p95 ms | OOM |
|---|---:|---:|---:|---:|---:|
| embedding-only | 2289.28 | 2304.00 | 3451.56（末次快照） | 47.52（query） | 0 |
| reranker-float32 | 3711.57 | 4492.00 | 5637.56 | 3957.39 | 0 |
| reranker-float16 | 1860.66 | 2264.00 | 3409.56 | 1216.23 | 0 |
| reranker-bfloat16 | 1860.66 | 2264.00 | 3411.56 | 1131.99 | 0 |
| coexist-float16 | 4133.37 | 4538.00 | 5685.56 | 1163.39 | 0 |
| coexist-bfloat16 | 4133.37 | 4538.00 | 5685.56 | 1098.38 | 0 |
| coexist-float32 | 4815.90 | 5038.00 | 8150.56 | 933.64 | 0 |
| coexist-float32-microbatch8 | 4831.90 | 5038.00 | 8150.56 | 3916.91 | 0 |

Embedding-only cold load+first query=4702.16 ms；warm n=20，p50/p95=40.83 / 47.52 ms。device=cuda:0、维度=1024，未修改 provider 的实现/模型/dtype策略。[Embedding 原始结果](phase-12-m0-results/embedding-only.json)。

## 8. 配置矩阵：67 个完成，1 个中止，4 个未执行

下列有完整数据的配置 load/forward 成功，CUDA OOM exception=0、fail count=0、n=20、max drift=0、ranking stable=True。FP32 共存中止配置不计为通过，也没有有效 p50/p95；详见异常边界。wall Σ 为 20 次 wall 样本合计秒数，不含 warmup。GPU 列单位 MiB，device 为边界采样峰值。原始 JSON 保存每次 raw score/ranking/latency 与逐候选截断率。

### reranker-float32

[原始结果](phase-12-m0-results/reranker-float32.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 420.88 | 425.47 | 407.37 | 8.42 | 2359.13 | 2476.00 | 3621.56 |
| 512 | 16 | 16 | 839.77 | 846.29 | 821.32 | 16.81 | 2543.19 | 2764.00 | 3909.56 |
| 512 | 16 | 8 | 853.19 | 857.97 | 822.17 | 17.07 | 2359.13 | 2476.00 | 3621.56 |
| 512 | 32 | 32 | 1694.41 | 1962.87 | 1887.15 | 35.29 | 2911.32 | 3340.00 | 4485.56 |
| 512 | 32 | 16 | 1687.40 | 1696.06 | 1642.75 | 33.76 | 2543.19 | 2764.00 | 3909.56 |
| 512 | 32 | 8 | 1640.32 | 1793.32 | 1722.23 | 33.49 | 2359.13 | 2476.00 | 3621.56 |
| 1024 | 8 | 8 | 875.50 | 882.40 | 863.49 | 17.54 | 2559.19 | 2764.00 | 3909.56 |
| 1024 | 16 | 16 | 1741.10 | 1749.14 | 1721.15 | 34.84 | 2943.32 | 3340.00 | 4485.56 |
| 1024 | 16 | 8 | 1750.44 | 1783.17 | 1745.50 | 35.12 | 2559.19 | 2764.00 | 3909.56 |
| 1024 | 32 | 32 | 3604.47 | 3922.47 | 3865.50 | 72.66 | 3711.57 | 4492.00 | 5637.56 |
| 1024 | 32 | 16 | 3872.69 | 3957.39 | 3897.20 | 77.14 | 2943.32 | 3340.00 | 4485.56 |
| 1024 | 32 | 8 | 3884.54 | 3941.05 | 3849.96 | 77.80 | 2559.19 | 2764.00 | 3909.56 |

### reranker-float16

[原始结果](phase-12-m0-results/reranker-float16.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 127.45 | 131.67 | 113.77 | 2.56 | 1184.22 | 1236.00 | 2381.56 |
| 512 | 16 | 16 | 252.42 | 257.61 | 231.56 | 5.06 | 1276.28 | 1400.00 | 2545.56 |
| 512 | 16 | 8 | 255.10 | 276.38 | 228.96 | 5.14 | 1184.22 | 1236.00 | 2381.56 |
| 512 | 32 | 32 | 521.74 | 533.13 | 483.47 | 10.46 | 1460.41 | 1688.00 | 2833.56 |
| 512 | 32 | 16 | 512.27 | 520.40 | 467.98 | 10.27 | 1276.28 | 1400.00 | 2545.56 |
| 512 | 32 | 8 | 516.92 | 538.42 | 463.91 | 10.40 | 1184.22 | 1236.00 | 2381.56 |
| 1024 | 8 | 8 | 280.02 | 284.74 | 266.46 | 5.62 | 1284.28 | 1400.00 | 2545.56 |
| 1024 | 16 | 16 | 567.35 | 578.09 | 549.83 | 11.38 | 1476.41 | 1688.00 | 2833.56 |
| 1024 | 16 | 8 | 563.29 | 577.73 | 536.25 | 11.32 | 1284.28 | 1400.00 | 2545.56 |
| 1024 | 32 | 32 | 1148.17 | 1216.23 | 1152.91 | 23.13 | 1860.66 | 2264.00 | 3409.56 |
| 1024 | 32 | 16 | 1141.30 | 1157.16 | 1097.85 | 22.84 | 1476.41 | 1688.00 | 2833.56 |
| 1024 | 32 | 8 | 1129.67 | 1147.13 | 1063.95 | 22.63 | 1284.28 | 1400.00 | 2545.56 |

### reranker-bfloat16

[原始结果](phase-12-m0-results/reranker-bfloat16.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 123.68 | 126.44 | 108.06 | 2.47 | 1184.22 | 1236.00 | 2383.56 |
| 512 | 16 | 16 | 247.94 | 258.20 | 231.74 | 4.97 | 1276.28 | 1400.00 | 2547.56 |
| 512 | 16 | 8 | 256.71 | 265.38 | 222.45 | 5.14 | 1184.22 | 1236.00 | 2383.56 |
| 512 | 32 | 32 | 500.39 | 525.38 | 450.32 | 10.08 | 1460.41 | 1688.00 | 2835.56 |
| 512 | 32 | 16 | 486.58 | 504.29 | 443.60 | 9.80 | 1276.28 | 1400.00 | 2547.56 |
| 512 | 32 | 8 | 502.10 | 521.31 | 438.83 | 10.06 | 1184.22 | 1236.00 | 2383.56 |
| 1024 | 8 | 8 | 263.18 | 270.12 | 249.56 | 5.29 | 1284.28 | 1400.00 | 2547.56 |
| 1024 | 16 | 16 | 522.03 | 532.73 | 502.66 | 10.48 | 1476.41 | 1688.00 | 2835.56 |
| 1024 | 16 | 8 | 527.61 | 535.44 | 498.25 | 10.58 | 1284.28 | 1400.00 | 2547.56 |
| 1024 | 32 | 32 | 1073.31 | 1131.99 | 1070.25 | 21.57 | 1860.66 | 2264.00 | 3411.56 |
| 1024 | 32 | 16 | 1050.35 | 1073.43 | 1001.67 | 21.04 | 1476.41 | 1688.00 | 2835.56 |
| 1024 | 32 | 8 | 1059.38 | 1077.64 | 1000.30 | 21.24 | 1284.28 | 1400.00 | 2547.56 |

### coexist-float16

[原始结果](phase-12-m0-results/coexist-float16.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 128.12 | 132.79 | 114.05 | 2.57 | 3456.93 | 3522.00 | 4669.56 |
| 512 | 16 | 16 | 254.34 | 263.46 | 236.82 | 5.10 | 3548.99 | 3674.00 | 4821.56 |
| 512 | 16 | 8 | 258.18 | 269.33 | 229.39 | 5.18 | 3456.93 | 3522.00 | 4669.56 |
| 512 | 32 | 32 | 529.09 | 561.00 | 506.87 | 10.66 | 3733.12 | 3962.00 | 5109.56 |
| 512 | 32 | 16 | 518.41 | 530.72 | 476.83 | 10.39 | 3548.99 | 3674.00 | 4821.56 |
| 512 | 32 | 8 | 519.49 | 536.71 | 454.93 | 10.55 | 3456.93 | 3522.00 | 4669.56 |
| 1024 | 8 | 8 | 281.39 | 286.31 | 266.76 | 5.63 | 3556.99 | 3674.00 | 4821.56 |
| 1024 | 16 | 16 | 567.44 | 582.10 | 550.46 | 11.41 | 3749.12 | 3962.00 | 5109.56 |
| 1024 | 16 | 8 | 565.01 | 572.18 | 532.69 | 11.31 | 3556.99 | 3674.00 | 4821.56 |
| 1024 | 32 | 32 | 1151.35 | 1158.60 | 1106.26 | 22.98 | 4133.37 | 4538.00 | 5685.56 |
| 1024 | 32 | 16 | 1138.57 | 1163.39 | 1101.23 | 22.28 | 3749.12 | 3962.00 | 5109.56 |
| 1024 | 32 | 8 | 1037.21 | 1079.75 | 965.70 | 21.02 | 3556.99 | 3674.00 | 4821.56 |

### coexist-bfloat16

[原始结果](phase-12-m0-results/coexist-bfloat16.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 109.87 | 112.63 | 95.46 | 2.21 | 3456.93 | 3522.00 | 4669.56 |
| 512 | 16 | 16 | 214.57 | 221.82 | 200.53 | 4.31 | 3548.99 | 3674.00 | 4821.56 |
| 512 | 16 | 8 | 225.22 | 232.79 | 197.53 | 4.51 | 3456.93 | 3522.00 | 4669.56 |
| 512 | 32 | 32 | 445.67 | 493.20 | 440.74 | 8.96 | 3733.12 | 3962.00 | 5109.56 |
| 512 | 32 | 16 | 510.96 | 537.86 | 460.55 | 10.23 | 3548.99 | 3674.00 | 4821.56 |
| 512 | 32 | 8 | 507.21 | 532.81 | 434.69 | 10.30 | 3456.93 | 3522.00 | 4669.56 |
| 1024 | 8 | 8 | 263.11 | 269.67 | 247.47 | 5.27 | 3556.99 | 3674.00 | 4821.56 |
| 1024 | 16 | 16 | 523.08 | 530.24 | 497.65 | 10.48 | 3749.12 | 3962.00 | 5109.56 |
| 1024 | 16 | 8 | 526.98 | 543.81 | 503.50 | 10.61 | 3556.99 | 3674.00 | 4821.56 |
| 1024 | 32 | 32 | 1063.64 | 1073.54 | 1016.17 | 21.23 | 4133.37 | 4538.00 | 5685.56 |
| 1024 | 32 | 16 | 1079.73 | 1095.34 | 1021.69 | 21.60 | 3749.12 | 3962.00 | 5109.56 |
| 1024 | 32 | 8 | 1080.17 | 1098.38 | 1012.50 | 21.63 | 3556.99 | 3674.00 | 4821.56 |

### coexist-float32

[原始结果](phase-12-m0-results/coexist-float32.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 448.28 | 465.75 | 447.56 | 9.01 | 4631.84 | 4750.00 | 5897.56 |
| 512 | 16 | 16 | 886.43 | 906.15 | 875.53 | 17.79 | 4815.90 | 5038.00 | 8150.56 |
| 512 | 16 | 8 | 909.58 | 933.64 | 895.62 | 18.17 | 4631.84 | 4750.00 | 5897.56 |

### coexist-float32-microbatch8

[原始结果](phase-12-m0-results/coexist-float32-microbatch8.json)

| L | C | batch | wall p50 ms | wall p95 ms | GPU forward p95 ms | wall Σ s | alloc peak | reserve peak | device sample peak |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 512 | 8 | 8 | 436.91 | 448.32 | 431.15 | 8.80 | 4631.84 | 4750.00 | 5897.56 |
| 512 | 16 | 8 | 881.85 | 903.54 | 870.94 | 17.73 | 4631.84 | 4750.00 | 5897.56 |
| 512 | 32 | 8 | 1805.53 | 1821.93 | 1753.80 | 36.00 | 4631.84 | 4750.00 | 5897.56 |
| 1024 | 8 | 8 | 970.35 | 980.65 | 962.49 | 19.42 | 4831.90 | 5038.00 | 8150.56 |
| 1024 | 16 | 8 | 1945.15 | 1984.97 | 1932.59 | 39.17 | 4831.90 | 5038.00 | 8150.56 |
| 1024 | 32 | 8 | 3888.74 | 3916.91 | 3841.23 | 77.91 | 4831.90 | 5038.00 | 8150.56 |

## 9. 长段落、Markdown 表格与截断率

| L | kind | C=32 数量 | 原 passage tokens | 保留 passage tokens | 丢弃比例 |
|---:|---|---:|---:|---:|---:|
| 512 | short | 11 | 30–55 | 30–55 | 0.00%–0.00% |
| 512 | paragraph | 11 | 5330–5355 | 495–495 | 90.71%–90.76% |
| 512 | table | 10 | 10584–10609 | 495–495 | 95.32%–95.33% |
| 1024 | short | 11 | 30–55 | 30–55 | 0.00%–0.00% |
| 1024 | paragraph | 11 | 5330–5355 | 1007–1007 | 81.11%–81.20% |
| 1024 | table | 10 | 10584–10609 | 1007–1007 | 90.49%–90.51% |

矩阵 query=13 tokens、pair 开销=4，passage 预算为 495/1007。所有实际输入的 query token IDs 完整一致；所有三类 passage 在各 dtype/驻留场景下得分有限。超长 query 在 forward 前拒绝，原输入/fixture/数据库均未改写。

全长计数时的 10607 > 8192 提示不代表将超长序列交给模型；实际 forward 均只接收 only_second 截到 512/1024 的临时输入。

[analysis-summary.json](phase-12-m0-results/analysis-summary.json)保存全部逐配置比较；原始数据未删除或改写。

## 10. SLO 建议、M1 验证范围与 Owner decisions

下表是**待审建议**，没有冻结生产参数或 SLO。所有直接 latency 证据仅来自单进程、单 Embedding 持有者的候选评分；未测项不得解释成已通过。

| 项目 | 建议提交 M0 Review | 依据与缺口 |
|---|---|---|
| warm rerank p95 | ≤2000 ms，限定本轮半精度 C≤32、L≤1024 的实验范围 | FP16/BF16 共存最慢完整配置 p95=1163.39 ms；20 次 synthetic 样本不能证明生产 SLO |
| retrieval incremental p95 | 暂建议预算 ≤2200 ms，接线后实测 | 2000 ms warm 目标另预留 200 ms 编排预算是待测假设；没有 Hybrid/RAG 实测，也不是两组 p95 相减 |
| busy fallback p95 | 暂建议工程目标 ≤50 ms，M1 独立测量后确认 | M0 没有 admission/worker，无法从 GPU forward 推导该路径时延；不是已验证的数字 |
| timeout fallback p95 | 暂建议候选 deadline=5000 ms，返回不晚于 deadline+100 ms；均需确认 | cold tokenizer/load/to CUDA/first wall 合计约 2.46–3.59 s，供预算参考；未包含新进程 import，没有实现 Future timeout 或 CUDA 强制取消 |
| GPU coexist safe ceiling | 暂建议设备采样使用量 ≤6500 MiB，约保留 1651 MiB 容量余量 | 半精度共存最大边界快照=5685.56 MiB；采样可能漏峰，需连续监测及并发边界验证，不能保证零 OOM |

FP32 共存虽有可完成的 micro-batch 配置，但 device 快照也多次 free=0，C=32/L=1024/batch=8 的 p95=3916.91 ms。**不建议将 FP32 共存作为 M1 优先档位。** 未隔离其他 GPU workload，也未分析驱动驻留/分页，不能把压力唯一归因于某一个 batch 参数。

推荐 M1 工程验证范围：直接 Transformers；优先 FP16，BF16 保留对照、FP32 保留数值参考；长度 512/1024、候选 C=8/16/32、micro-batch=8/16，整 batch 为对照。FP16 本轮跨 FP32 分差更小且 Top8 在全部独立运行配置一致；BF16 有 Top8 变化。这不是正式质量评估，也没有据此冻结 dtype。

Owner decisions 尚未解决：

1. 是否接受 FP32 压力配置中止及四个较大 batch 配置未执行的边界。
2. 审核 warm/增量/fallback 的目标和待测预算；busy/timeout 数字需要 M1 测量，不能冒充 M0 已验证 SLO。
3. 审核 GPU 安全上限、真实 Embedding 生命周期/并发边界及后续资源复核方法。
4. 是否批准进入 M1；本轮没有开始 M1，也没有提交。

生产 `C = RERANKER_CANDIDATE_LIMIT` 仍待 M5 质量/性能选参：K≤C 为 Hybrid(C)→rerank→Top K；K>C 只调用一次 Hybrid(K)，skip reranker，内部原因 `public_limit_exceeds_reranker_capacity`。禁止 C_effective=max(C,K)。M0 没有执行 Hybrid，运行通过的 C=32 不等于生产 capacity 已冻结。

三集合人工 gold、no second Hybrid、all-or-nothing/fail-open、deletion-before-reranker、Context/Citation 最终顺序、Graph 只由最终 Text Context 触发，均保持 canonical 设计。本次没有实现这些生产路径。

Scope conflict：**没有发现需要新依赖、remote code、量化、卸载/修改 Embedding 或生产接线才能获得合理可运行配置的阻塞。** FP16/BF16 共存矩阵成立；FP32 压力配置作为限制提交 Review，不宣称所有配置安全。

## 11. 最终回归与修改边界

| 检查 | 结果 |
|---|---|
| 真实 probe 前 Backend baseline | 1335 passed，28 deselected，1 warning，13.84 s |
| 最终 Probe unit tests | 51 passed，0.35 s |
| Search/Embedding regression（8 个现有文件） | 171 passed，1 warning，5.91 s |
| 最终 Backend full | 1386 passed，28 deselected，1 warning，17.17 s |

Full 命令使用 `python -B -m pytest -q -p no:cacheprovider --basetemp <fresh-local-temp> -m 'not integration and not phase12_local'`。28 个已有 integration 标记测试未执行、未计为通过；没有未解释 FAIL。Starlette/httpx deprecation 是已有 warning，未通过安装依赖消除。真实 BGE 的 1380 warm 样本与 pytest pass 数分别统计。

文件边界：精确模型 `.gitignore`；两个 test-only Python 文件；synthetic fixture；本报告和脱敏 JSON；已有 canonical Design/Plan 的 M0 evidence/待决更新。详细命令与结果见 [regression-results.json](phase-12-m0-results/regression-results.json)。

确认：未修改 `backend/app`、frontend、pyproject、`.env`/`.env.example`、Embedding、Hybrid、Context、Citation、Graph、OpenSearch mapping、migrations 或数据库；未实现 Provider、未接 RAG、未开始 M1、未 commit/push。仅下载指定 BGE 模型，权重不进入 Git。

**AWAITING_PROJECT_OWNER_PHASE12_M0_REVIEW**
