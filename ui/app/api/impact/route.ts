import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell over `portunus impact <name> --json` (PANT-1122) -- a
// reference's blast radius for DetailDrawer. Read-only, metadata-only; the
// suggested_related list is shown to the human, never applied here.
export async function GET(req: NextRequest) {
  const name = req.nextUrl.searchParams.get("name") || "";
  if (!name) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }
  const result = await runPortunus(["impact", name, "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "impact failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}
