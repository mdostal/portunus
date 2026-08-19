import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Audit-only as of portunus-petitio-rbac Story 02 -- see roles.py's own
// module docstring. This route lets the Settings page manage policy
// records that genuinely persist and feed a would-allow/would-deny audit
// line on every resolve, but check_injectable()/retag() never raise on
// them yet. Thin shell-out, same pattern every other route uses.
export async function GET() {
  const result = await runPortunus(["roles", "show", "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "roles show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const action = String(body.action || "set");
  const scopeType = String(body.scope_type || "").trim();
  const scopeValue = String(body.scope_value || "").trim();
  const role = String(body.role || "").trim();
  if (!scopeType || !scopeValue || !role) {
    return NextResponse.json(
      { error: "scope_type, scope_value, and role are all required" },
      { status: 400 },
    );
  }

  const principal = String(body.principal || "").trim();

  let args: string[];
  if (action === "delete") {
    args = ["roles", "delete", "--scope-type", scopeType, "--scope-value", scopeValue, "--role", role];
    if (principal) args.push("--principal", principal);
  } else {
    args = ["roles", "set", "--scope-type", scopeType, "--scope-value", scopeValue, "--role", role];
    const actions = String(body.actions || "");
    if (actions) args.push("--actions", actions);
    if (principal) args.push("--principal", principal);
  }

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "roles action failed") },
      { status: 422 },
    );
  }

  const showResult = await runPortunus(["roles", "show", "--json"]);
  if (showResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(showResult.stderr, "roles show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(showResult.stdout || "{}"));
}
