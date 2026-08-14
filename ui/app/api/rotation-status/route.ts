import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell-out to `portunus rotation-bindings show` -- same pattern
// every other route uses. Feeds DetailDrawer's Auto-rotate button: a
// reference's provider is real/rotatable only when this reports
// status="real" for it, which never happens today (every adapter is a
// stub) -- the button derives its disabled-ness from this response
// instead of a hardcoded `disabled` attribute.
export async function GET(req: NextRequest) {
  const provider = req.nextUrl.searchParams.get("provider") || "";
  const args = provider
    ? ["rotation-bindings", "show", provider, "--json"]
    : ["rotation-bindings", "show", "--json"];
  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "rotation-bindings show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}
