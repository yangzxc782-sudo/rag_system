export type RagCitationItem = {
  citation_id: number; chunk_id: string; document_id: string;
  original_filename?: string | null; chunk_index?: number | null;
  content: string; hybrid_score?: number | null; retrieval_source?: string | null;
};
export type RagLlmInfo = { provider?: string | null; model?: string | null };
export type GraphAnchor = {
  graph_id: string; anchor_id: string; heading_ref?: string | null; heading?: string | null;
} & ({ anchor_type: "table"; table_ref: string; table_no?: string | null } |
     { anchor_type: "clause"; clause_ref: string });
export type RagGraphEntity = {
  id: string; name: string; entity_type: string; properties: Record<string, unknown>;
};
export type RagGraphRelationship = {
  id: string; source_entity_id: string; source_name: string; type: string;
  target_entity_id: string; target_name: string; properties: Record<string, unknown>;
};
export type RagGraphEvidence = {
  anchor: GraphAnchor; source_citations: number[];
  source: { document_id: string; source_version: string; graph_build_id: string;
            unit_id: string; source_start: number; source_end: number };
  entities: RagGraphEntity[]; relationships: RagGraphRelationship[];
};
export type RagGraphDiagnostic = {
  anchor_id: string; graph_id: string; mapped: boolean; query_status: string;
  full_unit_covered: boolean; facts_used: boolean; use_status: string;
};
export type RagGraphData = {
  schema_version: 2; enabled: boolean; triggered: boolean;
  status: "success" | "partial" | "not_triggered" | "unavailable" | "not_used";
  truncated: boolean; evidence_count: number; evidence: RagGraphEvidence[];
  diagnostics: RagGraphDiagnostic[]; source_error?: string | null;
};
