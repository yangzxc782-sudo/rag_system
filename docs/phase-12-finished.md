# Phase 12 completion record

**PHASE12_COMPLETE / PHASE12_M7_ACCEPTED**

本文件记录 Owner 完成类别 Review 后的最终收口。M5已独立提交f6c1c01200ad859cd7555846a96da4b886333205；M6已独立提交df38c7bd8507601e63c5d9c7db515a4de1823a0b，状态PHASE12_M6_ACCEPTED_WITH_OWNER_WAIVER。M7 单独提交，以本文件所属提交为 M7 commit，不混入 M6。

Frozen profile 为 bf16-L1024-C32-B8，profile fingerprint=3c7efd44de1b2c7fece6b142ec58cc41d88f4dadf5160aef9cf439fcd565b5c7；BAAI/bge-reranker-v2-m3 revision=953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e；BF16/CUDA/max_length1024/C32/batch8/timeout5.0s，local Transformers，不改参数。

M6 真实 RAG：11次完整生成、40次使用LLM替身的真实性能请求，Citation/Graph final-context provenance/on-off、K<C/K=C/K>C、51/51 once-Hybrid 和 fallback 均已验收；保留首次LLM失败及恢复证据。

M7唯一Final已完成41条（40 quality + 1 robustness），Final run count=1。总体五项Gate通过，warm p95=908.43ms、incremental p95=908.70ms、GPU sampled peak=4953.56MiB，性能SLO通过。Backend full1808 passed/28 deselected/0 FAIL。

Owner category review: ACCEPTED；decision: CATEGORY_REGRESSION_ACCEPTED_AS_KNOWN_RISK。Final multi_condition nDCG@8从1.000000降至0.992788（delta=-0.007211913337），作为ACCEPTED_KNOWN_RISK_FOR_PHASE12_V1保留；p12-fin-025仍为1.000000 -> 0.963940（-0.036060）。M5 Selection paraphrase历史风险独立保留。风险接受不改变下降事实，不改Gold/qrels，Final未重跑，原始evidence及消费锁原字节保留。

Completion is subject to recorded known risks and deferred manual deletion acceptance. 收口补充安全单元回归48 passed；原Backend full1808 passed/28 deselected/0 FAIL沿用，没有改执行代码。

实际RERANKER_ENABLED=false；actual.env未修改；无生产代码改动，未push。Owner主动删除的两个manual LLM诊断脚本保持删除；原内容仍在M6提交可查。本次运行身份明确绑定此Owner例外。

M6真实destructive deletion仍为NOT_RUN / OWNER_WAIVED_FOR_PHASE12_M6 / MANUAL_ACCEPTANCE_DEFERRED；历史AUTHORIZATION_BLOCKED和mocked PASS保留，不能标记真实PASS。

Project Owner must later execute/inspect the dedicated real destructive deletion acceptance.

完整总体/八类别/表格格式/长段落/逐题下降/性能和证据见[Final evaluation](phase-12-final-evaluation.md)，后续操作见[handoff](phase-12-handoff.md)。
