import { test, expect } from "@playwright/test";

const MOCK_REFS = [
  {
    name: "GITHUB_TOKEN",
    sm_name: "github-token",
    kind: "api_key",
    scope: "org",
    org: "acme",
    provider: "github",
    project: "web",
    env: "prod",
    state: "active",
    tags: {},
    description: "GitHub personal access token",
    purpose: "",
    injected_as: {},
    group: "",
    related: [],
    repo: "",
    source_files: [],
  },
];

test.describe("SearchBar", () => {
  test("shows results when query matches", async ({ page }) => {
    // Mock /api/search to return one result
    await page.route("**/api/search?**", async (route) => {
      const url = route.request().url();
      const q = new URL(url).searchParams.get("query") ?? "";
      if (q.toLowerCase().includes("github")) {
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_REFS) });
      } else {
        await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      }
    });

    // Stub other API calls so the page loads without a real backend
    await page.route("**/api/vault-status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ initialized: true }) }),
    );
    await page.route("**/api/registry", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
    );
    await page.route("**/api/leak-status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ statuses: [] }) }),
    );
    await page.route("**/api/views", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
    );

    await page.goto("/");

    const input = page.getByTestId("search-input");
    await input.fill("github");

    const results = page.getByTestId("search-results");
    await expect(results).toBeVisible({ timeout: 5000 });
    await expect(results).toContainText("GITHUB_TOKEN");
  });

  test("shows empty-state message when query returns no matches", async ({ page }) => {
    await page.route("**/api/search?**", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
    );
    await page.route("**/api/vault-status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ initialized: true }) }),
    );
    await page.route("**/api/registry", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
    );
    await page.route("**/api/leak-status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ statuses: [] }) }),
    );
    await page.route("**/api/views", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
    );

    await page.goto("/");

    const input = page.getByTestId("search-input");
    await input.fill("zzznomatch");

    const empty = page.getByTestId("search-empty");
    await expect(empty).toBeVisible({ timeout: 5000 });
    await expect(empty).toContainText("No matches");
  });
});
