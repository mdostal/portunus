import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus, tagsToArg } from "@/lib/portunus";

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
    const tags = tagsToArg(body.tags);
    if (tags) args.push("--tags", tags);
  }
  if (body.description !== undefined) args.push("--description", String(body.description));
  if (body.purpose !== undefined) args.push("--purpose", String(body.purpose));
  if (body.injected_as !== undefined) {
    const injectedAs = tagsToArg(body.injected_as);
    if (injectedAs) args.push("--injected-as", injectedAs);
  }
  if (body.group !== undefined) args.push("--group", String(body.group));
  if (body.related !== undefined) {
    const related = Array.isArray(body.related) ? body.related.join(",") : String(body.related);
    if (related) args.push("--related", related);
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
