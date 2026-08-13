// Client-safe types (no node built-ins) shared by API routes and components.

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
  provider: string;
  project: string;
  env: string;
  tags: string;
}
