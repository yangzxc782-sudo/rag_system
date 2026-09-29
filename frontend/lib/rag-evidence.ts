export type RagCitationItem = {
  citation_id: number;
  chunk_id: string;
  document_id: string;
  original_filename?: string | null;
  chunk_index?: number | null;
  content: string;
  hybrid_score?: number | null;
  retrieval_source?: string | null;
};

export type RagLlmInfo = {
  provider?: string | null;
  model?: string | null;
};

export type RagGraphEntity = {
  id: string;
  name: string;
  entity_type: string;
  page: number | null;
};

export type RagGraphRelationship = {
  source_entity_id: string;
  source_name: string;
  type: string;
  target_entity_id: string;
  target_name: string;
};

export type RagGraphEvidence = {
  graph_id: string;
  anchor_id: string;
  anchor_type: string;
  table_ref: string | null;
  source_citations: number[];
  document: { doc_id: string };
  table: { table_id: string; table_ref: string; page: number | null; table_index: number | null };
  entities: RagGraphEntity[];
  relationships: RagGraphRelationship[];
};

export type RagGraphData = {
  enabled: boolean;
  triggered: boolean;
  status: "success" | "partial" | "not_triggered" | "unavailable";
  truncated: boolean;
  evidence_count: number;
  evidence: RagGraphEvidence[];
};

