import { existsSync } from "node:fs";
import { unlink } from "node:fs/promises";
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
  const path = String(body.path || "");
  if (target === "env") {
    args.push("--var", String(body.var || ""));
  } else if (target === "file") {
    args.push(
      "--path", path,
      "--format", String(body.format || ""),
      "--key", String(body.key || ""),
    );
  }

  // PANT-1003: a caller that times out deletes its target file before this
  // route has written it, then the file lands with nobody left to read or
  // remove it. So on client disconnect the CLI is killed, and a file this
  // request created is removed once the CLI has exited. A file that already
  // existed is left alone.
  const createsFile = target === "file" && path !== "" && !existsSync(path);
  const result = await runPortunus(args, undefined, req.signal);
  if (req.signal.aborted) {
    if (createsFile) {
      await unlink(path).catch((err: NodeJS.ErrnoException) => {
        if (err.code !== "ENOENT") {
          console.error(`inject: failed to remove abandoned file ${path}: ${err.code ?? err.message}`);
        }
      });
    }
    return new NextResponse(null, { status: 499 });
  }
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "injection failed") },
      { status: 422 },
    );
  }
  return NextResponse.json({ ok: true, message: result.stdout.trim() });
}
