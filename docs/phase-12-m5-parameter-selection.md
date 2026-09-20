# Phase 12 M5 — Parameter Selection

当前状态：**PHASE12_M5_ACCEPTED**；最新Owner决议、冻结profile与指纹见文末 M5 Owner Closure。下述实验阶段记录保留历史时点。

历史记录：状态：**OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW**。负责人要求停止剩余BF16 2048/4096实验，仅总结现有结果。M4已独立提交 `dbce60a07f6166c47b5f5a4e9e00ba274178381a`，其后工作区clean才开始M5。M5未提交，不宣称M5 acceptance完成。

## 现有结果结论

- Stage A：54个profile已全部筛选，25 feasible、29 rejected；FP16 12/27、BF16 13/27通过。1024通过12/12，2048通过6/12，4096通过7/30；C8通过11/12、C16通过8/18、C32通过6/24。0 OOM，12个4096/C32实验在5秒deadline timeout。
- Stage B：已有24份处理记录，22份完整40题质量报告（每题重复两次，1760条成功请求时间/分数记录）；2个4096/C8/full-batch实验因实际Selection显存超限拒绝。FP16该失败项另有80次成功调用的控制流证据，但精确峰值和分数未落盘，不计入1760条完整记录。
- 22份完整报告中：14个总体质量通过但改写类退化，8个C16配置因Recall@8从0.9125降到0.8875而拒绝。没有完全通过所有Gate且无需类别审查的profile，因此不进行生产winner宣告，成本Pareto/排序列表为空。
- 停止前后台已执行BF16/2048与部分4096；停止调度时正在运行的BF16/4096/C8/B2自然结束，调度器和子进程均已退出。`bf16-L4096-C8-B1`没有启动，标记owner stopped/not run，不作hardware或quality失败结论。未重跑任何profile。
- Final检索、评分、指标运行数始终0。最终只读live audit再次确认8docs/423chunks、删除/embedding/index/content身份一致，Golden和Selection snapshot hash未变。

详细54项硬件数值见[硬件报告](phase-12-m5-hardware-screen.md)；22项五指标、八类baseline/variant/delta、HTML/long paragraph切片和性能见[质量报告](phase-12-m5-quality-results.md)。[证据索引](../backend/tests/fixtures/phase12/m5_profile_results.json)只索引原始JSON及SHA，不复制一套事实源。

## Frozen inputs

- Corpus：`c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1`
- Golden：`cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a`
- Selection snapshot：`f5e7830cf93a1f492e57aef8bf98794dd9f0ad0c1dd418ba1140813ee993f3b7`
- 模型：BAAI/bge-reranker-v2-m3 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，本地六文件SHA核验；无下载。
- 首次Selection Hybrid：40道quality × 8/16/32 =120次只读调用，40/40前缀严格相同（ID、顺序、RRF score、keyword/vector rank）。只冻结40份C32有序快照，C8/C16派生，无profile重新检索。
- 仅整体manifest integrity检查读取冻结文件；不查看/评分Final题面和qrels，不生成Final snapshot/score/metric。
- qrels语义沿用Owner审批：本次冻结评测Gold，不是全库穷尽真值；没有改任何query或grade。

## 已声明流程（先于Selection BGE分数）

1. 重新核验实时PG/OpenSearch与M4 corpus一致，确认Gold hash、删除/embedding/index身份。
2. 生成上列Selection快照并禁止刷新。
3. Stage A矩阵固定为54个显式profile：FP16/BF16 × 1024/2048/4096 × C8/16/32；1024/2048分别测试可用full/16/8，4096另外分别测试4/2/1。
4. 每个profile独立实验进程；显式持有一个当前Qwen Embedding实例。进程隔离只属于实验工具，未改生产worker架构。单次run绝不自动缩batch/改dtype/转CPU/卸Embedding。
5. workload只用合成正常文本、长段落、HTML表格风格文本，真实tokenizer将pair填至预算的98%以上。不使用Selection labels或Final文本设计输入。
6. 单独记录冷加载；正式warm deadline=5秒。预加载仅在显式实验中进行，没有startup warmup。
7. 每profile 20 warm、20 deterministic snapshot reranker off/on incremental样本；CUDA同步。出现OOM/超显存等可提前判reject，不能把不足样本当pass。
8. device sampled peak、torch allocated/reserved/peak同时保留；设备采样不能保证捕获间隔中的瞬时峰值，记录实际最大间隔。
9. 全部Stage A完成后冻结feasible list，才运行其所有profile的Selection评分。每profile每题固定重复两次验证确定性；不根据Selection中间结果新增profile。
10. grade>0统一二值相关；Recall分母包含所有冻结相关Gold（即使不在C）；五指标、coverage/Recall上限按C独立报告。
11. baseline保持快照原顺序；BGE exactly同IDs，raw score DESC、original rank ASC、chunk ID ASC；无fusion/threshold/sigmoid。
12. 八类别、long paragraph、table overall/HTML/Markdown分列；Selection Markdown count=0，metrics=null。robustness不进入40题质量分母。

## 固定Gate / 成本规则

- Hardware：finite scores、稳定重复排序、无OOM；warm p95≤2000ms，incremental p95≤2200ms，coexist device sampled peak≤6500MiB。
- busy≤50ms、timeout≤deadline+100ms保持M0 SLO；M3状态机已验，M5不重做极端故障或扩大deadline。
- Primary：nDCG@8和MRR@8必须各自严格高于同C RRF。
- Non-regression：HR1、HR3、Recall8均不得下降。
- 任一类别指标下降列出CATEGORY_REVIEW_REQUIRED；总体通过但类别下降为QUALITY_PASS_WITH_CATEGORY_REVIEW，不自动推荐它为production。
- 完全通过者先Pareto剔除incremental p95与VRAM被支配者，再依次按更低incremental p95、GPU peak、warm p95、更小C；完全同成本交Owner处理。不新增dtype、length或质量提升幅度优先规则。
- batch差异必须报告候选identity、score/rank变化；同语义且通过Gate时按同一性能/显存规则比较。

## 最小生产修改与默认配置

附件第14节授权必要validator扩展。当前真实validator在`backend/app/retrieval/reranker.py:RerankerConfig`，而非附件第62节预期的`core/config.py`。只把合法集合从512/1024扩为512/1024/2048/4096；512兼容保留，8192拒绝。
两份`.env.example`只更新允许值注释，未写最终默认；actual `.env`未修改。RERANKER_ENABLED实际与默认均false。
未修改Hybrid、RAG orchestration、Context、Citation、Embedding、Graph、OpenSearch、DB或frontend。

## 可重复入口

从backend，先普通pytest（无真实gate）。真实实验只用既有`PHASE12_BGE_PROBE_ENABLED=1`；所有模型local_files_only，无下载。

- `parameter_selection.capture_selection(...)`：只读首次采集，输出已存在即拒绝刷新。
- `m5_hardware_screen.run_matrix('../docs/phase-12-m5-results/hardware')`：固定矩阵，逐进程生成profile artifact；已有同identity证据保留，不自动重跑。
- `parameter_selection.run_feasible_selection(hardware_directory, snapshot_path, output_directory)`：要求所有Stage A证据完备且hash匹配，先冻结feasible list，才执行Selection。
- Final没有CLI入口。默认real gate关闭时不创建模型或CUDA推理。

本轮已停止执行真实实验；以上入口只是工具接口说明，不表示授权自动继续或重跑。继续未执行项须遵循负责人后续指令。

## 实验边界与工具修正记录

- 共存是实验中显式持有一个现有Qwen Embedding实例；没有修改目前非进程singleton的生产Embedding factory，不能将数据外推为生产多请求RAG显存。
- Incremental使用同一deterministic Hybrid snapshot，执行生产`optional_rerank_chunks()` off/on差值；排除LLM/Graph和真实OpenSearch网络波动。未改RAG实现。
- 实际本机`.env`仍有旧provider/model值；所有M4/M5实验显式指定local_transformers/BGE正式身份，`.env`保持原样且实际enabled=false。
- 第一项4096实验在构造合成workload时停止，尚无BGE forward：单段token数无法线性推算拼接后token数。新增RED→GREEN后用真实拼接token数有界补足，已完成1024/2048输入和记录不变；固定profile矩阵没有变化。
- 显存或warm SLO已明确失败后允许提前拒绝；其后未执行的incremental测量标not measured，不把不足样本或零样本当通过。
- 实验子进程只为隔离独立profile及资源清理，不作为新增生产inference process架构。没有通过卸Embedding让某一profile通过。
- Selection `fp16-L4096-C8-B8` 在真实变长输入下触发device sampled peak >6500 MiB。初版工具到全部40题各两次完成后才检查并抛错，未落盘精确峰值/raw scores；因此该配置明确记为SELECTION_RUNTIME_REJECTED，精确峰值与质量指标不可恢复，不编造数值、不重跑。Stage A原有证据保持不变，Stage A通过不覆盖Selection实测拒绝。
- 针对此工具缺口补充3个RED→GREEN测试：实际Selection拒绝优先于Stage A、每请求检查历史GPU峰值、续跑必须逐文件验证既存结果hash。只改test harness；保存9份既有质量结果与上述失败记录的continuation ledger，继续附件第23节允许的其他预先声明实验，不新增profile、不重新检索、不重跑失败配置。后续工具在请求边界保存失败的脱敏数值并停止该profile，CUDA不健康仍立即停止整轮。
- 续跑前Backend full：1725 passed /28 deselected /0 FAIL；普通测试gate关闭，不加载真实BGE。唯一warning为既有Starlette/httpx弃用提醒。

## 质量 / 性能摘要与Owner审查

同C RRF Top8五指标相同：HR1=0.725、HR3=0.875、Recall8=0.9125、MRR8=0.80625、nDCG8=0.8286245621；candidate coverage分别C8=0.9125、C16=0.925、C32=0.975，独立计算且不缩小Gold分母。

完整结果中所有Primary Gate均通过，但8个C16项不满足Recall非退化。14个总体通过项均存在paraphrase类别退化，不能自动用总体提升覆盖该问题。qrels保持负责人冻结版本，没有用BGE输出改Gold。

Selection HTML structured_table共5题，Markdown为0题/metrics null；long_paragraph独立5题。没有把Development成绩并入Selection，Selection robustness未执行（没有获完整认可的最终候选），不进入40题质量分母。

同配置重复score最大drift=0，ranking均稳定。不同batch比较中，FP16/1024/C32 B8对B16在`p12-sel-025`有排序差异，score最大drift=0.0078125；不影响该组聚合五指标。其他组排序相同；跨batch最大score drift=0.015625。没有掩盖batch数值差异。

完整Selection报告的请求p50范围122.86–1062.07ms，p95范围252.97–1275.67ms，device sampled peak最高6175.56MiB。Stage A每profile的warm/incremental p50/p95、cold和allocated/reserved peak在硬件报告中逐项列出。设备采样最大间隔558.72ms，无法保证捕获所有瞬时峰值。

BF16/4096/C8/B8在Selection观测device peak=8150.56MiB，超过6500，判拒绝；FP16对应full-batch同样超限但精确峰值缺失。没有将设备级超标归因为已证实的生产内存泄漏，也没有放宽SLO或自动降参。Stage A可行不等于真实变长请求保证。

推荐profile=null；dtype、max_length、C、batch均未冻结。Timeout只验证5.0s候选，不是正式默认。Stage A的12次timeout返回5000.97–5014.92ms，满足deadline+100ms；busy/late-result状态机沿用M3验收，M5没有重新设计或声称新测busy。保持RERANKER_ENABLED=false，不进入M6。

## 测试与变更边界

- Config RED 4失败→GREEN42；M5 harness初始RED29失败，补充workload/warm gate与Selection失败记录RED→GREEN。M5 focused原84项通过，加3项失败恢复覆盖后总87项纳入通过的Backend full；补充Selection focused31 passed。
- M4 focused102 passed；M1–M3/RAG/Graph/Search/Embedding/LLM lifecycle组合回归518 passed。
- 最终Backend full1725 passed /28 deselected /0 FAIL，1条已有弃用warning；真实hardware/Selection实验单独报告，不计入unit数量。
- Production只修改`backend/app/retrieval/reranker.py`长度合法集合一行；根/backend `.env.example`只改允许值注释。没有修改实际`.env`、Hybrid、RAG、Context、Citation、Embedding、Graph、OpenSearch、数据库、frontend。
- 新增/修改test-only hardware/selection工具及测试、快照、结果索引；更新本报告、硬件/质量报告、canonical Design/Plan。
- 没有下载模型或依赖，没有尝试8192/FP32生产/量化，没有改Golden/质量Gate/SLO，没有commit或push M5。
- 无必须扩大生产改动范围的scope conflict；存在实际Selection显存拒绝、类别Review和Owner主动停止形成的未执行项。M5完整acceptance未完成。

## 最终Git与证据核验

Branch=`phase12-bge-reranker`，HEAD=`dbce60a07f6166c47b5f5a4e9e00ba274178381a`。M4独立commit已完成，M5全部改动未暂存、未提交、未push。

`git diff --check`通过；8个tracked文件变更，另91个新增test/docs/JSON证据文件（约10.8 MB）。新增文件另查尾随空白为0；结果artifact SHA全部匹配，结果JSON未发现完整query/content、model_path、password/credential等字段。

Golden、Corpus manifest及Hybrid/Embedding/RAG核心文件diff均为0。已结束的CPU调度器与GPU实验进程均不存在。没有为汇总再次加载模型，停止后只执行只读数据核验与文件汇总。

## M5 Owner Closure — 2026-09-20

**PHASE12_M5_ACCEPTED**。本节覆盖历史的 OWNER_STOPPED_PARTIAL / AWAITING_PROJECT_OWNER_CATEGORY_REVIEW；原始实验记录、自动Gate及失败证据完整保留，未重新计算Selection质量或执行模型实验。

Owner-selected frozen profile：`bf16-L1024-C32-B8`。model=`BAAI/bge-reranker-v2-m3`，revision=`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，provider=`local_transformers`，runtime=Transformers AutoTokenizer + AutoModelForSequenceClassification，local_files_only=true、trust_remote_code=false，dtype=BF16、device=CUDA、max_length=1024、C=32、batch=8、timeout=5.0s。5秒是fail-open safety deadline，不是正常请求延迟SLO。

selection policy = **owner quality-first decision**；不是自动成本排序winner。paraphrase category degradation = **accepted known Phase 12 v1 risk**，必须持续披露；不修改Gold、query、qrel、质量Gate或性能SLO。`bf16-L4096-C8-B1=OWNER_STOPPED_NOT_RUN`，不补跑任何失败、缺失或停止profile，不重跑Selection，不刷新snapshot。Final run count=0，production RERANKER_ENABLED=false，actual .env保持原样。

冻结记录：`backend/tests/fixtures/phase12/selected_profile.json`；test-only helper：`backend/tests/phase12_local/selected_profile.py`。复用既有`corpus_audit.fingerprint()`的sorted-key紧凑UTF-8 JSON/SHA-256格式，identity绑定全部运行参数、runtime/provider、model/revision和M0六个模型/tokenizer文件大小及SHA。确定性生成的profile fingerprint：

`3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7`

原始结果索引/decision仍保留当时自动Gate状态，不覆盖或改写原始证据；本Owner决议与selected_profile为后续M6/M7依据。现有Settings默认值不据此自动启用；integration harness显式构造完整profile并验证指纹。

M5 closure补充15项漂移/旧env隔离测试：15 RED（helper不存在）→GREEN。M5/config/Provider/runtime/RAG及M4 integrity组合321 passed。完整安全回归结果见下方收口验证记录。只提交M5与上下文交接文档，不含M6测试；独立commit后clean才进入M6。M7 Gate与SLO不变。

收口验证：Backend full `-m 'not integration'` 为1740 passed、28 deselected、0 FAIL，1条既有弃用warning；普通pytest real gate=0。200个边界文件SHA核验无变化（包含actual .env、Gold、Corpus、Selection、生产代码和原始实验JSON），82份artifact与索引SHA一致；git diff --check通过。M5收口未新增production diff，生产默认仍disabled；原始实验和Final运行数均未增加。
