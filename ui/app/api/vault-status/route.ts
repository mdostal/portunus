import { NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Drives the setup wizard's first-run detection -- shells out to `portunus
// vault status`, which checks file existence in Python (paths.home()'s own
// resolution) rather than duplicating that logic here (design-discussion.md
// §5): absence of BOTH registry.json and vault-bindings.json.
export async function GET() {
  const result = await runPortunus(["vault", "status", "--json"]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "vault status failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}
