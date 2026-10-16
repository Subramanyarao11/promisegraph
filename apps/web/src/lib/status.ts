import type { IconName } from "../components/Icon";

export type Tone = "neutral" | "info" | "progress" | "warn" | "danger" | "success" | "mode";
export interface StatusSpec {
  label: string;
  tone: Tone;
  icon: IconName;
}

const S = (label: string, tone: Tone, icon: IconName): StatusSpec => ({ label, tone, icon });

export const REQUEST_STATUS: Record<string, StatusSpec> = {
  open: S("Open", "neutral", "circle"),
  investigating: S("Investigating", "progress", "progress"),
  awaiting_input: S("Waiting for input", "warn", "question"),
  awaiting_review: S("Waiting for review", "warn", "eye"),
  awaiting_work: S("Waiting on work", "info", "clock"),
  awaiting_verification: S("Awaiting verification", "info", "shield"),
  resolved: S("Resolved · verified", "success", "check"),
  escalated: S("Escalated", "warn", "up"),
  closed: S("Closed", "neutral", "x"),
};

export const RUN_STATUS: Record<string, StatusSpec> = {
  queued: S("Queued", "neutral", "clock"),
  running: S("Running", "progress", "progress"),
  waiting_for_input: S("Waiting for input", "warn", "question"),
  awaiting_review: S("Waiting for authorization", "warn", "eye"),
  succeeded: S("Run finished", "success", "check"),
  failed: S("Run failed", "danger", "alert"),
  cancelled: S("Cancelled", "neutral", "ban"),
};

export const ACTION_STATUS: Record<string, StatusSpec> = {
  proposed: S("Proposed", "neutral", "circle"),
  accepted: S("Authorized · queued", "info", "clock"),
  dispatching: S("Dispatching", "progress", "progress"),
  succeeded: S("Executed", "success", "check"),
  failed: S("Execution failed", "danger", "alert"),
  unknown: S("Dispatch outcome unknown", "danger", "question"),
  cancelled: S("Cancelled", "neutral", "ban"),
};

export const VERIFICATION: Record<string, StatusSpec> = {
  pending: S("Verification pending", "neutral", "clock"),
  passed: S("Verification passed", "success", "check"),
  failed: S("Verification failed", "danger", "x"),
  inconclusive: S("Inconclusive", "warn", "question"),
};

export const VALIDITY: Record<string, StatusSpec> = {
  valid: S("Valid", "success", "check"),
  invalid: S("Invalid", "danger", "ban"),
  stale: S("Stale", "warn", "alert"),
};

export const DECISION_STATUS: Record<string, StatusSpec> = {
  active: S("Active", "success", "check"),
  superseded: S("Superseded", "neutral", "refresh"),
  revoked: S("Revoked", "danger", "ban"),
  expired: S("Expired", "neutral", "clock"),
};

export const WORK_STATUS: Record<string, StatusSpec> = {
  open: S("Open", "neutral", "circle"),
  in_progress: S("In progress", "progress", "progress"),
  blocked: S("Blocked", "danger", "ban"),
  done: S("Done", "success", "check"),
  cancelled: S("Cancelled", "neutral", "x"),
};

export const CONNECTOR_STATE: Record<string, StatusSpec> = {
  enabled: S("Enabled", "success", "check"),
  verified: S("Verified", "success", "shield"),
  unverified: S("Enabled · unverified", "warn", "question"),
  disabled: S("Disabled", "neutral", "ban"),
  unconfigured: S("Not configured", "neutral", "circle"),
  unavailable: S("Unavailable", "danger", "alert"),
};

export const ATTEMPT_OUTCOME: Record<string, StatusSpec> = {
  succeeded: S("Succeeded", "success", "check"),
  failed: S("Failed", "danger", "x"),
  unknown: S("Unknown", "warn", "question"),
};

export const KIND_LABEL: Record<string, string> = {
  answer: "Answer (draft)",
  clarify: "Clarify (draft question)",
  link_work: "Link existing work",
  create_work: "Create new work",
  escalate: "Escalate",
};

export const CONNECTOR_LABEL: Record<string, string> = { local: "Local task", linear: "Linear" };

export const ACTIVE_RUN = new Set(["queued", "running"]);

export function spec(map: Record<string, StatusSpec>, key: string | null | undefined): StatusSpec {
  if (!key) return S("Unknown", "neutral", "question");
  return map[key] ?? S(key.replace(/_/g, " "), "neutral", "question");
}
