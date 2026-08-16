import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Configured scan-path globs AND git-repo scan targets (portunus-leak-
// scan-git-awareness) -- both explicit, persisted, empty by default.
// GET lists both; POST add/removes one, disambiguated by `target`
// ("path" | "repo", defaulting to "path" for backward compatibility with
// callers that only ever knew about plain paths). Never auto-populated --
// a human must add every path/repo Portunus is allowed to read.
export async function GET() {
  const [pathsResult, reposResult] = await Promise.all([
    runPortunus(["leak-scan", "config", "show", "--json"]),
    runPortunus(["leak-scan", "config", "show-repos", "--json"]),
  ]);
  if (pathsResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(pathsResult.stderr, "leak-scan config show failed") },
      { status: 502 },
    );
  }
  if (reposResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(reposResult.stderr, "leak-scan config show-repos failed") },
      { status: 502 },
    );
  }
  return NextResponse.json({
    paths: JSON.parse(pathsResult.stdout || "[]"),
    repos: JSON.parse(reposResult.stdout || "[]"),
  });
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const action = String(body.action || "");
  const target = String(body.target || "path");
  const value = String(body.glob || body.repo_path || "").trim();
  if (!value || (action !== "add" && action !== "remove")) {
    return NextResponse.json(
      { error: "a path/repo value and action ('add' or 'remove') are required" },
      { status: 400 },
    );
  }

  const subcommand =
    target === "repo"
      ? action === "add"
        ? "add-repo"
        : "remove-repo"
      : action === "add"
        ? "add-path"
        : "remove-path";
  const result = await runPortunus(["leak-scan", "config", subcommand, value]);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "leak-scan config update failed") },
      { status: 422 },
    );
  }

  const [pathsResult, reposResult] = await Promise.all([
    runPortunus(["leak-scan", "config", "show", "--json"]),
    runPortunus(["leak-scan", "config", "show-repos", "--json"]),
  ]);
  return NextResponse.json({
    paths: JSON.parse(pathsResult.stdout || "[]"),
    repos: JSON.parse(reposResult.stdout || "[]"),
  });
}
