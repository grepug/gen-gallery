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

function matchesStatusFilter(job, statusFilter) {
  if (statusFilter === "all") return true;
  if (statusFilter === "active") {
    return ["queued", "running", "retry_waiting"].includes(job.status);
  }
  return job.status === statusFilter;
}

function compareJobs(left, right, sort) {
  const [field, direction] =
    sort === "created_asc"
      ? ["created_at", "asc"]
      : sort === "updated_desc"
        ? ["updated_at", "desc"]
        : sort === "updated_asc"
          ? ["updated_at", "asc"]
          : ["created_at", "desc"];
  const leftValue = Date.parse(left[field]);
  const rightValue = Date.parse(right[field]);
  if (leftValue === rightValue) {
    return direction === "asc"
      ? left.id.localeCompare(right.id)
      : right.id.localeCompare(left.id);
  }
  return direction === "asc" ? leftValue - rightValue : rightValue - leftValue;
}

function countStatuses(jobs) {
  return jobs.reduce(
    (counts, job) => {
      counts[job.status] += 1;
      return counts;
    },
    {
      queued: 0,
      running: 0,
      retry_waiting: 0,
      succeeded: 0,
      failed: 0,
      canceled: 0,
    },
  );
}

async function mockJobs(page, totalJobsOrItems = 55, options = {}) {
  const { delayMs = 0, onRequest = null } = options;
  const jobs = Array.isArray(totalJobsOrItems)
    ? totalJobsOrItems
    : Array.from({ length: totalJobsOrItems }, (_, index) => buildJob(index));
  await page.route("**/jobs?**", async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get("limit") || 40);
    const offset = Number(url.searchParams.get("offset") || 0);
    const statusFilter = url.searchParams.get("status") || "all";
    const sort = url.searchParams.get("sort") || "created_desc";
    if (typeof onRequest === "function") {
      onRequest({ limit, offset, statusFilter, sort });
    }
    const filtered = jobs
      .filter((job) => matchesStatusFilter(job, statusFilter))
      .sort((left, right) => compareJobs(left, right, sort));
    const items = filtered.slice(offset, offset + limit);
    if (delayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items,
        total: filtered.length,
        limit,
        offset,
        counts: countStatuses(jobs),
      }),
    });
  });

  return jobs;
}

async function scrollUntilCardCount(page, expectedCount) {
  for (;;) {
    const currentCount = await page.locator(".gallery-card").count();
    if (currentCount >= expectedCount) return;
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForFunction(
      (previousCount) =>
        document.querySelectorAll(".gallery-card").length > previousCount,
      currentCount,
    );
  }
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
  expect(firstJobId).toBe("job-55");

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
      firstCard?.dataset.jobId === "job-55"
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

test("copy and rerun creates a fresh queued job and switches to active jobs", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  const activeJobs = Array.from({ length: 45 }, (_, index) => ({
    ...buildJob(index),
    id: `active-job-${index + 1}`,
    status: "queued",
    prompt: `Active prompt ${index + 1}`,
    output_files: [],
  }));
  const succeededJobs = Array.from({ length: 6 }, (_, index) => ({
    ...buildJob(index + 100),
    id: `succeeded-job-${index + 1}`,
    prompt: `Prompt ${index + 1}`,
  }));
  const jobs = await mockJobs(page, [...activeJobs, ...succeededJobs], {
    delayMs: 700,
  });
  let duplicateCount = 0;

  await page.route("**/jobs/*/duplicate", async (route) => {
    const jobId = route.request().url().split("/jobs/")[1].split("/duplicate")[0];
    const sourceJob = jobs.find((job) => job.id === jobId);
    expect(sourceJob).toBeTruthy();
    duplicateCount += 1;
    const now = new Date(Date.UTC(2026, 3, 28, 8, duplicateCount, 0)).toISOString();
    const duplicatedJob = {
      ...sourceJob,
      id: `job-copy-${duplicateCount}`,
      status: "queued",
      attempt_count: 0,
      assigned_key_name: null,
      created_at: now,
      updated_at: now,
      started_at: null,
      finished_at: null,
      next_retry_at: null,
      last_error: null,
      output_files: [],
    };
    jobs.unshift(duplicatedJob);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(duplicatedJob),
    });
  });

  await page.goto("/");
  await page.locator("#sort-select").selectOption("created_asc");
  await page.locator(".gallery-card").first().click();
  await page.getByRole("button", { name: "Copy & rerun" }).click();

  await expect(page.locator("#detail-title")).toHaveText("Loading job...");
  await expect(page.locator("#detail-placeholder")).toContainText(
    "Loading selected job...",
  );
  await expect(page.getByRole("button", { name: "Cancel" })).toBeHidden();

  await expect(page.locator(".filter-chip.is-active")).toHaveAttribute(
    "data-filter",
    "active",
  );
  await expect(page.locator(".gallery-card")).toHaveCount(46);
  await expect(page.locator("#gallery-status")).toHaveText(
    "Showing 46 of 46 active jobs.",
  );
  await expect(page.locator("#detail-status")).toHaveText("queued");
  await expect(page.locator("#detail-prompt-preview")).toContainText("Prompt 1");
  await expect(page.locator("#detail-placeholder")).toContainText(
    "This job has not produced a generated image yet.",
  );
  await expect(page.locator("#global-message")).toHaveText("Job copied and queued.");
});

test("copy and rerun restores the previous selection when the active refresh fails", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  const jobs = await mockJobs(page, 6);

  await page.route("**/jobs/*/duplicate", async (route) => {
    const jobId = route.request().url().split("/jobs/")[1].split("/duplicate")[0];
    const sourceJob = jobs.find((job) => job.id === jobId);
    const duplicatedJob = {
      ...sourceJob,
      id: "job-copy-error",
      status: "queued",
      attempt_count: 0,
      assigned_key_name: null,
      created_at: new Date(Date.UTC(2026, 3, 28, 9, 0, 0)).toISOString(),
      updated_at: new Date(Date.UTC(2026, 3, 28, 9, 0, 0)).toISOString(),
      started_at: null,
      finished_at: null,
      next_retry_at: null,
      last_error: null,
      output_files: [],
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(duplicatedJob),
    });
  });

  await page.goto("/");
  await page.locator(".gallery-card").first().click();
  await expect(page.locator("#detail-prompt-preview")).toContainText("Prompt 6");

  await page.unroute("**/jobs?**");
  await page.route("**/jobs?**", async (route) => {
    const url = new URL(route.request().url());
    const statusFilter = url.searchParams.get("status") || "all";
    if (statusFilter === "active") {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Failed to load jobs" }),
      });
      return;
    }
    const filtered = jobs.filter((job) => matchesStatusFilter(job, statusFilter));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        items: filtered,
        total: filtered.length,
        limit: filtered.length,
        offset: 0,
        counts: countStatuses(jobs),
      }),
    });
  });

  await page.getByRole("button", { name: "Copy & rerun" }).click();

  await expect(page.locator(".filter-chip.is-active")).toHaveAttribute(
    "data-filter",
    "succeeded",
  );
  await expect(page.locator("#detail-prompt-preview")).toContainText("Prompt 6");
  await expect(page.locator("#global-message")).toHaveText("Failed to load jobs");
});

test("refresh caps the first preserve-selection request at 200 and keeps loaded cards", async ({
  page,
}) => {
  await clearLocalGalleryCache(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  const requestLog = [];
  await mockJobs(page, 240, {
    onRequest: (request) => {
      requestLog.push(request);
    },
  });

  await page.goto("/");
  await scrollUntilCardCount(page, 240);
  await expect(page.locator(".gallery-card")).toHaveCount(240);

  requestLog.length = 0;
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.locator(".gallery-card")).toHaveCount(240);

  const refreshRequests = requestLog.filter((request) => request.offset === 0);
  expect(refreshRequests.length).toBeGreaterThan(0);
  expect(refreshRequests[0].limit).toBe(200);
  expect(Math.max(...requestLog.map((request) => request.limit))).toBe(200);
});
