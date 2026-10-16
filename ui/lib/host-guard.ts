// The DNS-rebinding half of ui/proxy.ts's write guard, shared with
// /api/health so the health result reflects whether a write sent with the
// same Host would get through. proxy.ts skips GET/HEAD, so without this a
// deployment reached over a non-loopback Host (container-to-container by
// service name, say) reports healthy while every write is 403'd.

const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]"]);

function hostnameOf(host: string): string {
  try {
    return new URL(`http://${host}`).hostname;
  } catch {
    return "";
  }
}

// True when a mutating request carrying this Host header passes the
// rebinding check: either the Host is a loopback name, or the operator
// explicitly rebound the UI via PORTUNUS_UI_HOST.
export function writeHostAllowed(host: string): boolean {
  return !!process.env.PORTUNUS_UI_HOST || LOOPBACK_HOSTNAMES.has(hostnameOf(host));
}
