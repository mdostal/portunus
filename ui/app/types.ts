// Client-safe types (no node built-ins) shared by API routes and components.

export interface PortunusReference {
  name: string;
  sm_name: string;
  scope: string;
  kind: string;
  state: string;
  approval: string;
  sm_path: string;
  org: string;
  provider: string;
  project: string;
  env: string;
  tags: Record<string, string>;
  description: string;
  purpose: string;
  injected_as: Record<string, string>;
  group: string;
  related: string[];
  repo: string;
  source_files: string[];
  suggested: Record<string, { value: unknown; by: string; at: string }>;
}

// One entry from `portunus crawl --json` / portunus_crawl_candidates --
// discovery context only, never a value (portunus-metadata-crawl Slice 1).
export interface CrawlCandidate {
  name: string;
  sm_name: string;
  group: string;
  project: string;
  org: string;
  repo: string;
  source_files: string[];
  provider: string;
  env: string;
  missing: {
    description: boolean;
    purpose: boolean;
    org_or_project_or_tags: boolean;
  };
  vault_binding: {
    backend: string;
    sync_mode: string;
    account: string;
    wif_audience: string;
  } | null;
  rotation_binding: { status: string; account: string } | null;
}

// portunus_leak_status's own summary shape -- severity/finding_count/
// timestamps only, never a value, never a file's content (portunus-leak-
// scan Slice 4).
export interface LeakFindingDetail {
  path: string;
  line_number: number;
  first_detected_at: number;
  last_detected_at: number;
  // portunus-leak-scan-git-awareness -- WHERE this finding came from.
  // source_kind is a soft, named heuristic for "log"/"local" (getting it
  // wrong is cosmetic, never a security gap); repo_visibility is only
  // ever "public"/"private" when actually resolved via `gh repo view` --
  // "unknown" otherwise, never a guess.
  source_kind: "log" | "local" | "git-history";
  repo_path: string | null;
  repo_visibility: "public" | "private" | "unknown" | null;
}

export interface LeakSummary {
  ref_name: string;
  severity: "warn" | "urgent" | "critical" | null;
  finding_count: number;
  first_detected_at: number | null;
  last_detected_at: number | null;
  // Only present when fetched with ?name=/--detail (portunus-leak-visibility
  // Story 01) -- distinct_files is the "leaked in N conversations" headline
  // number (unique file paths, not raw finding_count, which can double-count
  // one secret matching many lines of the same transcript).
  distinct_files?: number;
  findings?: LeakFindingDetail[];
}

export interface PortunusView {
  name: string;
  description: string;
  ref_names: string[];
}

// STUB ONLY -- see roles.py. Persisted for real, but not enforced by
// check_injectable/retag yet (Petitio's future access-level engine).
export interface PortunusPolicy {
  scope_type: "org" | "project" | "env" | "repo";
  scope_value: string;
  role: string;
  actions: string[];
  // "" / "*" = applies to everyone. Audit-only as of portunus-petitio-rbac
  // Story 02 -- feeds a would-allow/would-deny audit line, never enforced
  // (raised on) yet; see roles.py's own module docstring.
  principal: string;
}

export interface AuditEntry {
  seq: number;
  actor: string;
  task: string;
  action: string;
  secret: string;
  result: string;
}

export interface AddSecretDraft {
  name: string;
  sm_name: string;
  kind: string;
  scope: string;
  backend: string;
  org: string;
  provider: string;
  project: string;
  env: string;
  tags: string;
  description: string;
  purpose: string;
  injected_as: string;
  group: string;
  related: string;
  repo: string;
  source_files: string;
}
