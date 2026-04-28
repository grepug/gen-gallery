const { test, expect } = require("@playwright/test");

async function clearLocalGalleryCache(page) {
  await page.addInitScript(() => {
    if (!window.sessionStorage.getItem("__galleryCacheCleared")) {
      window.localStorage.clear();
      window.sessionStorage.setItem("__galleryCacheCleared", "1");
    }
  });
}

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
    tags: [],
    is_favorite: false,
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
          favorites: 0,
        },
      }),
    });
  });
}

async function mockJobsWithDelay(page, totalJobs = 55, delayMs = 800) {
  const jobs = Array.from({ length: totalJobs }, (_, index) => buildJob(index));
  await page.route("**/jobs?**", async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") || 40);
    const offset = Number(url.searchParams.get("offset") || 0);
    const items = jobs.slice(offset, offset + limit);
    await new Promise((resolve) => setTimeout(resolve, delayMs));
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
          favorites: 0,
        },
      }),
    });
  });
}

test("home gallery appends more jobs without rebuilding existing cards or jumping to top", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 900 });
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
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 900 });
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
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 800, height: 1200 });
  await mockJobs(page, 12);
  await page.goto("/");

  await expect(page.locator(".desktop-only-control")).toBeHidden();
});

test("reload paints cached jobs before the background refresh finishes", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 1200 });
  await mockJobsWithDelay(page, 40, 900);

  await page.goto("/");
  await expect(page.locator(".gallery-card")).toHaveCount(40);

  await page.reload();

  await expect(page.locator(".gallery-card")).toHaveCount(40, {
    timeout: 300,
  });
  await expect(page.locator("#gallery-status")).toContainText("Refreshing", {
    timeout: 300,
  });
});

test("mobile viewer keeps fullscreen modal controls easy to reach", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await mockJobs(page, 8);
  await page.goto("/");

  await page.locator(".gallery-card").first().click();

  const shellBox = await page.locator(".viewer-shell").boundingBox();
  expect(shellBox).not.toBeNull();
  expect(shellBox.width).toBeGreaterThanOrEqual(388);
  expect(shellBox.height).toBeGreaterThanOrEqual(840);
  expect(shellBox.x).toBeLessThanOrEqual(1);
  expect(shellBox.y).toBeLessThanOrEqual(1);

  const closeBox = await page.getByRole("button", { name: "Close" }).boundingBox();
  expect(closeBox).not.toBeNull();
  expect(closeBox.x).toBeGreaterThan(220);

  const frameBox = await page.locator(".detail-preview-frame").boundingBox();
  const sidebarBox = await page.locator(".detail-sidebar").boundingBox();
  expect(frameBox).not.toBeNull();
  expect(sidebarBox).not.toBeNull();
  expect(sidebarBox.y).toBeGreaterThanOrEqual(frameBox.y + frameBox.height - 1);

  await page.getByRole("button", { name: "Fullscreen" }).click();
  await page.waitForTimeout(2000);

  const topbarOpacity = await page.locator(".viewer-topbar").evaluate((element) => {
    return getComputedStyle(element).opacity;
  });
  expect(topbarOpacity).toBe("1");

  const contentDisplay = await page.locator(".detail-content").evaluate((element) => {
    return getComputedStyle(element).display;
  });
  expect(contentDisplay).toBe("flex");

  const stripBox = await page.locator(".detail-strip").boundingBox();
  expect(stripBox).not.toBeNull();
  expect(stripBox.width).toBeGreaterThan(250);
  expect(stripBox.height).toBeLessThan(120);
});

test("favorites tab and heart toggles stay in sync between gallery and detail", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  const jobs = Array.from({ length: 3 }, (_, index) => buildJob(index));
  const favoriteSet = new Set();

  const countsPayload = () => ({
    queued: 0,
    running: 0,
    retry_waiting: 0,
    succeeded: jobs.length,
    failed: 0,
    canceled: 0,
    favorites: favoriteSet.size,
  });

  const serializeJob = (job) => ({
    ...job,
    tags: favoriteSet.has(job.id) ? ["favorite"] : [],
    is_favorite: favoriteSet.has(job.id),
  });

  await page.route("**/jobs?**", async (route) => {
    const url = new URL(route.request().url());
    const filter = url.searchParams.get("status") || "all";
    const items = jobs
      .map((job) => serializeJob(job))
      .filter((job) => (filter === "favorites" ? job.is_favorite : true));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items,
        total: items.length,
        limit: items.length,
        offset: 0,
        counts: countsPayload(),
      }),
    });
  });

  await page.route("**/jobs/*/favorite", async (route) => {
    const match = route.request().url().match(/\/jobs\/([^/]+)\/favorite/);
    const jobId = match?.[1];
    if (!jobId) {
      await route.abort();
      return;
    }
    if (route.request().method() === "POST") {
      favoriteSet.add(jobId);
    } else if (route.request().method() === "DELETE") {
      favoriteSet.delete(jobId);
    }
    const job = jobs.find((item) => item.id === jobId);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(serializeJob(job)),
    });
  });

  await page.goto("/");

  const firstCard = page.locator(".gallery-card").first();
  await firstCard.locator(".favorite-button").click();

  await expect(page.getByRole("button", { name: /^Favorites\(1\)$/ })).toBeVisible();
  await firstCard.click();
  await expect(page.locator("#favorite-button")).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("button", { name: /^Favorites\(1\)$/ }).click();
  await expect(page.locator(".gallery-card")).toHaveCount(1);
  await expect(page.locator("#gallery-status")).toHaveText(
    "Showing 1 of 1 favorite jobs.",
  );

  await page.locator(".gallery-card").first().click();
  await page.locator("#favorite-button").click();

  await expect(page.getByRole("button", { name: /^Favorites\(0\)$/ })).toBeVisible();
  await expect(page.locator(".gallery-card")).toHaveCount(0);
  await expect(page.locator("#gallery-empty")).toBeVisible();
});
