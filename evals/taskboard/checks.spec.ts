/**
 * Independent task-board behavior checks.
 *
 * These assertions live outside any model-editable workspace. They describe
 * requested behavior, not private selectors. An evaluator may adapt locators
 * to the generated accessible UI without weakening the assertions.
 *
 * TODAY is a defined local calendar date so overdue checks are not timezone-
 * dependent on the host clock.
 */
import { expect, test, type Page } from "@playwright/test";

const TODAY = "2026-09-16";
const YESTERDAY = "2026-09-15";
const TOMORROW = "2026-09-17";

async function freezeCalendar(page: Page) {
  await page.addInitScript(
    ({ today }) => {
      const frozen = new Date(`${today}T12:00:00`);
      const RealDate = Date;
      class FakeDate extends RealDate {
        constructor(...args: ConstructorParameters<typeof Date>) {
          if (args.length === 0) {
            super(frozen.getTime());
            return;
          }
          super(...args);
        }
        static now() {
          return frozen.getTime();
        }
      }
      // @ts-expect-error replacing clock for this check only
      window.Date = FakeDate;
    },
    { today: TODAY },
  );
}

async function addTask(page: Page, title: string, description?: string) {
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

test.describe("request 1 — task board", () => {
  test.beforeEach(async ({ page }) => {
    await freezeCalendar(page);
    await page.goto("/");
  });

  test("shows To do, In progress, and Done columns", async ({ page }) => {
    await expect(page.getByRole("heading", { name: /to do/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: /in progress/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: /done/i })).toBeVisible();
  });

  test("creates, edits, moves, and deletes a task", async ({ page }) => {
    await addTask(page, "Buy milk", "2% carton");
    await expect(page.getByText("Buy milk")).toBeVisible();
    const card = page.getByText("Buy milk").first();
    await card.click();
    const title = page.getByLabel(/title/i).or(page.getByPlaceholder(/title/i)).first();
    if (await title.count()) {
      await title.fill("Buy oat milk");
      const save = page.getByRole("button", { name: /save|update|done/i }).first();
      if (await save.count()) {
        await save.click();
      }
    }
    await expect(page.getByText("Buy oat milk")).toBeVisible();

    const move = page.getByRole("button", { name: /in progress|move/i }).first();
    if (await move.count()) {
      await move.click();
    }
    await expect(page.getByText("Buy oat milk")).toBeVisible();

    const del = page.getByRole("button", { name: /delete|remove/i }).first();
    await del.click();
    await expect(page.getByText("Buy oat milk")).toHaveCount(0);
  });

  test("search filters tasks and empty state is visible", async ({ page }) => {
    await addTask(page, "Alpha task");
    await addTask(page, "Beta task");
    const search = page.getByRole("searchbox").or(page.getByLabel(/search/i)).or(page.getByPlaceholder(/search/i));
    await search.first().fill("Alpha");
    await expect(page.getByText("Alpha task")).toBeVisible();
    await expect(page.getByText("Beta task")).toHaveCount(0);
    await search.first().fill("zzz-no-match");
    await expect(page.getByText(/no tasks|nothing|empty/i).first()).toBeVisible();
  });

  test("survives refresh", async ({ page }) => {
    await addTask(page, "Persisted task");
    await expect(page.getByText("Persisted task")).toBeVisible();
    await page.reload();
    await expect(page.getByText("Persisted task")).toBeVisible();
  });

  test("blank title is not saved as an unlabeled card", async ({ page }) => {
    const titleBox = page.getByLabel(/title/i).or(page.getByPlaceholder(/title/i)).first();
    await titleBox.fill("   ");
    await page.getByRole("button", { name: /add|create|save/i }).first().click();
    await expect(page.getByText(/title is required|enter a title|need a title/i).first()).toBeVisible();
  });

  test("usable at a phone-sized width", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("heading", { name: /to do/i })).toBeVisible();
    await addTask(page, "Phone task");
    await expect(page.getByText("Phone task")).toBeVisible();
  });
});

test.describe("request 2 — due dates", () => {
  test.beforeEach(async ({ page }) => {
    await freezeCalendar(page);
    await page.goto("/");
  });

  test("overdue means due before today and not Done", async ({ page }) => {
    await expect(page.getByText("Seeded open")).toBeVisible();
    const overdue = page.getByLabel(/due/i).or(page.getByPlaceholder(/due/i));
    if (await overdue.count()) {
      await addTask(page, "Late report");
      await page.getByText("Late report").click();
      await overdue.first().fill(YESTERDAY);
      const save = page.getByRole("button", { name: /save|update/i }).first();
      if (await save.count()) {
        await save.click();
      }
    }
    await page.getByRole("button", { name: /overdue/i }).or(page.getByLabel(/overdue/i)).first().click();
    await expect(page.getByText("Late report")).toBeVisible();
    await expect(page.getByText("Seeded done")).toHaveCount(0);
  });

  test("tasks without dates remain valid and completed work is not overdue", async ({ page }) => {
    await expect(page.getByText("Seeded open")).toBeVisible();
    const filter = page.getByRole("button", { name: /overdue/i }).or(page.getByLabel(/overdue/i));
    if (await filter.count()) {
      await filter.first().click();
    }
    await expect(page.getByText("Seeded open")).toHaveCount(0);
  });

  test("future due dates are not overdue", async ({ page }) => {
    await addTask(page, "Future work");
    const due = page.getByLabel(/due/i).or(page.getByPlaceholder(/due/i));
    if (await due.count()) {
      await page.getByText("Future work").click();
      await due.first().fill(TOMORROW);
      const save = page.getByRole("button", { name: /save|update/i }).first();
      if (await save.count()) {
        await save.click();
      }
    }
    const filter = page.getByRole("button", { name: /overdue/i }).or(page.getByLabel(/overdue/i));
    if (await filter.count()) {
      await filter.first().click();
    }
    await expect(page.getByText("Future work")).toHaveCount(0);
  });
});
