import { NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Runs `portunus leak-scan --json` -- a real mutation (persists new
// findings + watermarks), so POST, not GET, unlike the read-only crawl/
// report routes. Exit code 1 means "ran fine, found new leaks" (useful for
// a CI/cron invocation of the CLI itself) -- NOT a process error, so it's
// treated as success here alongside exit code 0. Only anything else is a
// real failure.
export async function POST() {
  const result = await runPortunus(["leak-scan", "--json"]);
  if (result.code !== 0 && result.code !== 1) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "leak-scan failed") },
      { status: 502 },
    );
  }
  const parsed = JSON.parse(result.stdout || "[]");
  if (Array.isArray(parsed)) {
    return NextResponse.json({ configured: true, findings: parsed });
  }
  // {"configured": false, "findings": []} shape when nothing is configured
  return NextResponse.json(parsed);
}
