import { expect, test, type BrowserContext } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

test("real local HTTP, Graph, PostgreSQL and browser persistence", async ({ page, context, browser, request }) => {
  test.setTimeout(90_000);
  const fixture = JSON.parse(readFileSync(process.env.QA_E2E_MANIFEST!, "utf8"));
  expect(fixture.verified_cluster).toMatch(/^phase13-m1-test-[a-f0-9]{12}$/);
  expect(fixture.api).toBe("http://127.0.0.1:18005");
  // A stale Next build must never send E2E writes to the business API.
  // This guard permits real network traffic only to the verified fixture server.
  const guard = (target: BrowserContext) => target.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== fixture.api || !url.pathname.startsWith("/api/v1/rag/sessions")) {
      await route.abort("blockedbyclient");
      throw new Error("Refusing E2E traffic outside the isolated conversation API");
    }
    await route.continue();
  });
  await guard(context);
  const counts: Record<string, number> = {};
  const messages = () => page.getByRole("list", { name: "会话消息" }).locator(":scope > li");
  async function send(question: string, count: number) {
    await page.getByLabel("本轮问题", { exact: true }).fill(question);
    await page.getByRole("button", { name: "发送问题", exact: true }).click();
    await expect(messages()).toHaveCount(count);
  }
  await page.goto("/rag"); await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page).toHaveURL(/\/rag\/[a-f0-9-]{36}$/);
  const mainId = page.url().split("/").at(-1)!;
  await send("冒口有什么作用？", 2); await send("那它尺寸怎么确定？", 4);
  await send("有哪些限制条件？", 6); await send("铝合金热裂的原因是什么？", 8);
  await expect(page.getByText("知识库回答", { exact: true })).toHaveCount(4);
  await page.getByRole("button", { name: "重命名 新会话", exact: true }).click();
  await page.getByLabel("会话标题").fill("M5 浏览器工艺讨论"); await page.getByRole("button", { name: "保存标题" }).click();
  await expect(page.getByRole("heading", { name: "M5 浏览器工艺讨论", exact: true })).toBeVisible();
  await page.reload(); await expect(messages()).toHaveCount(8); counts[mainId] = 8;
  await page.getByText("文本依据 · 1 条", { exact: true }).first().click();
  await expect(page.getByRole("article", { name: "引用 1", exact: true }).first()).toContainText("M3_SYNTHETIC_EVIDENCE");
  await page.screenshot({ path: "../backend/.phase13-m5.tmp/real-desktop.png", fullPage: true });

  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page).not.toHaveURL(new RegExp(mainId));
  const clarifyId = page.url().split("/").at(-1)!;
  await send("冒口和冷铁有什么区别？", 2); await send("它的尺寸怎么确定？", 4);
  await expect(page.getByText("需要澄清", { exact: true })).toBeVisible();
  await page.reload(); await send("冒口", 6); counts[clarifyId] = 6;

  // A fresh browser context has no copied browser storage or cached messages.
  const freshContext = await browser.newContext({ baseURL: "http://127.0.0.1:3306" });
  await guard(freshContext);
  const fresh = await freshContext.newPage();
  await fresh.goto(`/rag/${mainId}`);
  await expect(fresh.getByRole("list", { name: "会话消息" }).locator(":scope > li")).toHaveCount(8);
  await fresh.goto(`/rag/${fixture.recovery_thread}`);
  await fresh.getByRole("button", { name: "使用原请求重试" }).click();
  await expect(fresh.getByRole("list", { name: "会话消息" }).locator(":scope > li")).toHaveCount(2);
  counts[fixture.recovery_thread] = 2;
  const saved = await request.get(`${fixture.api}/api/v1/rag/sessions/${fixture.recovery_thread}/requests/${fixture.recovery_input.request_id}`);
  expect((await saved.json()).data.input).toEqual(fixture.recovery_input);
  const replay = await request.post(`${fixture.api}/api/v1/rag/sessions/${fixture.recovery_thread}/turns`, { data: fixture.recovery_input });
  expect(replay.status()).toBe(200); await freshContext.close();

  await page.goto(`/rag/${fixture.deleted_thread}`); await expect(messages()).toHaveCount(2);
  await expect(page.getByText(/来源已删除/)).toBeVisible();
  await expect(page.getByText(/M3_SYNTHETIC_ANSWER/)).toBeVisible();
  await expect(page.getByText(/M3_SYNTHETIC_EVIDENCE/)).toHaveCount(0); counts[fixture.deleted_thread] = 2;
  await page.goto(`/rag/${fixture.history_thread}`); await expect(messages()).toHaveCount(50);
  await page.getByRole("button", { name: "加载更早消息" }).click(); await expect(messages()).toHaveCount(64); counts[fixture.history_thread] = 64;

  await page.getByRole("button", { name: "新建对话", exact: true }).click(); await expect(page).not.toHaveURL(new RegExp(fixture.history_thread));
  const emptyId = page.url().split("/").at(-1)!;
  await page.getByText("本轮检索设置", { exact: true }).click(); await page.getByLabel("限定文档", { exact: true }).fill(fixture.empty_document);
  await send("冒口尺寸如何确定？", 2); await expect(page.getByText("暂无充分依据", { exact: true })).toBeVisible(); counts[emptyId] = 2;

  const created = await request.post(`${fixture.api}/api/v1/rag/sessions`, { data: { request_id: randomUUID() } });
  const slowId = (await created.json()).data.thread_id;
  const slow = request.post(`${fixture.api}/api/v1/rag/sessions/${slowId}/turns`, { data: { request_id: randomUUID(), question: "慢速工艺测试的作用是什么？", limit: 8 } });
  await expect.poll(async () => (await (await request.get(`${fixture.api}/api/v1/rag/sessions/${slowId}`)).json()).data.active_request?.execution_active).toBe(true);
  await page.goto(`/rag/${slowId}`); await expect(page.getByLabel("本轮问题", { exact: true })).toBeDisabled();
  // Exponential status polling can cross the 3 s provider completion boundary.
  await expect(messages()).toHaveCount(2, { timeout: 20_000 }); expect((await slow).status()).toBe(200); counts[slowId] = 2;
  const denied = await request.get(`${fixture.api}/api/v1/rag/sessions`, { headers: { "X-Forwarded-For": "127.0.0.1" } });
  expect(denied.status()).toBe(403);
  writeFileSync(fixture.journal, JSON.stringify({ messages: counts }));
});
