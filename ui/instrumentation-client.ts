// Sends the write credential (ui/lib/write-auth.ts) on the UI's own writes.
//
// Every non-GET /api/* call must carry X-Portunus-Write-Token. Rather than
// thread it through every component, this wraps window.fetch once, before the
// app becomes interactive (Next's instrumentation-client convention), and adds
// the header to same-origin /api/* writes only.
//
// Where the browser gets the token:
//   - a `#write-token=<token>` URL fragment, which the desktop app's sidecar
//     launcher navigates to. A fragment never reaches the server or its logs,
//     and it is stripped from the address bar as soon as it is read.
//   - otherwise, the first write answered 401 opens a small password dialog
//     and the write is retried once with what the human typed.
// It is kept in sessionStorage: this tab only, gone when the tab closes.
//
// A 503 (no credential configured on the server) is passed through untouched;
// the route's own error message says how to configure it.

const STORAGE_KEY = "portunus.writeToken";
const HEADER = "X-Portunus-Write-Token";
const SAFE_METHODS = new Set(["GET", "HEAD"]);

function readStored(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function store(token: string) {
  try {
    if (token) sessionStorage.setItem(STORAGE_KEY, token);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage blocked: the token then lives only for this page load.
  }
  memoryToken = token;
}

let memoryToken = "";

function currentToken(): string {
  return memoryToken || readStored();
}

function takeTokenFromFragment() {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return;
  const params = new URLSearchParams(hash);
  const token = params.get("write-token");
  if (token === null) return;
  store(token.trim());
  params.delete("write-token");
  const rest = params.toString();
  const url = window.location.pathname + window.location.search + (rest ? `#${rest}` : "");
  window.history.replaceState(window.history.state, "", url);
}

let pendingPrompt: Promise<string> | null = null;

// A native <dialog> with a password input, so the token is never echoed on
// screen the way window.prompt() would show it.
function askForToken(message: string): Promise<string> {
  if (pendingPrompt) return pendingPrompt;
  pendingPrompt = new Promise<string>((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "Portunus write credential");
    dialog.style.cssText = "max-width:28rem;padding:1.25rem;border-radius:8px;border:1px solid #888;font:inherit";
    const form = document.createElement("form");
    form.method = "dialog";
    const text = document.createElement("p");
    text.textContent = message;
    text.style.marginTop = "0";
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.name = "write-token";
    input.placeholder = "write token";
    input.style.cssText = "width:100%;box-sizing:border-box;margin-bottom:0.75rem";
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:0.5rem;justify-content:flex-end";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.type = "submit";
    ok.textContent = "Unlock writes";
    row.append(cancel, ok);
    form.append(text, input, row);
    dialog.append(form);
    document.body.append(dialog);

    let answer = "";
    form.addEventListener("submit", () => {
      answer = input.value.trim();
    });
    cancel.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      dialog.remove();
      pendingPrompt = null;
      resolve(answer);
    });
    dialog.showModal();
    input.focus();
  });
  return pendingPrompt;
}

function isApiWrite(input: RequestInfo | URL, init?: RequestInit): boolean {
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  if (SAFE_METHODS.has(method)) return false;
  const raw = input instanceof Request ? input.url : String(input);
  let url: URL;
  try {
    url = new URL(raw, window.location.href);
  } catch {
    return false;
  }
  return url.origin === window.location.origin && url.pathname.startsWith("/api/");
}

function withToken(init: RequestInit | undefined, token: string): RequestInit {
  const headers = new Headers(init?.headers);
  if (token) headers.set(HEADER, token);
  return { ...init, headers };
}

async function needsToken(res: Response): Promise<boolean> {
  if (res.status !== 401) return false;
  try {
    const body = await res.clone().json();
    return body?.code === "write_token_required";
  } catch {
    return false;
  }
}

function install() {
  takeTokenFromFragment();
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!isApiWrite(input, init)) return original(input, init);
    // A Request body can be read once; the UI always passes a string body in
    // init, which can be resent as is.
    const replayable = !(input instanceof Request);
    const sent = currentToken();
    const res = await original(input, withToken(init, sent));
    if (!replayable || !(await needsToken(res))) return res;

    const message = sent
      ? "That write credential was rejected. Enter the current Portunus write token:"
      : "This Portunus UI requires its write token to change the vault. Enter it to continue:";
    if (sent) store("");
    const typed = await askForToken(message);
    if (!typed) return res;
    store(typed);
    const retried = await original(input, withToken(init, typed));
    if (await needsToken(retried)) store("");
    return retried;
  };
}

if (typeof window !== "undefined") {
  try {
    install();
  } catch {
    // Never break the app over this: writes then get the server's 401 body.
  }
}
