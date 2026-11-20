import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Set a reference's lifecycle state (GH #257): `portunus state <name>
// <state>`. The UI uses it for the one-click "Enable" beside a dropped
// entry, the explicit step after `drop` that makes a value injectable.
// "requested" and "used" are not settable here: a request is fulfilled by
// dropping a value, and "used" is set by the resolver.
const SETTABLE = new Set(["enabled", "locked", "dropped", "revoked"]);

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const name = String(body.name || "").trim();
  const state = String(body.state || "").trim();
  if (!name || name.startsWith("-")) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }
  if (!SETTABLE.has(state)) {
    return NextResponse.json(
      { error: `state must be one of ${[...SETTABLE].join(", ")}` },
      { status: 400 },
    );
  }
  const result = await runPortunus(["state", name, state]);
  if (result.code !== 0) {
    return NextResponse.json({ error: cleanError(result.stderr, "state change failed") }, { status: 422 });
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
