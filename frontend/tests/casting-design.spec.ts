import { expect, test, type Page, type Route } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { A, B, MockConversations, answer, fail, ok } from "./mock-conversations";
import type { CastingAnswerInfo, CastingInputFile } from "../lib/casting-design";
import { readPending, savePending } from "../lib/qa-pending";
import { qaErrorMessage, safeIssues, type TurnInput } from "../lib/qa-sessions";

const raw = readFileSync("../backend/tests/fixtures/casting/converted.input.json");
const file = (name = "input.json", buffer = raw) => ({ name, mimeType: "application/json", buffer });
const input = (fid?: string): TurnInput => ({ request_id: randomUUID(), question: "按附件计算浇冒系统方案", limit: 8, document_id: null,
  ...(fid ? { casting_input_file_id: fid } : {}) });
const messages = (page: Page) => page.getByRole("list", { name: "会话消息" }).locator(":scope > li");
const pane = (page: Page) => page.getByRole("region", { name: "当前对话", exact: true });
async function send(page: Page, question = "按附件计算浇冒系统方案") {
  await page.getByLabel("本轮问题", { exact: true }).fill(question);
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
}
class CastingMock extends MockConversations {
  files: CastingInputFile[] = [];
  reusable: CastingInputFile | null = null;
  uploads: { sid: string; rid: string; filename: string; multipart: string }[] = [];
  onUpload?: (route: Route, saved: CastingInputFile) => Promise<void>;
  onList?: (route: Route, sid: string) => Promise<void>;
  addFile(sid = A, filename = "input.json", rid: string = randomUUID()): CastingInputFile {
    const saved: CastingInputFile = { thread_id: sid, file_id: randomUUID(), request_id: rid, original_filename: filename,
      content_type: "application/json", size_bytes: raw.length, sha256: "a".repeat(64), storage_state: "ready", admission_passed: false, created_at: "2026-09-30T00:00:00Z" };
    this.files.push(saved); return saved;
  }
  engineering(sid: string, q: TurnInput, patch: Partial<CastingAnswerInfo> = {}) {
    const selected = this.files.find(f => f.thread_id === sid && f.file_id === q.casting_input_file_id) ?? this.reusable;
    if (selected) { selected.admission_passed = true; if (q.casting_input_file_id) this.reusable = selected; }
    const result = { ...answer(q.question), context_status: "casting_design" as const, citations: [], graph: null,
      answer: "程序推荐 C01。冒口直径 170 mm。真实 CAE：Pending。",
      casting: { result_status: "success", route: "calculate", run_id: randomUUID(), result_file_id: randomUUID(), result_sha256: "b".repeat(64),
        input_file_id: selected?.file_id ?? null, input_reused: !q.casting_input_file_id, rule_id: "PMP-TRIAL-RULES", rule_version: "1", rule_sha256: "c".repeat(64),
        recommended_candidate_id: "C01", candidate_count: 4, candidate_rank: null, summary_mode: "llm_fact_refs", error: null, ...patch } as CastingAnswerInfo };
    const status = this.complete(sid, q, result);
    for (const m of this.messages.get(sid)!.filter(m => m.turn_id === status.turn_id)) {
      m.effective_casting_input_file_id = selected?.file_id; m.casting_input_filename = selected?.original_filename;
    }
    return status;
  }
  async install(page: Page) {
    await super.install(page);
    await page.route("**/api/v1/rag/sessions/*/casting-**", async route => {
      const req = route.request(), url = new URL(req.url()), sid = url.pathname.split("/")[5];
      if (req.method() === "OPTIONS") { await route.fulfill({ status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "content-type" } }); return; }
      if (url.pathname.endsWith("/recommendation")) {
        await route.fulfill({ contentType: "application/json", body: '{"candidates":[{"diameter_mm":170}]}', headers: { "Access-Control-Allow-Origin": "*" } }); return;
      }
      if (req.method() === "GET") {
        if (this.onList) { await this.onList(route, sid); return; }
        await ok(route, { items: this.files.filter(f => f.thread_id === sid), next_before_id: null, reusable_input: this.reusable?.thread_id === sid ? this.reusable : null }); return;
      }
      const body = req.postDataBuffer()!.toString("utf8");
      const rid = /name="request_id"\r\n\r\n([^\r]+)/.exec(body)![1];
      const filename = /filename="([^"]+)"/.exec(body)![1];
      this.uploads.push({ sid, rid, filename, multipart: body });
      const saved = this.files.find(f => f.request_id === rid && f.thread_id === sid) ?? this.addFile(sid, filename, rid);
      if (this.onUpload) { await this.onUpload(route, saved); return; }
      await ok(route, saved);
    });
    if (!this.onTurn) this.onTurn = async (route, sid, q) => { await ok(route, this.engineering(sid, q)); };
  }
}
async function open(page: Page, api: CastingMock) {
  await api.install(page); await page.goto(`/rag/${A}`);
  await page.getByRole("button", { name: "工程 JSON 附件", exact: true }).click();
}
async function upload(page: Page, name = "input.json") {
  await page.getByLabel("选择 JSON 文件", { exact: true }).setInputFiles(file(name));
  await expect(page.getByText("已选定，随本轮问题提交", { exact: true })).toBeVisible();
}

test("upload exact bytes, send only file id, show names and source after refresh, download", async ({ page }) => {
  const api = new CastingMock(); await open(page, api); await upload(page); await send(page);
  await expect(messages(page)).toHaveCount(2);
  expect(api.uploads[0].multipart).toContain(raw.toString("utf8"));
  expect(Object.keys(api.posts[0].input).sort()).toEqual(["casting_input_file_id", "document_id", "limit", "question", "request_id"]);
  expect(api.posts[0].input.casting_input_file_id).toBe(api.files[0].file_id);
  await expect(messages(page).first()).toContainText("工程附件：input.json");
  await expect(page.getByText("方案计算完成", { exact: true })).toBeVisible();
  await expect(page.getByText("规则：PMP-TRIAL-RULES · 版本 1", { exact: true })).toBeVisible();
  await page.reload(); await expect(messages(page)).toHaveCount(2);
  await expect(messages(page).first()).toContainText("input.json");
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 recommendation.json", exact: true }).click();
  expect((await downloaded).suggestedFilename()).toMatch(/^recommendation-.*\.json$/);
  expect(api.uploads).toHaveLength(1); expect(api.posts).toHaveLength(1);
});

for (const scenario of ["no_feasible_candidate", "admission_failed"] as const) test(`${scenario} is a visible business result`, async ({ page }) => {
  const api = new CastingMock();
  api.onTurn = async (route, sid, q) => { await ok(route, api.engineering(sid, q, { result_status: scenario,
    recommended_candidate_id: null, candidate_count: 0, result_file_id: scenario === "admission_failed" ? null : randomUUID(),
    error: scenario === "admission_failed" ? { category: "admission", code: "CASTING_ADMISSION_FAILED", message: "未确认", retryable: false,
      issues: [{ field_path: "casting_mass_kg", error_code: "Proposed", message: "质量需要确认" }] } : null })); };
  await open(page, api); await upload(page); await send(page); await expect(messages(page)).toHaveCount(2);
  await expect(page.getByText(scenario === "admission_failed" ? "输入未通过准入" : "暂无可行方案", { exact: true })).toBeVisible();
  if (scenario === "admission_failed") {
    await expect(page.getByRole("list", { name: "工程字段问题" })).toContainText("casting_mass_kg：质量需要确认");
    await expect(page.getByRole("button", { name: "下载 recommendation.json" })).toHaveCount(0);
  }
  await expect(page.getByLabel("本轮问题", { exact: true })).toBeEnabled();
});

test("server field errors stay visible; bad upload cannot silently send previous attachment", async ({ page }) => {
  const api = new CastingMock(); await open(page, api); await upload(page);
  api.onUpload = async route => { await route.fulfill({ status: 422, headers: { "Access-Control-Allow-Origin": "*" }, json: {
    success: false, error: { code: "CASTING_INPUT_INVALID", detail: { issues: [{ field_path: "input.hotspots.0.modulus_mm", error_code: "Invalid", message: "必须是正数" }], traceback: "PRIVATE_TRACE" } } } }); };
  await page.getByLabel("选择 JSON 文件").setInputFiles(file("broken.json"));
  await expect(page.getByRole("list", { name: "工程字段问题" })).toContainText("input.hotspots.0.modulus_mm");
  await page.getByLabel("本轮问题", { exact: true }).fill("请计算");
  await expect(page.getByRole("button", { name: "发送问题", exact: true })).toBeDisabled();
  await expect(page.getByText("PRIVATE_TRACE")).toHaveCount(0);
  await page.getByRole("button", { name: "移除本轮附件" }).click();
  await expect(page.getByRole("button", { name: "发送问题", exact: true })).toBeEnabled();
  expect(api.posts).toHaveLength(0);
});

for (const [name, size, message] of [["input.txt", 1, "请选择一个 .json 文件。"], ["big.json", 262145, "工程 JSON 文件不能超过 256 KiB。"]] as const) {
  test(`client rejects ${name} before upload`, async ({ page }) => {
    const api = new CastingMock(); await open(page, api);
    await page.getByLabel("选择 JSON 文件").setInputFiles(file(name, Buffer.alloc(size, 32)));
    await expect(page.getByText(message, { exact: true })).toBeVisible(); expect(api.uploads).toHaveLength(0);
  });
}

test("uncertain upload retries same request id and never stores file bytes", async ({ page }) => {
  const api = new CastingMock();
  api.onUpload = async (route, saved) => { if (api.uploads.length === 1) await route.abort("timedout"); else await ok(route, saved); };
  await open(page, api); await page.getByLabel("选择 JSON 文件").setInputFiles(file());
  await page.getByRole("button", { name: "使用原文件重试上传" }).click();
  await expect(page.getByText("已选定，随本轮问题提交", { exact: true })).toBeVisible();
  expect(api.uploads[0].rid).toBe(api.uploads[1].rid); expect(api.files).toHaveLength(1);
  const saved = await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }));
  expect(saved).not.toContain("casting_mass_kg"); expect(saved).not.toContain("parameter_metadata");
});

test("upload blocks send; cancelled late upload cannot select a file", async ({ page }) => {
  const api = new CastingMock(); let release!: () => void;
  const gate = new Promise<void>(r => { release = r; });
  api.onUpload = async (route, saved) => { await gate; await ok(route, saved).catch(() => {}); };
  await open(page, api); await page.getByLabel("本轮问题", { exact: true }).fill("请计算");
  await page.getByLabel("选择 JSON 文件").setInputFiles(file());
  await expect(page.getByText("正在上传与检查结构…", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "发送问题", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "移除本轮附件" }).click(); release();
  await expect(page.getByRole("button", { name: "发送问题", exact: true })).toBeEnabled();
  await expect(page.getByText("已选定，随本轮问题提交", { exact: true })).toHaveCount(0);
});

test("switching thread aborts pending attachment; late completion never enters B", async ({ page }) => {
  const api = new CastingMock(); let release!: () => void;
  const gate = new Promise<void>(r => { release = r; });
  api.onUpload = async (route, saved) => { await gate; await ok(route, saved).catch(() => {}); };
  await open(page, api); await page.getByLabel("选择 JSON 文件").setInputFiles(file("only-a.json"));
  await expect.poll(() => api.uploads.length).toBe(1);
  await page.getByRole("link", { name: "会话 B", exact: true }).click(); release();
  await page.getByRole("button", { name: "工程 JSON 附件", exact: true }).click();
  await expect(page.getByText("only-a.json", { exact: true })).toHaveCount(0);
  await send(page, "冒口的作用？"); await expect(messages(page)).toHaveCount(2);
  expect(api.posts[0].sid).toBe(B); expect(api.posts[0].input.casting_input_file_id).toBeUndefined();
});

test("network loss and refresh retry frozen attachment even after new upload exists", async ({ page }) => {
  const api = new CastingMock();
  api.onTurn = async (route, sid, q) => {
    if (api.posts.length === 1) { api.begin(sid, q, "needs_recovery"); await route.abort("timedout"); }
    else await ok(route, api.engineering(sid, q));
  };
  await open(page, api); await upload(page); await send(page);
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toBeVisible();
  api.addFile(A, "new-unselected.json"); await page.reload();
  await expect(page.getByText(/原请求工程附件/)).toContainText(api.files[0].file_id);
  await page.getByRole("button", { name: "使用原请求重试" }).click();
  await expect(messages(page)).toHaveCount(2); expect(api.posts[1]).toEqual(api.posts[0]); expect(api.uploads).toHaveLength(1);
});

test("202 polls without another calculation request and fresh browser uses server input", async ({ page }) => {
  const api = new CastingMock(), saved = api.addFile(); const q = input(saved.file_id);
  const active = api.begin(A, q, "needs_recovery");
  api.onTurn = async route => { Object.assign(active, { execution_active: true, status: "running", can_retry: false }); await ok(route, active, 202); };
  api.onStatus = async route => { await ok(route, api.engineering(A, q)); };
  await open(page, api); await page.getByRole("button", { name: "使用原请求重试" }).click();
  await expect(messages(page)).toHaveCount(2); expect(api.posts).toHaveLength(1); expect(api.posts[0].input).toEqual(q);
});

test("server-selected reusable input differs from latest unselected upload; followup omits explicit file", async ({ page }) => {
  const api = new CastingMock(), valid = api.addFile(A, "selected.json"); valid.admission_passed = true; api.reusable = valid;
  api.addFile(A, "unused.json");
  await open(page, api); await expect(page.getByText(/后端可复用最近明确选定/)).toContainText("selected.json");
  await send(page, "解释上次方案"); await expect(messages(page)).toHaveCount(2);
  expect(api.posts[0].input.casting_input_file_id).toBeUndefined();
  await expect(page.getByText(`复用会话有效输入：${valid.file_id}`, { exact: true })).toBeVisible();
});

test("ordinary question with JSON can still produce knowledge citations", async ({ page }) => {
  const api = new CastingMock(); api.onTurn = async (route, sid, q) => { await ok(route, api.complete(sid, q, answer(q.question))); };
  await open(page, api); await upload(page); await send(page, "冒口有什么作用？仅解释概念。");
  await expect(page.getByText("知识库回答", { exact: true })).toBeVisible();
  await page.getByText("文本依据 · 1 条", { exact: true }).click();
  await expect(page.getByRole("article", { name: "引用 1" })).toContainText("冒口用于补缩");
  await expect(page.getByText("浇冒系统设计", { exact: true })).toHaveCount(0);
});

test("disabled engineering upload leaves ordinary chat usable", async ({ page }) => {
  const api = new CastingMock(); api.onList = async route => { await fail(route, "CASTING_FEATURE_DISABLED", 404); };
  api.onTurn = async (route, sid, q) => { await ok(route, api.complete(sid, q)); };
  await open(page, api); await expect(pane(page).getByRole("alert")).toContainText("浇冒系统设计功能尚未启用");
  await send(page, "冒口是什么？"); await expect(messages(page)).toHaveCount(2);
});

test("foreign upload and altered pending attachment are rejected", async ({ page }) => {
  const api = new CastingMock(); api.onUpload = async (route, saved) => { await ok(route, { ...saved, thread_id: B }); };
  await open(page, api); await page.getByLabel("选择 JSON 文件").setInputFiles(file());
  await expect(pane(page).getByRole("alert")).toContainText("响应格式或会话归属异常"); expect(api.posts).toHaveLength(0);
  api.onUpload = undefined; await upload(page);
  api.onTurn = async (route, sid, q) => { await ok(route, { ...api.begin(sid, q), input: { ...q, casting_input_file_id: B } }, 202); };
  await send(page); await expect(pane(page).getByRole("alert")).toContainText("响应格式或会话归属异常");
  expect(api.posts).toHaveLength(1); await expect(page.getByRole("button", { name: "使用原请求重试" })).toHaveCount(0);
});

test("confirmed unaccepted file rejection permits correction and ordinary chat", async ({ page }) => {
  const api = new CastingMock();
  api.onTurn = async route => { await fail(route, "CASTING_FILE_NOT_FOUND", 404); };
  await open(page, api); await upload(page); await send(page);
  await expect(pane(page).getByRole("alert")).toContainText("找不到该工程文件");
  await expect(page.getByLabel("本轮问题", { exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toHaveCount(0);
  await page.getByRole("button", { name: "移除本轮附件" }).click();
  api.onTurn = async (route, sid, q) => { await ok(route, api.complete(sid, q)); };
  await send(page, "冒口有什么作用？"); await expect(messages(page)).toHaveCount(2);
  expect(api.posts[1].input.casting_input_file_id).toBeUndefined();
});

test("selecting an older uploaded JSON freezes that id", async ({ page }) => {
  const api = new CastingMock(), older = api.addFile(A, "older.json"), latest = api.addFile(A, "latest.json");
  api.onList = async route => { await ok(route, { items: new URL(route.request().url()).searchParams.has("before_id") ? [older] : [latest],
    next_before_id: new URL(route.request().url()).searchParams.has("before_id") ? null : latest.file_id, reusable_input: null }); };
  await open(page, api); await page.getByRole("button", { name: "加载更多工程文件" }).click();
  await page.getByLabel("本会话已上传 JSON").selectOption(older.file_id);
  await send(page); await expect(messages(page)).toHaveCount(2);
  expect(api.posts[0].input.casting_input_file_id).toBe(older.file_id); expect(api.uploads).toHaveLength(0);
});

test("pending v1 accepts old records, keeps only frozen file id and rejects foreign metadata", () => {
  const entries = new Map<string, string>();
  const storage = { getItem: (key: string) => entries.get(key) ?? null, setItem: (key: string, value: string) => entries.set(key, value) };
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: storage });
  try {
    const old = input(); savePending(A, old); expect(readPending(A)).toEqual(old);
    const attached = input(B); savePending(A, { ...attached, raw: "PRIVATE_JSON" } as TurnInput);
    expect(readPending(A)).toEqual(attached); expect(entries.get(`qa.pending.v1:${A}`)).not.toContain("PRIVATE_JSON");
    expect(readPending(B)).toBeNull();
    entries.set(`qa.pending.v1:${A}`, JSON.stringify({ ...attached, thread_id: A, casting_input_file_id: "/server/file.json" }));
    expect(readPending(A)).toBeNull();
  } finally { Reflect.deleteProperty(globalThis, "sessionStorage"); }
});

test("field detail decoding is bounded and ignores internal diagnostics", () => {
  const issues = Array.from({ length: 101 }, () => ({ field_path: "x", error_code: "Bad", message: "修正", traceback: "SECRET" }));
  expect(safeIssues({ issues })).toHaveLength(100); expect(JSON.stringify(safeIssues({ issues }))).not.toContain("SECRET");
  expect(safeIssues({ issues: [{ field_path: {}, error_code: 3, message: "x" }] })).toEqual([]);
  expect(qaErrorMessage("CASTING_INPUT_INVALID")).toContain("JSON");
});
