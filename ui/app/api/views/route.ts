import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus } from "@/lib/portunus";

// Thin shell-out to `portunus views ...` -- same pattern every other route
// uses. Views only ever hold reference NAMES, never a value -- there is no
// code path in this route that could return one.
export async function GET(req: NextRequest) {
  const name = req.nextUrl.searchParams.get("name");
  const args = name ? ["views", "show", name, "--json"] : ["views", "show", "--json"];
  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "views show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(result.stdout || "{}"));
}

export async function POST(req: NextRequest) {
  const body = await req.json();
  const action = String(body.action || "");
  const name = String(body.name || "").trim();
  if (!name) {
    return NextResponse.json({ error: "name is required" }, { status: 400 });
  }

  let args: string[];
  if (action === "create") {
    args = ["views", "create", name];
    if (body.description) args.push("--description", String(body.description));
  } else if (action === "add") {
    const refName = String(body.ref_name || "").trim();
    if (!refName) return NextResponse.json({ error: "ref_name is required" }, { status: 400 });
    args = ["views", "add", name, refName];
  } else if (action === "remove") {
    const refName = String(body.ref_name || "").trim();
    if (!refName) return NextResponse.json({ error: "ref_name is required" }, { status: 400 });
    args = ["views", "remove", name, refName];
  } else if (action === "delete") {
    args = ["views", "delete", name];
  } else {
    return NextResponse.json(
      { error: "action must be one of create/add/remove/delete" },
      { status: 400 },
    );
  }

  const result = await runPortunus(args);
  if (result.code !== 0) {
    return NextResponse.json(
      { error: cleanError(result.stderr, "views action failed") },
      { status: 422 },
    );
  }

  const showResult = await runPortunus(["views", "show", "--json"]);
  if (showResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(showResult.stderr, "views show failed") },
      { status: 502 },
    );
  }
  return NextResponse.json(JSON.parse(showResult.stdout || "{}"));
}
