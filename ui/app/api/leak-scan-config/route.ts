import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Configured scan-path globs -- explicit, persisted, empty by default.
// GET lists them; POST add/removes one. Never auto-populated -- a human
// must add every path Portunus is allowed to read.
export async function GET() {
  const result = await runPortunus(["leak-scan", "config", "show", "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "leak-scan config show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json({ paths: JSON.parse(result.stdout || "[]") });
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const action = String(body.action || "");
  const glob = String(body.glob || "").trim();
  if (!glob || (action !== "add" && action !== "remove")) {
    return NextResponse.json(
      { error: "glob and action ('add' or 'remove') are required" },
      { status: 400 },
    );
  }

  const subcommand = action === "add" ? "add-path" : "remove-path";
  const result = await runPortunus(["leak-scan", "config", subcommand, glob]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "leak-scan config update failed") },
      { status: 422 },
    );
  }

  const showResult = await runPortunus(["leak-scan", "config", "show", "--json"]);
  if (showResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(showResult.stderr, "leak-scan config show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json({ paths: JSON.parse(showResult.stdout || "[]") });
}
