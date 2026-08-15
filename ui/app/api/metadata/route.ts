import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell-out to `portunus metadata confirm/reject` -- the human-review
// counterpart to the portunus_suggest_metadata MCP tool. Confirm applies
// via the SAME `portunus retag` path a manual edit would use (no second
// write path); reject only ever clears the sidecar entry, never touches
// the live field.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const action = String(body.action || "");
  const name = String(body.name || "").trim();
  const field = String(body.field || "").trim();
  if (!name || !field) {
    return NextResponse.json({ error: "name and field are required" }, { status: 400 });
  }
  if (action !== "confirm" && action !== "reject") {
    return NextResponse.json({ error: "action must be confirm or reject" }, { status: 400 });
  }

  const result = await runPortunus(["metadata", action, name, field]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, `metadata ${action} failed`) },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
