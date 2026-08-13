import { NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Metadata only -- `portunus reg json` never includes a value.
export async function GET() {
  const result = await runPortunus(["reg", "json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "failed to load registry") },
      { status: 502 },
    );
  }
  const data = JSON.parse(result.stdout || "{}");
  return NextResponse.json(Object.values(data));
}
