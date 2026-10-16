import { NextResponse } from "next/server";
import { writeHostAllowed } from "@/lib/host-guard";
import { runPortunus } from "@/lib/portunus";

// Health for pantheon-v2's L2 service-descriptor contract
// (docs/PANTHEON-CONTRACTS.md §2a) and any dashboard or supervisor.
//
// GET /api/health            -> `portunus health --json`, the read-only deep
//                               self-check (never resolves a value):
//                               {status: ok|degraded|down, checks: [...]},
//                               plus a "ui-write-host" check (below).
//                               HTTP 200 for ok/degraded (still serving),
//                               503 for down or when the CLI can't run.
// GET /api/health?shallow=1  -> {"status":"ok"}, a liveness signal for this
//                               Next.js process only. Never spawns the CLI;
//                               the Tauri sidecar's readiness poll uses it.
//
// "ui-write-host": proxy.ts lets GET/HEAD through without its Host check, so
// this route runs that check against its own request's Host. A caller that
// reaches the UI over a Host whose writes proxy.ts would 403 (a non-loopback
// name with PORTUNUS_UI_HOST unset) gets status "down": nothing it sends can
// mutate, so it mustn't see a green health result.
export async function GET(request: Request) {
  const shallow = new URL(request.url).searchParams.get("shallow");
  if (shallow === "1" || shallow === "true") {
    return NextResponse.json({ status: "ok" });
  }

  const host = request.headers.get("host") ?? "";
  const hostCheck = writeHostAllowed(host)
    ? { name: "ui-write-host", ok: true, detail: "writes accepted on this Host" }
    : {
        name: "ui-write-host",
        ok: false,
        detail: `writes over Host "${host}" are rejected (non-loopback Host); set PORTUNUS_UI_HOST to accept them`,
      };

  let detail: string;
  try {
    const result = await runPortunus(["health", "--json"]);
    // Exit 0/1/2 all carry a result on stdout; anything else is a crash.
    try {
      const body = JSON.parse(result.stdout);
      if (["ok", "degraded", "down"].includes(body?.status)) {
        const checks = Array.isArray(body.checks) ? body.checks : [];
        const status = hostCheck.ok ? body.status : "down";
        return NextResponse.json(
          { ...body, status, checks: [...checks, hostCheck] },
          { status: status === "down" ? 503 : 200 },
        );
      }
    } catch {
      // fall through to the synthetic "down" result
    }
    detail = `portunus health exited ${result.code} without a result`;
  } catch (err) {
    detail = err instanceof Error ? err.message : "failed to spawn portunus";
  }
  return NextResponse.json(
    { status: "down", checks: [{ name: "cli", ok: false, detail }, hostCheck] },
    { status: 503 },
  );
}
