// Synthetic, loopback-only API. It has no upstream clients and never forwards requests.
import http from "node:http";
import { randomUUID } from "node:crypto";

if (process.env.PDF_STRUCTURE_E2E_MOCK !== "1") throw new Error("Explicit mock gate required");
const VERSION = "pdf-block-aware-codepoints-v1";
const DEFAULTS = { max_chunk_chars: 1800, min_chunk_chars: 200, overlap_chars: 0,
  max_table_chars: 4000, keep_table_intact: true, keep_formula_with_context: true };
const documents = new Map();
const stamp = "2026-10-08T00:00:00Z";
const envelope = data => ({ success: true, data, error: null });
const pagination = items => ({ items, total: items.length, limit: 50, offset: 0 });
function state(id) {
  if (!documents.has(id)) documents.set(id, { jobs: [], sets: [], requests: [], source: randomUUID(), build: randomUUID(),
    defaults: DEFAULTS, defaultsMissing: false, defaultsFail: false, published: false, failProcessOnce: false });
  return documents.get(id);
}
function contract(s) { return s.defaultsMissing ? {} : { segmentation_defaults: s.defaults, segmentation_version: VERSION }; }
function job(id, body) {
  return { job_id: randomUUID(), document_id: id, request_id: body.request_id, operation: "process", status: "queued", stage: "uploaded",
    source_version: null, graph_build_id: null, chunk_set_id: null, parse_run_id: null, attempt_count: 0, max_attempts: 5,
    last_error_code: null, lease_expires_at: null, can_retry: false, can_cancel: true, cancel_requested: false,
    requires_io_reconciliation: false, managed: true, config: body.config, created_at: stamp, updated_at: stamp,
    graph_status: null, unit_count: null, completed_units: 0, chunk_count: null, embedded_count: 0, chunk_set_status: null };
}
function chunkSet(s, body = {}) {
  return { chunk_set_id: randomUUID(), source_version: s.source, graph_build_id: s.build, request_id: body.request_id ?? randomUUID(),
    status: "indexed", job_status: "succeeded", stage: "indexed", chunk_count: 1, embedding_counts: { embedded: 1 },
    segmentation_config: body.config ?? s.defaults, segmentation_version: VERSION, segmentation_config_sha256: "a".repeat(64),
    is_current: true, publication_revision: 1, last_error_code: null, can_advance: false, search_enabled: true,
    managed: false, job_id: null };
}
const server = http.createServer(async (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  const send = (status, data) => { res.statusCode = status; res.end(JSON.stringify(data)); };
  if (req.method === "OPTIONS") return send(204, null);
  const path = new URL(req.url, "http://127.0.0.1:19081").pathname;
  if (path === "/health" && req.method === "GET") return send(200, { mock: "pdf-structure" });
  const match = path.match(/^\/(?:api\/v1\/documents|__test)\/([0-9a-f-]+)(.*)$/i);
  if (!match) return send(404, { error: "Unknown mock route" });
  const [, id, suffix] = match, s = state(id);
  let body = {};
  if (req.method === "POST") {
    const parts = [];
    for await (const part of req) parts.push(part);
    try { body = JSON.parse(Buffer.concat(parts).toString() || "{}"); } catch { return send(400, { error: "Invalid JSON" }); }
  }
  if (path.startsWith("/__test/")) {
    if (req.method === "POST" && suffix === "/state") {
      for (const key of ["defaults", "defaultsMissing", "defaultsFail", "published", "failProcessOnce", "jobs", "processStatus"]) {
        if (key in body) s[key] = body[key];
      }
      if (s.published && !s.sets.length) s.sets.push(chunkSet(s));
      if (body.pendingSet) s.sets = [{ ...chunkSet(s), status: "chunks_ready", job_status: "queued", stage: "chunks_ready",
        is_current: false, can_advance: true, segmentation_config: { ...DEFAULTS, max_chunk_chars: 1200, overlap_chars: 120 } }];
      return send(200, { configured: true });
    }
    if (req.method === "GET" && suffix === "/requests") return send(200, s.requests);
    return send(404, { error: "Unknown test route" });
  }
  if (req.method === "GET") {
    if (suffix === "") return send(200, envelope({ id, original_filename: "synthetic.pdf", file_type: ".pdf", mime_type: "application/pdf",
      file_size: 123, file_hash: "a".repeat(64), process_status: s.processStatus ?? "cleaned_source_ready", deletion_status: "normal",
      bucket_name: "synthetic", object_key: "synthetic.pdf", error_message: null, created_at: stamp, updated_at: stamp }));
    if (["/chunk-sets", "/processing-jobs"].includes(suffix) && s.defaultsFail) return send(503, { success: false, data: null, error: { code: "MOCK_UNAVAILABLE", message: "配置暂不可用" } });
    if (suffix === "/chunk-sets") return send(200, envelope({ ...pagination(s.sets), current: s.sets[0] ?? null,
      process_ready: { source_version: s.source, graph_build_id: s.build, request_id: s.source }, ...contract(s) }));
    if (suffix === "/processing-jobs") return send(200, envelope({ items: s.jobs, total: s.jobs.length,
      executor_enabled: true, search_enabled: true, can_process: !s.published && !s.jobs.length, ...contract(s) }));
    if (suffix.startsWith("/processing-jobs/")) {
      const found = s.jobs.find(j => j.job_id === suffix.split("/")[2]);
      return send(found ? 200 : 404, envelope(found ?? null));
    }
    if (suffix === "/parse-status") return send(200, envelope({ document_id: id, process_status: "cleaned_source_ready", latest_parse_run: null, active_parse_run: null }));
    if (suffix === "/parse-runs") return send(200, envelope(pagination([])));
    if (suffix === "/chunks") {
      const rows = s.published ? [{ id: s.source, document_id: id, chunk_index: 0, content: "合成表格😀", character_count: 6,
        section_title: "合成章节", chunk_type: "table", chunk_method: VERSION, content_format: "markdown", page_start: 1, page_end: 2,
        source_start: 0, source_end: 6, source_metadata: { section_path: ["上级", "合成章节"], block_types: ["table"], block_ids: [s.build], asset_keys: [], table_fragmented: true },
        embedding_status: "embedded", created_at: stamp, updated_at: stamp }] : [];
      return send(200, envelope({ ...pagination(rows), stats: { chunk_count: rows.length, total_characters: 6 * rows.length,
        min_characters: 6 * rows.length, max_characters: 6 * rows.length, avg_characters: 6 * rows.length } }));
    }
  }
  if (req.method === "POST") {
    s.requests.push({ method: "POST", suffix, body });
    if (suffix === "/process") {
      if (s.failProcessOnce) { s.failProcessOnce = false; return send(503, { success: false, data: null, error: { message: "模拟提交结果未知" } }); }
      const result = s.jobs.find(j => j.request_id === body.request_id) ?? job(id, body);
      if (!s.jobs.includes(result)) s.jobs.push(result);
      return send(202, envelope(result));
    }
    if (suffix === "/chunk-sets") { const result = chunkSet(s, body); s.sets.unshift(result); return send(200, envelope(result)); }
    if (/^\/chunk-sets\/[0-9a-f-]+\/advance$/.test(suffix)) {
      const found = s.sets.find(item => item.chunk_set_id === suffix.split("/")[2]);
      return send(found ? 200 : 404, envelope(found ?? null));
    }
    if (/^\/processing-jobs\/[0-9a-f-]+\/(retry|resume|cancel)$/.test(suffix)) {
      const found = s.jobs.find(j => j.job_id === suffix.split("/")[2]);
      return send(found ? 202 : 404, envelope(found ?? null));
    }
  }
  return send(404, { error: "Unknown mock route; no forwarding" });
});
server.listen(19081, "127.0.0.1");
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => server.close());
