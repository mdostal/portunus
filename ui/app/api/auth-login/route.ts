import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Triggers `gcloud auth login <email>` from the wizard -- this genuinely
// opens a real browser OAuth flow (gcloud's own, unchanged), Portunus
// doesn't remove or replace that step. The point is only that a user
// clicks a button in the Standalone UI to kick it off instead of needing
// to already know to run a CLI command in a terminal first. Never touches
// a secret value -- only an account email, which is not one.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const email = String(body.email || "").trim();
  if (!email) {
    return NextResponse.json({ error: "email is required" }, { status: 400 });
  }
  const result = await runPortunus(["auth", "login", email]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "gcloud auth login failed") },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
