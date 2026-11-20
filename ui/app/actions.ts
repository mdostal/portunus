// Client-safe write helpers for the gate / approval / state controls
// (GH #257). Each POST goes through ui/proxy.ts like every other write;
// instrumentation-client.ts adds the write credential.
import type { ApprovalStatus } from "./types";

async function postJson(url: string, body: unknown): Promise<string> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${url} failed (${res.status})`);
  return String(data.message || "ok");
}

export const setGate = (name: string, on: boolean) => postJson("/api/gate", { name, on });
export const approve = (name: string, ttl: string) => postJson("/api/approve", { name, ttl });
export const setState = (name: string, state: string) => postJson("/api/state", { name, state });

/** The approval durations offered in the UI. Wall-clock first: a human
 * approving from a browser thinks in minutes, not audit ticks. */
export const TTL_CHOICES: { value: string; label: string }[] = [
  { value: "15m", label: "15 minutes" },
  { value: "1h", label: "1 hour" },
  { value: "2h", label: "2 hours" },
  { value: "8h", label: "8 hours" },
  { value: "1d", label: "1 day" },
  { value: "1", label: "next access only" },
  { value: "3", label: "next 3 accesses" },
];

/** "approved · 1h 12m left" / "approved · 2 accesses left" / "" */
export function describeApproval(s: ApprovalStatus | undefined, now = Date.now() / 1000): string {
  if (!s || !s.approved) return "";
  if (s.expires_at !== null) {
    const left = Math.max(0, Math.round(s.expires_at - now));
    const h = Math.floor(left / 3600);
    const m = Math.floor((left % 3600) / 60);
    const until = new Date(s.expires_at * 1000).toLocaleString();
    return `approved until ${until} (${h ? `${h}h ` : ""}${m}m left)`;
  }
  const n = s.ticks_left ?? 0;
  return `approved for ${n} more access${n === 1 ? "" : "es"}`;
}

export async function fetchApprovals(project: string): Promise<Record<string, ApprovalStatus>> {
  const qs = project ? `?project=${encodeURIComponent(project)}` : "";
  const res = await fetch(`/api/approvals${qs}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "failed to load approvals");
  const map: Record<string, ApprovalStatus> = {};
  for (const s of data as ApprovalStatus[]) map[s.name] = s;
  return map;
}
