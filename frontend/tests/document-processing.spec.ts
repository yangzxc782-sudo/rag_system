import { randomUUID } from "node:crypto";
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";
import type { ReactElement } from "react";
import DocumentDetailPage from "../app/documents/[id]/page";
import DocumentProcessingPanel from "../components/DocumentProcessingPanel";
import DocumentChunkSetPanel from "../components/DocumentChunkSetPanel";
import { configError } from "../lib/chunk-sets";

const api = "http://127.0.0.1:19081";
const defaults = { max_chunk_chars: 1800, min_chunk_chars: 200, overlap_chars: 0,
  max_table_chars: 4000, keep_table_intact: true, keep_formula_with_context: true };
async function configure(request: APIRequestContext, id: string, value: object) {
  expect((await request.post(`${api}/__test/${id}/state`, { data: value })).ok()).toBeTruthy();
}
async function sent(request: APIRequestContext, id: string) { return (await request.get(`${api}/__test/${id}/requests`)).json(); }
const processing = (page: Page) => page.locator("section").filter({ has: page.getByRole("heading", { name: "PDF 完整处理", exact: true }) }).last();
const chunks = (page: Page) => page.locator("section").filter({ has: page.getByRole("heading", { name: "切片版本与重切分", exact: true }) }).last();

test.beforeEach(async ({ page }) => {
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1" && ["3306", "19081"].includes(url.port)) return route.continue();
    return route.abort("blockedbyclient");
  });
});

test("document panel keys are unique among siblings and stable per document", async () => {
  async function keysFor(id: string) {
    const tree = await DocumentDetailPage({ params: Promise.resolve({ id }) });
    const panelKeys: string[] = [];
    function isElement(value: unknown): value is ReactElement<{ children?: unknown }> {
      return value !== null && typeof value === "object" && "type" in value && "props" in value && "key" in value;
    }
    function visit(node: unknown) {
      if (!isElement(node)) return;
      if (node.type === DocumentProcessingPanel || node.type === DocumentChunkSetPanel) {
        expect(node.key).toEqual(expect.any(String));
        panelKeys.push(String(node.key));
      }
      // Production React omits key warnings, so inspect the actual page's element tree too.
      // Read raw keys without React.Children.toArray's positional key prefixes.
      const siblingKeys: string[] = [];
      const children = Array.isArray(node.props.children) ? node.props.children : [node.props.children];
      children.forEach(child => {
        if (isElement(child) && child.key != null) siblingKeys.push(String(child.key));
        visit(child);
      });
      expect(new Set(siblingKeys).size, `Sibling keys: ${JSON.stringify(siblingKeys)}`).toBe(siblingKeys.length);
    }
    visit(tree);
    expect(panelKeys).toHaveLength(2);
    return panelKeys;
  }
  const id = randomUUID();
  const initial = await keysFor(id);
  expect(await keysFor(id)).toEqual(initial);
  const other = await keysFor(randomUUID());
  expect(other.every(key => !initial.includes(key))).toBe(true);
});

test("document panels keep independent form state across router refresh without key warnings", async ({ page, request }) => {
  const id = randomUUID();
  const keyWarnings: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", message => {
    if (/same key|unique [\"']?key/i.test(message.text())) keyWarnings.push(message.text());
  });
  page.on("pageerror", error => pageErrors.push(error.message));
  await configure(request, id, { published: true });
  await page.goto(`/documents/${id}`);
  const processPanel = processing(page), chunkPanel = chunks(page);
  await expect(page.getByRole("heading", { name: "PDF 完整处理", exact: true })).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "切片版本与重切分", exact: true })).toHaveCount(1);
  await expect(processPanel.getByLabel("正文目标长度", { exact: true })).toHaveValue("1800");
  await processPanel.getByLabel("正文目标长度", { exact: true }).fill("1200");
  await chunkPanel.getByLabel("正文目标长度", { exact: true }).fill("3000");
  await Promise.all([
    page.waitForResponse(response => response.url().includes(`/documents/${id}?_rsc=`) && response.ok()),
    chunkPanel.getByRole("button", { name: "刷新状态", exact: true }).click(),
  ]);
  await expect(processPanel.getByLabel("正文目标长度", { exact: true })).toHaveValue("1200");
  await expect(chunkPanel.getByLabel("正文目标长度", { exact: true })).toHaveValue("3000");
  expect(keyWarnings).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(await sent(request, id)).toEqual([]);
});

test("backend defaults, six fields and default process request", async ({ page, request }) => {
  const id = randomUUID(); await page.goto(`/documents/${id}`);
  const panel = processing(page);
  await expect(panel.getByLabel("正文目标长度", { exact: true })).toHaveValue("1800");
  await expect(panel.getByLabel("小尾块阈值")).toHaveValue("200");
  await expect(panel.getByLabel("期望重叠长度（尽量）")).toHaveValue("0");
  await expect(panel.getByLabel("表格分组阈值")).toHaveValue("4000");
  await expect(panel.getByLabel("保持整表")).toBeChecked();
  await expect(panel.getByLabel("短公式与相邻说明合并")).toBeChecked();
  await expect(page.getByRole("option", { name: "固定字符数" })).toHaveCount(0);
  await expect(panel.getByText(/实际重叠可能更少或为 0/)).toBeVisible();
  await panel.getByRole("button", { name: "开始完整处理" }).click();
  await expect.poll(async () => (await sent(request, id)).length).toBe(1);
  const [post] = await sent(request, id);
  expect(post.suffix).toBe("/process"); expect(post.body.config).toEqual(defaults);
  expect(Object.keys(post.body).sort()).toEqual(["config", "request_id"]);
});

test("custom defaults come only from backend, invalid combinations cannot submit", async ({ page, request }) => {
  const id = randomUUID(); await configure(request, id, { defaults: { ...defaults, max_chunk_chars: 2300 } });
  await page.goto(`/documents/${id}`); const panel = processing(page);
  await expect(panel.getByLabel("正文目标长度", { exact: true })).toHaveValue("2300");
  await panel.getByLabel("正文目标长度", { exact: true }).fill("100");
  await expect(panel.getByRole("button", { name: "开始完整处理" })).toBeDisabled();
  await expect(panel.getByText(/缩小正文目标时请同时调整/)).toBeVisible();
  expect(await sent(request, id)).toEqual([]);
});

for (const mode of ["defaultsMissing", "defaultsFail"] as const) test(`missing/unavailable defaults block submission: ${mode}`, async ({ page, request }) => {
  const id = randomUUID(); await configure(request, id, { [mode]: true });
  await page.goto(`/documents/${id}`);
  await expect(processing(page).getByRole("button", { name: "开始完整处理" })).toBeDisabled();
  await expect(processing(page).getByRole("alert")).toBeVisible();
  await expect(chunks(page).getByRole("button", { name: "创建切片任务" })).toBeDisabled();
  await expect(chunks(page).getByRole("alert")).toBeVisible();
  expect(await sent(request, id)).toEqual([]);
});

for (const [name, raw] of Object.entries({ legacy: JSON.stringify({ request_id: randomUUID(), config: { chunk_size: 1200, overlap: 120, boundary: "paragraph" } }),
  malformed: "{broken", unknownVersion: JSON.stringify({ request_id: randomUUID(), config: defaults, segmentation_version: "unknown" }) })) {
  test(`unsupported cache needs explicit discard: ${name}`, async ({ page, request }) => {
    const id = randomUUID();
    await page.addInitScript(({ id, raw }) => sessionStorage.setItem(`pdf-process-request:${id}`, raw), { id, raw });
    await page.goto(`/documents/${id}`); const panel = processing(page);
    await expect(panel.getByRole("button", { name: "丢弃不支持的缓存请求" })).toBeVisible();
    await expect(panel.getByRole("button", { name: "开始完整处理" })).toBeDisabled();
    expect(await sent(request, id)).toEqual([]);
    await panel.getByRole("button", { name: "丢弃不支持的缓存请求" }).click();
    await expect(panel.getByRole("button", { name: "开始完整处理" })).toBeEnabled();
    expect(await sent(request, id)).toEqual([]);
  });
}

test("valid uncertain request survives reload with same identity and config", async ({ page, request }) => {
  const id = randomUUID(); await configure(request, id, { failProcessOnce: true });
  await page.goto(`/documents/${id}`); const panel = processing(page);
  await expect(panel.getByLabel("正文目标长度", { exact: true })).toBeVisible();
  await panel.getByLabel("正文目标长度", { exact: true }).fill("1200");
  await panel.getByLabel("期望重叠长度（尽量）").fill("120");
  await panel.getByRole("button", { name: "开始完整处理" }).click();
  await expect(panel.getByRole("alert")).toContainText("模拟提交结果未知");
  const [first] = await sent(request, id);
  await page.reload(); await expect(processing(page).getByLabel("正文目标长度", { exact: true })).toHaveValue("1200");
  expect((await sent(request, id)).length).toBe(1);
  await processing(page).getByRole("button", { name: "开始完整处理" }).click();
  await expect.poll(async () => (await sent(request, id)).length).toBe(2);
  expect((await sent(request, id))[1].body).toEqual(first.body);
});

test("rechunk sends six values; structure and disabled protection hints are accurate", async ({ page, request }) => {
  const id = randomUUID(); await configure(request, id, { published: true });
  await page.goto(`/documents/${id}`); const panel = chunks(page);
  await panel.getByLabel("正文目标长度", { exact: true }).fill("3000");
  await panel.getByLabel("期望重叠长度（尽量）").fill("100");
  await panel.getByLabel("保持整表").uncheck(); await panel.getByLabel("短公式与相邻说明合并").uncheck();
  await expect(panel.getByText(/普通片段可超过正文目标/)).toBeVisible();
  await expect(panel.getByText(/两种设置都不会拆断独立公式/)).toBeVisible();
  await expect(page.getByText("表格分片（未保持整表）", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "用当前规则创建重切分任务" }).click();
  await expect.poll(async () => (await sent(request, id)).length).toBe(1);
  const [post] = await sent(request, id);
  expect(post.body.operation).toBe("rechunk"); expect(post.body.auto_run).toBe(true);
  expect(post.body.config).toEqual({ ...defaults, max_chunk_chars: 3000, overlap_chars: 100, keep_table_intact: false, keep_formula_with_context: false });
});

test("default contract refresh failure blocks formerly enabled submission until recovery", async ({ page, request }) => {
  const id = randomUUID(); await page.goto(`/documents/${id}`);
  await expect(processing(page).getByRole("button", { name: "开始完整处理" })).toBeEnabled();
  await configure(request, id, { defaultsFail: true });
  await processing(page).getByRole("button", { name: "刷新任务状态" }).click();
  await expect(processing(page).getByRole("button", { name: "开始完整处理" })).toBeDisabled();
  await chunks(page).getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(chunks(page).getByRole("button", { name: "创建切片任务" })).toBeDisabled();
  expect(await sent(request, id)).toEqual([]);
  await configure(request, id, { defaultsFail: false });
  await processing(page).getByRole("button", { name: "刷新任务状态" }).click();
  await chunks(page).getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(processing(page).getByRole("button", { name: "开始完整处理" })).toBeEnabled();
  await expect(chunks(page).getByRole("button", { name: "创建切片任务" })).toBeEnabled();
  expect(await sent(request, id)).toEqual([]);
});

test("uploaded PDF cannot use legacy embedding or index controls", async ({ page, request }) => {
  const id = randomUUID(); await configure(request, id, { processStatus: "uploaded" });
  await page.goto(`/documents/${id}`);
  await expect(processing(page).getByRole("button", { name: "开始完整处理" })).toBeEnabled();
  await expect(page.getByText(/PDF 的切片、向量与索引由版本化处理链路生成/)).toBeVisible();
  await expect(page.getByRole("button", { name: /生成.*[Ee]mbedding|同步.*索引/ })).toHaveCount(0);
  expect(await sent(request, id)).toEqual([]);
});

test("single-step advance uses the frozen config, not changed form values", async ({ page, request }) => {
  const id = randomUUID(); await configure(request, id, { pendingSet: true });
  await page.goto(`/documents/${id}`); const panel = chunks(page);
  await expect(panel.getByText(/冻结配置：正文 1200/)).toBeVisible();
  await panel.getByLabel("正文目标长度", { exact: true }).fill("3000");
  await panel.getByRole("button", { name: "执行下一步", exact: true }).click();
  await expect.poll(async () => (await sent(request, id)).length).toBe(1);
  const [post] = await sent(request, id);
  expect(post.suffix).toMatch(/^\/chunk-sets\/[^/]+\/advance$/);
  expect(post.body).toEqual({ retry: false });
});

for (const operation of ["retry", "resume"] as const) test(`${operation} keeps the job's frozen config`, async ({ page, request }) => {
  const id = randomUUID(), jobId = randomUUID();
  await configure(request, id, { jobs: [{ job_id: jobId, request_id: randomUUID(), operation: "process", status: "failed", stage: "chunking",
    graph_build_id: randomUUID(), chunk_set_id: randomUUID(), managed: operation === "retry", can_retry: true, can_cancel: false,
    attempt_count: 1, max_attempts: 5, completed_units: 0, embedded_count: 0, config: { ...defaults, max_chunk_chars: 1200, overlap_chars: 120 } }] });
  await page.goto(`/documents/${id}`); const panel = processing(page);
  await panel.getByLabel("正文目标长度", { exact: true }).fill("3000");
  await panel.getByRole("button", { name: operation === "retry" ? "显式重试" : "接入后台处理（仍需显式重试）", exact: true }).click();
  await expect.poll(async () => (await sent(request, id)).length).toBe(1);
  const [post] = await sent(request, id);
  expect(post.suffix).toBe(`/processing-jobs/${jobId}/${operation}`);
  expect(post.body).toEqual({});
});

for (const invalid of [{ ...defaults, max_chunk_chars: "1800" }, { ...defaults, max_chunk_chars: 60001 },
  { ...defaults, overlap_chars: 1800 }, { ...defaults, min_chunk_chars: -1 }, { ...defaults, max_table_chars: 0 },
  { ...defaults, keep_table_intact: 1 }, { ...defaults, boundary: "line" }, { ...defaults, max_chunk_chars: 1.5 }]) {
  test(`strict config rejects ${JSON.stringify(invalid)}`, () => expect(configError(invalid)).not.toBeNull());
}
