import fs from "node:fs";
import http from "node:http";
import { test, expect } from "@playwright/test";
import { STUB_LOG } from "../playwright.config";

// ui/proxy.ts: mutating /api/* calls must be same-origin JSON on a loopback
// Host, and a rejected call must never reach the `portunus` CLI. Uses raw
// node:http (not the browser or fetch) so Origin/Host/Content-Type go out
// exactly as a hostile page or rebinding attack would send them.

const PORT = 3100;
const INJECT_BODY = JSON.stringify({ tags: { provider: "github" }, target: "file", path: "/tmp/x", format: "env", key: "K" });

function post(headers: Record<string, string>, body = INJECT_BODY): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port: PORT, path: "/api/inject", method: "POST", headers },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}

function stubCalls(): string[] {
  try {
    return fs.readFileSync(STUB_LOG, "utf8").split("\n").filter(Boolean);
  } catch {
    return [];
  }
}

test.describe("API guard (proxy.ts)", () => {
  test("cross-origin POST is 403 and never invokes the CLI", async () => {
    const before = stubCalls().length;
    const res = await post({ Origin: "https://evil.example", "Content-Type": "application/json" });
    expect(res.status).toBe(403);
    expect(stubCalls().length).toBe(before);
  });

  test("text/plain POST (CORS simple request) is 415 and never invokes the CLI", async () => {
    const before = stubCalls().length;
    const res = await post({ "Content-Type": "text/plain" });
    expect(res.status).toBe(415);
    expect(stubCalls().length).toBe(before);
  });

  test("DNS-rebound Host is 403 even when Origin matches it", async () => {
    const before = stubCalls().length;
    const res = await post({
      Host: `evil.example:${PORT}`,
      Origin: `http://evil.example:${PORT}`,
      "Content-Type": "application/json",
    });
    expect(res.status).toBe(403);
    expect(stubCalls().length).toBe(before);
  });

  test("same-origin JSON POST still reaches the CLI", async () => {
    const before = stubCalls().length;
    const res = await post({
      Origin: `http://127.0.0.1:${PORT}`,
      "Content-Type": "application/json; charset=utf-8",
    });
    expect(res.status).toBe(200);
    expect(JSON.parse(res.body)).toEqual({ ok: true, message: "stub ok" });
    const calls = stubCalls();
    expect(calls.length).toBe(before + 1);
    expect(calls[calls.length - 1]).toContain("inject --tags");
  });

  test("the UI's own browser fetch still works", async ({ page }) => {
    await page.goto("/");
    const status = await page.evaluate(async () => {
      const res = await fetch("/api/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: { provider: "github" }, target: "env", var: "X" }),
      });
      return res.status;
    });
    expect(status).toBe(200);
  });
});
