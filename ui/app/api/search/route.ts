import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Metadata-only search -- thin wrapper around `portunus search <query> --json`.
// Never returns a value: search_references() has no path to a backend at all.
export async function GET(req: NextRequest) {
  const { searchParams } = req.nextUrl;
  const query = searchParams.get("query");
  if (!query) {
    return NextResponse.json({ error: "query is required" }, { status: 400 });
  }

  const args = ["search", query, "--json"];
  const project = searchParams.get("project");
  const provider = searchParams.get("provider");
  const env = searchParams.get("env");
  const state = searchParams.get("state");
  if (project) args.push("--project", project);
  if (provider) args.push("--provider", provider);
  if (env) args.push("--env", env);
  if (state) args.push("--state", state);

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "search failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "[]"));
}
