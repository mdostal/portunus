import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test, expect } from "@playwright/test";
import { STUB_LOG } from "../playwright.config";

// PANT-1003: a caller that gives up on a file-target inject (core-api aborts
// after 5s) must not get the file written after it left. The stub CLI waits
// 2s before writing any path containing "slow-".

const PORT = 3100;

function postThenAbort(body: string, abortAfterMs: number): Promise<void> {
  return new Promise((resolve) => {
    const req = http.request({
      host: "127.0.0.1",
      port: PORT,
      path: "/api/inject",
      method: "POST",
      headers: { Origin: `http://127.0.0.1:${PORT}`, "Content-Type": "application/json" },
    });
    req.on("error", () => resolve());
    req.on("close", () => resolve());
    req.end(body);
    setTimeout(() => req.destroy(), abortAfterMs);
  });
}

test("an aborted file inject leaves no file behind", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "portunus-e2e-"));
  const target = path.join(dir, "slow-abort.secret");
  await postThenAbort(
    JSON.stringify({ tags: { provider: "github" }, target: "file", path: target, format: "env", key: "VALUE" }),
    500,
  );
  await new Promise((r) => setTimeout(r, 3000));
  expect(fs.existsSync(target)).toBe(false);
});

test("a completed slow file inject still writes the file", async ({ request }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "portunus-e2e-"));
  const target = path.join(dir, "slow-ok.secret");
  const res = await request.post("/api/inject", {
    headers: { Origin: `http://127.0.0.1:${PORT}` },
    data: { tags: { provider: "github" }, target: "file", path: target, format: "env", key: "VALUE" },
  });
  expect(res.status()).toBe(200);
  expect(fs.readFileSync(target, "utf8")).toBe("VALUE=stub\n");
});

test("a file inject passes mode and group through and returns the file description", async ({ request }) => {
  // PANT-1142: a caller on another uid needs a group-readable file, and an
  // "ok" it can check against the file's real owner/group/mode.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "portunus-e2e-"));
  const target = path.join(dir, "grouped.env");
  const res = await request.post("/api/inject", {
    headers: { Origin: `http://127.0.0.1:${PORT}` },
    data: {
      tags: { provider: "github" }, target: "file", path: target,
      format: "env", key: "VALUE", mode: "0640", group: "10002",
    },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body).toMatchObject({ ok: true, message: "stub ok" });
  expect(body).toHaveProperty("file");
  const call = fs.readFileSync(STUB_LOG, "utf8").split("\n").find((line) => line.includes(`--path ${target}`));
  expect(call).toContain("--mode 0640 --group 10002 --json");
});
