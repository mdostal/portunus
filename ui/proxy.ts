import { NextRequest, NextResponse } from "next/server";
import { writeHostAllowed } from "@/lib/host-guard";
import { WRITE_TOKEN_HEADER, checkWrite, loadWriteAuth } from "@/lib/write-auth";

// Guards every mutating /api/* call before it reaches a route handler (and so
// before any `portunus` CLI spawn). The routes themselves take caller-supplied
// args (e.g. /api/inject's target=file + path), so without this any web page
// open in the user's browser could POST to them: a `text/plain` body is a CORS
// "simple" request with no preflight, and `req.json()` never checks the
// content-type. Next.js 16 renamed the `middleware` convention to `proxy`.
//
//   - Origin present and not this UI's own origin  -> 403
//   - Host not a loopback name (DNS rebinding)      -> 403, unless the operator
//     explicitly rebound the UI via PORTUNUS_UI_HOST
//   - Content-Type not application/json             -> 415
//   - X-Portunus-Write-Token missing or wrong        -> 401
//   - no write credential configured                 -> 503, unless the operator
//     explicitly opted out with PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES=1
//
// The first three stop a hostile browser page. They do nothing against a
// client that sets its own headers (curl from another container reaching the
// host's loopback port, another local user), which is what the write
// credential is for (lib/write-auth.ts). It covers EVERY non-GET method on
// every /api/* path, so a new mutating route is guarded without being listed.
//
// GET/HEAD pass through untouched: they don't mutate, and a cross-origin page
// can't read their responses without CORS headers, which no route sets. No
// GET route returns a secret value (docs/http-routes.md classifies each). The
// deep GET /api/health still runs the Host check itself (lib/host-guard.ts)
// and reports "down" when it would fail, so a misconfigured non-loopback
// deployment doesn't look healthy while every write is rejected.

const SAFE_METHODS = new Set(["GET", "HEAD"]);

function reject(status: number, error: string) {
  return NextResponse.json({ error }, { status });
}

function isSameOrigin(origin: string, host: string): boolean {
  try {
    const url = new URL(origin);
    return (url.protocol === "http:" || url.protocol === "https:") && url.host === host;
  } catch {
    // "null" (sandboxed iframe, file://) and anything unparseable.
    return false;
  }
}

export function proxy(req: NextRequest) {
  if (SAFE_METHODS.has(req.method)) {
    return NextResponse.next();
  }

  const host = req.headers.get("host") ?? "";
  const origin = req.headers.get("origin");
  if (origin !== null && !isSameOrigin(origin, host)) {
    return reject(403, "cross-origin request rejected");
  }
  if (!writeHostAllowed(host)) {
    return reject(403, "non-loopback Host rejected");
  }

  const mediaType = (req.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  if (mediaType !== "application/json") {
    return reject(415, "Content-Type must be application/json");
  }

  const decision = checkWrite(loadWriteAuth(), req.headers.get(WRITE_TOKEN_HEADER));
  if (!decision.ok) {
    return NextResponse.json({ error: decision.error, code: decision.code }, { status: decision.status });
  }

  return NextResponse.next();
}

export const config = {
  matcher: "/api/:path*",
};
