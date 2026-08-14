import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus, tagsToArg } from "@/lib/portunus";

// The ONE route in this app that ever touches a plaintext value -- the
// deliberate human-plaintext-entry point (Grill U1 resolution). The value
// is piped to `portunus drop --stdin` via stdin ONLY: never an argv
// element (would land in `ps`/shell history on the server), never included
// in a log line, and never echoed back in this route's own response --
// success/failure is reported by name only.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const name = String(body.name || "").trim();
  const smName = String(body.sm_name || "").trim();
  const value = String(body.value ?? "");

  if (!name || !smName || !value) {
    return NextResponse.json(
      { error: "name, sm_name, and value are all required" },
      { status: 400 },
    );
  }

  const args = ["drop", name, smName, "--stdin"];
  if (body.kind) args.push("--kind", String(body.kind));
  if (body.scope) args.push("--scope", String(body.scope));
  if (body.backend) args.push("--backend", String(body.backend));
  if (body.provider) args.push("--provider", String(body.provider));
  if (body.project) args.push("--project", String(body.project));
  if (body.env) args.push("--env", String(body.env));
  const tags = tagsToArg(body.tags);
  if (tags) args.push("--tags", tags);
  if (body.description) args.push("--description", String(body.description));
  if (body.purpose) args.push("--purpose", String(body.purpose));
  const injectedAs = tagsToArg(body.injected_as);
  if (injectedAs) args.push("--injected-as", injectedAs);
  if (body.group) args.push("--group", String(body.group));
  if (body.related) args.push("--related", String(body.related));

  const result = await runPortunus(args, value + "\n");

  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "drop failed") },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
