# Phase 11 M6.5 — Graph Evidence API / Frontend review

Date: 2026-09-10. Status: AWAITING_PROJECT_OWNER_PHASE11_M65_REVIEW.

## Git gate and authority

- Branch: `feature/phase11-graph-retrieval`.
- Starting HEAD: `8f20a061f97cb40685bd36bf5b0de17bade962c1`.
- Starting status/diff check: clean/pass; no owner parallel changes present.
- Independent milestone commits: M1 `183440b`, M2 `d9a3d16`, M3 `da5dca5`, M4 `44f11df`,
  M5 `2c38d33`, M6 `8f20a06`. Basic retirement: `396868c`.
- Owner's M6.5 decision authorizes an additive public graph response, superseding the earlier
  M4/M6 internal-only response boundary. No citation schema or graph query change is authorized here.

## CURRENT_RAG_RESPONSE_FLOW

Audited before implementation:

```text
GraphRetrievalService.retrieve → GraphRetrievalResult
→ build_graph_context → final bounded GraphContext
→ build_rag_prompt → LLMGenerateRequest → LLM provider.generate

answer_question → RagAnswerResult → RagAskData.from_service_result
→ POST /api/v1/rag/ask → frontend/lib/rag.ts:ragAsk
→ RagAskPanel:AnswerPanel → CitationItem
```

The original loss occurred in `answer_question`: `graph_context` was used for the prompt but omitted
from `RagAnswerResult`. The public schema and frontend types had no graph property.

After M6.5, `RagPrompt` records the actual applied context and `RagAnswerResult` retains it.
`RagAskData.from_service_result` calls the pure `build_graph_response` mapper. The endpoint's existing
schema conversion needs no API route edit. Success reuses the same context object; no retrieval result
is rebuilt or queried. M6 formatter fallback is detected by comparing the resulting user prompt to
the existing text baseline. In that case, an empty immutable view is reported, not unused evidence.
No prompt wording, trigger, text/graph budget, or graph formatter behavior changes.

## Public contract

`RagAskData.graph` is optional and defaults to null. Original question, answer, context_status,
citations, retrieval and llm fields retain their values/types. Old payloads still validate.

| Field | Contract |
|---|---|
| enabled / triggered | Boolean; trigger reporting inspects only final text context refs, without a query |
| status | success / partial / not_triggered / unavailable |
| truncated | Final context's `was_truncated`, including an exhausted budget with zero retained records |
| evidence_count | Length of public final evidence, not the raw query's anchor/entity count |
| evidence | Explicit safe DTO list in final context order |

Feature off → null. Enabled/no valid final refs (including no_context) → not_triggered.
Triggered/no usable evidence → unavailable. Retained evidence plus failed anchor diagnostics → partial.
Only context-budget truncation, with evidence and no failed anchors → success + truncated=true.
Internal timeout/budget/ambiguity/error diagnostics do not become public messages.

Evidence fields: graph_id, anchor_id, anchor_type, nullable table_ref, source_citations, document,
table, entities, relationships. Document exposes only doc_id; source_pdf can contain a private path
and is omitted. Table exposes table_id/table_ref/page/table_index. Entity exposes id/name/entity_type/page.
Relationship exposes source_entity_id/source_name/type/target_entity_id/target_name. Names come from
the already retained entities; no additional Neo4j lookup. Relation endpoints remain complete.

The mapper does not dump dataclasses, raw Neo4j objects, diagnostics, credentials or private source paths.
It creates independent public values and does not mutate GraphContext. Text citation content is untouched.

Mock API summary (not a real T-P8-1 query):

```json
{
  "enabled": true,
  "triggered": true,
  "status": "success",
  "truncated": true,
  "evidence_count": 1,
  "evidence": [{
    "graph_id": "G",
    "anchor_id": "A",
    "anchor_type": "table",
    "table_ref": "T-P8-1",
    "source_citations": [1],
    "document": {"doc_id": "D"},
    "table": {"table_id": "D_T-P8-1", "table_ref": "T-P8-1", "page": 8, "table_index": 1},
    "entities": [
      {"id": "E1", "name": "0.25 ≤0.5% 600℃", "entity_type": "参数", "page": 8},
      {"id": "E2", "name": "材料", "entity_type": "材料", "page": 8}
    ],
    "relationships": []
  }]
}
```

In that fixture, the raw result had one relationship which did not fit the final budget. Neither
the prompt nor response/UI includes it. Numeric names are fixture data, not numerical conclusions.

## Frontend

`GraphEvidencePanel` sits after the answer and before unchanged citations. It uses native details/summary,
starts collapsed on every successful answer, and has no fetch, graph endpoint or state dependency.
Anchor/Table/graph identities, entity and directional relationship tables, and source citation numbers
are displayed. Existing citations have no jump behavior, so plain numbered badges preserve their contract.
Long record lists scroll within bounded regions; labels and identities wrap at narrow widths.

Truncation explicitly states that the displayed records are only those used within this answer's budget.
Partial results preserve successful evidence and show a safe warning. Unavailable uses a safe text-only
fallback message. Off/legacy/no-ref responses render no empty panel. Numeric authority remains with text.

Scoped durable UI context is recorded in `frontend/DESIGN.md` and `frontend/UX-CONTRACT.md`; existing
runtime tokens, forms, navigation and citation implementation remain authoritative.

## TDD and regression evidence

Before production edits: 27 new tests failed (missing public graph field/mapper/context handoff).
After minimal implementation: 27 passed. Tests cover final-context identity, prompt/response record parity,
truncation, nullable values, explicit DTO fields, no mutation, status aggregation, fallback formatter/builder/
retriever, legacy compatibility, single repository call, and actual FastAPI serialization.

| Check | Result |
|---|---|
| Fresh backend baseline | 1308 passed / 28 deselected |
| M6.5 focused | 27 passed |
| M6.5 + M6 context/fusion + RAG API/service + M4 propagation | 201 passed |
| M1—M6 focused aggregate | 372 passed |
| R-RAG (context, service, API) | 57 passed |
| Search (index, engine, hybrid, vector) | 73 passed |
| Hard Delete, operation guard, knowledge deletion, Basic retirement | 229 passed |
| Final backend full | 1335 passed / 28 deselected |
| Targeted ESLint: GraphEvidencePanel, RagAskPanel, lib/rag | PASS |
| Full lint | Existing `DocumentParseResults.tsx:254` set-state-in-effect error only |
| TypeScript / Next build | Existing `KnowledgeItemsPanel.tsx:270` TS2345 only; build compiled successfully before type-check failure |
| Premium static audit, strict/no-write | 0 findings |

Backend command uses `backend/.venv/Scripts/pytest.exe`; full marker selection:
`-q -m "not integration and not phase11_integration"`. Runs use `-p no:cacheprovider`; tests needing
temporary files use unique `--basetemp` directories. One pre-existing Starlette/httpx deprecation warning.
Full lint and TypeScript failures were recorded before any production edits and left unchanged.
No test framework/dependency installed. DESIGN official npm linter was not installed or downloaded;
the available local premium auditor and runtime mapping review were used.

## Browser UX matrix (local fake API, actual frontend)

The actual Next RAG page ran on isolated loopback port 3901 with a fake API on 3902, using process-local
NEXT_PUBLIC_API_BASE_URL, without editing .env files. The fake API called no downstream service.

| Case | Observed result |
|---|---|
| A Graph off | No panel |
| B success using T-P8-1-shaped fixture | Collapsed panel; Anchor/Table/entity/relationship present |
| C truncated | Badge and full budget explanation; omitted relationship absent |
| D many entities | 30 entity rows, bounded list, keyboard End reaches final rows |
| E relationships | 29 rows in long fixture; source/type/target direction and names correct |
| F source citations | Text basis [1] displayed; backend tests also verify [1,2] |
| G unavailable | Safe fallback explanation, no exception text |
| H partial | Successful evidence retained plus partial warning |
| I no refs / legacy graph omitted | No empty graph card |
| J existing citation | Number, text, filename and identities remain visible and unchanged |
| Keyboard | Enter and Space toggle disclosure; focus outline visible |
| Narrow screen | 390×844 viewport: page width within viewport, no record-table horizontal overflow |
| Loading/error/retry | Loading disables submit; failure clears answer through existing flow; retry succeeds |
| Consecutive answers | Every successful response starts collapsed; fixed after browser revealed inherited open state |

These are fake-backed UI checks, not live graph acceptance. Owner-reported T-P8-1 Graph OFF/ON technical
E2E remains accepted as prior evidence (anchors=1, evidence=1, unavailable=0, timeout=0, truncated=True).
No new real T-P8-1 Graph/API/UI smoke or multi-table M7 acceptance was executed in this turn.

## Scope review

Production changes are limited to backend RAG service/schema/response mapper and frontend RAG types,
answer display and GraphEvidencePanel. Existing API route requires no edit. Tests update only additive
public-field expectations, with new M6.5 contracts. Citation core schema is unchanged.

No additional Neo4j query, graph endpoint, graph trigger/budget change, schema/Cypher/template/repository
change, Markdown/MinerU change, Hard Delete change, OpenSearch change, migration, Basic restoration,
visualization dependency, M7 work, staging, commit or push.

Final Git inspection: HEAD unchanged, 7 tracked modifications (+121/-9) and 6 new files, all belonging
to M6.5. No unknown paths or staged entries. `git diff --check` passed; new files separately passed
trailing-whitespace/EOF checks. Git emitted only its normal LF→CRLF working-copy notices.
The temporary UI tab and both loopback test servers were closed after verification.
