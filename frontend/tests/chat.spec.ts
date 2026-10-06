import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { A, B, answer, fail, MockConversations, ok } from "./mock-conversations";
import { qaErrorMessage, type TurnInput } from "../lib/qa-sessions";

const input = (question = "冒口有什么作用？"): TurnInput => ({ request_id: randomUUID(), question, limit: 8, document_id: null });
async function open(page: Page, api: MockConversations, sid = A) { await api.install(page); await page.goto(`/rag/${sid}`); }
async function send(page: Page, question = "冒口有什么作用？") {
  await page.getByLabel("本轮问题", { exact: true }).fill(question);
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
}
const messages = (page: Page) => page.getByRole("list", { name: "会话消息" }).locator(":scope > li");

test("rewrite format failure stays technical after refresh and retries the original request", async ({ page }) => {
  const api = new MockConversations();
  api.onTurn = async (route, sid, q) => {
    if (api.posts.length === 1) {
      const state = api.begin(sid, q, "failed");
      Object.assign(state, { can_retry: true, error_code: "QA_REWRITE_OUTPUT_INVALID" });
      await fail(route, "QA_REWRITE_OUTPUT_INVALID", 502);
    } else await ok(route, api.complete(sid, q));
  };
  await open(page, api); await send(page, "它的成分含有什么？");
  await expect(messages(page)).toHaveCount(1);
  await expect(page.getByRole("region", { name: "当前对话", exact: true }).getByRole("alert"))
    .toContainText("问题理解服务返回的格式或引用无效");
  await expect(page.getByText("需要澄清", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(messages(page)).toHaveCount(1);
  await page.getByRole("button", { name: "使用原请求重试" }).click();
  await expect(messages(page)).toHaveCount(2);
  expect(api.posts[0]).toEqual(api.posts[1]);
});

for (const code of ["QA_CONTEXT_BUDGET_EXCEEDED", "QA_REWRITE_STRATEGY_UNSUPPORTED"]) {
  test(`${code} remains a nonretryable failure without clarification`, async ({ page }) => {
    const api = new MockConversations(), state = api.begin(A, input(), "failed");
    Object.assign(state, { can_retry: false, error_code: code });
    await open(page, api);
    await expect(messages(page)).toHaveCount(1);
    await expect(page.getByRole("region", { name: "当前对话", exact: true }).getByText(qaErrorMessage(code), { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "使用原请求重试" })).toHaveCount(0);
    await expect(page.getByText("需要澄清", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("本轮问题", { exact: true })).toBeEnabled();
    expect(api.posts).toHaveLength(0);
  });
}

test("create survives timeout + reload using one request id; rename persists", async ({ page }) => {
  const api = new MockConversations(); api.createAbort = true; await api.install(page); await page.goto("/rag");
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.getByRole("button", { name: "重试新建对话" })).toBeVisible();
  await page.reload(); await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page).toHaveURL(/000c$/); expect(api.creates).toHaveLength(2); expect(api.creates[0]).toBe(api.creates[1]);
  await page.getByRole("button", { name: "重命名 新会话", exact: true }).click();
  await page.getByLabel("会话标题").fill("冒口设计讨论"); await page.getByRole("button", { name: "保存标题" }).click();
  await expect(page.getByRole("heading", { name: "冒口设计讨论", exact: true })).toBeVisible();
  await page.reload(); await expect(page.getByRole("heading", { name: "冒口设计讨论", exact: true })).toBeVisible();
});

test("session cursor pages deduplicate and new navigation replaces legacy UI", async ({ page }) => {
  const api = new MockConversations(); api.listPages = true; await api.install(page); await page.goto("/rag");
  await page.getByRole("button", { name: "加载更多会话" }).click();
  const list = page.getByRole("list", { name: "会话列表" });
  await expect(list.getByRole("link")).toHaveCount(2); await list.getByRole("link", { name: "会话 B" }).click();
  await expect(page).toHaveURL(`/rag/${B}`); await expect(page.getByRole("heading", { name: "多轮知识问答" })).toBeVisible();
  await expect(page.getByText("单轮知识问答", { exact: true })).toHaveCount(0);
  await page.goto("/rag/single"); await expect(page.getByRole("heading", { name: "404" })).toBeVisible();
});

test("first question, pronoun, omission and topic switch keep thread and only send fixed parameters", async ({ page }) => {
  const api = new MockConversations(); await open(page, api);
  const questions = ["冒口有什么作用？", "那它尺寸怎么确定？", "有哪些限制条件？", "铝合金热裂的原因是什么？"];
  for (const [i, q] of questions.entries()) { await send(page, q); await expect(messages(page)).toHaveCount(2 * (i + 1)); }
  expect(api.posts.map(p => p.sid)).toEqual([A, A, A, A]); expect(new Set(api.posts.map(p => p.input.request_id)).size).toBe(4);
  expect(api.posts.map(p => p.input.question)).toEqual(questions);
  for (const p of api.posts) expect(Object.keys(p.input).sort()).toEqual(["document_id", "limit", "question", "request_id"]);
  await page.reload(); await expect(messages(page)).toHaveCount(8); expect(api.posts).toHaveLength(4);
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter(k => k.startsWith("qa.pending")))).toEqual([]);
});

test("direct URL restores committed history; older pages maintain order and viewport", async ({ page }) => {
  const api = new MockConversations();
  for (let n = 0; n < 32; n++) api.complete(A, input(`问题 ${n}`));
  await open(page, api); await expect(messages(page)).toHaveCount(50);
  const area = page.getByRole("region", { name: "聊天记录" });
  await area.evaluate(el => { el.scrollTop = 0; });
  const first = messages(page).first(); const id = await first.getAttribute("data-message-id");
  const y = (await first.boundingBox())!.y;
  await page.getByRole("button", { name: "加载更早消息" }).click();
  await expect(messages(page)).toHaveCount(64);
  const y2 = (await page.locator(`[data-message-id="${id}"]`).boundingBox())!.y;
  expect(Math.abs(y2 - y)).toBeLessThan(65); // Removed page button accounts for its own height.
  expect(await messages(page).evaluateAll(nodes => nodes.map(n => Number(n.getAttribute("data-sequence"))))).toEqual(Array.from({ length: 64 }, (_, i) => i + 1));
  expect(api.reads.some(p => p.includes("before_seq=15"))).toBe(true);
});

test("202 follows status URL without repeating POST", async ({ page }) => {
  const api = new MockConversations(); let polls = 0;
  api.onTurn = async (route, sid, q) => { await ok(route, api.begin(sid, q), 202, { "Retry-After": "1", "Access-Control-Expose-Headers": "Retry-After,Location" }); };
  api.onStatus = async (route, sid, rid) => {
    polls++; const state = api.states.get(`${sid}:${rid}`)!;
    await ok(route, polls === 1 ? state : api.complete(sid, state.input!));
  };
  await open(page, api); await send(page);
  await expect(page.getByLabel("本轮问题", { exact: true })).toBeDisabled();
  await expect(messages(page)).toHaveCount(2); expect(api.posts).toHaveLength(1); expect(polls).toBe(2);
});

test("network interruption queries first and explicit retry preserves question and parameters", async ({ page }) => {
  const api = new MockConversations();
  api.onTurn = async (route, sid, q) => {
    if (api.posts.length === 1) { api.begin(sid, q, "needs_recovery"); await route.abort("timedout"); }
    else await ok(route, api.complete(sid, q));
  };
  await open(page, api);
  await page.getByText("本轮检索设置", { exact: true }).click();
  await page.getByLabel("检索条数", { exact: true }).fill("3"); await page.getByLabel("限定文档", { exact: true }).fill(B);
  await send(page, "  冒口尺寸？  ");
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toBeVisible();
  expect(api.posts).toHaveLength(1); expect(api.reads.some(p => p.includes("/requests/"))).toBe(true);
  await page.reload(); await page.getByRole("button", { name: "使用原请求重试" }).click();
  await expect(messages(page)).toHaveCount(2); expect(api.posts[1]).toEqual(api.posts[0]);
});

test("fresh browser with no pending storage recovers active request from server input", async ({ page }) => {
  const api = new MockConversations(), q = { ...input(), limit: 4, document_id: B };
  api.begin(A, q, "needs_recovery"); await open(page, api);
  await page.getByRole("button", { name: "使用原请求重试" }).click();
  await expect(messages(page)).toHaveCount(2); expect(api.posts[0].input).toEqual(q);
});

test("active request running on refresh polls and blocks a new question", async ({ page }) => {
  const api = new MockConversations(), q = input(); api.begin(A, q);
  api.onStatus = async (route, sid) => { await ok(route, api.complete(sid, q)); };
  await open(page, api); await expect(page.getByLabel("本轮问题", { exact: true })).toBeDisabled();
  await expect(messages(page)).toHaveCount(2); expect(api.posts).toHaveLength(0);
});

for (const canRetry of [true, false]) test(`failed latest turn can_retry=${canRetry}`, async ({ page }) => {
  const api = new MockConversations(), state = api.begin(A, input(), "failed");
  Object.assign(state, { can_retry: canRetry, error_code: canRetry ? "LLM_TIMEOUT" : "QA_TURN_SUPERSEDED" });
  await open(page, api); await expect(messages(page)).toHaveCount(1);
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toHaveCount(canRetry ? 1 : 0);
  await expect(page.getByLabel("本轮问题", { exact: true })).toBeEnabled(); expect(api.posts).toHaveLength(0);
});

test("unknown request after network loss needs explicit original retry", async ({ page }) => {
  const api = new MockConversations(); api.onTurn = async route => { await route.abort("timedout"); };
  await open(page, api); await send(page);
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toBeVisible(); expect(api.posts).toHaveLength(1);
  api.onTurn = undefined;
  await page.getByRole("button", { name: "使用原请求重试" }).click(); await expect(messages(page)).toHaveCount(2);
  expect(api.posts[0]).toEqual(api.posts[1]);
});

test("THREAD_BUSY loads actual active request without inserting the rejected question", async ({ page }) => {
  const api = new MockConversations();
  api.onTurn = async route => { api.begin(A, input("另一窗口的问题"), "needs_recovery"); await fail(route, "THREAD_BUSY", 409); };
  await open(page, api); await send(page);
  await expect(page.getByText("本轮问题：另一窗口的问题")).toBeVisible();
  await expect(messages(page)).toHaveCount(1); await expect(page.getByLabel("本轮问题", { exact: true })).toBeDisabled();
});

test("IDEMPOTENCY_CONFLICT stays visible and does not automatically resubmit", async ({ page }) => {
  const api = new MockConversations(); api.onTurn = async route => { await fail(route, "IDEMPOTENCY_CONFLICT", 409); };
  await open(page, api); await send(page);
  await expect(page.getByRole("region", { name: "当前对话", exact: true }).getByRole("alert")).toContainText("原请求编号已用于不同的问题或参数");
  expect(api.posts).toHaveLength(1); await expect(messages(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toHaveCount(0);
});

test("graph disclosure is keyboard accessible and each answer owns its evidence", async ({ page }) => {
  const api = new MockConversations(), result = answer();
  result.graph = { enabled: true, triggered: true, status: "success", truncated: false, evidence_count: 1, evidence: [{
    graph_id: "G", anchor_id: "A1", anchor_type: "table", table_ref: "T1", source_citations: [1], document: { doc_id: A },
    table: { table_id: "table-1", table_ref: "T1", page: 8, table_index: 1 },
    entities: [{ id: "E1", name: "冒口", entity_type: "工艺结构", page: 8 }], relationships: [],
  }] };
  api.complete(A, input(), result); api.onTurn = async (route, sid, q) => { await ok(route, api.complete(sid, q, { ...result, question: q.question })); };
  await open(page, api);
  const graphs = page.getByRole("region", { name: "知识图谱检索结果", exact: true });
  await graphs.first().locator("summary").focus(); await page.keyboard.press("Enter");
  await expect(graphs.first().locator("details")).toHaveAttribute("open", "");
  await expect(graphs.first().getByRole("cell", { name: "冒口", exact: true })).toBeVisible();
  await send(page, "它的尺寸呢？"); await expect(graphs).toHaveCount(2);
  await expect(graphs.last().locator("details")).not.toHaveAttribute("open", "");
  await expect(graphs.first().locator("details")).toHaveAttribute("open", "");
});

test("late history read from A cannot replace B messages", async ({ page }) => {
  const api = new MockConversations(); api.complete(A, input("A 的历史")); api.complete(B, input("B 的历史"));
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  api.onMessages = async (route, sid) => { if (sid === A) await gate; await ok(route, { thread_id: sid, items: api.messages.get(sid), next_before_seq: null }).catch(() => {}); };
  await open(page, api); await page.getByRole("link", { name: "会话 B", exact: true }).click();
  await expect(messages(page)).toHaveCount(2); release();
  await expect(messages(page).first()).toContainText("B 的历史"); await expect(page.getByText("A 的历史", { exact: true })).toHaveCount(0);
});

test("temporary status failure never clears committed history", async ({ page }) => {
  const api = new MockConversations(); api.complete(A, input());
  api.onTurn = async route => { await fail(route, "LLM_UNAVAILABLE"); };
  api.onStatus = async route => { await fail(route, "QA_SERVICE_UNAVAILABLE"); };
  await open(page, api); await expect(messages(page)).toHaveCount(2); await send(page, "新的问题");
  await expect(page.getByRole("region", { name: "当前对话", exact: true }).getByRole("alert")).toContainText("对话服务暂不可用");
  await expect(messages(page)).toHaveCount(2); expect(api.posts).toHaveLength(1);
});

test("creation retry retains id even when browser storage is unavailable", async ({ page }) => {
  await page.addInitScript(() => { Storage.prototype.getItem = () => { throw new Error("blocked"); }; Storage.prototype.setItem = () => { throw new Error("blocked"); }; });
  const api = new MockConversations(); api.createAbort = true; await api.install(page); await page.goto("/rag");
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await page.getByRole("button", { name: "重试新建对话" }).click(); await expect(page).toHaveURL(/000c$/);
  expect(api.creates[0]).toBe(api.creates[1]);
});

for (const code of ["QA_FEATURE_DISABLED", "QA_LOCAL_TRANSPORT_REQUIRED", "QA_LOCAL_ACCESS_ONLY"]) {
  test(`closed service boundary shows safe error without legacy fallback: ${code}`, async ({ page }) => {
    const api = new MockConversations(); await api.install(page);
    await page.route("**/api/v1/rag/sessions?*", route => fail(route, code, 503));
    await page.goto("/rag");
    await expect(page.getByRole("complementary", { name: "历史会话" }).getByRole("alert")).toBeVisible();
    expect(api.posts).toHaveLength(0);
  });
}

test("A late POST cannot contaminate B; returning to A recovers the completed turn", async ({ page }) => {
  const api = new MockConversations(); let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  api.onTurn = async (route, sid, q) => { api.begin(sid, q); await gate; await ok(route, api.complete(sid, q)).catch(() => {}); };
  await open(page, api); await send(page, "A 独有的问题");
  await expect.poll(() => api.posts.length).toBe(1);
  await page.getByRole("link", { name: "会话 B", exact: true }).click();
  await expect(page.getByRole("heading", { name: "会话 B", exact: true })).toBeVisible(); release();
  await expect(messages(page)).toHaveCount(0); await expect(page.getByLabel("本轮问题", { exact: true })).toHaveValue("");
  await expect(page.getByText("A 独有的问题", { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "会话 A", exact: true }).click(); await expect(messages(page)).toHaveCount(2);
  expect(api.posts).toHaveLength(1);
});

test("three outcomes, clarification follow-up, per-message model and citations", async ({ page }) => {
  const api = new MockConversations();
  api.complete(A, input("冒口和冷铁的区别？")); api.complete(A, input("它的尺寸？"), answer("它的尺寸？", "clarification"));
  api.complete(B, input("未知工艺？"), answer("未知工艺？", "no_context"));
  await open(page, api); await expect(page.getByText("需要澄清", { exact: true })).toBeVisible();
  await send(page, "冒口"); await expect(messages(page)).toHaveCount(6);
  await expect(page.getByText("知识库回答", { exact: true })).toHaveCount(2);
  await expect(page.getByText("模型：test-model · mock", { exact: true })).toHaveCount(3);
  await page.getByRole("link", { name: "会话 B", exact: true }).click();
  await expect(page.getByText("暂无充分依据", { exact: true })).toBeVisible(); await expect(messages(page)).toHaveCount(2);
});

test("deleted sources keep body, remove excerpts/graph and retain citation numbering", async ({ page }) => {
  const api = new MockConversations(), result = answer();
  result.answer = "历史正文 [1] [2]";
  result.sources = [{ snapshot_id: A, kind: "citation", citation_id: 1, document_ids: [A], chunk_ids: [B], status: "source_deleted" },
    { snapshot_id: B, kind: "graph", citation_id: null, document_ids: [A], chunk_ids: [B], status: "source_deleted" }];
  result.citations = [{ ...result.citations[0], citation_id: 2, content: "仍然有效的引用" }];
  api.complete(A, input(), result); await open(page, api);
  await expect(page.getByText("历史正文 [1] [2]", { exact: true })).toBeVisible();
  await expect(page.getByText(/来源已删除/)).toHaveCount(2);
  await page.getByText("文本依据 · 1 条", { exact: true }).click();
  await expect(page.getByRole("article", { name: "引用 2", exact: true })).toContainText("仍然有效的引用");
  await expect(page.getByRole("article", { name: "引用 1", exact: true })).toHaveCount(0);
  await expect(page.getByText("知识图谱检索结果", { exact: true })).toHaveCount(0); expect(api.posts).toHaveLength(0);
});

test("mobile layout and keyboard input remain usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = new MockConversations(); await open(page, api); await send(page); await expect(messages(page)).toHaveCount(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("mobile.png"), fullPage: true });
});

test("bounded polling stops, can_retry=false never submits automatically", async ({ page }) => {
  await page.clock.install();
  const api = new MockConversations(); const state = api.begin(A, input());
  await open(page, api); await expect(page.getByText("请求正在执行，正在查询进度…", { exact: true })).toBeVisible();
  for (let n = 0; n < 10; n++) { await page.clock.runFor(11000); await expect.poll(() => api.reads.filter(p => p.endsWith(state.request_id)).length).toBe(n + 1); }
  await expect(page.getByText(/自动查询已暂停/)).toBeVisible();
  await page.clock.runFor(60000); expect(api.reads.filter(p => p.endsWith(state.request_id))).toHaveLength(10);
  await expect(page.getByRole("button", { name: "使用原请求重试" })).toHaveCount(0); expect(api.posts).toHaveLength(0);
});

test("foreign status URL is refused instead of leaking a request to another thread", async ({ page }) => {
  const api = new MockConversations();
  api.onTurn = async (route, sid, q) => {
    const state = api.begin(sid, q); state.status_url = `https://attacker.example/requests/${q.request_id}`;
    await ok(route, state, 202);
  };
  await open(page, api); await send(page); await expect(page.getByRole("region", { name: "当前对话", exact: true }).getByRole("alert")).toContainText("响应格式或会话归属异常");
  expect(api.posts).toHaveLength(1);
});
