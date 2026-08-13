import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Resolve-only: the Ask Bar always previews a match before any injection is
// committed. Actual injection goes through /api/inject with the resolved
// reference's own tags -- this route never accepts a --target.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const request = String(body.request || "").trim();
  if (!request) {
    return NextResponse.json({ error: "describe what you need, e.g. \"the vercel secret for mdostal.com\"" }, { status: 400 });
  }

  const result = await runPortunus(["ask", request, "--json"]);
  if (result.code !== 0) {
    // Fail-closed responses (ambiguous / no match) are expected outcomes,
    // not server errors -- surfaced as 200 with `resolved: false` so the
    // Ask Bar can render the clarifying question inline.
    return NextResponse.json({
      resolved: false,
      message: cleanError(result.stderr, "could not resolve that request"),
    });
  }
  const ref = JSON.parse(result.stdout || "{}");
  return NextResponse.json({ resolved: true, reference: ref });
}
