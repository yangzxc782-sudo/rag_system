# Phase 12 M4 — Frozen Golden Dataset

状态：**PHASE12_M4_ACCEPTED**。2026-09-16 项目负责人已原样批准120道质量题、3道robustness及全部query/category/split/group/qrel/grade/source/topic/table format。
审核引用：`Phase12 M4 owner approval`；reviewer_role=`project_owner`。本次仅更改review metadata与冻结状态，实质字段hash前后相同。

## 1. 唯一事实源与审核语义

- [golden_manifest.json](../backend/tests/fixtures/phase12/golden_manifest.json)：123道题全部review_status=approved，qrel_completeness_status=approved，5个groups同样approved。
- annotation_status保留assistant_evidence_draft，明确标注起源；review字段记录后续真实Owner批准。
- 当前qrels是 **Phase 12 frozen evaluation gold**，不是“穷尽全知识库所有可能相关证据”的全球真值；未标注项不是人工确认不相关。
- 149 qrels：grade0=11、grade1=12、grade2=126。完整query/qrel与证据位置只维护在canonical manifest，本文不建立第二份题目事实源。
- 已批准原样保留的边界：JB/T5106上传原文是XXXX征求意见稿；Markdown合并表头等解析限制仍属于本次冻结语料特征；批准不等于认证标准版本或纠正原始解析。

## 2. Frozen identity

- CORPUS_FINGERPRINT：`c4e5ffcc12e119e8e3b36d3750ed80e1a0945cc4dfcb400cd8eb5b21762412c1`
- FROZEN_GOLDEN_FINGERPRINT：`cdbe63039b4d9a674b2fca3ff7ab1bb3fe941c88c06bec557d165f21b4294c2a`
- Development prefix snapshots：`2c84ef7368a4135b7648cdd52286df01182b84dfb9ca2b7744d05addb5a641eb`
- Development smoke C8 snapshots（含独立robustness）：`f53ac71b9d5623e1083a367d965a3f988e23b8c90e3a2f79968d433300aed3d9`
- Golden digest覆盖整个canonical JSON，唯独排除digest字段自身；query/split/category/groups/qrels/grade/hash/review变更都会使验证失败。
- Corpus code pin为M3 HEAD `97bf6031292853c334d68fe926270ddd344410c1`。后续测试/文档commit单列为实验执行代码身份，不通过改写语料pin掩盖数据变化。

## 3. Fresh read-only integrity gate

冻结前和真实smoke后重新读取8份Document、423个DocumentChunk；所有内容hash、metadata、parse/embedding identity、状态与旧目录一致。
全部normal、Qwen3-Embedding-0.6B、实际1024维；逐chunk OpenSearch lexical内容和1024维finite vector核对423/423。
alias/concrete index/UUID/version/creation_date/mapping SHA一致。PG明确只读事务；OpenSearch仅查询，无存储修改。
query/qrel identity、source/document/topic交集、相同原文件/正相关内容hash、normalized near-duplicate、stale corpus检查全通过。
具体实时审计时间及前后报告见[smoke artifact](phase-12-m4-development-smoke.json)。

## 4. Split/category/table coverage

|类别|Development|Selection|Final|
|---|---:|---:|---:|
|definition|5|5|5|
|paraphrase|5|5|5|
|exact_identifier|5|5|5|
|numeric|5|5|5|
|multi_condition|5|5|5|
|long_paragraph|5|5|5|
|structured_table|5|5|5|
|hard_negative|5|5|5|
|质量题|40|40|40|
|独立robustness|1|1|1|
|第7类Markdown|5|0|0|
|第7类HTML|0|5|5|

|group_id|split|质量题|robustness|
|---|---|---:|---:|
|dev-steel-materials|development|31|1|
|dev-gating-flow|development|9|0|
|selection-geometry-core|selection|40|1|
|final-aluminium-acceptance|final|12|0|
|final-machine-safety|final|28|1|

同原文多格式不拆source；Selection/Final无共享source/document/topic。Markdown仅一个原始source，保留在Development；Selection/Final Markdown slice为count0/null。
HTML是正式合法第7类，不按语法排除；表格题需有实际行列/单元格证据依赖。

## 5. Development real smoke（不用于选参）

固定：BAAI/bge-reranker-v2-m3 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`；六个模型文件hash复核。
production RerankingService + LocalCrossEncoderProvider，local-only；FP16/C8/batch8/max_length1024/timeout5s。
只通过显式`PHASE12_BGE_PROBE_ENABLED=1`运行；普通unit零模型。load=1，41/41成功；Selection/Final run=0。
40道质量题使用已有同一C8快照；1道Development robustness首次补只读Hybrid8/16/32前缀审计，实际评测只用C8。
原始RRF列表作为baseline；variant exactly同IDs；保留raw score，未用sigmoid/fusion。

|指标|RRF C8|BGE C8|
|---|---:|---:|
|HitRate@1|0.825000|0.950000|
|HitRate@3|0.975000|1.000000|
|Recall@8|0.987500|0.987500|
|MRR@8|0.892500|0.970833|
|nDCG@8|0.912238|0.973929|
|candidate coverage / Recall ceiling|0.987500|0.987500|

这些数值只证明Gold→snapshot→真实production provider→report完整连通，不是M5质量Gate，不选生产参数。robustness单独计数且metrics=null，不进入40题均值。
最初两次尝试在首请求前因测试profile继承本机旧provider/model配置返回configuration_invalid，未发生BGE load；安全诊断定位后新增RED→GREEN，只修正test-only显式model/provider配置。`.env`未修改，Gold未修改，没有根据排名调参。

## 6. Tests / reproducibility

- 原M4 TDD：metrics/corpus/evaluation/token/snapshot与完整identity/leakage检查。
- Finalization新增RED 5 failures→GREEN 5；旧env配置隔离RED 1→GREEN，最终收口工具6 tests。
- M4 focused：102 passed。
- M1/M2/M3非真实、RAG/Hybrid/Embedding/lifecycle：410 passed。
- Search/vector/index/embedding/API LLM lifecycle：140 passed。
- Backend full：1670 passed / 28 deselected / 0 FAIL（普通run零真实BGE）。
- 既有Starlette/httpx弃用warning1；没有安装或修改依赖。

运行目录backend，使用`.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp <unique>.tmp -m "not integration and not phase12_local"`。
真实smoke单独设置既有gate，再调用`tests.phase12_local.finalization.run_development_smoke('../docs/phase-12-m4-development-smoke.json')`。
该入口先验证已审核的frozen Gold与实时语料，复用生产模型，不下载；输出只有身份/分数/数值，未复制正文。

## 7. M4 commit boundary

只提交tests/fixtures及M4报告、canonical Design/Plan。production diff=0；未修改Hybrid/Reranker/Embedding/RAG/Graph/OpenSearch/数据库/frontend/环境配置。
本次M4允许独立提交：`test: freeze phase 12 reranker evaluation dataset`。提交后clean才进入M5；M5变更不得混入。
尚未执行Selection参数比较或Final；未尝试8192；未冻结生产C/batch/max_length/timeout；没有push。

**PHASE12_M4_ACCEPTED**
