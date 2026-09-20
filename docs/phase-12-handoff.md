# Phase 12 handoff

## Current status

PHASE12_M7_ACCEPTED / PHASE12_COMPLETE。M7正式Final已完整运行一次，不可重跑。M6独立commit=df38c7bd8507601e63c5d9c7db515a4de1823a0b；M5=f6c1c01200ad859cd7555846a96da4b886333205；branch=phase12-bge-reranker。M7独立提交保存报告、tests、脱敏证据和Owner决议，以本交接文件所属提交为M7 commit。

Owner category review已明确ACCEPTED：Final multi_condition nDCG@8下降（1.000000 -> 0.992788，delta=-0.007211913337）作为Phase12 v1已知风险接受。p12-fin-025仍为1.000000 -> 0.963940（-0.036060），不改变评测事实。最新决议见phase-12-m7-results/owner-review-acceptance.json；原始evaluation中的待审状态为历史事实，保持原字节。

Completion is subject to recorded known risks and deferred manual deletion acceptance. Final was NOT rerun. Final run count remains 1. Final held-out must never be rerun for tuning.

## Known risks

1. M5 Selection paraphrase category historical regression：历史已知风险继续保留，Final paraphrase提升不能抹去此事实。
2. M7 Final multi_condition nDCG small regression accepted by Owner：CATEGORY_REGRESSION_ACCEPTED_AS_KNOWN_RISK / ACCEPTED_KNOWN_RISK_FOR_PHASE12_V1，delta=-0.007211913337。

Owner主动删除两个manual LLM诊断脚本，已确认并要求继续M7；删除保持不动。M6提交后clean边界已成立，preflight明确绑定后续两个删除例外，未把当前工作区虚报为clean。

## Frozen candidate

- profile_id=bf16-L1024-C32-B8
- profile_fingerprint=3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7
- model=BAAI/bge-reranker-v2-m3
- revision=953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
- provider=local_transformers；runtime=Transformers AutoTokenizer/AutoModelForSequenceClassification
- BF16/CUDA；max_length=1024；C=32；batch=8；timeout=5.0 seconds
- local_files_only=true；trust_remote_code=false；不下载、不量化、不自动降参
- Owner quality-first decision；M5 Selection paraphrase degradation为已接受的v1风险，历史证据不改写

Profile helper位于backend/tests/phase12_local/selected_profile.py。身份含六个模型/tokenizer文件hash；不能仅凭模型目录名判断revision。public K仍独立于C；K>C保持一次Hybrid(K)、跳过BGE及原fallback reason。

## Evidence and locks

- docs/phase-12-m7-results/preflight.json：完整不可变身份及Final子集fingerprint。
- docs/phase-12-m7-results/final-run-lock.json：唯一许可已消耗，Final run count=1；严禁删除、覆盖或绕过。
- docs/phase-12-m7-results/evaluation.json：41条（40质量+1健壮性）完整结果、same-pool snapshots/scores、质量/性能和完整性。
- docs/phase-12-final-evaluation.md：总体、八类别、Markdown/HTML/长段落、全部类别下降、单题下降及限制。
- docs/phase-12-m7-results/regression.json：M7 focused33；Backend full1808 passed/28 deselected/0 FAIL，以及分组计数。
- docs/phase-12-m7-results/final-audit.json：静态边界、历史artifact和文件SHA。
- docs/phase-12-m6-real-regression.md及phase-12-m6-results：历史LLM失败、授权恢复和Owner waiver；不得覆盖。

Final真实模型计数与普通unit tests分开。正式命令只执行一次；synthetic readiness在消费前完成。M7无远程LLM请求，没有重新运行M5/Selection。后续只读分析现有结果不等于获得再次评测许可。

## Manual follow-up

Real destructive deletion: NOT_RUN / OWNER_WAIVED_FOR_PHASE12_M6 / MANUAL_ACCEPTANCE_DEFERRED。

历史AUTHORIZATION_BLOCKED和mocked deletion-before-model PASS保留。本次没有新增真实删除授权。

**Project Owner must later execute/inspect the dedicated real destructive deletion acceptance.**

必须使用Phase10既有dedicated target、逐项授权、备份/恢复/cleanup机制；不得选择现有8份Phase12 corpus或共享业务数据。

## Enable procedure — documentation only

M7 Owner类别Review已完成，但真实生产启用仍需Owner另行决定。本轮不执行下述启用操作，不修改actual.env，生产RERANKER_ENABLED=false。

将来获准启用时，由Owner在受控部署配置中确认完整参数，而不是直接信任历史.env或单独切true：

```dotenv
RERANKER_ENABLED=true
RERANKER_PROVIDER=local_transformers
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_DEVICE=cuda
RERANKER_DTYPE=bf16
RERANKER_MAX_LENGTH=1024
RERANKER_CANDIDATE_LIMIT=32
RERANKER_BATCH_SIZE=8
RERANKER_TIMEOUT_SECONDS=5.0
```

RERANKER_MODEL_PATH使用已经存在且通过六文件hash校验的本地目录，不复制权重到Git；revision以文件身份校验。不得将public K固定为C；RERANKER_TOP_K是deprecated兼容项，不能用它改变K/C语义。5秒仅为fail-open safety deadline；warm/incremental/busy/timeout/GPU仍遵守2000ms/2200ms/50ms/deadline+100ms/6500MiB。

启用前核验selected_profile指纹、全部文件及runtime依赖、Embedding共存和Owner批准；通过正常backend lifecycle关闭旧进程再启动，令Settings/provider cache按生命周期重新创建，不在每请求路径清缓存。仅使用Development或独立合成query做服务健康检查；不得重跑Final。监测现有fallback原因、延迟和GPU；违规时关闭而非自动降参。

## Disable / rollback procedure — documentation only

Owner需要关闭时在受控运行配置设置RERANKER_ENABLED=false，并通过正常backend lifecycle重启；等待worker自然关闭，不能强杀活跃CUDA或并行创建第二个模型来救请求。关闭后继续原Hybrid/RRF -> Context -> Citation/Graph链路，保留现有API、LLM和检索错误语义。确认生产开关false、无BGE forward，用非Final query核验；不需修改数据库、索引、Graph或回滚Golden。保留所有证据和Final消费锁，不能以部署回滚名义重置评测次数。

## Safe continuation

M7已按Owner最新决议收口并独立提交；不push，不启动新Phase。后续生产启用与deferred真实删除验收由Owner另行决定。收口补充安全unit48 passed，原Backend full1808 passed/28 deselected/0 FAIL沿用；未改执行代码或重新运行Final。任何后续代码变动需独立评审和相关安全回归，绝不自动解锁Final。
