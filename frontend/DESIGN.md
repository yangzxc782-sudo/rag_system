---
version: alpha
name: "铸型工艺知识库 RAG 管理系统"
description: "以文本依据为权威的中文知识检索与问答工作台；M6.5 图谱证据展示范围。"
colors:
  background: "#f6f7f9"
  foreground: "#18202a"
  surface: "#ffffff"
typography:
  sans:
    fontFamily: "Arial, Helvetica, sans-serif"
rounded:
  md: "0.375rem"
spacing:
  panel-padding: "1rem"
  page-max: "80rem"
components:
  graph-evidence-panel: {}
  text-citation: {}
---

# RAG evidence design context

## Overview

Scope: the M6.5 addition to `/rag`. This records existing runtime choices, not a global redesign mandate.
The product owner approved public exposure of the final graph evidence on 2026-09-10, superseding
the earlier Phase 11 plan's internal-only graph response boundary. Business sources: the owner’s M6.5
request and `../docs/superpowers/specs/2026-09-08-phase-11-graph-retrieval-design.md`.

Audience: Chinese-speaking casting-process knowledge users inspecting an answer and its evidence.
Register: a restrained product workbench, similar to a technical evidence sheet. Locale is zh-CN;
no regional legal or Japanese-market requirement is inferred. Desktop is primary, narrow screens remain usable.
The signature is a traceable answer → graph evidence → numbered text basis, not a decorative node diagram.

Token ownership is Model B: existing `app/globals.css`, Tailwind v4 defaults and `AppShell.tsx` remain canonical.
This document mirrors them; it does not generate tokens or introduce a theme.

## Colors

Background/foreground map to `--background`/`--foreground`; surfaces use `bg-white`.
Existing `slate-*` tokens express text/borders; `amber-*` expresses partial/truncated states.
Status is always conveyed by text as well as color. No palette values changed.

## Typography

Body inherits the existing Arial/Helvetica/system fallback from `globals.css`.
Graph headings use existing `text-sm font-semibold`; dense records use `text-xs leading-5`.
Mixed Chinese, identifiers, units and mathematical operators are displayed verbatim, with wrapping.

## Layout

`AppShell` owns the `max-w-7xl` page. `RagAskPanel` owns the answer sequence.
Graph evidence sits between the answer and unchanged text citations; native disclosure starts collapsed.
Graph identity metadata becomes two columns at `sm`, one below. Record tables retain all columns.
Long entity/relationship lists own a `max-h-80` scroll region with stable scrollbar gutter and keyboard focus.
The page and form retain natural document scrolling; no viewport locks or fixed-height shell.

## Elevation & Depth

Use the existing white surface, 1px slate borders and section dividers. No additional shadow hierarchy.

## Shapes

`rounded-md` maps to Tailwind's 0.375rem and is shared with adjacent answer/citation badges.

## Components

`GraphEvidencePanel` owns display-only graph disclosure, status, entities, relationships and source numbers.
Native `details/summary` owns pointer/Enter/Space interaction; explicit focus-visible outline remains visible.
Each successful answer remounts the answer display so its graph starts collapsed.
`RagAskPanel` retains loading, error handling, request ownership and `CitationItem`; no independent graph fetch.
Existing form controls, navigation and citations are reused without redesign. See `UX-CONTRACT.md`.

## Do / Don't

Show only final evidence used in the prompt. Explain budget truncation and text authority.
Do not infer numeric conclusions, render raw HTML, query more graph data or add artificial citation links.
Do not install a graph visualization/UI/test library for this increment.

## Verification

Run targeted ESLint on the three changed TS/TSX files, full lint, TypeScript and Next build.
Exercise mock UI success/off/no refs/legacy/truncated/partial/unavailable, keyboard and narrow viewport.
Pre-existing blockers: `DocumentParseResults.tsx:254` lint and `KnowledgeItemsPanel.tsx:270` TS2345.
Global form/scrollbar ownership debt is outside this increment; no blanket UX compliance claim is made.
