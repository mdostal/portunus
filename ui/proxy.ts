import { NextRequest, NextResponse } from "next/server";

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
//
// GET/HEAD pass through untouched: they don't mutate, and a cross-origin page
// can't read their responses without CORS headers, which no route sets.

const SAFE_METHODS = new Set(["GET", "HEAD"]);
const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]"]);

function reject(status: number, error: string) {
  return NextResponse.json({ error }, { status });
}

function hostnameOf(host: string): string {
  try {
    return new URL(`http://${host}`).hostname;
  } catch {
    return "";
  }
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
  if (!process.env.PORTUNUS_UI_HOST && !LOOPBACK_HOSTNAMES.has(hostnameOf(host))) {
    return reject(403, "non-loopback Host rejected");
  }

  const mediaType = (req.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  if (mediaType !== "application/json") {
    return reject(415, "Content-Type must be application/json");
  }

  return NextResponse.next();
}

export const config = {
  matcher: "/api/:path*",
};
