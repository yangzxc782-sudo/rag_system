# Phase 10 Document Hard Delete Design

**Status:** `PHASE10_PLAN_APPROVED`

**Phase 9 baseline:** `docs/phase-9-finished.md`

**Canonical implementation plan:** `docs/superpowers/plans/2026-08-27-phase-10-document-hard-delete-implementation-plan.md`

## 1. Purpose

Phase 10 adds recoverable hard deletion for one Document. A successful deletion removes the Document and every derivative that belongs only to it from PostgreSQL, MinIO, and OpenSearch. The deletion must converge after process crashes, ambiguous external responses, network failures, or temporary storage outages, without deleting data still required by another Document.

The feature is a hard delete. Hiding a row, removing only the original PDF, or removing only PostgreSQL records is not success. A completed deletion has no recoverable business copy inside the systems covered by this phase.

This design is authoritative for behavior and architecture. The canonical implementation plan contains the only M0-M7 task breakdown; this document intentionally does not reproduce those milestones.

## 2. Goals and non-goals

### 2.1 Goals

- Persist deletion intent in PostgreSQL before returning HTTP 202.
- Remove all OpenSearch chunk documents for the target `document_id` and original `chunk_id` values.
- Remove all versions and delete markers of the target raw MinIO object and all objects under each target MinerU parse-run prefix.
- Remove document-owned PostgreSQL rows, including chunks and their inline vectors, parse intermediates, mappings, and source relations.
- Delete a Knowledge Item only when removal of the target source leaves it without another source.
- Preserve shared Knowledge Items, their reviews and versions, while removing or redacting provenance that belongs to the deleted Document.
- Make every Saga step idempotent and safe to resume after a crash.
- Support multiple FastAPI processes through PostgreSQL atomic claim, lease, fencing, and heartbeat.
- Prevent parse, embedding, indexing, extraction, and new retrieval from reintroducing or exposing a Document after deletion begins.
- Expose a safe asynchronous API and frontend state flow for deleting, failed deletion, and manual retry.
- Delete the successful deletion job itself; no deletion audit row remains.

### 2.2 Non-goals

- No task queue, external worker service, outbox, or independent scheduler.
- No chat, multi-turn RAG, agent workflow, RAG redesign, embedding upgrade, OpenSearch mapping redesign, or MinerU parser redesign.
- No public multi-source management API and no `sources[]` addition to the Phase 7 REST schema.
- No index rebuild as a deletion strategy.
- No restoration from `delete_failed` to normal business use.
- No retention of a successful deletion job for audit.

## 3. Repository facts that constrain the design

### 3.1 PostgreSQL and SQLAlchemy

- `documents` stores the raw MinIO `bucket_name` and unique `object_key`. Its existing `process_status` represents ingestion state and must not be reused for deletion state.
- `document_chunks.embedding` is the vector store. There is no separate embedding table.
- `document_chunks.parse_run_id`, `document_parse_runs`, `document_blocks`, `document_assets`, and `document_chunk_blocks` form the Phase 8 parse intermediate graph. Several foreign keys are `RESTRICT` and require explicit deletion order.
- `knowledge_items.source_document_id/source_filename` is currently singular. `knowledge_item_chunks` also stores `knowledge_item_id`, `chunk_id`, `document_id`, `chunk_index`, and a content-bearing `source_text` copy.
- `knowledge_item_versions.snapshot` stores item content plus `source_document_id`, `source_filename`, and `source_chunk_ids`. It currently has no hash, checksum, signature, or other snapshot-integrity field.
- `knowledge_item_reviews` and `knowledge_item_versions` depend on `knowledge_items`; `knowledge_items.revises_item_id` is a self-reference.
- `knowledge_item_chunks.chunk_id` and `knowledge_item_chunks.document_id` are both `NOT NULL` in the current model and migration.
- `retrieval_logs` points only to QA session/message rows. It has no Document or Chunk foreign key. Current vector, hybrid, and RAG services do not write retrieval logs.
- There is no document-specific index-sync metadata table.
- SQLAlchemy uses synchronous sessions and PostgreSQL through psycopg.

### 3.2 MinIO object namespaces

Raw uploads use one exact key:

```text
raw/{yyyy}/{mm}/{document_id}.{extension}
```

MinerU normalization scopes outputs by Document and parse run:

```text
{persisted_output_prefix}/{document_id}/{parse_run_id}/output.md
{persisted_output_prefix}/{document_id}/{parse_run_id}/output.json
{persisted_output_prefix}/{document_id}/{parse_run_id}/...
```

The default base is `parsed-assets`, but deletion must use persisted `document_parse_runs.output_prefix`, output keys, and asset keys rather than the current environment setting. A parse-run prefix is sufficiently isolated only after it is normalized, ends at the exact parse-run segment, and is used with a trailing slash.

The installed MinIO client supports `list_objects(..., include_version=True)` and version-specific `remove_object(..., version_id=...)`. Phase 10 therefore removes every visible object version and delete marker, not just the current version.

### 3.3 OpenSearch

- Retrieval uses the configured `casting_chunks_current` alias.
- The current physical index is configured as `casting_chunks_v1`.
- Each OpenSearch document has stable `_id=chunk_id` and keyword fields `document_id` and `chunk_id`.
- The current index service already performs document-scoped `delete_by_query` with a term filter, `refresh=true`, and `conflicts=proceed` before document-scoped reindexing.
- Phase 10 extends that capability with explicit physical-index targeting, alias-aware verification, and zero-hit completion checks. It does not change mapping or vector dimension.

### 3.4 Application lifecycle and frontend

- FastAPI lifespan currently closes the cached LLM provider only; there is no worker or background executor infrastructure.
- Request sessions come from `SessionLocal`; the executor must create and close its own sessions.
- Document list/detail pages and parse/embedding/index controls already exist. There is no frontend automated-test runner in the current package scripts.

## 4. Deletion dependency matrix

| Resource | Storage | Relation | Identifier captured or queried | Delete strategy | Idempotent | Shared |
|---|---|---|---|---|---|---|
| `documents` | PostgreSQL | root | `document_id` | Delete in final PostgreSQL transaction | yes, absent is success | no |
| `document_chunks` and inline embedding | PostgreSQL | direct by `document_id` | `chunk_ids` | Delete after all chunk dependents | yes | no |
| `document_chunk_blocks` | PostgreSQL | indirect by chunk/block | `chunk_ids`, `block_ids` | Explicit delete before chunks/blocks | yes | no |
| `document_parse_runs` | PostgreSQL | direct by `document_id` | `parse_run_ids` | Delete after blocks/assets/chunks | yes | no |
| `document_blocks` | PostgreSQL | direct by Document and parse run | `block_ids` | Explicit delete after mappings | yes | no |
| `document_assets` | PostgreSQL | direct by Document and parse run | row IDs and `asset_key` | Delete metadata after MinIO verification | yes | no |
| `knowledge_item_sources` | PostgreSQL | direct source fact | `(knowledge_item_id, document_id)` | Remove only target source relation | yes | yes |
| `knowledge_item_chunks` | PostgreSQL | indirect content-bearing copy | `(knowledge_item_id, chunk_id, document_id)` | Delete target-document links before chunks | yes | yes by item |
| orphan `knowledge_items` | PostgreSQL | source-aware | affected item IDs | Delete only affected items with zero remaining sources | yes | yes |
| surviving `knowledge_items` projection | PostgreSQL | indirect | affected item IDs | Repoint singular projection to deterministic remaining source or null | yes | yes |
| `knowledge_item_versions` | PostgreSQL | indirect | knowledge item IDs | Delete with orphan item; redact deleted provenance for survivors | yes | yes |
| `knowledge_item_reviews` | PostgreSQL | indirect | knowledge item IDs | Delete only with orphan item | yes | yes |
| revision self-links | PostgreSQL | indirect graph | cycle-safe closure of affected IDs | Lock closure; null links whose parent is deleted; delete orphan nodes | yes | yes |
| `qa_sessions`, `qa_messages` | PostgreSQL | none | none | Retain; they have no document provenance field | not applicable | shared/system |
| `retrieval_logs` | PostgreSQL | no targetable provenance contract | none | Retain; current services do not write them and no exact document relation exists | not applicable | shared/system |
| raw upload | MinIO | direct | bucket plus exact `object_key` | Delete all versions/delete markers for exact key | yes | no |
| MinerU outputs/assets/intermediates | MinIO | direct via parse-run namespace | bucket, exact keys, validated parse-run prefixes | Delete all versions/delete markers under each validated prefix | yes | no |
| chunk search documents | OpenSearch | direct and indirect | physical index, alias, `document_id`, `chunk_ids` | DBQ by `document_id`, refresh, verify Document and Chunk zero hits | yes | no |
| `document_deletion_jobs` | PostgreSQL | recovery fact | job ID and `document_id` snapshot | Delete last in successful final transaction | yes | no |

`retrieval_logs.result_summary` is unstructured JSON with no supported Document/Chunk identity contract. Phase 10 must not guess by substring or delete unrelated QA history. The M7 pre-delete snapshot confirms that the dedicated Phase 10 fixture has produced no such rows. A future writer that stores content-bearing retrieval history must first add normalized provenance and its own deletion contract.

## 5. Authoritative deletion semantics

### 5.1 Final state

A completed job means all of the following are true:

- the Document row is absent;
- the deletion job is absent;
- all document chunks and inline vectors are absent;
- all parse runs, blocks, assets, and chunk-block mappings are absent;
- all target `knowledge_item_chunks` and `knowledge_item_sources` are absent;
- affected Knowledge Items with no remaining source, plus their versions and reviews, are absent;
- shared Knowledge Items and their remaining source relations are intact;
- raw and derived MinIO namespaces contain no object version or delete marker belonging to the Document;
- OpenSearch returns zero hits by `document_id` and every original `chunk_id`;
- retrieval through hybrid search, vector search, and RAG cannot return the Document;
- unrelated Documents and shared knowledge remain byte-for-byte or row-for-row unchanged according to the acceptance snapshot.

### 5.2 Source-aware Knowledge deletion

`knowledge_item_sources` is the only multi-source identity fact. Its `document_id` identifies the source; `source_filename` is only a provenance snapshot and never participates in source identity.

When deleting Document A:

1. lock the affected Knowledge revision closure in stable UUID order;
2. remove A's `knowledge_item_chunks` and A-to-item source relations;
3. recompute source counts while locks are held;
4. retain an item with another source, including its reviews and versions;
5. update the retained item's singular compatibility projection to the earliest remaining source ordered by `created_at, document_id`;
6. redact A's `source_document_id`, filename, and chunk IDs from surviving version snapshots;
7. delete an affected item that now has zero sources, together with its chunks, versions, and reviews;
8. detach any surviving revision whose parent is being deleted by setting `revises_item_id` to null before parent deletion.

A source-less manual Knowledge Item is not an orphan candidate merely because it has zero source rows. Only items that had the target Document source removed are evaluated for orphan deletion.

### 5.3 Revision graph safety

The self-reference is treated as an arbitrary graph, not a guaranteed tree. Closure traversal maintains a visited set and has a defensive node limit derived from the locked candidate set. It traverses parents and children, terminates on cycles, sorts all resulting UUIDs, then locks in that order. Before deleting nodes, affected self-links are nulled so cycles cannot block deterministic deletion.

## 6. Persistent job and database design

### 6.1 Document deletion status

Migration 0007 adds `documents.deletion_status` as a non-null string with server default `normal`, a check constraint over:

```text
normal
deleting
delete_failed
```

It also adds an index on the field. `process_status` remains unchanged and continues to describe upload/parse state.

### 6.2 Job table

`document_deletion_jobs` has:

| Column | Type | Rule |
|---|---|---|
| `id` | UUID | primary key |
| `document_id` | UUID | immutable snapshot, unique, deliberately no FK |
| `status` | varchar | `pending`, `processing`, `retry_wait`, or `delete_failed` |
| `current_step` | varchar | one approved Saga step name |
| `step_attempts` | integer | non-negative attempts for the current step only |
| `max_attempts` | integer | positive snapshot, default 5 |
| `manifest` | JSONB | non-null, versioned, identifiers only |
| `locked_at` | timestamptz | database time at claim |
| `lease_expires_at` | timestamptz | database-time lease deadline |
| `lease_token` | UUID | fencing token for one ownership generation |
| `next_retry_at` | timestamptz | database-time retry eligibility |
| `last_error_code` | varchar | safe internal error code, never raw exception text |
| `created_at` | timestamptz | database default `now()` |
| `updated_at` | timestamptz | database-managed mutation timestamp |

The table has a unique constraint on `document_id`, a claim-oriented index over status/due/lease/creation fields, status and step check constraints, and consistency checks for lease fields. It has no `ON DELETE CASCADE`; the immutable `document_id` snapshot lets it survive a missing Document during recovery.

There is no persisted `succeeded` job. Success is the atomic deletion of the Document and job in the final transaction.

### 6.3 Document-to-job invariant matrix

| Document state | Job state | Valid meaning |
|---|---|---|
| `normal` | no job | normal business use |
| `deleting` | `pending`, `processing`, or `retry_wait` | active or automatically retrying deletion |
| `delete_failed` | `delete_failed` | automatic retries exhausted |
| Document absent | job exists | valid crash-recovery state |
| Document absent | no job | deletion completed |

Every other combination raises `DOCUMENT_DELETION_STATE_INCONSISTENT`. DELETE, status, retry, claim, recovery, and finalization all use the same validator. The service does not infer intent, create a missing job, remove an unexpected job, or restore a Document to `normal`.

Invariant validation is the first read-only decision after loading the Document/job pair. An invalid pair returns `DOCUMENT_DELETION_STATE_INCONSISTENT` even when the executor is disabled. For a valid pair, DELETE/retry then applies the disabled 503 rule before any mutation.

## 7. Expand, dual-write, enforce migrations

### 7.1 Migration 0007: expand

Migration 0007:

- adds `documents.deletion_status`;
- creates `document_deletion_jobs`;
- creates `knowledge_item_sources` with a UUID primary key, `knowledge_item_id`, `document_id`, provenance-snapshot `source_filename`, and database timestamps;
- adds a unique constraint on `(knowledge_item_id, document_id)` and indexes by both directions;
- keeps foreign keys from the source table to `knowledge_items` and `documents` as `RESTRICT`/no-action semantics;
- backfills the union of non-null `knowledge_items.source_document_id` and distinct `knowledge_item_chunks.document_id` pairs;
- chooses the existing singular filename when it matches the source and otherwise snapshots `documents.original_filename`;
- leaves source-less manual items valid;
- does not add the composite chunk-to-source foreign key.

All current knowledge create, update, revise, and extraction paths then dual-write source relations in the same transaction as item/chunk rows. The existing singular fields remain service-maintained REST compatibility projections. There is no reverse composite FK from `knowledge_items` to `knowledge_item_sources`, avoiding a cycle and a second enforced fact source.

### 7.2 Migration 0008: enforce

Migration 0008 runs only after all writers dual-write. Its preflight fails explicitly when:

- a `knowledge_item_chunks` row has a missing `chunk_id` or `document_id`, including schema/data drift despite the current `NOT NULL` definitions;
- a chunk relation's `document_id` does not match the referenced chunk's Document;
- a chunk relation lacks matching `(knowledge_item_id, document_id)` in `knowledge_item_sources`;
- duplicate or otherwise inconsistent source facts prevent enforcement.

An item with no source row is legal if it has no chunk relation. The migration then adds the named constraint `fk_knowledge_item_chunks_item_document_source`:

```text
knowledge_item_chunks(knowledge_item_id, document_id)
    -> knowledge_item_sources(knowledge_item_id, document_id)
```

The existing individual foreign keys remain. Before altering surviving `knowledge_item_versions.snapshot`, M3 rechecks the live model, migration history, and writer for hash/checksum/signature semantics. The current repository has none. If one exists at implementation time, the same transaction must update it with the established algorithm; if consistency cannot be preserved, M3 stops before data mutation.

## 8. Manifest

The immutable manifest is built in the same transaction that locks the Document, changes it from `normal` to `deleting`, and inserts the job. It contains only:

```json
{
  "schema_version": 1,
  "document_id": "uuid",
  "bucket_name": "bucket",
  "raw_object_key": "exact-key",
  "derived_object_keys": ["exact-key"],
  "derived_prefixes": ["validated-prefix/"],
  "parse_run_ids": ["uuid"],
  "block_ids": ["uuid"],
  "asset_ids": ["uuid"],
  "chunk_ids": ["uuid"],
  "knowledge_item_ids": ["uuid"],
  "search_index_name": "configured-physical-index",
  "search_index_alias": "configured-alias"
}
```

It never contains document text, chunk text, Knowledge Item text, version snapshots, prompt/model response content, vectors, binary content, API keys, credentials, or raw exception messages.

Manifest validation rejects an empty Document identity, unsafe object prefixes, a derived prefix not scoped to a captured Document and parse run, and conflicting buckets. Exact keys plus prefixes are both retained: exact keys cover registered assets, while the validated parse-run prefix covers intermediate objects created before metadata persistence.

## 9. State machine, claim, retry, and lease

### 9.1 Job state machine

```text
schedule transaction
    -> pending(step_attempts=0)

pending or due retry_wait
    -> atomic claim
    -> processing(step_attempts += 1, lease_token=new UUID)

processing + step success
    -> pending(next step, step_attempts=0, lease cleared)

processing + retryable failure + step_attempts < max_attempts
    -> retry_wait(next_retry_at from database time, lease cleared)

processing + failure + step_attempts >= max_attempts
    -> delete_failed
    -> Document delete_failed when the row still exists

delete_failed + manual retry
    -> pending(same safe current_step, step_attempts=0)
    -> Document deleting

final PostgreSQL success
    -> Document and job deleted in one transaction
```

Each successful step returns the job to `pending`; the next claim is a new fenced ownership generation. This makes the attempt increment and durable step boundary unambiguous.

### 9.2 Retry policy

- `step_attempts` counts only the current step and resets to zero when `current_step` advances.
- Default `max_attempts` is 5 per step.
- Default retry delays after failed attempts 1-4 are 5, 30, 120, and 300 seconds.
- Attempt 5 transitions to `delete_failed`; retries are never infinite.
- Manual retry retains `current_step`, resets only `step_attempts` and retry/lease/error fields, and does not create a second job.
- Retry settings are configurable, but `max_attempts` is snapshotted into the job so a running job is not silently reinterpreted after restart.

### 9.3 PostgreSQL database time

PostgreSQL `now()` and interval arithmetic are authoritative for `locked_at`, `lease_expires_at`, `next_retry_at`, due checks, expired checks, and heartbeat renewal. Python wall-clock time never determines ownership or retry eligibility. A monotonic timer may only decide when the executor should ask PostgreSQL to renew.

Atomic claim uses one transaction with `SELECT ... FOR UPDATE SKIP LOCKED` in a CTE and `UPDATE ... RETURNING`. Claimable rows are:

- `pending`;
- `retry_wait` with database `next_retry_at <= now()`;
- `processing` with database-expired lease and `step_attempts < max_attempts`.

Expired `processing` rows with `step_attempts >= max_attempts` are handled only by an exclusive recovery sweep that transitions them to `delete_failed`. They are excluded from claim SQL, preventing an extra automatic attempt.

### 9.4 Fencing and heartbeat

Every claim installs a new random `lease_token`. All progress, failure, and lease-renewal updates use `WHERE id=:job_id AND status='processing' AND lease_token=:token AND lease_expires_at > now()`.

Default lease duration is 120 seconds. A long step renews at least every `lease/3`; MinIO enumeration/deletion also renews after every 25 processed object versions. If renewal fails or the fencing token no longer owns the row, the old executor stops initiating external calls and cannot update job state.

Fencing prevents stale database writes. Heartbeat additionally preserves single-owner behavior during long external cleanup so an old executor does not continue issuing new MinIO or OpenSearch operations after lease loss.

## 10. Saga

| Step | Input | Completion criterion | Retry/idempotency |
|---|---|---|---|
| Schedule | locked Document plus dependency query | Document is `deleting`; one job and complete manifest committed | unique job and invariant check make duplicate DELETE safe |
| `delete_opensearch` | physical index, alias, Document ID, chunk IDs | DBQ completed with refresh; physical index and active alias report zero hits by Document and original chunks | missing index/alias and zero hits are success; outage retries |
| `delete_minio_derived` | bucket, exact derived keys, validated parse-run prefixes | version listing returns no version or delete marker for every target | missing key/version is success; partial deletion resumes from listing |
| `delete_minio_raw` | bucket and exact raw key | version listing returns no version or delete marker for the exact key | missing key/version is success |
| `finalize_postgresql` | manifest IDs and Document snapshot | one transaction removes target DB dependencies, Document, and job | absent rows are success; rollback leaves job for retry |

External resources are removed before the PostgreSQL recovery facts. The final transaction does not call MinIO or OpenSearch.

An OpenSearch response is not completion merely because DBQ returned without an exception. The deleter refreshes and checks both `document_id` and batched original `chunk_id` terms. A MinIO delete is not completion until a version-aware listing is empty.

If a final PostgreSQL commit response is ambiguous, restart observes either the intact job and retries idempotently, or both Document and job absent and treats deletion as complete.

## 11. Concurrency barriers and retrieval isolation

### 11.1 DELETE scheduling

The scheduling transaction locks the Document row with `FOR UPDATE`, validates the Document/job matrix, builds the manifest from a consistent dependency snapshot, inserts or returns the unique job, changes `deletion_status`, and commits. Concurrent DELETE calls serialize; only one job can exist.

### 11.2 Write-side barriers

Parse, embedding, index sync, knowledge create/update/revise, and extraction must reject a non-`normal` Document with a stable deletion-state error.

Long computation may occur without a row lock, but the final persistence barrier is mandatory:

- parsing acquires `FOR UPDATE` before the first persistent MinIO/DB output write and holds it through that persistence transaction;
- embedding computes vectors first, then locks and rechecks before saving them;
- extraction calls the LLM first, then locks all source Documents in stable order and rechecks before writing items, chunks, versions, and source rows;
- knowledge create/update/revise locks every referenced source Document before writing;
- index sync locks one Document, rechecks, performs only that Document's delete/bulk write, then commits/releases.

If a writer holds the Document lock first, DELETE waits and its later manifest includes the completed write. If DELETE commits first, the writer's final check rejects the write.

All-scope index sync first enumerates IDs without holding locks, then processes each Document independently. It never locks many Documents across a large OpenSearch bulk.

### 11.3 Read-side isolation

The consistency boundary is commit-based:

- a retrieval request that begins after the deletion-state transaction commits must not return the Document;
- a read that began before that commit may finish normally.

PostgreSQL vector search adds `Document.deletion_status='normal'`. Hybrid search obtains OpenSearch candidates, performs one short PostgreSQL query for their Document IDs, filters non-normal/missing Documents, and immediately releases the transaction. RAG consumes the filtered result and never holds a database lock through LLM generation.

Early OpenSearch deletion reduces the stale window, while the PostgreSQL post-filter closes it for new requests even during an OpenSearch outage.

## 12. Executor lifecycle

### 12.1 Disabled semantics

`DOCUMENT_DELETION_EXECUTOR_ENABLED=false` is the default for development tests, CI, migration rollout, and the first post-migration start.

When disabled:

- DELETE returns HTTP 503 with `DOCUMENT_DELETION_EXECUTOR_DISABLED`;
- manual retry returns the same 503;
- neither endpoint creates or resets a job;
- Document state does not change;
- status API can read an existing job;
- startup performs no recovery scan and claims no job;
- existing jobs and existing `deleting`/`delete_failed` states remain unchanged.

Repeated DELETE against an already deleting Document also returns 503 while disabled; clients use status GET for observation.

### 12.2 Startup

When enabled, lifespan creates one executor thread per FastAPI process and returns control without synchronously draining jobs. The executor uses a daemon thread and `threading.Event.wait(timeout)` for both polling and immediate `wake()`. PostgreSQL claim/lease makes multiple processes safe.

Every claim, heartbeat, transition, recovery sweep, and finalization opens a fresh `SessionLocal` and closes it in `finally`. Request sessions are never reused by the thread.

### 12.3 Shutdown and in-flight responses

Shutdown sets `stop_event`, wakes the loop, and prevents new claims and new external calls. Before each MinIO/OpenSearch call and after every external response, the executor checks `stop_event` and verifies current lease/fencing ownership with a fresh database operation.

Once `stop_event` is set, an already in-flight external request may have changed the external system, but its successful response must not advance `current_step`, mark success, or delete the job. The executor leaves the durable state unchanged; a later owner resumes the idempotent step after lease expiry. A bounded join does not erase or falsify the job.

## 13. API contract

All endpoints use the existing API envelope for non-204 responses and expose no manifest, object key, OpenSearch internal identifier, stack trace, raw storage error, or content.

### 13.1 Delete

```http
DELETE /api/v1/documents/{document_id}
```

- normal Document, enabled executor: atomically schedules and returns 202;
- deleting Document with its valid active job: returns the existing safe status and 202;
- `delete_failed`: returns 409 `DOCUMENT_DELETION_RETRY_REQUIRED`; retry is explicit;
- absent Document and no job: returns 204;
- absent Document with an active job: returns that job's safe status and 202; an absent Document with a failed job still requires the explicit retry endpoint;
- disabled executor: returns 503 without mutation;
- invalid Document/job combination: returns 409 `DOCUMENT_DELETION_STATE_INCONSISTENT`.

### 13.2 Status

```http
GET /api/v1/documents/{document_id}/deletion-status
```

Returns 200 while a job exists, including when the Document row is already absent. After completion it returns 404 `DOCUMENT_NOT_FOUND`; the frontend treats that as successful removal. The safe response contains only:

```json
{
  "document_id": "uuid",
  "status": "deleting | retrying | delete_failed",
  "error_code": "safe-code-or-null",
  "retry_available": false,
  "updated_at": "database-timestamp"
}
```

`pending` and `processing` map to `deleting`; `retry_wait` maps to `retrying`.
`retry_available` is true only for a `delete_failed` job. It describes the job state; the retry call can still return the disabled 503 without changing that state.

### 13.3 Manual retry

```http
POST /api/v1/documents/{document_id}/deletion-retry
```

A valid `delete_failed + delete_failed` pair is reset to the same `current_step` and returns 202. A non-failed job returns 409. Disabled executor returns 503 without mutation. Retry never creates a duplicate job and never restores normal business access.

## 14. Frontend behavior

Document list and detail surfaces show a destructive `删除` action only for `normal`. Confirmation copy is:

```text
永久删除该文档？

删除后将同时删除：
- 原始文件
- 解析结果
- 向量数据
- 检索索引
- 该文档独有的知识条目

此操作完成后不可恢复。
```

The confirmation button is `永久删除`.

After HTTP 202 the UI shows `正在删除`, disables parse/embedding/index/extraction and ordinary actions, and polls status. HTTP 202 is never displayed as completion. `retrying` remains a deletion-in-progress state. `delete_failed` shows `删除失败` and `重试删除`. Completion is recognized only when status returns Document-not-found and a refreshed list no longer contains the row.

The 503 disabled error is shown as a safe operator-facing message and does not optimistically alter local state.

## 15. Failure recovery

| Failure | Durable outcome | Recovery |
|---|---|---|
| process exits after 202 before wake | pending job remains | enabled startup/poll claims it |
| process exits after OpenSearch deletion | step remains processing until lease expiry | new owner repeats DBQ and zero-hit check |
| MinIO first object removed, second fails | retry_wait or processing until expiry | next listing skips absent version and continues |
| external response returns after shutdown | no progress update | lease expires; next process rechecks idempotently |
| heartbeat loses ownership | old owner stops new calls and cannot mutate job | fenced owner continues |
| OpenSearch/MinIO outage | bounded per-step retries | `delete_failed` after attempt 5; manual retry resumes step |
| final DB transaction fails | all final DB changes roll back | final step retries |
| commit response is ambiguous | job either remains or was atomically removed with Document | recovery or idempotent 204 |
| two workers claim simultaneously | `SKIP LOCKED` and update fencing choose one | loser receives no job |
| A and B delete shared K concurrently | stable item/revision locks serialize source recount | last source remover deletes K without orphan race |

## 16. Security and no-residual acceptance

- API and logs expose only stable error codes and bounded operational metadata.
- Manifest and job errors contain no document/chunk/Knowledge Item text, vectors, prompts, binary data, secret, full upstream response, or stack trace.
- Object-prefix validation prevents bucket-wide or neighboring-Document deletion.
- OpenSearch deletion targets one captured physical index and one Document identity; it never deletes an index.
- Integration fixtures use unique Phase 10 Document IDs and record unrelated resource sets before deletion.
- Tests never truncate a table, clear a bucket, delete an index, rebuild an index, prune a volume, or operate on existing knowledge-base Documents.

Final acceptance proves:

```text
PostgreSQL target rows = 0
MinIO target versions/delete markers = 0
OpenSearch target document_id hits = 0
OpenSearch original chunk_id hits = 0
normal RAG recall for target = 0
deletion job = 0
unrelated resource snapshot = unchanged
shared Knowledge sources/reviews/versions = preserved
```

## 17. Rollout gate

Real deletion is prohibited until this exact sequence completes:

```text
0007 and 0008 migrations applied
-> Alembic head verified
-> application starts with executor=false
-> guards, invariant handling, DELETE/status/retry APIs verified
-> executor setting changed to true
-> application fully restarted
-> executor health/recovery behavior verified
-> first real DELETE limited to a dedicated Phase 10 fixture
```

If any deletion guard is incomplete, the executor remains disabled. The final accepted implementation is recorded only in `docs/phase-10-finished.md`.
