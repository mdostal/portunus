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

export interface PortunusView {
  name: string;
  description: string;
  ref_names: string[];
}

// STUB ONLY -- see roles.py. Persisted for real, but not enforced by
// check_injectable/retag yet (Petitio's future access-level engine).
export interface PortunusPolicy {
  scope_type: "org" | "project" | "env";
  scope_value: string;
  role: string;
  actions: string[];
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
