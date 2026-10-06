import { expect, test } from "@playwright/test";
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

test("real browser upload, engine, saved answer, historical candidate, errors and download", async ({ page, context }) => {
  test.setTimeout(90_000);
  const fixture = JSON.parse(readFileSync(process.env.CASTING_E2E_MANIFEST!, "utf8"));
  expect(fixture.verified_cluster).toMatch(/^phase13-m1-test-[a-f0-9]{12}$/);
  expect(fixture.api).toBe("http://127.0.0.1:18005");
  await context.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== fixture.api || !url.pathname.startsWith("/api/v1/rag/sessions")) {
      await route.abort("blockedbyclient"); throw new Error("Refusing traffic outside the isolated casting fixture");
    }
    await route.continue();
  });
  const messages = () => page.getByRole("list", { name: "会话消息" }).locator(":scope > li");
  async function send(question: string, count: number) {
    await page.getByLabel("本轮问题", { exact: true }).fill(question);
    await page.getByRole("button", { name: "发送问题", exact: true }).click();
    await expect(messages()).toHaveCount(count, { timeout: 25_000 });
  }
  const raw = readFileSync("../backend/tests/fixtures/casting/converted.input.json");
  async function upload(name: string, buffer = raw) {
    await page.getByLabel("选择 JSON 文件", { exact: true }).setInputFiles({ name, buffer, mimeType: "application/json" });
    await expect(page.getByText("已选定，随本轮问题提交", { exact: true })).toBeVisible();
  }
  await page.goto("/rag"); await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page).toHaveURL(/\/rag\/[a-f0-9-]{36}$/);
  const sid = page.url().split("/").at(-1)!;
  await page.getByRole("button", { name: "工程 JSON 附件", exact: true }).click();
  await upload("工程输入.json"); await send("请按附件计算浇冒系统方案", 2);
  await expect(messages().first()).toContainText("工程输入.json");
  await expect(page.getByText("方案计算完成", { exact: true })).toBeVisible();
  await expect(messages().last()).toContainText("170 mm");
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 recommendation.json", exact: true }).click();
  const download = await downloading, path = await download.path();
  expect(path).toBeTruthy();
  const bytes = readFileSync(path!), result = JSON.parse(bytes.toString("utf8"));
  expect(result.candidates).toHaveLength(4);
  const rid = download.suggestedFilename().replace("recommendation-", "").replace(".json", "");
  await page.reload(); await expect(messages()).toHaveCount(2); await expect(messages().first()).toContainText("工程输入.json");
  await send("第二个候选的冒口尺寸是多少？", 4);
  await expect(messages().last()).toContainText(result.candidates[1].id);
  await expect(messages().last()).toContainText("没有重新计算");
  await page.getByRole("button", { name: "工程 JSON 附件", exact: true }).click();
  await expect(page.getByText(/后端可复用最近明确选定/)).toContainText("工程输入.json");
  await upload("无候选.json", readFileSync("../backend/tests/fixtures/casting/no_candidate.input.json"));
  await send("请计算新附件方案", 6);
  await expect(messages().last()).toContainText("暂无可行方案");
  await send("冒口有什么作用？", 8);
  await expect(messages().last()).toContainText("知识库回答");
  const invalid = JSON.parse(raw.toString("utf8")); invalid.parameter_metadata.casting_mass_kg.status = "Proposed";
  await upload("待确认.json", Buffer.from(JSON.stringify(invalid)));
  await send("请计算待确认附件", 10);
  await expect(messages().last()).toContainText("输入未通过准入");
  await expect(messages().last().getByRole("list", { name: "工程字段问题" })).toContainText("casting_mass_kg");
  await page.screenshot({ path: test.info().outputPath("engineering-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("engineering-mobile.png"), fullPage: true });
  writeFileSync(fixture.journal, JSON.stringify({ thread_id: sid, run_id: rid, download_sha256: createHash("sha256").update(bytes).digest("hex") }));
});
