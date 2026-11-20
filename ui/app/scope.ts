// Client-safe (no node built-ins): used by API routes and components alike.

/** The same "is this reference in project X" rule as the CLI's
 * `diagnose.in_project` (src/portunus/diagnose.py): the project field, the
 * repo field, or the first segment of the group path. Keep the two in step. */
export function inProject(
  ref: { project?: string; repo?: string; group?: string },
  project: string,
): boolean {
  if (!project) return false;
  const groupRoot = (ref.group || "").replace(/^\/+|\/+$/g, "").split("/", 1)[0];
  return project === ref.project || project === ref.repo || project === groupRoot;
}

/** Wall-clock (90s/30m/2h/1d) or tick-count TTLs `portunus approve --ttl`
 * accepts. Validated before it ever reaches argv. */
export const TTL_PATTERN = /^[1-9]\d{0,5}[smhd]?$/;
