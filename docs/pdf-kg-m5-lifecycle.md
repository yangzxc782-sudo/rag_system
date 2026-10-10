# M5：持久处理与删除生命周期

> S4 更新：当前处理请求使用 BlockChunkerConfig 完整六字段、唯一结构切分版本和配置指纹；
> 本文后半的 M5 初次验收记录保留为历史，不代表已执行 S5 或本轮真实服务验收。

本阶段复用 M0 的 DocumentProcessingJob、SourceDocumentVersion、GraphBuild、KGExtractionUnit、ChunkSet；不新增概念表，不修改旧迁移。任务 checkpoint 的 `pipeline` 保存版本 1 的处理请求（顺序切分配置、解析/模型/向量配置指纹），与原有阶段检查点合并保存。请求 UUID 与输入指纹共同去重；相同请求不得更换配置。

完整入口只接受 PDF，创建持久任务后返回 202。默认关闭的本机执行器逐步执行解析/清洗冻结、构图、切分、向量批次、索引验证和发布；网络调用在 SQL 事务外。处理请求不会启用新版搜索准入。已有新版来源可继续处理；已有 legacy chunk 的 PDF 不自动重解析或迁移。

租约按 Document → Job 的顺序加锁，心跳只续约当前执行器仍有效的租约。成功阶段排队进入下一阶段；失败停止，过期租约不自动接管，只有用户显式重试才允许恢复。取消是协作式的：运行中的阶段完成后停止，不发布后续阶段；过期任务取消保留外部 IO 不确定诊断。模型调用不承诺 exactly-once，已持久化的单元检查点不重复执行。

解析首次开始就将 parse_run_id 写入任务检查点，解析成功与任务 source_version 绑定在同一事务提交；迟到解析不得覆盖新租约。切分始终使用一个连续区间，且 content 等于 canonical 的精确子串。

删除沿用现有 Saga，清单新增版本 2，版本 1 的历史删除任务仍可完成（不属于旧图谱查询兼容）。删除准入先检查没有运行中的处理任务，再取消待执行任务、撤销当前版本指针、增加发布修订号、冻结全部版本资产清单。删除处理中不再准入处理、检索和证据恢复。曾接管过期租约或外部写入结果不确定的任务要求另行核验 IO 静止，不能仅凭租约过期宣布安全删除。

版本 2 清单列出文档所属 source/build/chunk-set/job ID、精确图谱归属和每个物理索引，以及来源、构图、切分的独占对象前缀。扩展原 delete_opensearch 步骤先处理新版索引和图谱，之后保留派生产物、原始对象、SQL 最终化的顺序。外部归属冲突必须停止，禁止全库搜索相似对象或覆盖其他文档。

SQL 最终化继续执行知识条目清理和问答证据脱敏，保留聊天文本/来源删除标记；按引用依赖删除 processing jobs、chunk/block 关联、chunks、chunk sets、KG units、graph builds、source versions，再删除解析资产与文档。仅显式删除文档会触发此路径；re-chunk 不回收历史版本。

首次启用顺序仍为代码/离线验收 → 单独授权的隔离真实验收 → 单独授权准备新版 PDF 资产 → 校验可检索文档数量后启用新版准入。执行器、真实模型、迁移、图谱、索引和存量数据操作均不在本轮实际运行。

## 接口与页面

以下路径均带 `/api/v1` 前缀。

| 接口 | 行为 |
| --- | --- |
| `POST /documents/{id}/process` | `{request_id, config:{max_chunk_chars,min_chunk_chars,overlap_chars,max_table_chars,keep_table_intact,keep_formula_with_context}}`；可省略 config/合法部分补齐，null/旧字段拒绝；202 返回持久任务，配置冲突返回 409 |
| `GET /documents/{id}/processing-jobs` | 最近任务、开关、版本、统计与恢复能力；附权威 segmentation_defaults/segmentation_version |
| `GET /documents/{id}/processing-jobs/{job}` | 只读查询一个任务；不隐式 claim、恢复或触发模型 |
| `POST .../{job}/retry` | 仅失败或已过期的 managed 任务；保留身份和计数，不刷新为新的 parse_run 来伪装 re-chunk |
| `POST .../{job}/cancel` | 排队/失败任务终止；运行任务请求协作取消；索引返回后、SQL 发布前再次检查取消 |
| `POST .../{job}/resume` | 显式接入 M2/M3 queued/failed；失败仍需显式 retry；省略 config 复用冻结配置（未冻结才用默认），null 拒绝，显式不同配置冲突 |
| `POST /documents/{id}/chunk-sets` | 新增 `auto_run`，默认 false；页面在执行器已启用时提交 true，显式 re-chunk 后由后台继续 |
| `GET /documents/{id}/chunk-sets` | 版本、冻结配置及相同的 segmentation_defaults/segmentation_version；不新增 defaults endpoint |
| `POST /documents/{id}/chunk-sets/{set}/advance` | 只接受 retry，拒绝配置覆盖；使用该 set 的冻结六字段、版本与指纹 |

默认值只有后端 BlockChunkerConfig 一处定义，前端表单必须取得有效 defaults 才能提交。
缓存保存请求 UUID、六字段及切分版本；旧/损坏/未知版本缓存须用户显式丢弃，不自动转换或重发。
合法缓存 reload 保留原配置，确认任务不存在后才允许用户显式重发原请求。

已接入后台的任务拒绝手动 advance 并发推进。页面缓存请求 UUID 与配置；网络结果不确定时先 GET，再复用原请求。页面轮询不会自动重试失败或过期任务。取消后的任务不可原地恢复；已发布的旧 ChunkSet 继续可用，未发布资产留待授权清理。重切分后历史版本继续保留。UI 明确区分 ready_empty 成功空图与 failed。

解析仍复用 MinerU 和既有清洗器。原手动解析接口保留来源冻结诊断用途，拒绝与持久任务并行；其解析运行也记录外部写入未决标记，避免失败后的对象写入结果不确定被当作可安全删除。早期无持久任务的卡住解析不自动猜测已停止。

## 本轮文件变化

| 类型 | 文件 | 变化 |
| --- | --- | --- |
| ADD | `backend/app/services/document_processing.py` | 完整处理、配置/请求幂等、接管、重试、取消、解析租约、状态统计 |
| ADD | `backend/app/tasks/document_processing_executor.py` | 默认关闭的执行器、启动发现、短阶段调度、独立 SQL 心跳、异常退避、停止与有界等待 |
| ADD | `backend/app/schemas/document_processing.py` | 请求和公开任务 DTO；不暴露私有检查点、provider endpoint 或密钥 |
| MODIFY | `backend/app/services/document_parsing.py` | parse_run/来源与任务绑定，迟到结果拒绝；未决对象写入标记；修复 MinerU 超时错误构造的多余参数 |
| MODIFY | `backend/app/services/document_graph_builds.py`、`document_chunk_sets.py` | 复用同一持久任务，合并检查点，禁止手动竞争 managed 任务；索引发布前取消检查 |
| MODIFY | `backend/app/api/v1/documents.py`、`app/main.py`、`core/config.py`、`backend/.env.example` | 完整处理 API、任务唤醒、执行器生命周期、默认关闭配置；未修改实际 `.env` |
| MODIFY | `backend/app/models/document_processing_job.py`、`schemas/document_chunk_set.py` | 更新执行契约说明、ChunkSet 增加 job_id/managed/auto_run；不新增数据库字段 |
| ADD | `backend/app/services/document_version_deletion.py` | 新版精确资产清单、索引/图谱归属校验与清理、禁止未确认 IO 的删除 |
| MODIFY | `document_deletion.py`、`document_deletion_manifest.py` | 接入 V2 清单，撤销准入、取消任务、按 FK 依赖最终化；旧删除清单仍按原版本处理 |
| MODIFY | `backend/app/services/knowledge_items.py` | 更新前先锁定/校验所有来源文档，避免 ensure_source_relation 的提前 flush 反转锁顺序 |
| ADD | `frontend/components/DocumentProcessingPanel.tsx`、`frontend/lib/document-processing.ts` | 完整处理、配置、进度、刷新恢复、显式接管/重试/取消 |
| MODIFY | `frontend/app/documents/[id]/page.tsx`、`DocumentChunkSetPanel.tsx`、`DocumentDeletionControls.tsx`、`lib/chunk-sets.ts` | 详情入口、后台重切分、任务竞争提示、完整删除范围和保护原因 |
| ADD / MODIFY | `backend/tests/test_document_processing*.py`、`test_document_version_deletion.py` 及相关旧 fixture | 真实 ORM 的离线流程、故障、任务与精确删除测试；独立授权 PG 测试只收集 |
| KEEP | M0 迁移、锚点/连续区间定义、Hybrid/RRF/BGE、M4 证据模型与 Phase 13 指纹 | 本轮没有改动算法、业务协议或指纹检查；历史报告、锁文件、存量业务数据保持原样 |

没有新增源码 DELETE 清单；Markdown Native 的已有退役改动继续保留。

## 恢复和真实验收边界

外部写入前先保存未决标记，成功阶段提交时清除；异常/超时则保留 `requires_io_reconciliation`。这不是“已确认存在迟到写入”，而是无法证明已停止时的保守阻断。再次处理成功不会自动清除该标记。需要另行授权核验工作进程、外部任务与精确资产，再决定恢复删除；本轮未添加无检查的一键解锁或自动垃圾回收。

默认单机进程内执行器可运行多个实例，SQL 行锁、唯一活动任务索引和 fencing 是执行依据；claim 竞争失败不替换其他执行器状态。每次网络调用均释放业务 SQL 事务。进程退出停止新阶段，允许已开始的阶段在其租约内结束；超过退出宽限后交由过期租约与显式恢复处理，不承诺强杀远程模型请求。

已有版本化资产的 SQL 删除保护触发器保持有效。图谱仅删除验证过的实体和关系，使用精确参数，不使用全库模糊搜索或 `DETACH DELETE`；新增外来关系会令事务失败回滚。索引要求物理名称、完整 `_meta` 归属一致且没有 alias，禁止替换 alias 或清理其他文档。真实图谱清理使用独立 `KG_NEO4J_*` 身份，必须另行核验部署权限。

本轮提前了 Knowledge 更新来源守卫，但不把静态/SQLite 验证宣称为解决全部 Phase 10 并发问题。共享知识、revision closure、删除 claim/finalization 与并行编辑的真实 PostgreSQL 锁和死锁恢复仍是正式启用前的隔离验收门槛。

功能回退只需关闭 processing executor，停止接受新的完整处理请求；不会恢复旧图谱 fallback，也不会删除已冻结来源、构图或切片。新版检索准入继续独立控制。正在执行的阶段须按上述取消/租约边界处理，不能把关闭开关当作外部 IO 已停止的证据。

## 验收记录（2026-10-07）

| 层次 | 本轮结果 | 证据边界 |
| --- | --- | --- |
| 后端综合离线回归 | **1774 passed，57.89s** | tests 根目录非 PostgreSQL 测试，排除独立 casting engine 真实子进程验收；无共享业务数据操作 |
| M5 与直接依赖复核 | **159 passed，8.88s** | 完整处理/接管/取消/重试/重切分、删除清单、旧服务回归；与综合回归重叠，不相加 |
| 最后接口与清理收尾复核 | **70 passed，4.27s** | 包含最新应用 settings 传递、接管、精确图谱清理模板和故障检查；与前两批重叠 |
| 前端 TypeScript | **通过** | `--noEmit --incremental false` |
| 相关前端 ESLint | **通过** | 两个处理面板、删除控件、客户端类型、文档详情页 |
| Git 空白检查 | **通过** | `git -c core.safecrlf=false diff-files --check`；已有 M0–M4 改动继续保留 |
| M5 PostgreSQL 用例 | **3 tests collected；未执行** | 全链路 M0 约束、版本化删除触发器、并发同请求/claim 排他；有独立集群和迁移授权门槛 |
| 浏览器交互/Next build | **未执行** | 本轮只有前端静态验证，不宣称页面 E2E 通过 |
| 真实 PDF/MinerU、模型、embedding、Neo4j、MinIO、OpenSearch | **未执行** | 流程集成使用合成 PDF 返回值和网关替身；不是外部服务或模型语义验收 |
| 数据库迁移/部署/存量处理 | **未执行** | 本轮无新增迁移，M0 的 0013 没有在此任务执行；没有重启服务、改 `.env`、提交 commit、删除业务数据 |

命令（在 `D:\rag_system\backend` 执行，每次 basetemp 为本轮唯一目录）：

```powershell
$m5Temp = Join-Path (Get-Location) ('.test-artifacts/m5-regression-' + [guid]::NewGuid().ToString('N'))
$m5Tests = Get-ChildItem tests -File -Filter test_*.py | Where-Object { $_.Name -notmatch 'postgresql|^test_casting_engine\.py$' } | ForEach-Object { $_.FullName }
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $m5Temp --tb=short @m5Tests

$m5Temp = Join-Path (Get-Location) ('.test-artifacts/m5-final-' + [guid]::NewGuid().ToString('N'))
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $m5Temp --tb=short tests/test_document_processing.py tests/test_document_processing_api.py tests/test_document_processing_executor.py tests/test_document_version_deletion.py tests/test_kg_v2_builds.py tests/test_chunk_set_builds.py tests/test_document_deletion_service.py tests/test_document_deletion_manifest.py tests/test_knowledge_item_service.py tests/test_pdf_source_admission.py

$m5Temp = Join-Path (Get-Location) ('.test-artifacts/m5-close-' + [guid]::NewGuid().ToString('N'))
./.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp $m5Temp --tb=short tests/test_document_processing.py tests/test_document_processing_api.py tests/test_document_processing_executor.py tests/test_document_version_deletion.py tests/test_chunk_set_api_context.py tests/test_document_deletion_manifest.py

./.venv/Scripts/python.exe -B -m pytest --collect-only -q -p no:cacheprovider tests/test_document_processing_postgresql.py
```

前端命令（`D:\rag_system\frontend`）：

```powershell
node node_modules/typescript/bin/tsc --noEmit --incremental false
node node_modules/eslint/bin/eslint.js components/DocumentProcessingPanel.tsx components/DocumentChunkSetPanel.tsx components/DocumentDeletionControls.tsx lib/document-processing.ts lib/chunk-sets.ts app/documents/[id]/page.tsx
```

最初回归发现旧路由测试替身没有 processing_lease/settings 参数、旧 manifest 测试把现在支持的版本 2 当作未知版本、旧生命周期测试未提供默认关闭标志。已调整测试 fixture 或默认关闭读取，并使用新的真实 ORM 流程覆盖并行守卫；没有通过移除保护条件来让测试通过。另有一次命令引用了不存在的 test_knowledge_items.py，没有运行测试，随后使用实际 test_knowledge_item_service.py 完成验收。

本轮核心证明：首次完整任务只创建一个 job；解析成功时 chunk 数为 0；图谱 ready/ready_empty 后才创建 chunk；失败不会静默前进；模型单元检查点复用；任务发现可以跨执行器实例恢复，失败不自动重试；取消发生在索引网络返回后仍阻止 SQL 发布；三套顺序重切分配置都不调用解析/抽取/图谱 writer；正常删除撤销当前版本并按依赖顺序清理，外来图谱归属、索引 alias 或未决 IO 均拒绝执行。

对原验收目标的回答：

- PDF-only 和旧 Markdown/图谱运行路径退役：沿用已验收 M1/M4，本轮综合回归通过，未添加旧 fallback。
- 新包 table/clause 和匹配新查询：沿用已验收 M2/M4，协议/模板/来源检查保持；未重写参考包或真实图谱。
- 解析清洗 → 构图 → 切分 → 向量化 → 发布：M5 已在代码中以持久任务串联，并有离线集成证明顺序；真实端到端尚未验收。
- 只改顺序切分不重构图：M5 worker 的三套配置测试通过，原图谱/单元身份保留，相关调用为零；这是离线证明。
- 锚点不进入正文、最终前缀约束：M3/M4 实现保持，相关回归通过；本期没有引入多区间或投影框架。
- 多轮问答、引用、存量边界：Phase 13 与历史引用回归通过，re-chunk 不删历史版本；显式删除继续脱敏证据并保留聊天文本。真实并发/外部删除仍需隔离验收，未自动迁移或清理存量。

后续需要单独授权：隔离 PostgreSQL 迁移及并发验收、真实模型/PDF/图谱/索引链路、版本化删除的外部服务验收、准备存量 PDF 新版资产、正式启用处理执行器及检索准入，以及存在未决 IO 任务的人工核验/回收。M5 到此停止，不自动进入其他里程碑。
