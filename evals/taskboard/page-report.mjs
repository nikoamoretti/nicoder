/**
 * Independent page report / seed / storage snapshot. Not used by the model.
 */
import { chromium } from "@playwright/test";

const url = process.env.TASKBOARD_URL || process.argv[3] || "http://127.0.0.1:4173/";
const mode = process.argv[2] || "report";

async function addTask(page, title, description) {
  const titleBox = page.getByLabel(/title/i).or(page.getByPlaceholder(/title/i)).first();
  await titleBox.fill(title);
  if (description) {
    const desc = page.getByLabel(/description/i).or(page.getByPlaceholder(/description/i)).first();
    if (await desc.count()) {
      await desc.fill(description);
    }
  }
  await page.getByRole("button", { name: /add|create|save/i }).first().click();
}

async function storage(page) {
  return page.evaluate(() => {
    const rows = {};
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key) rows[key] = localStorage.getItem(key);
    }
    return rows;
  });
}

const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (err) => errors.push({ kind: "pageerror", text: String(err) }));
page.on("console", (msg) => {
  if (msg.type() === "error") errors.push({ kind: "console", text: msg.text() });
});
await page.goto(url, { waitUntil: "domcontentloaded", timeout: 20_000 });
await page.waitForTimeout(1500);

if (mode === "seed") {
  const seeds = [
    { title: "Seeded open", description: "keep this after due dates", column: "todo" },
    { title: "Seeded done", description: "already finished", column: "done" },
    { title: "Keep me", description: "must survive refresh", column: "todo" },
  ];
  for (const seed of seeds) {
    const col = page.locator("select").first();
    if (await col.count()) {
      await col.selectOption(seed.column).catch(() => {});
    }
    await addTask(page, seed.title, seed.description);
  }
  await page.waitForTimeout(500);
}

const body = ((await page.locator("body").innerText().catch(() => "")) || "").slice(0, 2000);
const headings = await page.getByRole("heading").allTextContents().catch(() => []);
const payload = {
  mode,
  url,
  title: await page.title().catch(() => ""),
  headings,
  body,
  errors,
  storage: await storage(page).catch(() => ({})),
  visibleSeededOpen: await page.getByText("Seeded open").count().catch(() => 0),
  visibleSeededDone: await page.getByText("Seeded done").count().catch(() => 0),
  visibleKeepMe: await page.getByText("Keep me").count().catch(() => 0),
};
console.log(JSON.stringify(payload, null, 2));
await browser.close();
