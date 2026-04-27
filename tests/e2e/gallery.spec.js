const { test, expect } = require("@playwright/test");

function fakeImageDataUrl(label) {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="720" height="960" viewBox="0 0 720 960">
      <rect width="720" height="960" fill="#dbe4f0"/>
      <rect x="40" y="40" width="640" height="880" rx="32" fill="#f8fbff"/>
      <text x="360" y="490" text-anchor="middle" font-family="Arial, sans-serif" font-size="56" fill="#6c7a90">${label}</text>
    </svg>
  `;
  return `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;
}

function buildJob(index) {
  return {
    id: `job-${index + 1}`,
    status: "succeeded",
    prompt: `Prompt ${index + 1}`,
    image_action: "generate",
    model: "gpt-5.5",
    tool_model: "gpt-image-2",
    attempt_count: 1,
    max_retries: 2,
    retry_delay_seconds: 60,
    assigned_key_name: index % 2 === 0 ? "key-a" : "key-b",
    created_at: new Date(Date.UTC(2026, 3, 27, 12, index, 0)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 3, 27, 12, index, 30)).toISOString(),
    started_at: new Date(Date.UTC(2026, 3, 27, 12, index, 2)).toISOString(),
    finished_at: new Date(Date.UTC(2026, 3, 27, 12, index, 20)).toISOString(),
    next_retry_at: null,
    last_error: null,
    input_files: [],
    output_files: [
      {
        filename: `image-${index + 1}.png`,
        kind: "output",
        size_bytes: 1234,
        url: fakeImageDataUrl(`Image ${index + 1}`),
      },
    ],
  };
}

async function mockJobs(page, totalJobs = 55) {
  const jobs = Array.from({ length: totalJobs }, (_, index) => buildJob(index));
  await page.route("**/jobs?**", async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") || 40);
    const offset = Number(url.searchParams.get("offset") || 0);
    const items = jobs.slice(offset, offset + limit);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items,
        total: totalJobs,
        limit,
        offset,
        counts: {
          queued: 0,
          running: 0,
          retry_waiting: 0,
          succeeded: totalJobs,
          failed: 0,
          canceled: 0,
        },
      }),
    });
  });
}

test("home gallery appends more jobs without rebuilding existing cards or jumping to top", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 1200 });
  await mockJobs(page);

  await page.goto("/");

  const cards = page.locator(".gallery-card");
  await expect(cards).toHaveCount(40);

  const firstJobId = await cards.first().getAttribute("data-job-id");
  expect(firstJobId).toBe("job-1");

  await page.evaluate(() => {
    window.__firstGalleryCardRef = document.querySelector(".gallery-card");
  });

  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const scrollBefore = await page.evaluate(() => window.scrollY);

  await page.waitForFunction(() => document.querySelectorAll(".gallery-card").length === 55);
  await expect(cards).toHaveCount(55);

  const scrollAfter = await page.evaluate(() => window.scrollY);
  expect(scrollAfter).toBeGreaterThan(scrollBefore - 120);

  const firstCardPreserved = await page.evaluate(() => {
    const firstCard = document.querySelector(".gallery-card");
    return (
      Boolean(window.__firstGalleryCardRef) &&
      window.__firstGalleryCardRef.isConnected &&
      firstCard === window.__firstGalleryCardRef &&
      firstCard?.dataset.jobId === "job-1"
    );
  });
  expect(firstCardPreserved).toBe(true);

  await expect(page.locator("#gallery-status")).toHaveText(
    "Showing 55 of 55 succeeded jobs.",
  );
  await expect(page.locator("#gallery-load-more")).toHaveText(
    "All 55 succeeded jobs loaded.",
  );
});

test("desktop column selector defaults to 4 and updates the gallery grid", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 1200 });
  await mockJobs(page, 12);
  await page.goto("/");

  const columnSelect = page.locator("#column-count-select");
  await expect(columnSelect).toBeVisible();
  await expect(columnSelect).toHaveValue("4");

  const initialColumns = await page.locator("#gallery-grid").evaluate((element) => {
    return getComputedStyle(element).gridTemplateColumns.split(" ").length;
  });
  expect(initialColumns).toBe(4);

  await columnSelect.selectOption("3");

  const updatedColumns = await page.locator("#gallery-grid").evaluate((element) => {
    return getComputedStyle(element).gridTemplateColumns.split(" ").length;
  });
  expect(updatedColumns).toBe(3);
});

test("column selector stays hidden on mobile layouts", async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 1200 });
  await mockJobs(page, 12);
  await page.goto("/");

  await expect(page.locator(".desktop-only-control")).toBeHidden();
});
