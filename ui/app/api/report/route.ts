import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell over `portunus report` -- always reads stdout (never passes
// --out), so this route never touches the filesystem; the download itself
// happens client-side from the response body. Matches `portunus report`'s
// own output byte-for-byte (portunus-metadata-crawl Slice 2).
export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const org = searchParams.get("org") || "";
  const project = searchParams.get("project") || "";

  const args = ["report"];
  if (org) args.push("--org", org);
  if (project) args.push("--project", project);

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "report failed") },
      { status: 502 },
    );
  }
  return new NextResponse(result.stdout, {
    status: 200,
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Content-Disposition": 'attachment; filename="portunus-report.md"',
    },
  });
}
