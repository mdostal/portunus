import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus, tagsToArg } from "@/lib/portunus";

// Dispatches to the resolved reference's own tags -- never re-parses free
// text here. The value itself never appears in this route; `portunus
// inject` resolves and injects it entirely inside the gated Python process.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const tags = tagsToArg(body.tags);
  const target = String(body.target || "");
  if (!tags || !target) {
    return NextResponse.json({ error: "tags and target are required" }, { status: 400 });
  }

  const args = ["inject", "--tags", tags, "--target", target];
  if (target === "env") {
    args.push("--var", String(body.var || ""));
  } else if (target === "file") {
    args.push(
      "--path", String(body.path || ""),
      "--format", String(body.format || ""),
      "--key", String(body.key || ""),
    );
  }

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "injection failed") },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
