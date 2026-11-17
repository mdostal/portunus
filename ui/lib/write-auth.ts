import { createHash, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

// The write credential (ui/proxy.ts). Every non-GET /api/* request must carry
// it in X-Portunus-Write-Token. The proxy's Origin/Host/Content-Type checks
// only stop a hostile *browser page*; anything that can open a TCP connection
// to the UI (another container reaching the host's loopback port, another
// local user, a script) sends whatever headers it likes. The credential is
// what stops those.
//
// The expected value lives in a file named by PORTUNUS_WRITE_TOKEN_FILE (a
// compose/Kubernetes secret mount, or a 0600 file you made). The file is read
// on every write, so replacing its content rotates the credential without a
// restart.
//
// Four states, decided per request from the environment:
//   token        file set and holds a usable token  -> writes need it (401 without)
//   unavailable  file set but unreadable/empty/short -> every write refused (503)
//   unconfigured no file, no opt-out                 -> every write refused (503)
//   open         no file, PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES=1
//                                                    -> writes need nothing (the
//                                                       pre-credential behaviour,
//                                                       for a single-user machine)
// A configured file always wins over the opt-out.

export const WRITE_TOKEN_HEADER = "x-portunus-write-token";
export const WRITE_TOKEN_FILE_ENV = "PORTUNUS_WRITE_TOKEN_FILE";
export const OPEN_WRITES_ENV = "PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES";
// 16 characters is the floor; `openssl rand -hex 32` (64) is what the docs mint.
export const MIN_TOKEN_LENGTH = 16;

export type WriteAuth =
  | { mode: "token"; token: string }
  | { mode: "unavailable"; reason: string }
  | { mode: "unconfigured" }
  | { mode: "open" };

export type WriteDecision =
  | { ok: true }
  | { ok: false; status: 401 | 503; code: string; error: string };

type Env = Record<string, string | undefined>;

export function loadWriteAuth(env: Env = process.env): WriteAuth {
  const file = (env[WRITE_TOKEN_FILE_ENV] ?? "").trim();
  if (file) {
    let raw: string;
    try {
      raw = readFileSync(file, "utf8");
    } catch (err) {
      // The error code only, never the path's content.
      const code = (err as NodeJS.ErrnoException).code ?? "read error";
      return { mode: "unavailable", reason: `${WRITE_TOKEN_FILE_ENV} could not be read (${code})` };
    }
    const token = raw.trim();
    if (token.length < MIN_TOKEN_LENGTH) {
      return {
        mode: "unavailable",
        reason: `${WRITE_TOKEN_FILE_ENV} holds no token of at least ${MIN_TOKEN_LENGTH} characters`,
      };
    }
    return { mode: "token", token };
  }
  if (env[OPEN_WRITES_ENV] === "1") return { mode: "open" };
  return { mode: "unconfigured" };
}

// Constant-time: both sides are hashed first, so the comparison runs over two
// equal-length digests and neither the content nor the length of the expected
// token shows in the timing.
export function tokensMatch(presented: string, expected: string): boolean {
  const a = createHash("sha256").update(presented, "utf8").digest();
  const b = createHash("sha256").update(expected, "utf8").digest();
  return timingSafeEqual(a, b);
}

export function checkWrite(auth: WriteAuth, presented: string | null): WriteDecision {
  switch (auth.mode) {
    case "open":
      return { ok: true };
    case "unconfigured":
      return {
        ok: false,
        status: 503,
        code: "write_token_unconfigured",
        error:
          `writes are disabled: no write credential is configured. Set ${WRITE_TOKEN_FILE_ENV} ` +
          `to a file holding a token, or ${OPEN_WRITES_ENV}=1 on a single-user machine`,
      };
    case "unavailable":
      return {
        ok: false,
        status: 503,
        code: "write_token_unavailable",
        error: `writes are disabled: ${auth.reason}`,
      };
    case "token":
      if (presented && tokensMatch(presented, auth.token)) return { ok: true };
      return {
        ok: false,
        status: 401,
        code: "write_token_required",
        error: presented ? "write credential rejected" : "write credential required (X-Portunus-Write-Token)",
      };
  }
}

// One line for /api/health and the startup log. Never includes the token.
export function describeWriteAuth(auth: WriteAuth): { ok: boolean; detail: string } {
  switch (auth.mode) {
    case "token":
      return { ok: true, detail: `writes require X-Portunus-Write-Token (${WRITE_TOKEN_FILE_ENV})` };
    case "open":
      return {
        ok: true,
        detail: `OPEN: writes need no credential (${OPEN_WRITES_ENV}=1); anything that reaches this port can change the vault`,
      };
    case "unconfigured":
      return {
        ok: false,
        detail: `writes refused: set ${WRITE_TOKEN_FILE_ENV}, or ${OPEN_WRITES_ENV}=1 on a single-user machine`,
      };
    case "unavailable":
      return { ok: false, detail: `writes refused: ${auth.reason}` };
  }
}
