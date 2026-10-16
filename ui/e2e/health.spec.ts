import http from "node:http";
import { test, expect } from "@playwright/test";

// /api/health must not report healthy to a caller whose writes ui/proxy.ts
// would 403 for its Host (lib/host-guard.ts). The stub CLI reports "ok", so
// the "ui-write-host" check alone decides the result. Raw node:http so the
// Host header goes out exactly as given.

const PORT = 3100;

function get(path: string, host?: string): Promise<{ status: number; body: any }> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port: PORT, path, method: "GET", headers: host ? { Host: host } : {} },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body: JSON.parse(data) }));
      },
    );
    req.on("error", reject);
    req.end();
  });
}

function hostCheck(body: any) {
  return body.checks.find((c: any) => c.name === "ui-write-host");
}

test.describe("/api/health write-Host check", () => {
  test("loopback Host is healthy and includes the passing check", async () => {
    const res = await get("/api/health");
    expect(res.status).toBe(200);
    expect(res.body.status).toBe("ok");
    expect(hostCheck(res.body)).toMatchObject({ ok: true });
  });

  test("non-loopback Host is down (503) because its writes would be 403'd", async () => {
    const res = await get("/api/health", `portunus-ui:${PORT}`);
    expect(res.status).toBe(503);
    expect(res.body.status).toBe("down");
    expect(hostCheck(res.body)).toMatchObject({ ok: false });
    expect(hostCheck(res.body).detail).toContain("PORTUNUS_UI_HOST");
  });

  test("shallow liveness ignores the Host check", async () => {
    const res = await get("/api/health?shallow=1", `portunus-ui:${PORT}`);
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ status: "ok" });
  });
});
