import type { ActionKind, ActionStatus, Attempt, Connector, ConnectorState, Decision, DecisionStatus, PlannerMode, ProposalValidity, RequestStatus, RunStatus, Verification, WorkStatus } from "@contracts";
import type { IconName } from "../components/Icon";

export type Tone = "neutral" | "info" | "progress" | "warn" | "danger" | "success" | "mode";
export interface StatusSpec {
  label: string;
  tone: Tone;
  icon: IconName;
}

const S = (label: string, tone: Tone, icon: IconName): StatusSpec => ({ label, tone, icon });

// Plain-language labels for a non-engineer support lead. Enum values (keys) are the contract's and never change.
export const REQUEST_STATUS = {
  open: S("New", "neutral", "circle"),
  investigating: S("Being looked into", "progress", "progress"),
  awaiting_input: S("Waiting for customer reply", "warn", "question"),
  awaiting_review: S("Waiting for your approval", "warn", "eye"),
  awaiting_work: S("Waiting on engineering", "info", "clock"),
  awaiting_verification: S("Waiting for you to confirm it's solved", "info", "shield"),
  resolved: S("Solved (confirmed)", "success", "check"),
  escalated: S("Handed off to a person", "warn", "up"),
  closed: S("Closed", "neutral", "x"),
} satisfies Record<RequestStatus, StatusSpec>;

/** An investigation is one pass of PromiseGraph looking into a request (a "run" in the API). */
export const RUN_STATUS = {
  queued: S("Starting", "neutral", "clock"),
  running: S("Looking into it", "progress", "progress"),
  waiting_for_input: S("Paused: waiting for customer reply", "warn", "question"),
  awaiting_review: S("Paused: waiting for your approval", "warn", "eye"),
  succeeded: S("Finished", "success", "check"),
  failed: S("Stopped with a problem", "danger", "alert"),
  cancelled: S("Stopped", "neutral", "ban"),
} satisfies Record<RunStatus, StatusSpec>;

export const ACTION_STATUS = {
  proposed: S("Suggested", "neutral", "circle"),
  accepted: S("Approved, in progress", "info", "clock"),
  dispatching: S("Doing it now", "progress", "progress"),
  succeeded: S("Done", "success", "check"),
  failed: S("Couldn't be done", "danger", "alert"),
  unknown: S("Unclear if it went through", "danger", "question"),
  cancelled: S("Cancelled", "neutral", "ban"),
} satisfies Record<ActionStatus, StatusSpec>;

export const VERIFICATION = {
  pending: S("Not confirmed solved yet", "neutral", "clock"),
  passed: S("Confirmed solved", "success", "check"),
  failed: S("Not solved", "danger", "x"),
  inconclusive: S("Unclear", "warn", "question"),
} satisfies Record<Verification, StatusSpec>;

export const VALIDITY = {
  valid: S("Up to date", "success", "check"),
  invalid: S("Can't be done", "danger", "ban"),
  stale: S("Out of date", "warn", "alert"),
} satisfies Record<ProposalValidity, StatusSpec>;

export const DECISION_STATUS = {
  active: S("In effect", "success", "check"),
  superseded: S("Replaced", "neutral", "refresh"),
  revoked: S("Withdrawn", "danger", "ban"),
  expired: S("Expired", "neutral", "clock"),
} satisfies Record<DecisionStatus, StatusSpec>;

export const WORK_STATUS = {
  open: S("Not started", "neutral", "circle"),
  in_progress: S("In progress", "progress", "progress"),
  blocked: S("Blocked", "danger", "ban"),
  done: S("Done", "success", "check"),
  cancelled: S("Cancelled", "neutral", "x"),
} satisfies Record<WorkStatus, StatusSpec>;

export const CONNECTOR_STATE = {
  enabled: S("On", "success", "check"),
  verified: S("Connected and checked", "success", "shield"),
  unverified: S("On, not yet checked", "warn", "question"),
  disabled: S("Off", "neutral", "ban"),
  unconfigured: S("Not set up", "neutral", "circle"),
  unavailable: S("Not working", "danger", "alert"),
} satisfies Record<ConnectorState, StatusSpec>;

export const ATTEMPT_OUTCOME = {
  succeeded: S("Worked", "success", "check"),
  failed: S("Didn't work", "danger", "x"),
  unknown: S("Unclear", "warn", "question"),
} satisfies Record<Attempt["outcome"], StatusSpec>;

/** Recommended next steps (ActionKind). */
export const KIND_LABEL = {
  answer: "Reply with an answer",
  clarify: "Ask the customer a question",
  link_work: "Attach to existing work",
  create_work: "Create a new task",
  escalate: "Hand off to a person",
} satisfies Record<ActionKind, string>;
export const KIND_EXPLAIN = {
  answer: "PromiseGraph drafts a reply for you to send. Nothing is sent automatically.",
  clarify: "The request is unclear, so PromiseGraph drafts one question for the customer. Nothing is sent automatically.",
  link_work: "Engineering is already working on this, so the request is attached to that work instead of creating a duplicate.",
  create_work: "Nothing existing covers this, so PromiseGraph prepares a new task with clear acceptance criteria.",
  escalate: "This needs a person's judgement, so it is handed to an owner.",
} satisfies Record<ActionKind, string>;

export const CONNECTOR_LABEL = { local: "PromiseGraph task list", linear: "Linear" } satisfies Record<Connector, string>;

export const RULE_LABEL = {
  forbidden_workaround: "Don't use this workaround",
  scope_reduction_allowed: "Smaller scope is allowed",
  date_extension_allowed: "Deadline extension is allowed",
  allowed_action_types: "Only these next steps are allowed",
  routing_owner: "Send requests to this person",
} satisfies Record<Decision["rule_type"], string>;

export const MODE_LABEL = { scripted: "Rule-based mode", model: "AI model" } satisfies Record<PlannerMode, string>;

export const ACTIVE_RUN = new Set(["queued", "running"]);

/** Looks up a label by a (possibly unvalidated) string key; unknown values are shown as-is, never guessed. */
export function spec(map: Readonly<Record<string, StatusSpec>>, key: string | null | undefined): StatusSpec {
  if (!key) return S("Unknown", "neutral", "question");
  return Object.hasOwn(map, key) ? (map[key] as StatusSpec) : S(key.replace(/_/g, " "), "neutral", "question");
}

/** Label lookup for string-keyed label maps (e.g. KIND_LABEL) when the key comes from the wire. */
export function labelOf(map: Readonly<Record<string, string>>, key: string | null | undefined): string {
  if (!key) return "";
  return Object.hasOwn(map, key) ? (map[key] as string) : key.replace(/_/g, " ");
}
