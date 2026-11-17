import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test, expect } from "@playwright/test";
import { STUB_LOG, WRITE_TOKEN, WRITE_TOKEN_FILE } from "../playwright.config";
import {
  MIN_TOKEN_LENGTH,
  checkWrite,
  loadWriteAuth,
  tokensMatch,
} from "../lib/write-auth";

// The write credential (lib/write-auth.ts, enforced in proxy.ts). Every
// state-changing route must answer 401 without it, or with a wrong one, and
// never reach the `portunus` CLI, which is the only way any route changes the
// vault. The stub CLI (e2e/fixtures/bin/portunus) logs every call, so "never
// reached the CLI" is "no stub-log line carries this request's marker".

const PORT = 3100;
const UI_DIR = path.join(__dirname, "..");
const STUB_BIN = path.join(__dirname, "fixtures", "bin");
const RUN = `${process.pid}-${Date.now()}`;

type Reply = { status: number; body: string };

function send(
  port: number,
  method: string,
  urlPath: string,
  headers: Record<string, string>,
  body?: string,
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, path: urlPath, method, headers }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

function postJson(port: number, urlPath: string, body: unknown, token?: string): Promise<Reply> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token !== undefined) headers["X-Portunus-Write-Token"] = token;
  return send(port, "POST", urlPath, headers, JSON.stringify(body));
}

function stubLines(match: (line: string) => boolean): number {
  try {
    return fs.readFileSync(STUB_LOG, "utf8").split("\n").filter(match).length;
  } catch {
    return 0;
  }
}

const has = (marker: string) => (line: string) => line.includes(marker);

// Every state-changing route, with a body that makes the route reach the CLI
// carrying `m` in its argv. docs/http-routes.md lists the same set.
const WRITE_ROUTES: { route: string; body: (m: string) => unknown; seen?: (m: string) => (l: string) => boolean }[] = [
  { route: "ask", body: (m) => ({ request: m }) },
  { route: "auth-login", body: (m) => ({ email: m }) },
  { route: "bindings", body: (m) => ({ project: m }) },
  { route: "discover", body: (m) => ({ project: m }) },
  { route: "drop", body: (m) => ({ name: m, sm_name: "sm-t1c", value: "not-a-real-value" }) },
  { route: "inject", body: (m) => ({ tags: { provider: m }, target: "env", var: "T1C" }) },
  { route: "leak-scan-config", body: (m) => ({ action: "add", glob: m }) },
  // Takes no arguments, so its argv carries no marker; no other spec calls it.
  { route: "leak-scan", body: () => ({}), seen: () => (l) => l === "leak-scan --json" },
  { route: "leak-status", body: (m) => ({ name: m }) },
  { route: "metadata", body: (m) => ({ action: "confirm", name: m, field: "description" }) },
  {
    route: "oauth-store",
    body: (m) => ({
      provider: "acme", account: m, name: `n-${m}`,
      client_id: "c", refresh_token: "r", token_endpoint: "https://auth.example.test/token",
    }),
  },
  { route: "retag", body: (m) => ({ name: m, provider: "x" }) },
  { route: "roles", body: (m) => ({ scope_type: "project", scope_value: m, role: "reader" }) },
  { route: "rotation-status", body: (m) => ({ provider: m }) },
  { route: "views", body: (m) => ({ action: "create", name: m }) },
];

test.describe("write credential: every state-changing route", () => {
  for (const { route, body, seen } of WRITE_ROUTES) {
    test(`POST /api/${route}: 401 without or with a wrong credential, reaches the CLI with it`, async () => {
      const m = `t1c-${route}-${RUN}`;
      const match = (seen ?? has)(m);
      const before = stubLines(match);

      const missing = await postJson(PORT, `/api/${route}`, body(m));
      expect(missing.status).toBe(401);
      expect(JSON.parse(missing.body)).toMatchObject({ code: "write_token_required" });

      const wrong = await postJson(PORT, `/api/${route}`, body(m), `${WRITE_TOKEN}x`);
      expect(wrong.status).toBe(401);
      expect(JSON.parse(wrong.body)).toMatchObject({ error: "write credential rejected" });

      const empty = await postJson(PORT, `/api/${route}`, body(m), "");
      expect(empty.status).toBe(401);

      expect(stubLines(match)).toBe(before);

      const ok = await postJson(PORT, `/api/${route}`, body(m), WRITE_TOKEN);
      expect([401, 403, 415, 503]).not.toContain(ok.status);
      expect(stubLines(match)).toBeGreaterThan(before);
    });
  }

  test("any other method, and a path no route serves, need the credential too", async () => {
    for (const method of ["PUT", "PATCH", "DELETE"]) {
      // Content-Length set: Next answers 400 to a DELETE whose body is chunked.
      const res = await send(PORT, method, "/api/inject", { "Content-Type": "application/json", "Content-Length": "2" }, "{}");
      expect(res.status, method).toBe(401);
    }
    const unknown = await postJson(PORT, "/api/no-such-route", {});
    expect(unknown.status).toBe(401);
  });

  test("path spellings of a write route never reach the CLI without the credential", async () => {
    const m = `t1c-spelling-${RUN}`;
    const body = { tags: { provider: m }, target: "env", var: "X" };
    for (const spelling of ["/api/inject/", "//api/inject", "/api/./inject", "/api/x/../inject", "/%61pi/inject", "/api/%69nject"]) {
      const res = await postJson(PORT, spelling, body);
      expect(res.status, spelling).not.toBe(200);
    }
    expect(stubLines(has(m))).toBe(0);
  });

  test("GET stays open: metadata reads need no credential", async () => {
    const before = stubLines((l) => l === "reg json");
    const res = await send(PORT, "GET", "/api/registry", {});
    expect([401, 503]).not.toContain(res.status);
    expect(stubLines((l) => l === "reg json")).toBe(before + 1);
  });

  test("the HTTP inject route never forwards the CLI-only --replace", async () => {
    const m = `t1c-replace-${RUN}`;
    const res = await postJson(
      PORT,
      "/api/inject",
      { tags: { provider: m }, target: "file", path: `/nonexistent/${m}`, format: "env", key: "K", replace: true },
      WRITE_TOKEN,
    );
    expect(res.status).toBe(200);
    const line = fs.readFileSync(STUB_LOG, "utf8").split("\n").find(has(m));
    expect(line).toBeDefined();
    expect(line).not.toContain("--replace");
  });

  test("deep health reports the credential is required, never the token", async () => {
    const res = await send(PORT, "GET", "/api/health", {});
    const body = JSON.parse(res.body);
    const check = body.checks.find((c: { name: string }) => c.name === "ui-write-auth");
    expect(check).toMatchObject({ ok: true });
    expect(check.detail).toContain("X-Portunus-Write-Token");
    expect(res.body).not.toContain(WRITE_TOKEN);
  });
});

test.describe("write credential: the UI's own browser writes", () => {
  test("a #write-token fragment is taken, stripped from the URL, and sent on writes", async ({ page }) => {
    const m = `t1c-fragment-${RUN}`;
    await page.goto(`/#write-token=${encodeURIComponent(WRITE_TOKEN)}`);
    await expect.poll(() => page.url()).not.toContain("write-token");
    const status = await page.evaluate(async (marker) => {
      const res = await fetch("/api/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: { provider: marker }, target: "env", var: "X" }),
      });
      return res.status;
    }, m);
    expect(status).toBe(200);
    expect(stubLines(has(m))).toBe(1);
  });

  test("without a token, a write asks for it once and is retried with it", async ({ page }) => {
    const m = `t1c-dialog-${RUN}`;
    await page.goto("/");
    const pending = page.evaluate(async (marker) => {
      const res = await fetch("/api/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: { provider: marker }, target: "env", var: "X" }),
      });
      return res.status;
    }, m);
    const input = page.locator("dialog input[type=password]");
    await expect(input).toBeVisible();
    expect(stubLines(has(m))).toBe(0);
    await input.fill(WRITE_TOKEN);
    await page.getByRole("button", { name: "Unlock writes" }).click();
    expect(await pending).toBe(200);
    expect(stubLines(has(m))).toBe(1);
  });

  test("a rejected stored token is dropped and asked for again", async ({ page }) => {
    const m = `t1c-rejected-${RUN}`;
    await page.goto(`/#write-token=wrong-token-that-is-long-enough`);
    const pending = page.evaluate(async (marker) => {
      const res = await fetch("/api/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: { provider: marker }, target: "env", var: "X" }),
      });
      return res.status;
    }, m);
    await expect(page.locator("dialog")).toContainText("rejected");
    await page.locator("dialog input[type=password]").fill(WRITE_TOKEN);
    await page.getByRole("button", { name: "Unlock writes" }).click();
    expect(await pending).toBe(200);
    expect(stubLines(has(m))).toBe(1);
  });
});

// The same build, started again with a different environment, for the states
// the shared test server (which has a token file) can't be in.
function startServer(port: number, env: Record<string, string>): Promise<{ child: ChildProcess; output: () => string }> {
  const base = { ...process.env };
  delete base.PORTUNUS_WRITE_TOKEN_FILE;
  delete base.PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES;
  delete base.PORTUNUS_UI_HOST;
  const child = spawn(
    process.execPath,
    [require.resolve("next/dist/bin/next"), "start", "-p", String(port), "-H", "127.0.0.1"],
    {
      cwd: UI_DIR,
      env: { ...base, PATH: `${STUB_BIN}${path.delimiter}${process.env.PATH}`, PORTUNUS_STUB_LOG: STUB_LOG, ...env },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let out = "";
  child.stdout?.on("data", (c) => (out += c));
  child.stderr?.on("data", (c) => (out += c));
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 30000;
    const poll = () => {
      send(port, "GET", "/api/health?shallow=1", {})
        .then((r) => (r.status === 200 ? resolve({ child, output: () => out }) : retry()))
        .catch(retry);
    };
    const retry = () => {
      if (Date.now() > deadline) {
        child.kill();
        reject(new Error(`server on ${port} did not start:\n${out}`));
      } else setTimeout(poll, 200);
    };
    poll();
  });
}

test.describe("write credential: server states", () => {
  test("no credential configured: every write is refused with 503, loudly", async () => {
    const port = 3101;
    const server = await startServer(port, {});
    try {
      const m = `t1c-unconfigured-${RUN}`;
      const res = await postJson(port, "/api/inject", { tags: { provider: m }, target: "env", var: "X" }, WRITE_TOKEN);
      expect(res.status).toBe(503);
      expect(JSON.parse(res.body).error).toContain("PORTUNUS_WRITE_TOKEN_FILE");
      expect(stubLines(has(m))).toBe(0);
      const health = JSON.parse((await send(port, "GET", "/api/health", {})).body);
      expect(health.status).toBe("degraded");
      expect(health.checks.find((c: { name: string }) => c.name === "ui-write-auth")).toMatchObject({ ok: false });
      expect(server.output()).toContain("!!!!");
    } finally {
      server.child.kill();
    }
  });

  test("a token file that can't be read refuses every write with 503", async () => {
    const port = 3102;
    const server = await startServer(port, { PORTUNUS_WRITE_TOKEN_FILE: path.join(os.tmpdir(), `t1c-missing-${RUN}`) });
    try {
      const m = `t1c-unavailable-${RUN}`;
      const res = await postJson(port, "/api/retag", { name: m, provider: "x" }, WRITE_TOKEN);
      expect(res.status).toBe(503);
      expect(JSON.parse(res.body)).toMatchObject({ code: "write_token_unavailable" });
      expect(stubLines(has(m))).toBe(0);
    } finally {
      server.child.kill();
    }
  });

  test("the explicit opt-out opens writes and says so at startup", async () => {
    const port = 3103;
    const server = await startServer(port, { PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES: "1" });
    try {
      const m = `t1c-open-${RUN}`;
      const res = await postJson(port, "/api/retag", { name: m, provider: "x" });
      expect(res.status).toBe(200);
      expect(stubLines(has(m))).toBe(1);
      expect(server.output()).toContain("OPEN: writes need no credential");
    } finally {
      server.child.kill();
    }
  });
});

test.describe("write credential: lib/write-auth.ts", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "t1c-write-auth-"));

  test("modes come from the environment, and a token file beats the opt-out", () => {
    expect(loadWriteAuth({})).toEqual({ mode: "unconfigured" });
    expect(loadWriteAuth({ PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES: "true" })).toEqual({ mode: "unconfigured" });
    expect(loadWriteAuth({ PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES: "1" })).toEqual({ mode: "open" });
    expect(
      loadWriteAuth({ PORTUNUS_WRITE_TOKEN_FILE: WRITE_TOKEN_FILE, PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES: "1" }),
    ).toEqual({ mode: "token", token: WRITE_TOKEN });
    expect(loadWriteAuth({ PORTUNUS_WRITE_TOKEN_FILE: path.join(dir, "absent") }).mode).toBe("unavailable");
  });

  test("a short or empty token file refuses writes rather than accepting a guessable token", () => {
    const file = path.join(dir, "short");
    fs.writeFileSync(file, "x".repeat(MIN_TOKEN_LENGTH - 1) + "\n");
    expect(loadWriteAuth({ PORTUNUS_WRITE_TOKEN_FILE: file }).mode).toBe("unavailable");
    fs.writeFileSync(file, "\n");
    expect(loadWriteAuth({ PORTUNUS_WRITE_TOKEN_FILE: file }).mode).toBe("unavailable");
  });

  test("the file is read per request, so rewriting it rotates the token", () => {
    const file = path.join(dir, "rotating");
    fs.writeFileSync(file, "first-token-0123456789\n");
    const env = { PORTUNUS_WRITE_TOKEN_FILE: file };
    expect(checkWrite(loadWriteAuth(env), "first-token-0123456789").ok).toBe(true);
    fs.writeFileSync(file, "second-token-0123456789\n");
    expect(checkWrite(loadWriteAuth(env), "first-token-0123456789")).toMatchObject({ ok: false, status: 401 });
    expect(checkWrite(loadWriteAuth(env), "second-token-0123456789").ok).toBe(true);
  });

  test("tokensMatch compares exactly", () => {
    expect(tokensMatch("abc", "abc")).toBe(true);
    expect(tokensMatch("abc", "abd")).toBe(false);
    expect(tokensMatch("abc", "abcd")).toBe(false);
    expect(tokensMatch("", "abc")).toBe(false);
  });
});
