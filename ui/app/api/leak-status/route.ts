import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// GET returns every reference with active leak findings (severity, finding
// count, timestamps -- never a value or file content, matching
// portunus_leak_status's own read-only posture). POST is the one mutation
// this route supports: mark-rotated, a human's own assertion Portunus
// cannot independently verify (design-discussion.md §7).
export async function GET() {
  const result = await runPortunus(["leak", "status", "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "leak status failed") },
      { status: 502 },
    );
  }
  return NextResponse.json({ statuses: JSON.parse(result.stdout || "[]") });
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const name = String(body.name || "").trim();
  if (!name) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }

  const result = await runPortunus(["leak", "mark-rotated", name]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "mark-rotated failed") },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
