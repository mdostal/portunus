import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell-out to `portunus bindings show/set` -- same pattern every
// other route uses. No gating/routing logic duplicated in TypeScript; the
// backend dropdown/sync_mode toggle only ever reflect what the CLI itself
// reports. The WIF audience value itself is never returned by `bindings
// show` -- unchanged restraint from before this route existed.
export async function GET(req: NextRequest) {
  const project = req.nextUrl.searchParams.get("project");
  if (!project) {
    return NextResponse.json({ error: "project is required" }, { status: 400 });
  }
  const result = await runPortunus(["bindings", "show", project, "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "bindings show failed") },
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
  const args = ["bindings", "set", project];
  if (body.backend) args.push("--backend", String(body.backend));
  if (body.sync_mode) args.push("--sync-mode", String(body.sync_mode));

  const setResult = await runPortunus(args);
  if (setResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(setResult.stderr, "bindings set failed") },
      { status: 502 },
    );
  }
  const showResult = await runPortunus(["bindings", "show", project, "--json"]);
  if (showResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(showResult.stderr, "bindings show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(showResult.stdout || "{}"));
}
