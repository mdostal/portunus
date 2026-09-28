import { NextResponse } from "next/server";
import { runPortunus } from "@/lib/portunus";

// Health for pantheon-v2's L2 service-descriptor contract
// (docs/PANTHEON-CONTRACTS.md §2a) and any dashboard or supervisor.
//
// GET /api/health            -> `portunus health --json`, the read-only deep
//                               self-check (never resolves a value):
//                               {status: ok|degraded|down, checks: [...]}.
//                               HTTP 200 for ok/degraded (still serving),
//                               503 for down or when the CLI can't run.
// GET /api/health?shallow=1  -> {"status":"ok"}, a liveness signal for this
//                               Next.js process only. Never spawns the CLI;
//                               the Tauri sidecar's readiness poll uses it.
export async function GET(request: Request) {
  const shallow = new URL(request.url).searchParams.get("shallow");
  if (shallow === "1" || shallow === "true") {
    return NextResponse.json({ status: "ok" });
  }

  let detail: string;
  try {
    const result = await runPortunus(["health", "--json"]);
    // Exit 0/1/2 all carry a result on stdout; anything else is a crash.
    try {
      const body = JSON.parse(result.stdout);
      if (["ok", "degraded", "down"].includes(body?.status)) {
        return NextResponse.json(body, { status: body.status === "down" ? 503 : 200 });
      }
    } catch {
      // fall through to the synthetic "down" result
    }
    detail = `portunus health exited ${result.code} without a result`;
  } catch (err) {
    detail = err instanceof Error ? err.message : "failed to spawn portunus";
  }
  return NextResponse.json(
    { status: "down", checks: [{ name: "cli", ok: false, detail }] },
    { status: 503 },
  );
}
