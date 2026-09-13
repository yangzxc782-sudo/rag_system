# M6.5 RAG evidence UX contract

This is a scoped addition to the existing RAG workflow, not a retrofit of unrelated CRUD screens.
Source of authority: the owner's M6.5 request; visual/runtime mapping: `DESIGN.md`.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Table Selection | GraphEvidencePanel display tables | Final API graph evidence | No selection or bulk actions | Keyboard/scroll inspection |
| Select/Listbox | Existing forms | No new select in M6.5 | Existing controls retained | Targeted diff review |
| Date | Existing forms | No date input in M6.5 | None added | Targeted diff review |
| Form | RagAskPanel | Existing ragAsk API call | Graph does not submit independently | Loading/error/retry smoke |
| Scrollbar | Browser native scrolling; GraphEvidencePanel bounded record regions | Existing runtime, no global scrollbar theme | Stable gutter on new tables only | Narrow viewport and long lists |
| Toast | Existing inline RAG error region | Existing error mapping | No new toast | Fake request failure/recovery |
| CRUD | Existing module owners outside M6.5 | Graph display is read-only | No new create/update/delete | No additional network request |

| Graph response | Visible behavior |
|---|---|
| graph omitted/null/disabled | No graph panel |
| enabled, triggered=false | No empty graph panel |
| success | Default-collapsed panel; retained anchor/table/entities/relationships and text citation numbers |
| truncated=true | Visible "已截断" badge; expanded explanation that only this answer's retained evidence is shown |
| partial | Successful evidence retained, "部分可用" badge and safe partial-failure explanation |
| unavailable | Safe text-only fallback explanation; no diagnostic payload |

Each response shares exactly the final graph evidence seen by the LLM. Entity/relationship names are data,
escaped by React, never interpreted as HTML or new numeric conclusions. Entity IDs remain in the API;
the UI prioritizes names/type/page and relationship direction.

Text citation numbering/content/identity is unchanged. Existing citations have no jump interaction,
so graph source numbers are plain badges rather than nonfunctional links.

During a request the existing submit/loading behavior remains. On failure the existing error replaces
the answer; retry uses the same question form. Each successful response starts a fresh collapsed display.
Only `ragAsk` submits the request; disclosure and scrolling issue no API call.
