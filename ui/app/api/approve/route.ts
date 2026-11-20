import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";
import { TTL_PATTERN } from "@/app/scope";

// Grant a time-boxed approval for a gated reference (GH #257): `portunus
// approve <name> --ttl <ttl>`. A tick count ("3") or a wall-clock duration
// ("30m", "2h", "1d"), validated here before it reaches argv.
//
// The gate is honour-system against an agent running as the same OS user
// (it can run `portunus approve` itself); this button exists so the human
// path is the easy one, not to make the gate stronger.
export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const name = String(body.name || "").trim();
  const ttl = String(body.ttl ?? "").trim().toLowerCase();
  if (!name || name.startsWith("-")) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }
  if (!TTL_PATTERN.test(ttl)) {
    return NextResponse.json(
      { error: "ttl must be a tick count (3) or a duration (90s, 30m, 2h, 1d)" },
      { status: 400 },
    );
  }
  const result = await runPortunus(["approve", name, "--ttl", ttl]);
  if (result.code !== 0) {
    return NextResponse.json({ error: cleanError(result.stderr, "approve failed") }, { status: 422 });
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
