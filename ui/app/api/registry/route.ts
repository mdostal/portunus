import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";
import { inProject } from "@/app/scope";

// Metadata only -- `portunus reg json` never includes a value.
//
// ?project=<name> (GH #257) returns only that project's references, so a
// handoff tab opened on one project never receives the names and
// descriptions of every other project. This scopes what the page shows; it
// is not an access boundary (anything that can reach this port can still
// ask without the parameter).
export async function GET(req: NextRequest) {
  const result = await runPortunus(["reg", "json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "failed to load registry") },
      { status: 502 },
    );
  }
  const data = JSON.parse(result.stdout || "{}");
  let refs = Object.values(data) as { project?: string; repo?: string; group?: string }[];
  const project = (req.nextUrl.searchParams.get("project") || "").trim();
  if (project) refs = refs.filter((r) => inProject(r, project));
  return NextResponse.json(refs);
}
