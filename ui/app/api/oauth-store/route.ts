import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { NextRequest, NextResponse } from "next/server";
import { cleanError, runPortunus, tagsToArg } from "@/lib/portunus";

// The one well-known path this route ever reads from outside PORTUNUS_HOME
// -- never an arbitrary user-supplied path (portunus-oauth-token-broker
// Story 05's own risk mitigation). GET only ever reports whether it looks
// usable; POST's own use_local_adc branch is the only place its contents
// are actually read, and even then they flow straight into `portunus oauth
// store`'s stdin -- never back to the browser, never logged.
const ADC_PATH = join(homedir(), ".config", "gcloud", "application_default_credentials.json");

async function readAdcCredential(): Promise<Record<string, string> | null> {
  try {
    const raw = await readFile(ADC_PATH, "utf8");
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      parsed.type === "authorized_user" &&
      parsed.client_id &&
      parsed.client_secret &&
      parsed.refresh_token
    ) {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

// GET ?detect=adc -- presence/shape check only, never the credential
// fields themselves. A human opts in to actually using it via POST's own
// use_local_adc flag; this never auto-fills anything silently.
export async function GET(req: NextRequest) {
  if (req.nextUrl.searchParams.get("detect") !== "adc") {
    return NextResponse.json({ error: "unsupported ?detect= value" }, { status: 400 });
  }
  const credential = await readAdcCredential();
  return NextResponse.json({ available: credential !== null });
}

// The one route that ever touches an OAuth credential bundle -- mirrors
// api/drop's own discipline exactly: the value (here, the whole bundle)
// flows to `portunus oauth store`'s stdin only, never an argv element,
// never logged, never echoed back. Chains a second `portunus drop
// --backend oauth` call to register the pointing reference with its own
// full metadata, the same two real CLI steps a human would otherwise run
// by hand -- this route only saves the manual chaining, it doesn't
// introduce a new storage/minting mechanism (Stories 01-04 already shipped
// and live-verified that).
export async function POST(req: NextRequest) {
  const body = await req.json();
  const provider = String(body.provider || "").trim();
  const account = String(body.account || "").trim();
  const name = String(body.name || "").trim();
  if (!provider || !account || !name) {
    return NextResponse.json(
      { error: "provider, account, and name are all required" },
      { status: 400 },
    );
  }

  let credential: Record<string, string>;
  if (body.use_local_adc) {
    const adc = await readAdcCredential();
    if (!adc) {
      return NextResponse.json(
        { error: "no usable gcloud ADC file found -- enter the credential manually" },
        { status: 400 },
      );
    }
    credential = {
      client_id: adc.client_id,
      client_secret: adc.client_secret,
      refresh_token: adc.refresh_token,
      token_endpoint: "https://oauth2.googleapis.com/token",
    };
  } else {
    const client_id = String(body.client_id || "").trim();
    const client_secret = String(body.client_secret || "");
    const refresh_token = String(body.refresh_token || "");
    const token_endpoint = String(body.token_endpoint || "").trim();
    if (!client_id || !client_secret || !refresh_token || !token_endpoint) {
      return NextResponse.json(
        { error: "client_id, client_secret, refresh_token, and token_endpoint are all required" },
        { status: 400 },
      );
    }
    credential = { client_id, client_secret, refresh_token, token_endpoint };
  }

  const storeResult = await runPortunus(
    ["oauth", "store", provider, account, "--stdin"],
    JSON.stringify(credential) + "\n",
  );
  if (storeResult.code !== 0) {
    return NextResponse.json(
      { error: cleanError(storeResult.stderr, "oauth store failed") },
      { status: 422 },
    );
  }

  // The reference's own "value" is never actually used once backend=oauth
  // routes it through OAuthBackend.access() instead -- a placeholder is
  // the correct, honest choice here (never a real value, never anything
  // that looks like one).
  const dropArgs = ["drop", name, `${provider}:${account}`, "--backend", "oauth", "--stdin"];
  if (body.org) dropArgs.push("--org", String(body.org));
  if (body.provider) dropArgs.push("--provider", provider);
  if (body.project) dropArgs.push("--project", String(body.project));
  if (body.env) dropArgs.push("--env", String(body.env));
  const tags = tagsToArg(body.tags);
  if (tags) dropArgs.push("--tags", tags);
  if (body.description) dropArgs.push("--description", String(body.description));
  if (body.purpose) dropArgs.push("--purpose", String(body.purpose));
  const injectedAs = tagsToArg(body.injected_as);
  if (injectedAs) dropArgs.push("--injected-as", injectedAs);
  if (body.group) dropArgs.push("--group", String(body.group));
  if (body.related) dropArgs.push("--related", String(body.related));
  if (body.repo) dropArgs.push("--repo", String(body.repo));
  if (body.source_files) dropArgs.push("--source-files", String(body.source_files));

  const dropResult = await runPortunus(dropArgs, "oauth-backed -- this placeholder is never used\n");
  if (dropResult.code !== 0) {
    return NextResponse.json(
      {
        error: cleanError(
          dropResult.stderr,
          "credential stored, but registering the reference failed -- run `portunus drop` yourself to finish",
        ),
      },
      { status: 422 },
    );
  }

  return NextResponse.json({ ok: true, message: dropResult.stdout.trim() });
}
