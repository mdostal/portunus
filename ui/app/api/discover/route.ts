import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Read-only diff (GET) + register (POST) -- both thin runPortunus() wrappers
// around `portunus discover`, same shape as every other route. Never a
// value: discover.py has no path to a SecretBackend.access() call at all.
// wif_configured is boolean-only (never the WIF audience string), matching
// `portunus auth gcp`'s own restraint.
export async function GET(req: NextRequest) {
  const project = req.nextUrl.searchParams.get("project");
  if (!project) {
    return NextResponse.json({ error: "project is required" }, { status: 400 });
  }
  const result = await runPortunus(["discover", "--provider", "gcp", "--project", project, "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "discover failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const project = String(body.project || "").trim();
  if (!project) {
    return NextResponse.json({ error: "project is required" }, { status: 400 });
  }
  const result = await runPortunus([
    "discover", "--provider", "gcp", "--project", project, "--register", "--json",
  ]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "discover --register failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}
