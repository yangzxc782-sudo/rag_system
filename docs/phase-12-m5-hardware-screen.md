# Phase 12 M5 — Hardware / Runtime Feasibility

54个已声明profile全部完成，25 feasible、29 rejected；FP16=12/27 feasible，BF16=13/27 feasible。
0 CUDA OOM；12个4096/C32 profile在5秒deadline timeout，均拒绝进入质量阶段；没有CUDA不健康事件。

Matrix fingerprint：`eaccd156aade33d1dadc7a8c1c6e7db167e336c4ba91afeaaadf64c2c7ef7b73`。

Feasibility fingerprint：`196a02dd33771d7d695a02eeabafe696841497c9c59933fac46a8e1864f43742`。

## 分组摘要

|dtype|length|feasible / tested|
|---|---:|---:|
|fp16|1024|6/6|
|fp16|2048|3/6|
|fp16|4096|3/15|
|bf16|1024|6/6|
|bf16|2048|3/6|
|bf16|4096|4/15|

|C|feasible / tested|
|---:|---:|
|8|11/12|
|16|8/18|
|32|6/24|

## 全部实测

|profile|warm n|warm p50 / p95 ms|incremental n|incremental p50 / p95 ms|allocated peak MiB|reserved peak MiB|device sampled peak MiB|结果|
|---|---:|---:|---:|---:|---:|---:|---:|---|
|fp16-L1024-C8-B8|20|237.70 / 244.99|20|238.78 / 240.34|3549.12|3664.00|4819.56|FEASIBLE|
|fp16-L1024-C16-B16|20|488.54 / 492.93|20|490.99 / 513.17|3725.24|3936.00|5091.56|FEASIBLE|
|fp16-L1024-C16-B8|20|525.11 / 574.41|20|482.70 / 536.33|3549.12|3664.00|4819.56|FEASIBLE|
|fp16-L1024-C32-B32|20|986.00 / 1000.06|20|996.30 / 1004.77|4077.49|4480.00|5635.56|FEASIBLE|
|fp16-L1024-C32-B16|20|981.19 / 1016.64|20|985.85 / 1057.42|3725.24|3936.00|5091.56|FEASIBLE|
|fp16-L1024-C32-B8|20|958.70 / 1077.82|20|985.14 / 1055.71|3549.12|3664.00|4819.56|FEASIBLE|
|fp16-L2048-C8-B8|20|650.21 / 719.89|20|609.24 / 675.49|3789.24|4000.00|5155.56|FEASIBLE|
|fp16-L2048-C16-B16|20|1217.47 / 1226.55|20|1221.71 / 1270.24|4205.49|4608.00|5763.56|FEASIBLE|
|fp16-L2048-C16-B8|20|1213.25 / 1218.83|20|1216.97 / 1219.31|3789.24|4000.00|5155.56|FEASIBLE|
|fp16-L2048-C32-B32|0|not measured|0|not measured|5037.99|5824.00|8150.56|gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L2048-C32-B16|20|2443.22 / 2652.28|20|2505.06 / 2679.10|4205.49|4608.00|5763.56|warm_latency_rejected, incremental_latency_rejected|
|fp16-L2048-C32-B8|20|2431.70 / 2487.59|20|2437.71 / 2664.66|3789.24|4000.00|5155.56|warm_latency_rejected, incremental_latency_rejected|
|fp16-L4096-C8-B8|20|1799.32 / 1883.23|20|1846.12 / 1859.19|4333.49|4608.00|5763.56|FEASIBLE|
|fp16-L4096-C8-B4|20|1680.01 / 1733.24|20|1844.07 / 1927.80|3853.24|4000.00|5155.56|FEASIBLE|
|fp16-L4096-C8-B2|20|1813.19 / 2038.01|20|1826.92 / 1896.11|3613.12|3696.00|4851.56|warm_latency_rejected|
|fp16-L4096-C8-B1|20|1539.48 / 1553.15|20|1581.99 / 1760.79|3461.05|3516.00|4671.56|FEASIBLE|
|fp16-L4096-C16-B16|0|not measured|0|not measured|5293.99|5824.00|8150.56|gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L4096-C16-B8|20|3702.73 / 3843.96|20|3359.58 / 3396.46|4333.49|4608.00|5763.56|warm_latency_rejected, incremental_latency_rejected|
|fp16-L4096-C16-B4|20|3699.89 / 3940.26|0|not measured|3853.24|4000.00|5155.56|warm_latency_rejected, incremental_samples_invalid|
|fp16-L4096-C16-B2|20|3228.92 / 3237.69|0|not measured|3613.12|3696.00|4851.56|warm_latency_rejected, incremental_samples_invalid|
|fp16-L4096-C16-B1|20|3077.04 / 3084.24|0|not measured|3461.05|3516.00|4671.56|warm_latency_rejected, incremental_samples_invalid|
|fp16-L4096-C32-B32|0|not measured|0|not measured|7214.99|8256.00|8150.56|timeout, gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L4096-C32-B16|0|not measured|0|not measured|5293.99|5824.00|8150.56|timeout, gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L4096-C32-B8|0|not measured|0|not measured|4333.49|4608.00|5763.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L4096-C32-B4|0|not measured|0|not measured|3853.24|4000.00|5155.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L4096-C32-B2|0|not measured|0|not measured|3613.12|3696.00|4851.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|fp16-L4096-C32-B1|0|not measured|0|not measured|3461.05|3516.00|4671.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L1024-C8-B8|20|248.59 / 264.08|20|246.32 / 254.33|3549.12|3664.00|4819.56|FEASIBLE|
|bf16-L1024-C16-B16|20|503.34 / 511.49|20|504.78 / 511.60|3725.24|3936.00|5091.56|FEASIBLE|
|bf16-L1024-C16-B8|20|501.85 / 545.54|20|499.50 / 517.82|3549.12|3664.00|4819.56|FEASIBLE|
|bf16-L1024-C32-B32|20|1000.56 / 1015.09|20|1005.31 / 1019.52|4077.49|4480.00|5635.56|FEASIBLE|
|bf16-L1024-C32-B16|20|999.38 / 1031.44|20|1003.31 / 1014.27|3725.24|3936.00|5091.56|FEASIBLE|
|bf16-L1024-C32-B8|20|997.74 / 1017.34|20|1001.06 / 1012.42|3549.12|3664.00|4819.56|FEASIBLE|
|bf16-L2048-C8-B8|20|630.94 / 647.21|20|632.73 / 641.32|3789.24|4000.00|5155.56|FEASIBLE|
|bf16-L2048-C16-B16|20|1267.13 / 1279.53|20|1269.06 / 1302.10|4205.49|4608.00|5763.56|FEASIBLE|
|bf16-L2048-C16-B8|20|1269.00 / 1324.94|20|1267.71 / 1282.88|3789.24|4000.00|5155.56|FEASIBLE|
|bf16-L2048-C32-B32|0|not measured|0|not measured|5037.99|5824.00|8150.56|gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L2048-C32-B16|20|2535.86 / 2545.50|0|not measured|4205.49|4608.00|5763.56|warm_latency_rejected, incremental_samples_invalid|
|bf16-L2048-C32-B8|20|2536.01 / 2547.08|0|not measured|3789.24|4000.00|5155.56|warm_latency_rejected, incremental_samples_invalid|
|bf16-L4096-C8-B8|20|1763.80 / 1793.06|20|1766.19 / 1775.43|4333.49|4608.00|5763.56|FEASIBLE|
|bf16-L4096-C8-B4|20|1762.86 / 1779.59|20|1766.46 / 1793.49|3853.24|4000.00|5155.56|FEASIBLE|
|bf16-L4096-C8-B2|20|1727.79 / 1742.27|20|1731.53 / 1748.64|3613.12|3696.00|4851.56|FEASIBLE|
|bf16-L4096-C8-B1|20|1667.06 / 1675.25|20|1671.67 / 1680.61|3461.05|3516.00|4671.56|FEASIBLE|
|bf16-L4096-C16-B16|0|not measured|0|not measured|5293.99|5824.00|8150.56|gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L4096-C16-B8|20|3534.76 / 3554.99|0|not measured|4333.49|4608.00|5763.56|warm_latency_rejected, incremental_samples_invalid|
|bf16-L4096-C16-B4|20|3529.86 / 3543.86|0|not measured|3853.24|4000.00|5155.56|warm_latency_rejected, incremental_samples_invalid|
|bf16-L4096-C16-B2|20|3438.24 / 3876.45|0|not measured|3613.12|3696.00|4851.56|warm_latency_rejected, incremental_samples_invalid|
|bf16-L4096-C16-B1|20|3341.08 / 3376.64|0|not measured|3461.05|3516.00|4671.56|warm_latency_rejected, incremental_samples_invalid|
|bf16-L4096-C32-B32|0|not measured|0|not measured|7214.99|8256.00|8150.56|timeout, gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L4096-C32-B16|0|not measured|0|not measured|5293.99|5824.00|8150.56|timeout, gpu_peak_rejected, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L4096-C32-B8|0|not measured|0|not measured|4333.49|4608.00|5763.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L4096-C32-B4|0|not measured|0|not measured|3853.24|4000.00|5155.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L4096-C32-B2|0|not measured|0|not measured|3613.12|3696.00|4851.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|
|bf16-L4096-C32-B1|0|not measured|0|not measured|3461.05|3516.00|4671.56|timeout, warm_samples_invalid, incremental_samples_invalid, score_invalid|

## Contract、异常与局限

- 使用production loader/provider/service，本地六文件SHA与M0完全一致；FP16/BF16；eval/inference_mode；strict [N,1]与finite/identity检查由生产Provider执行。
- 全部54项模型load成功；25可行项均完成20 warm +20 off/on incremental样本，20组重复raw score/rank稳定。未达到完整Gate的项从未进入Selection。
- 固定模型revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e；torch2.11.0+cu128、transformers4.57.6、sentence-transformers5.6.0、CUDA12.8、RTX5060 Laptop。
- 每个独立实验进程显式持有一个原Embedding实例，正常forward期间未卸载/改device/改模型；不是生产RAG多请求生命周期的显存保证。
- 合成正常文本、长段落、HTML表格风格workload，pair长度>=对应max_length的98%，没有读取Final query/qrels设计输入。
- cold_model_load_ms包含模型/tokenizer载入、to(cuda)、eval与同步；冷加载不混入warm SLO。各项冷加载和首次请求时间保留在原始JSON。
- first_forward_wall_ms字段对timeout项表示首次调用的返回时间，不是后台CUDA完成时间；没有声称杀死CUDA forward。
- 12项timeout首次调用返回范围 5000.97–5014.92 ms；deadline=5000ms，均未增加deadline。
- 显存超标或warm SLO已失败可提前reject，后续未执行测量明确标not measured；不能用不足样本取得feasible。
- 设备目标10ms采样，最大实际间隔558.72ms；采样不能保证捕获间隔内瞬时设备峰值。torch allocator peak另行完整记录。
- 第一次4096输入构造在forward前被token长度Gate拒绝；修复拼接token非线性假设并新增RED→GREEN后继续。profile矩阵未变，已完成输入和记录未刷新。
- 后续只跳过已确定warm SLO不通过项的增量测量，未调整任何feasible阈值或profile。
- GPU超标/timeout在Stage A是该profile拒绝依据，不是自动修改生产参数的理由。未发生量化、CPU fallback、自动降dtype、batch缩小或Embedding卸载。
- 本报告不选最终参数；Stage B才计算Selection质量。8192和FP32生产候选均排除。

原始数值见[hardware evidence](phase-12-m5-results/hardware/matrix.json)同目录各profile JSON；报告为派生展示，immutable raw artifacts是实验依据。
