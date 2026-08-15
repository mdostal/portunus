import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell over `portunus crawl --json` -- a discovery bundle for an LLM
// session to read, never an automatic writer. Same shape the CLI and the
// portunus_crawl_candidates MCP tool already produce (portunus-metadata-
// crawl Slice 1); this route adds no logic of its own.
export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const org = searchParams.get("org") || "";
  const project = searchParams.get("project") || "";

  const args = ["crawl", "--json"];
  if (org) args.push("--org", org);
  if (project) args.push("--project", project);

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "crawl failed") },
      { status: 502 },
    );
  }
  return NextResponse.json({ candidates: JSON.parse(result.stdout || "[]") });
}
