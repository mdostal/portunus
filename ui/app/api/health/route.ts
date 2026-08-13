import { NextResponse } from "next/server";

// A trivial liveness signal for the Next.js process itself -- per
// pantheon-v2's L2 service-descriptor contract (docs/PANTHEON-CONTRACTS.md
// §2a). Deliberately never calls runPortunus() or touches any subprocess:
// it answers "is this process alive", not "is the portunus CLI healthy".
export async function GET() {
  return NextResponse.json({ status: "ok" });
}
