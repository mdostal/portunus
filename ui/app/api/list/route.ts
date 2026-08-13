import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Metadata-only project browse -- thin wrapper around `portunus list --json`
// (which already exists from portunus-vault-metadata story 05). Never a
// value: list_by_project() has no path to a backend at all.
export async function GET(req: NextRequest) {
  const project = req.nextUrl.searchParams.get("project");
  if (!project) {
    return NextResponse.json({ error: "project is required" }, { status: 400 });
  }
  const result = await runPortunus(["list", "--project", project, "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "list failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "[]"));
}
