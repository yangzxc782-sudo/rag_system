# Phase 10 Document Hard Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement recoverable, source-aware hard deletion that removes one Document and every exclusively owned derivative from PostgreSQL, MinIO, and OpenSearch without harming unrelated Documents or shared Knowledge Items.

**Architecture:** A PostgreSQL deletion job is the durable source of intent. FastAPI hosts a disabled-by-default in-process executor that claims one Saga step using PostgreSQL database-time lease, fencing, heartbeat, and bounded step-local retry; external stores are cleaned first, and one final PostgreSQL transaction removes relational derivatives, the Document, and the job. Knowledge provenance is introduced through expand, transactional dual-write, and later composite-FK enforcement.

**Tech Stack:** Python 3.11+, FastAPI, synchronous SQLAlchemy 2.x, Alembic, PostgreSQL/pgvector, MinIO Python SDK, OpenSearch, pytest, Next.js 16 App Router, React 19, TypeScript, ESLint.

**Spec:** `docs/superpowers/specs/2026-08-27-phase-10-document-hard-delete-design.md`

## Global Constraints

- Treat `docs/phase-9-finished.md` as the sole Phase 9 completion baseline.
- Preserve MinerU V4 parsing, block-aware chunking, local `Qwen3-Embedding-0.6B`, dimension 1024, `casting_chunks_current`, hybrid retrieval, RAG REST schemas, and Phase 9 LLM Provider behavior.
- Do not add a public Knowledge Item `sources[]` field or multi-source management API.
- `knowledge_item_sources` is the sole multi-source fact; singular source fields are service-maintained REST compatibility projections.
- `source_filename` is a provenance snapshot; `document_id` is source identity.
- Use PostgreSQL time for every lease/retry scheduling decision. Python wall-clock time is never authoritative.
- Count retries per current step with `step_attempts`; default maximum is 5 and default delays are 5, 30, 120, and 300 seconds.
- Default `DOCUMENT_DELETION_EXECUTOR_ENABLED=false`; tests and rollout enable it only where explicitly stated.
- Every executor database operation owns a fresh `SessionLocal` and closes it in `finally`.
- Every deletion action is idempotent; an already absent target is success.
- A successful final transaction deletes the job; no deletion audit record remains.
- Do not clear buckets, truncate tables, delete/rebuild an index, prune volumes, or operate on existing knowledge-base Documents.
- Each milestone ends at its stop point and waits for project-owner approval. Do not start the next milestone implicitly.
- Do not stage, commit, or push unless the project owner separately authorizes that Git action after milestone review.

## Approved interfaces and file map

The implementation uses these focused modules:

| File | Responsibility |
|---|---|
| `backend/app/models/document_deletion_job.py` | durable job ORM model and persisted status/step constants |
| `backend/app/models/knowledge_item_source.py` | normalized Knowledge Item source fact |
| `backend/app/services/document_deletion_jobs.py` | invariant validation, atomic repository transitions, claim/lease/fencing |
| `backend/app/services/knowledge_sources.py` | dual-write and singular projection maintenance |
| `backend/app/services/document_deletion_manifest.py` | typed minimal manifest, validation, dependency snapshot builder |
| `backend/app/services/document_deletion_minio.py` | version/delete-marker aware object cleanup and verification |
| `backend/app/services/document_deletion_opensearch.py` | DBQ, refresh, Document/Chunk zero-hit verification |
| `backend/app/services/document_deletion_knowledge.py` | stable-lock source cascade, cycle-safe revision closure, snapshot redaction |
| `backend/app/services/document_deletion.py` | schedule, retry, status mapping, Saga step dispatch, final DB transaction |
| `backend/app/services/document_deletion_recovery.py` | expired-lease exhaustion sweep and recoverable-job wake scan |
| `backend/app/services/document_deletion_executor.py` | thread/Event lifecycle, heartbeat, ownership checks, polling |
| `backend/app/services/document_guards.py` | reusable read/write deletion-state guards |

Approved internal signatures are:

```python
def validate_document_job_invariant(
    document: Document | None,
    job: DocumentDeletionJob | None,
) -> None: ...

def claim_next_job(
    db: Session,
    *,
    lease_seconds: int,
    lease_token: UUID,
) -> DocumentDeletionJob | None: ...

def renew_lease(
    db: Session,
    *,
    job_id: UUID,
    lease_token: UUID,
    lease_seconds: int,
) -> bool: ...

def build_document_deletion_manifest(
    db: Session,
    *,
    document: Document,
    settings: Settings,
) -> DocumentDeletionManifest: ...

def delete_minio_targets(
    manifest: DocumentDeletionManifest,
    *,
    client: Minio,
    checkpoint: Callable[[], None],
) -> None: ...

def delete_opensearch_targets(
    manifest: DocumentDeletionManifest,
    *,
    client: SearchEngineClientProtocol,
    checkpoint: Callable[[], None],
) -> None: ...

def apply_source_aware_knowledge_deletion(
    db: Session,
    *,
    manifest: DocumentDeletionManifest,
) -> KnowledgeDeletionResult: ...

class DocumentDeletionExecutor:
    def start(self) -> None: ...
    def wake(self) -> None: ...
    def stop(self) -> None: ...
    def run_once(self) -> bool: ...
```

Public endpoints are fixed as:

```text
DELETE /api/v1/documents/{document_id}
GET    /api/v1/documents/{document_id}/deletion-status
POST   /api/v1/documents/{document_id}/deletion-retry
```

## M0: Canonical documents and Phase 9 completion baseline

### Goal

Make the approved design and this file the only Phase 10 planning authority, and make every project-level reference treat Phase 9 as finished.

### Exact files

- Verify: `docs/superpowers/specs/2026-08-27-phase-10-document-hard-delete-design.md`
- Verify: `docs/superpowers/plans/2026-08-27-phase-10-document-hard-delete-implementation-plan.md`
- Modify: `docs/phase-9-finished.md`
- Modify: `README.md`
- Modify: `docs/local-development.md`
- Modify: `docs/manual-acceptance.md`

### Tests first

- [ ] Record `git branch --show-current`, `git status --short`, `git diff --check`, and the current Alembic head without changing the database.
- [ ] Search `README.md` and `docs/` for legacy Phase 9 review-state wording and for Phase 9 handoff references that do not point to `docs/phase-9-finished.md`.
- [ ] Enumerate `docs/superpowers/plans/*phase-10*` and prove this plan is unique.
- [ ] Check both Phase 10 documents for forbidden placeholders, unclosed fences, duplicate headings, credentials, personal absolute paths, and implementation-complete claims.

### Implementation steps

- [ ] Update `docs/phase-9-finished.md` status and terminal marker to state final acceptance while preserving its as-built evidence.
- [ ] Update the README Phase 9 link to `docs/phase-9-finished.md`.
- [ ] Add a short Phase 10 configuration/operations placeholder to local development only if it describes disabled-by-default behavior and does not claim implementation.
- [ ] Add Phase 10 safe-fixture acceptance headings to manual acceptance; all checkboxes remain unexecuted until their milestone.
- [ ] Cross-link the design and canonical plan in both directions and ensure the design does not duplicate the milestone body.

### Commands

```powershell
git branch --show-current
git status --short
git diff --check
git grep -n -I -E 'phase-9-.*handoff|Phase 9.*(review|pending)' -- README.md docs
Get-ChildItem docs/superpowers/plans -File | Where-Object Name -Like '*phase-10*'
```

### Acceptance criteria

- Exactly one Phase 10 design and one Phase 10 implementation plan exist.
- All Phase 9 links use `docs/phase-9-finished.md`, and no Phase 9 review-pending statement remains in maintained documentation.
- The design is behavior/architecture-focused; this file alone contains M0-M7 execution detail.
- No code, tests, migrations, environment files, or external systems changed.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M0_REVIEW` and wait for approval.

### Rollback strategy

Revert only the M0 documentation edits with an explicit patch; do not reset the working tree or touch earlier accepted documentation content outside the edited paragraphs.

### Forbidden scope

No ORM, service, API, frontend, migration, database, MinIO, or OpenSearch changes.

## M1: Expand migration, source dual-write, and deletion job/state repository

### Goal

Add backward-compatible schema and repository foundations, backfill Knowledge sources, and convert every current Knowledge writer to transactional dual-write without enforcing the composite FK yet.

### Exact files

- Create: `backend/alembic/versions/0007_add_document_deletion_expand.py`
- Create: `backend/app/models/document_deletion_job.py`
- Create: `backend/app/models/knowledge_item_source.py`
- Create: `backend/app/services/document_deletion_jobs.py`
- Create: `backend/app/services/knowledge_sources.py`
- Create: `backend/tests/test_document_deletion_models.py`
- Create: `backend/tests/test_document_deletion_jobs.py`
- Create: `backend/tests/test_knowledge_sources.py`
- Modify: `backend/app/models/document.py`
- Modify: `backend/app/models/knowledge_item.py`
- Modify: `backend/app/models/knowledge_item_chunk.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/services/knowledge_items.py`
- Modify: `backend/app/services/knowledge_extraction.py`
- Modify: `backend/app/core/errors.py`
- Modify: `backend/tests/test_knowledge_item_models.py`
- Modify: `backend/tests/test_knowledge_item_service.py`
- Modify: `backend/tests/test_knowledge_extraction.py`

### Tests first

- [ ] Add metadata tests for `documents.deletion_status`, its three-value check, default `normal`, and index.
- [ ] Add metadata tests for every job column, unique `document_id`, no Document FK, allowed states/steps, checks, and claim index.
- [ ] Add metadata tests for `knowledge_item_sources`, including unique `(knowledge_item_id, document_id)` and `source_filename` non-identity semantics.
- [ ] Add 0007 text/operation tests proving backfill uses the union of singular source and chunk-document pairs and does not add the 0008 composite FK.
- [ ] Add repository tests for the complete Document/job invariant matrix; every invalid pair must raise `DOCUMENT_DELETION_STATE_INCONSISTENT` without mutation.
- [ ] Add PostgreSQL-dialect SQL contract tests proving claim uses `FOR UPDATE SKIP LOCKED`, database `now()`, `step_attempts < max_attempts`, and excludes exhausted expired jobs.
- [ ] Add dual-write tests for create, chunk update, revise, and extraction. Assert source rows and singular projections commit/rollback together.
- [ ] Add source-less manual-item tests proving no source row is required when there is no chunk relation.
- [ ] Run the focused tests and confirm they fail because the migration/models/repository do not yet exist.

### Implementation steps

- [ ] Implement revision `0007_document_deletion_expand` with down revision `0006_add_document_parse`.
- [ ] Add `documents.deletion_status VARCHAR(32) NOT NULL DEFAULT 'normal'`, check constraint, and index without changing `process_status`.
- [ ] Name the Document constraint/index `ck_documents_deletion_status` and `ix_documents_deletion_status`.
- [ ] Create `document_deletion_jobs` exactly as specified: UUID primary key; unique immutable `document_id` without FK; persisted status/current step; `step_attempts`; `max_attempts`; JSONB manifest; lease/retry/error fields; database timestamps; checks and claim index.
- [ ] Name job objects `uq_document_deletion_jobs_document_id`, `ck_document_deletion_jobs_status`, `ck_document_deletion_jobs_current_step`, `ck_document_deletion_jobs_step_attempts`, `ck_document_deletion_jobs_max_attempts`, `ck_document_deletion_jobs_lease_fields`, and `ix_document_deletion_jobs_claimable`.
- [ ] Create `knowledge_item_sources` with UUID primary key, `knowledge_item_id`, identity `document_id`, provenance-snapshot `source_filename`, and database timestamps. Use restrictive/no-action FKs and unique `(knowledge_item_id, document_id)`.
- [ ] Name source objects `uq_knowledge_item_sources_item_document`, `ix_knowledge_item_sources_knowledge_item_id`, and `ix_knowledge_item_sources_document_id`.
- [ ] Backfill one row for every distinct source pair from the union of `knowledge_items.source_document_id` and `knowledge_item_chunks.document_id`. Use the singular filename only for its matching source; otherwise use the Document filename snapshot.
- [ ] Leave source-less items untouched and leave the composite chunk-to-source FK absent.
- [ ] Add ORM relationships that do not cascade the job when a Document is deleted and do not create a circular source FK.
- [ ] Implement `validate_document_job_invariant()` with only the five approved valid combinations.
- [ ] Implement repository transitions with conditional UPDATE predicates and database timestamps. `claim_next_job()` increments `step_attempts` at claim and returns one fenced row.
- [ ] Implement a separate exhausted-lease selector/update that can only mark `processing AND lease_expires_at <= now() AND step_attempts >= max_attempts` failed.
- [ ] Implement idempotent `ensure_source_relation()` and deterministic projection selection ordered by source `created_at, document_id`.
- [ ] Call source dual-write from `create_knowledge_item`, source-chunk update, `revise_knowledge_item`, and extraction through the existing create service, all before the enclosing commit.
- [ ] Keep public request/response schemas unchanged.
- [ ] Make 0007 downgrade abort if active deletion jobs/non-normal deletion states exist or if more than one source cannot be represented by the singular compatibility projection; only then drop the new objects and column.

### Commands

```powershell
cd backend
.\.venv\Scripts\pytest.exe tests/test_document_deletion_models.py tests/test_document_deletion_jobs.py tests/test_knowledge_sources.py tests/test_knowledge_item_models.py tests/test_knowledge_item_service.py tests/test_knowledge_extraction.py -q -p no:cacheprovider
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\alembic.exe check
cd ..
git diff --check
git status --short
```

Do not execute `alembic upgrade` against the shared development database in M1. Real migration application is gated to M7 or a separately authorized disposable PostgreSQL integration environment.

### Acceptance criteria

- 0007 is additive and existing REST contracts remain unchanged.
- Every supported Knowledge write path dual-writes the normalized source fact in its own existing transaction.
- Current source-less manual knowledge remains valid.
- Claim and exhausted-recovery SQL are mutually exclusive and database-time based.
- A Document cannot have two job rows, and deleting the Document cannot cascade-delete its job.
- Focused tests pass; Phase 7-9 Knowledge and LLM regression tests pass.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M1_REVIEW` and wait for approval.

### Rollback strategy

Before any shared migration application, revert M1 files by explicit patch. In an authorized disposable database, downgrade only after its safety preflight passes. Never drop populated source facts or jobs manually.

### Forbidden scope

No composite source FK, external deletion, executor thread, API endpoint, frontend control, or real migration application.

## M2: Minimal manifest and idempotent OpenSearch/MinIO deleters

### Goal

Build and validate the recovery manifest and implement external-store deletion primitives that prove absence rather than assuming a successful request means completion.

### Exact files

- Create: `backend/app/services/document_deletion_manifest.py`
- Create: `backend/app/services/document_deletion_minio.py`
- Create: `backend/app/services/document_deletion_opensearch.py`
- Create: `backend/tests/test_document_deletion_manifest.py`
- Create: `backend/tests/test_document_deletion_minio.py`
- Create: `backend/tests/test_document_deletion_opensearch.py`
- Modify: `backend/app/services/object_storage.py`
- Modify: `backend/app/search_engine/client.py`
- Modify: `backend/app/services/search_index.py`
- Modify: `backend/tests/test_search_engine.py`
- Modify: `backend/tests/test_search_index.py`

### Tests first

- [ ] Add manifest serialization/validation tests for schema version, IDs, exact raw key, derived keys, validated trailing-slash parse-run prefixes, index/alias snapshots, and deterministic ordering.
- [ ] Assert manifest serialization never contains content, source text, version snapshot, vector, prompt, binary bytes, API key, or exception body.
- [ ] Add namespace rejection tests for empty/root prefixes, sibling Document prefixes, parse-run mismatch, traversal, conflicting buckets, and an exact raw-key/prefix collision.
- [ ] Add MinIO fake tests covering unversioned object, multiple versions, delete markers, `NoSuchKey`, partial batch failure, first-success/second-failure retry, and completion listing.
- [ ] Add a long-list test that calls the checkpoint after every 25 object versions.
- [ ] Add OpenSearch fake tests for DBQ by `document_id`, `refresh=true`, conflicts proceed, physical-index missing, alias missing, partial failure, remaining Document hit, remaining Chunk hit, and zero-hit success.
- [ ] Add a regression test that document-scoped index sync still uses stable `_id=chunk_id` and existing mapping.
- [ ] Run focused tests and confirm missing modules cause the expected failures.

### Implementation steps

- [ ] Define immutable `DocumentDeletionManifest` and typed `from_payload()`/`to_payload()` methods with deterministic UUID/key ordering and `schema_version=1`.
- [ ] Build the manifest under the scheduler's Document row lock from persisted Document, parse-run, block, asset, chunk, source, and configured index identifiers.
- [ ] Include every persisted output/asset key plus each validated parse-run prefix so failed pre-metadata intermediate uploads remain targetable.
- [ ] Validate each derived prefix against captured Document and parse-run IDs; never infer deletion from the current `MINERU_OUTPUT_PREFIX` alone.
- [ ] Extend the object-storage wrapper with version-aware listing and version-specific removal; normalize missing object/version responses to success.
- [ ] Enumerate `include_version=True`, delete exact versions and delete markers by `version_id`, invoke checkpoint before each new external call and every 25 processed versions, then relist until empty.
- [ ] Do not use bucket-wide listing without an exact raw key or validated parse-run prefix.
- [ ] Extend `SearchEngineClientProtocol` only with the `count`/refresh surface required for verification.
- [ ] Execute DBQ against the captured physical index with term `document_id`, `conflicts='proceed'`, `refresh=True`, and completion waiting.
- [ ] Treat a missing captured physical index as absence success. If the alias exists, verify it too; a missing alias is not an error after physical verification.
- [ ] Verify zero count by `document_id`, then batch captured `chunk_ids` through a `terms` query and require zero hits.
- [ ] Return stable deletion-specific error codes without raw OpenSearch/MinIO response bodies.

### Commands

```powershell
cd backend
.\.venv\Scripts\pytest.exe tests/test_document_deletion_manifest.py tests/test_document_deletion_minio.py tests/test_document_deletion_opensearch.py tests/test_search_engine.py tests/test_search_index.py -q -p no:cacheprovider
cd ..
git diff --check
git status --short
```

### Acceptance criteria

- Manifest contains all required identifiers and no content or secret.
- MinIO cleanup handles all versions/delete markers and proves the targeted namespace empty.
- OpenSearch cleanup proves both Document and original Chunk identities absent after refresh.
- Missing targets are success; service outage and nonzero verification are retryable failures.
- Existing search mapping, alias, hybrid retrieval, and document sync behavior remain intact.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M2_REVIEW` and wait for approval.

### Rollback strategy

Remove only the new deletion primitives and protocol additions by explicit patch. No external store is contacted by unit tests, so rollback has no data cleanup step.

### Forbidden scope

No real MinIO deletion, real OpenSearch mutation, Saga execution, startup wiring, API, or composite FK.

## M3: Enforce migration, source-aware cascade, and cycle-safe revision closure

### Goal

Enforce the chunk-to-source invariant after dual-write and implement concurrency-safe Knowledge cleanup for shared sources and revision graphs.

### Exact files

- Create: `backend/alembic/versions/0008_enforce_knowledge_item_sources.py`
- Create: `backend/app/services/document_deletion_knowledge.py`
- Create: `backend/tests/test_document_deletion_knowledge.py`
- Create: `backend/tests/test_knowledge_source_enforcement.py`
- Modify: `backend/app/models/knowledge_item_chunk.py`
- Modify: `backend/app/models/knowledge_item.py`
- Modify: `backend/app/models/knowledge_item_version.py` only if the implementation-time integrity recheck requires mapping changes
- Modify: `backend/app/services/knowledge_items.py`
- Modify: `backend/tests/test_knowledge_item_models.py`
- Modify: `backend/tests/test_knowledge_item_service.py`

### Tests first

- [ ] Add 0008 preflight tests for missing chunk/document identity, chunk-to-Document mismatch, missing source relation, duplicate source facts, and successful clean data.
- [ ] Add a legal source-less manual-item case with no chunk relation.
- [ ] Add model tests for the composite FK from `(knowledge_item_id, document_id)` to `knowledge_item_sources` and prove no reverse FK from `knowledge_items` exists.
- [ ] Add A/B/K1/K2 tests: A and B share K1; only A owns K2; deleting A retains K1/B relation/reviews/versions and deletes K2/reviews/versions.
- [ ] Add surviving snapshot redaction tests for `source_document_id`, `source_filename`, and target chunk IDs while preserving content and non-target provenance.
- [ ] Add deterministic projection tests selecting the earliest remaining source.
- [ ] Add parent, child, diamond, and explicit cycle revision graphs; traversal must terminate, lock IDs must be sorted, surviving children must detach only from deleted parents.
- [ ] Add SQL/lock-contract tests for stable sorted `SELECT ... FOR UPDATE`; these tests do not claim real PostgreSQL deadlock proof.
- [ ] Add a schema/writer inspection test that fails if snapshot integrity semantics appear without an approved updater.
- [ ] Run focused tests and confirm they fail before 0008 and cascade implementation.

### Implementation steps

- [ ] Implement revision `0008_knowledge_source_enforce` with down revision `0007_document_deletion_expand`.
- [ ] In preflight, inspect actual nullability and query defensively for null `chunk_id`/`document_id`; fail with a bounded count and no content.
- [ ] Verify every chunk relation points to a chunk belonging to the same `document_id` and has a matching source pair.
- [ ] Do not reject a Knowledge Item merely for having no sources when it has no chunk relation.
- [ ] Add `fk_knowledge_item_chunks_item_document_source` only after all preflight checks pass; downgrade removes only that FK.
- [ ] Reinspect `KnowledgeItemVersion` model, 0005 migration, and snapshot writer immediately before provenance mutation. If integrity semantics now exist, implement and test their established recomputation in the same transaction; otherwise preserve the current direct JSONB redaction design.
- [ ] Build a bidirectional revision closure from manifest-affected item IDs with a visited set and bounded traversal; sort UUIDs before locking all closure items.
- [ ] Under those locks, delete target-document chunk links and source relations, then recount remaining sources.
- [ ] Repoint a survivor's singular projection deterministically and redact only target provenance from every surviving version snapshot.
- [ ] Mark only directly affected zero-source items as orphans; do not classify unrelated source-less manual items.
- [ ] Null self-links from surviving nodes to deleted parents and null internal orphan self-links before deleting orphan chunks, reviews, versions, and items.
- [ ] Use explicit SQL/ORM delete order so `RESTRICT` constraints remain a safety net.

### Commands

```powershell
cd backend
.\.venv\Scripts\pytest.exe tests/test_document_deletion_knowledge.py tests/test_knowledge_source_enforcement.py tests/test_knowledge_item_models.py tests/test_knowledge_item_service.py -q -p no:cacheprovider
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\alembic.exe check
cd ..
git diff --check
git status --short
```

Real two-transaction/no-deadlock validation remains in M7 unless the project owner separately authorizes a dedicated PostgreSQL integration run during M3.

### Acceptance criteria

- 0008 cannot enforce over invalid/drifted data and does not reject valid manual items.
- Composite FK exists only in 0008 and the source table remains the sole fact.
- Shared K1 is preserved; A-only K2 and its derivatives are deleted.
- Revision closure is cycle-safe and all candidate locks use stable UUID order.
- Snapshot provenance redaction cannot invalidate an integrity mechanism silently.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M3_REVIEW` and wait for approval.

### Rollback strategy

Downgrade 0008 removes the composite FK without dropping source facts. Revert cascade code/tests by explicit patch. Never reverse a completed real Document deletion.

### Forbidden scope

No real PostgreSQL integration concurrency claim, external deletion, executor, API, frontend, or startup wiring.

## M4: Saga executor, heartbeat, recovery, and disabled default

### Goal

Implement durable scheduling/Saga orchestration and a process-local executor with database-time claim, heartbeat, fencing, recovery, bounded retry, and safe shutdown, while leaving production startup wiring inactive.

### Exact files

- Create: `backend/app/services/document_deletion.py`
- Create: `backend/app/services/document_deletion_recovery.py`
- Create: `backend/app/services/document_deletion_executor.py`
- Create: `backend/tests/test_document_deletion_service.py`
- Create: `backend/tests/test_document_deletion_saga.py`
- Create: `backend/tests/test_document_deletion_executor.py`
- Create: `backend/tests/test_document_deletion_recovery.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/core/errors.py`
- Modify: `.env.example`
- Modify: `backend/.env.example`
- Modify: `backend/app/services/document_deletion_jobs.py`
- Modify: `backend/app/models/__init__.py` if import ordering needs correction

### Tests first

- [ ] Add settings tests for default disabled, lease 120 seconds, poll 5 seconds, max 5 step attempts, backoff sequence, heartbeat batch 25, and bounded shutdown join.
- [ ] Add schedule tests proving one transaction locks Document, validates invariant, builds manifest, sets `deleting`, creates one job, and rolls back all changes on failure.
- [ ] Add per-step tests proving success advances exactly one step and resets `step_attempts`; failure schedules database-time retry or terminal failure.
- [ ] Add claim SQL tests proving due/expired logic uses database time and exhausted expired rows cannot be reclaimed.
- [ ] Add heartbeat tests for renewal at lease/3 and every 25 MinIO objects, lost token, expired lease, and stale owner rejection.
- [ ] Add executor session tests proving each repository action gets its own session and every path closes it.
- [ ] Add Event tests for wake, poll timeout, stop wakeup, and no fixed sleep.
- [ ] Add startup-recovery service tests for pending, due retry, expired processing, valid processing lease, and exhausted expired processing.
- [ ] Add shutdown tests where MinIO and OpenSearch calls return success after `stop_event`: no current-step advance, success state, or job deletion is allowed.
- [ ] Add partial failure/crash tests at every external step and final DB rollback, using fakes only.
- [ ] Add test configuration that keeps executor disabled for all ordinary existing TestClient suites and enables it only in dedicated executor tests.

### Implementation steps

- [ ] Add validated server settings and examples:

```text
DOCUMENT_DELETION_EXECUTOR_ENABLED=false
DOCUMENT_DELETION_POLL_SECONDS=5
DOCUMENT_DELETION_LEASE_SECONDS=120
DOCUMENT_DELETION_MAX_STEP_ATTEMPTS=5
DOCUMENT_DELETION_RETRY_BACKOFF_SECONDS=5,30,120,300
DOCUMENT_DELETION_HEARTBEAT_OBJECT_INTERVAL=25
DOCUMENT_DELETION_SHUTDOWN_GRACE_SECONDS=5
```

- [ ] Implement scheduling as a single row-lock transaction; a duplicate valid active job is returned rather than recreated.
- [ ] Persist approved steps in order: `delete_opensearch`, `delete_minio_derived`, `delete_minio_raw`, `finalize_postgresql`.
- [ ] After each external step succeeds and ownership is revalidated, transition to `pending` for the next step, reset `step_attempts=0`, and clear lease fields.
- [ ] On failure, use the current claimed attempt to choose database-time retry or terminal `delete_failed`; update the Document only if it still exists.
- [ ] Keep `current_step` on manual retry; reset only its attempts/lease/retry/error fields and set an existing Document back to `deleting`.
- [ ] Implement final PostgreSQL deletion in one transaction: source-aware Knowledge cleanup, chunk/block mappings, remaining Knowledge chunk links, chunks/vectors, assets, blocks, parse runs, Document, and job last.
- [ ] Treat absent manifest-target rows as success. Validate the Document/job matrix before and during finalization.
- [ ] Implement recovery sweep and wake scan as short database operations; do not execute historical jobs synchronously in startup.
- [ ] Implement `DocumentDeletionExecutor` as one daemon thread with `threading.Event.wait(timeout)`, `wake()`, and bounded `stop()`.
- [ ] Generate fencing UUIDs in Python but perform every ownership-time comparison and lease deadline calculation in PostgreSQL.
- [ ] Implement a checkpoint that checks `stop_event`, renews when due, and verifies token ownership. Call it before every new external request and immediately after every response.
- [ ] If shutdown occurs during a request, discard its response for state-progression purposes and leave the processing lease to expire.
- [ ] Do not connect the executor to FastAPI lifespan in M4. Dedicated tests construct it explicitly with enabled settings.

### Commands

```powershell
cd backend
.\.venv\Scripts\pytest.exe tests/test_document_deletion_service.py tests/test_document_deletion_saga.py tests/test_document_deletion_executor.py tests/test_document_deletion_recovery.py tests/test_llm_startup.py -q -p no:cacheprovider
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase10-m4-pytest-tmp
cd ..
git diff --check
git status --short
```

### Acceptance criteria

- Saga resumes safely from every durable step boundary and uses no in-memory task as its fact source.
- `step_attempts` is step-local; claim cannot create a sixth automatic attempt.
- Heartbeat preserves one owner during an operation longer than the initial lease.
- Stop is immediate at the loop level; post-stop external responses cannot progress durable state.
- Ordinary tests spawn no executor thread.
- Default/application examples remain disabled, and `backend/app/main.py` is not yet wired.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M4_REVIEW` and wait for approval.

### Rollback strategy

Keep migrations/schema intact and disable executor configuration. Revert only M4 services/config/tests by explicit patch. Durable jobs remain untouched and are not normalized or deleted.

### Forbidden scope

No FastAPI startup executor, DELETE/status/retry route, business guard, frontend, real external call, or real job execution.

## M5: Guards, API, retrieval isolation, and startup wiring

### Goal

Close every concurrent write/read race, expose the safe asynchronous API, and only then wire the disabled-by-default executor into FastAPI lifespan.

### Exact files

- Create: `backend/app/services/document_guards.py`
- Create: `backend/tests/test_document_deletion_api.py`
- Create: `backend/tests/test_document_deletion_guards.py`
- Create: `backend/tests/test_document_deletion_lifecycle.py`
- Modify: `backend/app/api/v1/documents.py`
- Modify: `backend/app/schemas/document.py`
- Modify: `backend/app/core/errors.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/documents.py`
- Modify: `backend/app/services/document_parsing.py`
- Modify: `backend/app/services/embeddings.py`
- Modify: `backend/app/services/search_index.py`
- Modify: `backend/app/services/hybrid_search.py`
- Modify: `backend/app/services/vector_search.py`
- Modify: `backend/app/services/knowledge_items.py`
- Modify: `backend/app/services/knowledge_extraction.py`
- Modify: `backend/tests/test_documents.py`
- Modify: `backend/tests/test_document_parsing.py`
- Modify: `backend/tests/test_document_parsing_mineru.py`
- Modify: `backend/tests/test_document_embeddings.py`
- Modify: `backend/tests/test_search_index.py`
- Modify: `backend/tests/test_hybrid_search.py`
- Modify: `backend/tests/test_vector_search.py`
- Modify: `backend/tests/test_rag_service.py`
- Modify: `backend/tests/test_knowledge_item_service.py`
- Modify: `backend/tests/test_knowledge_extraction.py`
- Modify: `backend/tests/test_llm_startup.py`

### Tests first

- [ ] Add DELETE API tests: 202 schedule, duplicate 202/current job, explicit-retry-required 409, completed 204, invalid invariant 409, and disabled 503 with zero mutation.
- [ ] Add status tests for all internal-to-safe status mappings, absent Document with existing job, completed 404, safe error code, and no internal fields.
- [ ] Add retry tests for valid reset, duplicate prevention, non-failed conflict, inconsistent state, and disabled zero mutation.
- [ ] Assert API/OpenAPI responses contain no manifest, bucket/key, index/internal ID, stack trace, raw error, or content.
- [ ] Add write-guard races for parse, MinerU persistence, embedding final save, document/all-scope index sync, knowledge create/update/revise, and extraction final save.
- [ ] For each writer, test both orders: writer owns lock first so DELETE waits/includes result; DELETE commits first so writer rejects persistence.
- [ ] Add all-scope index tests proving it enumerates IDs then locks/writes/commits one Document at a time and never holds multiple Document locks across a bulk.
- [ ] Add PostgreSQL vector filter tests for `deletion_status='normal'`.
- [ ] Add hybrid tests proving one short batch Document-status lookup removes deleting/missing candidates without holding a lock through RAG/LLM.
- [ ] Add lifecycle tests: disabled creates no thread/recovery/claim; enabled starts one thread; shutdown signals/wakes/stops executor and still closes LLM provider.
- [ ] Run tests before implementation and verify the new assertions fail for missing guards/endpoints/wiring.

### Implementation steps

- [ ] Implement `require_document_normal()` and `lock_document_normal_for_write()` with stable deletion-state errors.
- [ ] Keep list/detail/status readable for deleting/failed Documents; include `deletion_status` in Document summary/detail schemas.
- [ ] Add the three approved endpoints to the existing documents router; do not add a separate public job resource.
- [ ] Load the Document/job pair and validate its invariant before applying enabled/disabled operation semantics. Invalid pairs always return `DOCUMENT_DELETION_STATE_INCONSISTENT`; valid DELETE/retry requests then return disabled 503 before mutation when the executor is off.
- [ ] Check `DOCUMENT_DELETION_EXECUTOR_ENABLED` before any DELETE/retry mutation. Disabled returns 503, creates/resets nothing, changes no Document, performs no wake, and leaves existing states intact.
- [ ] For enabled DELETE, schedule/return status, commit, then call only the local executor's `wake()` as an optimization.
- [ ] Map `pending/processing` to `deleting`, `retry_wait` to `retrying`, and terminal job state to `delete_failed`.
- [ ] Set `retry_available=true` only for `delete_failed`; it describes state and does not bypass the disabled 503. Return 202 for an absent Document with an active job, and require explicit retry for an absent Document with a failed job.
- [ ] Apply the shared invariant validator to DELETE, status, retry, claim, recovery, and finalization; never auto-repair invalid combinations.
- [ ] Acquire the parsing lock immediately before the first persistent MinIO/DB output write and hold it through commit; remote MinerU computation remains outside the lock.
- [ ] Compute embeddings/LLM extraction outside locks, then acquire stable Document locks and recheck immediately before persistence.
- [ ] Lock one Document during its OpenSearch delete/bulk operation. Refactor all-scope sync to release that lock/transaction before moving to the next ID.
- [ ] Add `Document.deletion_status == 'normal'` to PostgreSQL vector query.
- [ ] Batch candidate Document IDs from hybrid OpenSearch hits into one short PostgreSQL status query, filter results, and close the transaction before RAG context/LLM work.
- [ ] In lifespan, construct/start an executor only when enabled, store it on `app.state`, and stop it before final LLM cache cleanup. When disabled, install no recovery scan and no executor thread.
- [ ] Preserve `create_app(settings=...)` testability so explicit settings control lifecycle without reading a mutable global.

### Commands

```powershell
cd backend
.\.venv\Scripts\pytest.exe tests/test_document_deletion_api.py tests/test_document_deletion_guards.py tests/test_document_deletion_lifecycle.py tests/test_documents.py tests/test_document_parsing.py tests/test_document_parsing_mineru.py tests/test_document_embeddings.py tests/test_search_index.py tests/test_hybrid_search.py tests/test_vector_search.py tests/test_rag_service.py tests/test_knowledge_item_service.py tests/test_knowledge_extraction.py tests/test_llm_startup.py -q -p no:cacheprovider
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase10-m5-pytest-tmp
cd ..
git diff --check
git status --short
```

### Acceptance criteria

- Disabled DELETE/retry semantics are exactly 503/no mutation; status remains readable.
- Every invalid Document/job pair returns `DOCUMENT_DELETION_STATE_INCONSISTENT` without repair.
- No write can persist after deletion-state commit, and all-scope sync locks only one Document at a time.
- New retrieval after deletion-state commit cannot expose the Document even before OpenSearch deletion.
- Existing pre-commit read may finish; RAG never holds a DB lock through LLM generation.
- Executor startup is present but disabled by default and cannot be enabled while any guard test fails.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M5_REVIEW` and wait for approval.

### Rollback strategy

Set executor false first. Revert API/lifecycle/guard edits by explicit patch while retaining schema and durable jobs. Do not normalize `deleting`/`delete_failed` rows; status remains evidence for recovery after the corrected code is redeployed.

### Forbidden scope

No frontend, real DELETE, shared database migration, real executor enablement, or external integration mutation.

## M6: Frontend permanent-delete UX

### Goal

Add explicit confirmation, polling, failure/retry state, and operation disabling without treating HTTP 202 as completion.

### Exact files

- Create: `frontend/components/DocumentDeletionControls.tsx`
- Modify: `frontend/lib/documents.ts`
- Modify: `frontend/components/DocumentTable.tsx`
- Modify: `frontend/components/DocumentDetail.tsx`
- Modify: `frontend/components/DocumentParseButton.tsx`
- Modify: `frontend/components/DocumentEmbeddingPanel.tsx`
- Modify: `frontend/app/documents/page.tsx`
- Modify: `frontend/app/documents/[id]/page.tsx`
- Modify: `docs/manual-acceptance.md`

### Tests first

The repository has no frontend test runner. M6 does not add a framework dependency solely for this feature. The tests-first artifact is the exact manual interaction matrix in `docs/manual-acceptance.md`, followed by TypeScript/ESLint/build gates.

- [ ] Add manual cases for confirmation cancel/confirm, 202 polling, retry-wait display, failed display, retry, completion refresh, disabled 503, network failure, and ordinary-action disabling.
- [ ] Add expected visible copy and API sequence for each case before component implementation.
- [ ] Extend TypeScript API types/functions first; run TypeScript/build and observe missing UI integration without weakening compiler settings.
- [ ] Record pre-existing frontend lint/build failures separately; do not change unrelated components to make the milestone green.

### Implementation steps

- [ ] Add `deletion_status` to `DocumentSummary/DocumentDetail` and add safe status response types.
- [ ] Implement `deleteDocument`, `getDocumentDeletionStatus`, and `retryDocumentDeletion` through the existing envelope parser with no local secret/internal detail.
- [ ] Create one reusable client component for delete button, confirmation dialog, status display, polling, retry, and `router.refresh()`.
- [ ] Render `删除` for normal Documents and the approved confirmation text with `永久删除` as the destructive action.
- [ ] On 202, display `正在删除`, poll status at a bounded interval, and do not remove the row optimistically.
- [ ] Display `retrying` as deletion in progress. Display `删除失败` plus `重试删除` only when the safe status permits retry.
- [ ] Treat status `DOCUMENT_NOT_FOUND` as completion, stop polling, refresh the route/list, and let the server-rendered list remove the row.
- [ ] Display `DOCUMENT_DELETION_EXECUTOR_DISABLED` without changing local deletion state.
- [ ] Disable parse, embedding, and index controls when `deletion_status` is not normal; backend guards remain authoritative.
- [ ] Stop polling on component unmount and avoid overlapping status requests.

### Commands

```powershell
cd frontend
npm.cmd run lint
npm.cmd run build
npx.cmd eslint app/documents/page.tsx "app/documents/[id]/page.tsx" components/DocumentDeletionControls.tsx components/DocumentTable.tsx components/DocumentDetail.tsx components/DocumentParseButton.tsx components/DocumentEmbeddingPanel.tsx lib/documents.ts
cd ..
git diff --check
git status --short
```

### Acceptance criteria

- Confirmation copy and destructive button are exact and cancellation makes no request.
- HTTP 202 is visibly in progress, not complete.
- Deleting/retrying disables ordinary operations; failed offers one explicit retry.
- Successful deletion disappears only after status proves absence and route data refreshes.
- Disabled executor and safe backend errors are handled without optimistic mutation.
- No new frontend dependency or test framework is introduced.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M6_REVIEW` and wait for approval.

### Rollback strategy

Remove the new control and revert only Document-page/client changes. Backend hard-delete remains available for API testing; executor configuration remains unchanged.

### Forbidden scope

No UI framework, global state library, unrelated frontend cleanup, backend behavior change, migration, or real deletion.

## M7: Real concurrency, fault injection, rollout, and final acceptance

### Goal

Prove convergence and isolation against real PostgreSQL, MinIO, and OpenSearch using dedicated Phase 10 fixtures, then execute the controlled rollout gate and publish the final handoff.

### Exact files

- Create: `backend/tests/integration/test_document_deletion_postgresql.py`
- Create: `backend/tests/integration/test_document_deletion_concurrency.py`
- Create: `backend/tests/integration/test_document_deletion_storage.py`
- Create: `backend/tests/integration/test_document_deletion_recovery.py`
- Create: `docs/phase-10-finished.md`
- Modify: `backend/pyproject.toml`
- Modify: `docs/manual-acceptance.md`
- Modify: `docs/local-development.md`
- Modify: `README.md`
- Modify implementation files only when a failing M7 test demonstrates a Phase 10 defect; return the fix to the owning milestone's focused test boundary before continuing.

### Tests first

- [ ] Register an explicit `integration` pytest marker that is excluded from ordinary unit runs.
- [ ] Require owner-provided test configuration for a dedicated PostgreSQL integration database and a pre-provisioned version-enabled MinIO test bucket. Tests never create/drop a database or create/delete a bucket.
- [ ] Generate a unique Phase 10 Document UUID and unique MinIO/OpenSearch prefixes; record all target and unrelated resource sets before writing fixtures.
- [ ] Add real 0006-to-0007-to-0008 upgrade tests and 0008-to-0007 downgrade tests in the dedicated database; verify Alembic head. Exercise 0007 downgrade only in a disposable clean state satisfying its safety preflight.
- [ ] Add two-session atomic claim tests proving only one claimant returns the job.
- [ ] Add expired lease tests for below-limit reclaim and at-limit recovery-to-failed with no extra claim.
- [ ] Add A/B simultaneous deletion of shared K1 and A-only K2. Require no deadlock, K1 deleted only after both sources are gone, and no orphan.
- [ ] Add cyclic revision graph concurrent deletion and require stable completion/no deadlock.
- [ ] Add a MinIO cleanup lasting beyond the initial lease and require heartbeat to prevent a second owner; verify every object version/delete marker is absent.
- [ ] Add OpenSearch DBQ verification by both `document_id` and original `chunk_id`, followed by a normal RAG/search non-recall check.
- [ ] Add failures: OpenSearch outage, MinIO first-success/second-failure, final DB rollback, crash after OpenSearch, crash halfway through MinIO, post-shutdown successful response, automatic exhaustion, manual retry, and already-absent resources.
- [ ] Add idempotent DELETE after completed deletion and require 204.
- [ ] Add unrelated-resource snapshot comparison and fail on any changed unrelated Document/source/object/index hit.

### Implementation steps

- [ ] Run the real tests only after the project owner approves the exact dedicated database, bucket, index target, and fixture UUID namespace.
- [ ] Apply migrations to the dedicated integration database, verify `0008_knowledge_source_enforce` is head, and run integration tests with executor explicitly enabled only inside their controlled process.
- [ ] For MinIO, operate only on the pre-provisioned test bucket and generated prefixes. Leave the bucket in place and empty of the fixture's versions/markers.
- [ ] For OpenSearch, use the configured existing mapping/alias with unique fixture IDs; delete only fixture documents and never create/delete/rebuild an index.
- [ ] Resolve each demonstrated defect with a focused regression test, rerun the owning milestone suite, then rerun the failing integration case.
- [ ] Run complete backend regression, frontend lint/build, Alembic checks, dependency check, Git whitespace/status, and documentation scans.
- [ ] Execute the rollout gate in order:
  1. obtain explicit migration authorization;
  2. apply 0007 and 0008;
  3. verify Alembic head;
  4. start the application with executor false;
  5. verify every guard and DELETE/status/retry disabled behavior;
  6. change only the local runtime setting to executor true;
  7. fully restart the application;
  8. verify recovery thread health;
  9. perform the first real DELETE only on the dedicated Phase 10 fixture.
- [ ] Verify final PostgreSQL counts, MinIO version listings, OpenSearch searches, RAG non-recall, shared knowledge, and unrelated snapshots.
- [ ] Write `docs/phase-10-finished.md` with as-built behavior, exact verified commands/results, safe fixture identities, known limits, and final acceptance marker. Do not include secrets, object contents, or raw error bodies.

### Commands

The following external-write commands require separate project-owner authorization and dedicated test configuration:

```powershell
cd backend
.\.venv\Scripts\alembic.exe heads
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe check
.\.venv\Scripts\pytest.exe -m integration tests/integration/test_document_deletion_postgresql.py tests/integration/test_document_deletion_concurrency.py tests/integration/test_document_deletion_storage.py tests/integration/test_document_deletion_recovery.py -q -p no:cacheprovider
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase10-m7-pytest-tmp
.\.venv\Scripts\python.exe -m pip check
cd ..\frontend
npm.cmd run lint
npm.cmd run build
cd ..
git diff --check
git status --short
git --no-pager diff --stat
```

Migration application command, only after the rollout authorization point:

```powershell
cd backend
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

### Acceptance criteria

- Real PostgreSQL proves single claim, database-time expiry, no sixth attempt, shared-source correctness, and no deadlock under simultaneous A/B deletion.
- Real MinIO proves raw/derived objects, all versions, and delete markers absent while unrelated keys are unchanged.
- Real OpenSearch proves zero hits by Document and every original Chunk ID without index rebuild.
- Crash/fault cases converge after dependencies recover; manual retry resumes the same safe step.
- PostgreSQL has zero target Document/chunk/vector/parse/asset/source/orphan/job rows and no orphan FK.
- Shared Knowledge data is retained exactly when another source remains.
- Upload, parse, embedding, hybrid search, RAG, knowledge extraction, and Phase 9 Provider regressions pass or any pre-existing unrelated failure is separately evidenced without alteration.
- Rollout never enables executor before both migrations, guards, and APIs are verified.
- `docs/phase-10-finished.md` is the sole final Phase 10 handoff.

### Stop point

Stop with `AWAITING_PROJECT_OWNER_PHASE10_M7_FINAL_REVIEW` and wait for final acceptance.

### Rollback strategy

Before any incident response, set executor false and restart so no new claim occurs. Preserve all jobs and Document states. Roll back application code only to a version that still understands the applied schema; do not downgrade while jobs or non-normal Documents exist. A failed external step is recovered by forward repair and idempotent retry, never by restoring deleted content or rebuilding/clearing shared storage.

### Forbidden scope

No existing knowledge-base Document deletion, bucket clearing, table truncation, index rebuild/deletion, volume operation, embedding-model change, unrelated refactor, automatic Git commit, or push.

## Final implementation completion contract

Phase 10 is complete only after M0-M7 receive separate project-owner approvals and the M7 no-residual evidence is recorded in `docs/phase-10-finished.md`. Until then, the presence of these documents means only that design and execution instructions are approved; it does not mean hard deletion is implemented.
