import { NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

export async function GET() {
  const result = await runPortunus(["auth", "status", "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "auth status failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}
