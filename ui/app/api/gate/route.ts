import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Turn a reference's approval gate on or off (GH #257): `portunus gate
// <name> [--off]`, the same Broker.gate the CLI runs, audited there. Like
// every write it passes ui/proxy.ts first (same-origin, loopback Host, JSON,
// write credential).
export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const name = String(body.name || "").trim();
  if (!name || name.startsWith("-")) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }
  if (typeof body.on !== "boolean") {
    return NextResponse.json({ error: "on must be true or false" }, { status: 400 });
  }
  const args = ["gate", name];
  if (!body.on) args.push("--off");
  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json({ error: cleanError(result.stderr, "gate failed") }, { status: 422 });
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
