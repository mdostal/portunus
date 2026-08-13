import { spawn } from "node:child_process";

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface PortunusReference {
  name: string;
  sm_name: string;
  scope: string;
  kind: string;
  state: string;
  approval: string;
  sm_path: string;
  provider: string;
  project: string;
  env: string;
  tags: Record<string, string>;
  description: string;
  purpose: string;
  injected_as: Record<string, string>;
  group: string;
  related: string[];
}

export interface AuditEntry {
  seq: number;
  actor: string;
  task: string;
  action: string;
  secret: string;
  result: string;
  prev: string;
  h: string;
}

/**
 * Spawn the `portunus` console script -- the SAME gated binary the CLI
 * uses, never a reimplementation. This is the whole reason the UI has no
 * second privileged path: every write still goes through Broker.
 * check_injectable inside the Python process, exactly as it would from a
 * terminal.
 *
 * Value handling: a secret value, when one is involved (only the drop
 * route ever supplies one), MUST be passed as `stdin` -- never as an argv
 * element. This function never logs, returns, or otherwise surfaces
 * `stdin`'s content; only `args` (which never contains a value) appears in
 * any error this throws.
 */
export function runPortunus(args: string[], stdin?: string): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn("portunus", args, {
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    child.on("error", (err) => {
      // Process-spawn error only (e.g. ENOENT) -- never touches `stdin`.
      reject(new Error(`failed to spawn portunus ${args.join(" ")}: ${err.message}`));
    });

    child.on("close", (code) => {
      resolve({ code: code ?? -1, stdout, stderr });
    });

    if (stdin !== undefined) {
      child.stdin.write(stdin);
    }
    child.stdin.end();
  });
}

export function tagsToArg(tags: Record<string, string> | string | undefined): string {
  if (!tags) return "";
  if (typeof tags === "string") return tags;
  return Object.entries(tags)
    .filter(([, v]) => v !== "" && v !== undefined && v !== null)
    .map(([k, v]) => `${k}=${v}`)
    .join(",");
}

/** Strip the CLI's "portunus: " error prefix for cleaner UI-facing messages. */
export function cleanError(stderr: string, fallback: string): string {
  const msg = stderr.trim().replace(/^portunus:\s*/i, "");
  return msg || fallback;
}
