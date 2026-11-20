import { test, expect, type Page } from "@playwright/test";

// GH #257 / PANT-1401: gate + approval controls, the Requests tab, the
// Console filter (?q=), ?project= scoping, and a fill form that survives a
// stray click. The API is mocked with page.route; the routes' own CLI wiring
// is covered by write-credential.spec.ts (stub log) and the Python tests.

function ref(name: string, extra: Record<string, unknown> = {}) {
  return {
    name,
    sm_name: `sm-${name}`,
    kind: "api_key",
    scope: "project",
    org: "acme",
    provider: "stripe",
    project: "web",
    env: "prod",
    state: "enabled",
    approval: "",
    tags: {},
    description: `${name} description`,
    purpose: "",
    injected_as: {},
    group: "",
    related: [],
    repo: "",
    source_files: [],
    ...extra,
  };
}

const REFS = [
  ref("free-reign-card-1", { approval: "required", provider: "privacy", project: "free-reign" }),
  ref("free-reign-card-2", { approval: "required", provider: "privacy", project: "free-reign" }),
  ref("stripe-live", { project: "web" }),
  ref("vercel-token", { state: "requested", provider: "vercel", project: "web" }),
  ref("old-key", { state: "dropped", project: "web" }),
];

const APPROVALS = [
  {
    name: "free-reign-card-1", sm_name: "sm-free-reign-card-1", state: "enabled", gated: true,
    approved: false, approved_by: null, expires_at: null, ticks_left: null, pending: true,
    last_requested_seq: 7, last_requested_ts: "2026-11-20T10:00:00Z",
  },
  {
    name: "free-reign-card-2", sm_name: "sm-free-reign-card-2", state: "enabled", gated: true,
    approved: true, approved_by: "human", expires_at: Math.floor(Date.now() / 1000) + 3600,
    ticks_left: null, pending: false, last_requested_seq: null, last_requested_ts: null,
  },
];

type Posted = { url: string; body: Record<string, unknown> };

async function mockApi(page: Page): Promise<{ posts: Posted[]; registryUrls: string[] }> {
  const posts: Posted[] = [];
  const registryUrls: string[] = [];
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/api/vault-status", (r) => r.fulfill(json({ initialized: true })));
  await page.route("**/api/leak-status**", (r) => r.fulfill(json({ statuses: [] })));
  await page.route("**/api/views", (r) => r.fulfill(json({})));
  await page.route("**/api/rotation-status**", (r) => r.fulfill(json({})));
  await page.route("**/api/audit**", (r) => r.fulfill(json([])));
  await page.route("**/api/impact**", (r) => r.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ error: "not mocked" }) }));
  await page.route("**/api/registry**", (r) => {
    const url = new URL(r.request().url());
    registryUrls.push(url.search);
    const project = url.searchParams.get("project");
    r.fulfill(json(project ? REFS.filter((x) => x.project === project) : REFS));
  });
  await page.route("**/api/approvals**", (r) => r.fulfill(json(APPROVALS)));
  for (const route of ["gate", "approve", "state", "drop"]) {
    await page.route(`**/api/${route}`, (r) => {
      posts.push({ url: route, body: r.request().postDataJSON() });
      r.fulfill(json({ ok: true, message: `${route} ok` }));
    });
  }
  return { posts, registryUrls };
}

const rows = (page: Page) => page.locator(".console-table-row");

test.describe("vault UI: search, scope, gate, requests (GH #257)", () => {
  test("the Console filter narrows the list and stays in ?q=", async ({ page }) => {
    await mockApi(page);
    await page.goto("/");
    await expect(rows(page)).toHaveCount(REFS.length);

    await page.getByTestId("console-filter").fill("card");
    await expect(rows(page)).toHaveCount(2);
    await expect(page).toHaveURL(/[?&]q=card/);

    await page.getByTestId("console-filter").fill("");
    await expect(rows(page)).toHaveCount(REFS.length);
    await expect(page).not.toHaveURL(/[?&]q=/);
  });

  test("?q=<exact name> filters to that entry and opens it", async ({ page }) => {
    await mockApi(page);
    await page.goto("/?q=free-reign-card-1");
    await expect(page.getByTestId("console-filter")).toHaveValue("free-reign-card-1");
    await expect(rows(page)).toHaveCount(1);
    await expect(page.locator(".drawer")).toContainText("free-reign-card-1");
  });

  test("?project=<name> shows only that project's references", async ({ page }) => {
    const { registryUrls } = await mockApi(page);
    await page.goto("/?project=free-reign");
    await expect(page.getByTestId("scope-banner")).toContainText("free-reign");
    await expect(rows(page)).toHaveCount(2);
    expect(registryUrls.some((s) => s.includes("project=free-reign"))).toBe(true);
  });

  test("rows show the gate; the drawer approves for a chosen time box", async ({ page }) => {
    const { posts } = await mockApi(page);
    await page.goto("/");
    const card1 = rows(page).filter({ hasText: "free-reign-card-1" });
    await expect(card1.locator(".gate-badge")).toContainText("pending");
    await expect(rows(page).filter({ hasText: "free-reign-card-2" }).locator(".gate-badge")).toContainText("approved");
    await expect(rows(page).filter({ hasText: "stripe-live" }).locator(".gate-badge")).toHaveCount(0);

    await card1.click();
    const controls = page.locator(".drawer").getByTestId("gate-controls");
    await controls.getByLabel("approval duration").selectOption("2h");
    await controls.getByRole("button", { name: "Approve" }).click();
    await expect.poll(() => posts.find((p) => p.url === "approve")?.body).toEqual({ name: "free-reign-card-1", ttl: "2h" });

    await controls.getByRole("button", { name: "Remove gate" }).click();
    await expect.poll(() => posts.find((p) => p.url === "gate")?.body).toEqual({ name: "free-reign-card-1", on: false });
  });

  test("an ungated reference can be gated from the drawer", async ({ page }) => {
    const { posts } = await mockApi(page);
    await page.goto("/?q=stripe-live");
    await page.locator(".drawer").getByRole("button", { name: "Require approval" }).click();
    await expect.poll(() => posts.find((p) => p.url === "gate")?.body).toEqual({ name: "stripe-live", on: true });
  });

  test("the Requests tab lists requests, pending approvals and dropped values", async ({ page }) => {
    const { posts } = await mockApi(page);
    await page.goto("/?tab=requests");
    const view = page.getByTestId("requests-view");
    await expect(view.getByTestId("request-vercel-token")).toBeVisible();
    await expect(view.getByTestId("approval-free-reign-card-1")).toBeVisible();
    await expect(view.getByTestId("approval-free-reign-card-2")).toHaveCount(0);
    await expect(view.getByTestId("dropped-old-key")).toBeVisible();
    await expect(page.getByRole("button", { name: /Requests/ })).toContainText("2");

    await view.getByTestId("dropped-old-key").getByRole("button", { name: "Enable" }).click();
    await expect.poll(() => posts.find((p) => p.url === "state")?.body).toEqual({ name: "old-key", state: "enabled" });

    await view.getByTestId("approval-free-reign-card-1").getByRole("button", { name: "Approve" }).click();
    await expect.poll(() => posts.find((p) => p.url === "approve")?.body).toMatchObject({ name: "free-reign-card-1" });
  });

  test("the fill form survives a stray click, keeps ?fulfill=, and enables on save", async ({ page }) => {
    const { posts } = await mockApi(page);
    await page.goto("/?tab=requests");
    await page.getByTestId("request-vercel-token").getByRole("button", { name: /Fill/ }).click();
    const form = page.getByTestId("add-secret-form");
    await expect(form).toBeVisible();
    await expect(page).toHaveURL(/[?&]fulfill=vercel-token/);

    // a click on the backdrop, outside the form, must not close it
    await page.mouse.click(5, 5);
    await expect(form).toBeVisible();

    await form.locator('input[type="password"], textarea').first().fill("not-a-real-value");
    await form.getByRole("button", { name: /save|store|drop|fill/i }).last().click();
    await expect.poll(() => posts.find((p) => p.url === "drop")?.body?.name).toBe("vercel-token");
    await expect.poll(() => posts.find((p) => p.url === "state")?.body).toEqual({ name: "vercel-token", state: "enabled" });
    await expect(form).toHaveCount(0);
    await expect(page).not.toHaveURL(/[?&]fulfill=/);
  });

  test("narrow window: the fill form fits the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 380, height: 700 });
    await mockApi(page);
    await page.goto("/?fulfill=vercel-token");
    const form = page.getByTestId("add-secret-form");
    await expect(form).toBeVisible();
    const box = await form.boundingBox();
    expect(box && box.width).toBeLessThanOrEqual(380);
    expect(box && box.x).toBeGreaterThanOrEqual(0);
  });
});
