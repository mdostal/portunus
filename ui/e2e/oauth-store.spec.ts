import fs from "node:fs";
import http from "node:http";
import { test, expect } from "@playwright/test";
import { STUB_LOG } from "../playwright.config";

// POST /api/oauth-store with a manually entered credential. A public (PKCE)
// OAuth client has no client_secret: the route must accept that and hand
// `portunus oauth store` a bundle WITHOUT the key (an empty string would be
// sent to the token endpoint as an explicit empty secret). The stub CLI logs
// the bundle's key names only (e2e/fixtures/bin/portunus).

const PORT = 3100;
const HEADERS = { Origin: `http://127.0.0.1:${PORT}`, "Content-Type": "application/json" };

function post(body: Record<string, unknown>): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port: PORT, path: "/api/oauth-store", method: "POST", headers: HEADERS },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
      },
    );
    req.on("error", reject);
    req.end(JSON.stringify(body));
  });
}

// Accounts are unique to this spec, so only its own stub-log lines count. The
// log outlives a local run, so each test counts from the lines already there.
function stubLines(account: string): string[] {
  try {
    return fs.readFileSync(STUB_LOG, "utf8").split("\n").filter((line) => line.includes(account));
  } catch {
    return [];
  }
}

const BASE = { provider: "acme", client_id: "client-123", refresh_token: "rt-e2e", token_endpoint: "https://auth.example.test/token" };

test.describe("POST /api/oauth-store (manual credential)", () => {
  test("a public client (no client_secret) is stored without a client_secret key", async () => {
    const account = "oauth-store-spec-public";
    const before = stubLines(account).length;
    const res = await post({ ...BASE, account, name: "acme-public-oauth" });
    expect(res.status).toBe(200);
    expect(JSON.parse(res.body)).toMatchObject({ ok: true });
    expect(stubLines(account).slice(before)).toEqual([
      `oauth store acme ${account} --stdin`,
      `oauth-store-keys acme ${account}: client_id refresh_token token_endpoint `,
      `drop acme-public-oauth acme:${account} --backend oauth --stdin --provider acme`,
    ]);
  });

  test("an empty client_secret counts as none", async () => {
    const account = "oauth-store-spec-empty";
    const before = stubLines(account).length;
    const res = await post({ ...BASE, account, name: "acme-empty-oauth", client_secret: "" });
    expect(res.status).toBe(200);
    expect(stubLines(account).slice(before)).toContain(`oauth-store-keys acme ${account}: client_id refresh_token token_endpoint `);
  });

  test("a confidential client keeps its client_secret", async () => {
    const account = "oauth-store-spec-confidential";
    const before = stubLines(account).length;
    const res = await post({ ...BASE, account, name: "acme-confidential-oauth", client_secret: "cs-e2e" });
    expect(res.status).toBe(200);
    expect(stubLines(account).slice(before)).toContain(`oauth-store-keys acme ${account}: client_id client_secret refresh_token token_endpoint `);
  });

  test("a missing refresh_token is 400 and never invokes the CLI", async () => {
    const account = "oauth-store-spec-incomplete";
    const before = stubLines(account).length;
    const res = await post({ provider: "acme", account, name: "acme-incomplete-oauth", client_id: "client-123", token_endpoint: BASE.token_endpoint });
    expect(res.status).toBe(400);
    expect(JSON.parse(res.body).error).toBe("client_id, refresh_token, and token_endpoint are all required");
    expect(stubLines(account).length).toBe(before);
  });
});
