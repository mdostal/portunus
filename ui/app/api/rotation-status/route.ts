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

// Sets ONLY the free-text `account` hint (e.g. a Vercel team slug or GitHub
// org) -- never `status`. `status` ("stub"|"real") reflects whether a real
// RotationAdapter exists in CODE for this provider; every provider is a
// stub today, and letting this handler accept a status value from the
// browser would let the UI claim a provider "really rotates" with no
// adapter backing that claim -- the same class of risk the two-zone
// real/stub backend picker (bindings route) already guards against.
// Structurally: this handler never reads `body.status` at all, so a future
// UI change can't silently start forwarding one without also editing here.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const provider = String(body.provider || "").trim();
  if (!provider) {
    return NextResponse.json({ error: "provider is required" }, { status: 400 });
  }
  const args = ["rotation-bindings", "set", provider];
  if (body.account) args.push("--account", String(body.account));

  const setResult = await runPortunus(args);
  if (setResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(setResult.stderr, "rotation-bindings set failed") },
      { status: 502 },
    );
  }
  const showResult = await runPortunus(["rotation-bindings", "show", provider, "--json"]);
  if (showResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(showResult.stderr, "rotation-bindings show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(showResult.stdout || "{}"));
}
