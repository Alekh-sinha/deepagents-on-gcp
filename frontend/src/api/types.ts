// The shapes the privai web backend returns. They mirror the engine's records; nothing here is invented by the UI.

export type AutoApprove = { kinds: string[]; max_per_turn: number } | null;

export interface Session {
  id: string;
  user_id: string;
  title: string;
  model: string | null;
  created_at: string;
  last_active_at: string;
  archived: boolean;
  schedule_id: string;
  auto_approve: AutoApprove;
  sandbox_expires_at: string | null;
}

export type TurnStatus = "running" | "awaiting_approval" | "completed" | "failed" | "cancelled" | "interrupted" | "blocked";

export interface Turn {
  id: string;
  session_id: string | null;
  status: TurnStatus;
  prompt: string;
  output: string;
  error: string;
  model: string | null;
  source: string;
  created_at: string;
  updated_at: string;
}

export type ApprovalKind = "shell" | "email" | "github" | "plan" | "question" | "interrupt" | "mcp" | "other";
export type DecisionType = "approve" | "edit" | "reject" | "respond";

/** One thing the agent wants to do (one card). `preview` depends on `kind`. */
export interface ApprovalItem {
  tool: string;
  kind: ApprovalKind;
  title: string;
  allowed_decisions: DecisionType[];
  preview: Record<string, any>;
}

export interface ActionRequest { name: string; args: Record<string, any>; description?: string; allowed_decisions?: DecisionType[]; interrupt_id?: string }

export interface Approval {
  id: string;
  run_id: string;
  status: "pending" | "approved" | "rejected" | "expired";
  expires_at: string;
  action_requests: ActionRequest[];
  previews: ApprovalItem[];
  decided_by: string | null;
}

export type Decision =
  | { type: "approve" }
  | { type: "reject"; message?: string }
  | { type: "respond"; message: string }
  | { type: "edit"; edited_action: { name: string; args: Record<string, any> } };

export interface Todo { content: string; status: "pending" | "in_progress" | "completed" }

export interface SessionView {
  session: Session;
  turns: Turn[];
  active_turn_id: string | null;
  pending_approvals: Approval[];
  todos: Todo[] | null;
}

export interface EngineEvent { seq: number; type: string; payload: Record<string, any>; ts?: string }

export interface TranscriptTurn { turn: Turn; events: EngineEvent[]; truncated: boolean; next_seq: number }
export interface Transcript { session_id: string; turns: TranscriptTurn[]; has_more: boolean }

export interface ModelInfo { id: string; provider: string; label: string; rank: number; use_for: string; location: string }
export interface ModelList { default: string; models: ModelInfo[]; helper_model_choice: boolean }

export interface AutoApproveOptions { kinds: string[]; default_kinds: string[]; never: string[]; never_tools: string[]; default_max_per_turn: number; max_per_turn_limit: number }

export interface Config {
  user: string;
  default_model: string;
  models: ModelInfo[];
  limits: Record<string, number>;
  email: Record<string, any>;
  github: Record<string, any>;
  approvals: { shell_commands: boolean; ttl_seconds: number; auto_approve: AutoApproveOptions };
  warm_sandbox: { mode: string };
  features?: { sandbox: boolean; artifacts: boolean };  // absent on an older engine = both on
}

export interface McpTool { name: string; description: string; needs_approval: boolean }
export interface McpServer {
  id: string;
  name: string;
  url: string;
  transport: "http" | "sse";
  header_names: string[];
  enabled: boolean;
  requires_approval: boolean;
  no_approval_tools: string[];
  tools: McpTool[];
  tool_count: number;
  tools_checked_at: string | null;
  last_error: string;
}
export interface McpImportResult { name: string; saved: boolean; reachable: boolean; tool_count: number; error: string; warnings: string[] }

export interface Schedule {
  id: string;
  title: string;
  instructions: string;
  cron: string;
  timezone: string;
  model: string | null;
  auto_approve: AutoApprove;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
  last_session_id: string;
  run_count: number;
}
export interface ScheduleRun { id: string; schedule_id: string; trigger: string; status: string; session_id: string; turn_id: string; note: string; created_at: string }

export interface Artifact { id: string; session_id: string; run_id: string; name: string; path: string; size: number; content_type: string; created_at: string }

export type AuthConfig = { mode: "google"; client_id: string } | { mode: "iap" } | { mode: "dev"; email?: string };
