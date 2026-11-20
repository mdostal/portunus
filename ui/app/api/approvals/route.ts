import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";
import { inProject } from "@/app/scope";

// Gate status of every gated reference (GH #257): `portunus approvals
// --json`. Metadata only -- gate on/off, the live approval's time box, and
// whether an access is waiting on a human. Never a value.
//
// ?project=<name> keeps only that project's rows, the same scoping as
// /api/registry?project=.
export async function GET(req: NextRequest) {
  const result = await runPortunus(["approvals", "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "failed to load approvals") },
      { status: 502 },
    );
  }
  let rows = JSON.parse(result.stdout || "[]") as { name: string }[];
  const project = (req.nextUrl.searchParams.get("project") || "").trim();
  if (project) {
    const reg = await runPortunus(["reg", "json"]);
    if (reg.code !== 0) {
      return NextResponse.json(
        { error: cleanError(reg.stderr, "failed to load registry") },
        { status: 502 },
      );
    }
    const data = JSON.parse(reg.stdout || "{}") as Record<string, { project?: string; repo?: string; group?: string }>;
    rows = rows.filter((r) => data[r.name] && inProject(data[r.name], project));
  }
  return NextResponse.json(rows);
}
