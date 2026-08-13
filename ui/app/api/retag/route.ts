import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Metadata-only Move action. Shells out to `portunus retag` -- the
// collision check lives entirely in Registry.retag() (Python); this route
// never re-implements it, same pattern as every other write route.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const name = String(body.name || "").trim();
  if (!name) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }

  const args = ["retag", name];
  if (body.provider !== undefined) args.push("--provider", String(body.provider));
  if (body.project !== undefined) args.push("--project", String(body.project));
  if (body.env !== undefined) args.push("--env", String(body.env));
  if (body.tags !== undefined) {
    const tags = typeof body.tags === "string"
      ? body.tags
      : Object.entries(body.tags as Record<string, string>).map(([k, v]) => `${k}=${v}`).join(",");
    if (tags) args.push("--tags", tags);
  }

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "retag failed") },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
