# Phase 10 Document Hard Delete 交接

状态：`REAL_ROLLOUT_PENDING`

Phase 10 M0—M6 已分别通过项目负责人验收并形成独立提交。M7-A 已建立非破坏性安全门禁、专用 integration fixtures、跨存储 snapshot/isolation helper、真实 PostgreSQL concurrency 测试代码、storage/fencing/fault-injection 测试代码和 rollout checklist。

本文档当前不是“Phase 10 已完成”的声明。项目负责人尚未给出 `PHASE10_M7_ROLLOUT_AUTHORIZED`；因此 0007/0008 未在本轮执行真实 upgrade/downgrade，未执行真实 Document DELETE，未删除真实 MinIO 对象，也未执行真实 OpenSearch delete-by-query。

## 已实现边界

- PostgreSQL persistent deletion job、Document deletion state 与 invariant matrix；
- DB-time claim/lease/retry、fencing、heartbeat、recovery、graceful shutdown；
- versioned minimal Manifest；
- MinIO all-version/delete-marker hard delete 与验证；
- OpenSearch Manifest∪current target DBQ 与 0-hit 验证；
- `knowledge_item_sources`、dual-write、0008 enforce 与 source-aware Knowledge cleanup；
- cycle-safe revision closure、stable UUID lock order 和 post-delete source recount；
- DELETE/status/retry API、write guards、retrieval isolation、per-Document index lock；
- disabled-by-default Executor startup wiring；
- Document Hard Delete 前端确认、轮询、失败重试和 non-normal UX。

## M7-A 自动化资产

- `backend/tests/test_phase10_integration_safety.py`：双开关、专用 target confirmation、唯一 fixture identity、snapshot isolation fail-closed；
- `backend/tests/integration/phase10_support.py`：安全 gate、resource snapshot、mapping checksum、Alembic dedicated-target helper；
- `backend/tests/integration/phase10_fixtures.py`：只生成 `phase10-hard-delete-<UUID>` 资源，不接受既有 Document ID；
- `backend/tests/integration/test_document_deletion_postgresql.py`：0006→0007→0008、enforce-only downgrade、finalization rollback/retry；
- `backend/tests/integration/test_document_deletion_concurrency.py`：双 claim、expired/fencing/exhausted、A/B shared Knowledge 与 revision cycle concurrency；
- `backend/tests/integration/test_document_deletion_storage.py`：跨存储 isolation、OpenSearch failure/response-loss retry、慢 MinIO heartbeat、lease lost；
- `backend/tests/integration/test_document_deletion_recovery.py`：完整 Saga 0 residual、duplicate DELETE 204、retrieval/RAG isolation。

所有 integration tests 标记为 `integration`，没有 `PHASE10_INTEGRATION_ENABLED=1` 与 `PHASE10_M7_ROLLOUT_AUTHORIZED=1` 时必须 skip。即使双开关存在，缺少 dedicated environment、两个独立 `phase10_*` database confirmation、`phase10-*` versioned bucket confirmation 或专用 OpenSearch confirmation 也会 fail closed，不会 fallback 到普通开发资源。

## M7-B 待授权证据

以下项目必须在单独 rollout 授权后填入实际、可复核结果；不得预填“通过”：

- PostgreSQL backup/restore 记录：待授权；
- 0007/0008 dedicated migration upgrade/downgrade：待授权；
- PostgreSQL real two-transaction/no-deadlock：待授权；
- 专用 MinIO version/delete-marker cleanup：待授权；
- 专用 OpenSearch DBQ/0-hit：待授权；
- unique Phase 10 Document full Saga：待授权；
- PostgreSQL/MinIO/OpenSearch pre/post isolation sets：待授权；
- Vector/Hybrid/RAG no-recall：待授权；
- 最终 backend/frontend 与 Git evidence：待授权后复核。

## Rollout 入口与停止条件

执行入口、环境变量、命令顺序、abort criteria 和人工验收矩阵见：

- `docs/local-development.md` 的“第十阶段”章节；
- `docs/manual-acceptance.md` 的“第十阶段”章节；
- canonical plan：`docs/superpowers/plans/2026-08-27-phase-10-document-hard-delete-implementation-plan.md`。

任何缺少授权、confirmation、专用 target、backup/restore、正确 Alembic head、1024 vector mapping 或 cross-document snapshot 的情况都必须停止；不得自动修复、不得换用共享资源、不得清 bucket/index/database。
