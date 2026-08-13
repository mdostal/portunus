import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Metadata only -- `portunus audit --json` never includes a value.
export async function GET(req: NextRequest) {
  const secret = req.nextUrl.searchParams.get("secret") || "";
  const n = req.nextUrl.searchParams.get("n") || "25";
  const args = ["audit", n, "--json"];
  if (secret) args.push("--secret", secret);

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "failed to load audit trail") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "[]"));
}
