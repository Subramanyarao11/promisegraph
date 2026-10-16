/* Generated from contracts/bundle.schema.json (contract 1.0.0). Do not edit. */

/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RequestStatus".
 */
export type RequestStatus =
  | "open"
  | "investigating"
  | "awaiting_input"
  | "awaiting_review"
  | "awaiting_work"
  | "awaiting_verification"
  | "resolved"
  | "escalated"
  | "closed";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RunStatus".
 */
export type RunStatus =
  "queued" | "running" | "waiting_for_input" | "awaiting_review" | "succeeded" | "failed" | "cancelled";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RunStage".
 */
export type RunStage = "investigate" | "decide" | "prepare" | "act" | "observe" | "verify" | "remember";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "PlannerMode".
 */
export type PlannerMode = "scripted" | "model";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ActionKind".
 */
export type ActionKind = "answer" | "clarify" | "link_work" | "create_work" | "escalate";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ActionStatus".
 */
export type ActionStatus = "proposed" | "accepted" | "dispatching" | "succeeded" | "failed" | "unknown" | "cancelled";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Verification".
 */
export type Verification = "pending" | "passed" | "failed" | "inconclusive";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "DecisionStatus".
 */
export type DecisionStatus = "active" | "superseded" | "revoked" | "expired";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ProposalValidity".
 */
export type ProposalValidity = "valid" | "invalid" | "stale";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Connector".
 */
export type Connector = "local" | "linear";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "JobKind".
 */
export type JobKind = "investigate" | "dispatch" | "reconcile" | "refresh";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "JobCompletion".
 */
export type JobCompletion = "succeeded" | "waiting" | "retryable_failure" | "failed" | "unknown";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ClaimKind".
 */
export type ClaimKind = "source_assertion" | "derived" | "inference" | "unknown";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "EventType".
 */
export type EventType =
  | "run_started"
  | "tool_started"
  | "tool_completed"
  | "proposal_ready"
  | "action_updated"
  | "waiting_for_input"
  | "observation_recorded"
  | "run_finished"
  | "error";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "WorkStatus".
 */
export type WorkStatus = "open" | "in_progress" | "blocked" | "done" | "cancelled";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Origin".
 */
export type Origin = "synthetic" | "public" | "owned_permitted";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Proposal".
 */
export type Proposal =
  | {
      id: string;
      workspace_id: string;
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      validity: ProposalValidity;
      /**
       * @maxItems 30
       */
      validation_reasons: string[];
      authorization_id: string | null;
      created_at: string;
      version: number;
      kind: "answer";
      payload: {
        draft_text: string;
        /**
         * @maxItems 20
         */
        resolution_criteria:
          | []
          | [string]
          | [string, string]
          | [string, string, string]
          | [string, string, string, string]
          | [string, string, string, string, string]
          | [string, string, string, string, string, string]
          | [string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string, string, string, string]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ];
      };
    }
  | {
      id: string;
      workspace_id: string;
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      validity: ProposalValidity;
      /**
       * @maxItems 30
       */
      validation_reasons: string[];
      authorization_id: string | null;
      created_at: string;
      version: number;
      kind: "clarify";
      payload: {
        question: string;
        /**
         * @maxItems 20
         */
        missing_fields:
          | []
          | [string]
          | [string, string]
          | [string, string, string]
          | [string, string, string, string]
          | [string, string, string, string, string]
          | [string, string, string, string, string, string]
          | [string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string, string, string]
          | [string, string, string, string, string, string, string, string, string, string, string, string, string]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ]
          | [
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string,
              string
            ];
      };
    }
  | {
      id: string;
      workspace_id: string;
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      validity: ProposalValidity;
      /**
       * @maxItems 30
       */
      validation_reasons: string[];
      authorization_id: string | null;
      created_at: string;
      version: number;
      kind: "link_work";
      payload: {
        work_item_id: string;
        comment_text: string | null;
      };
    }
  | {
      id: string;
      workspace_id: string;
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      validity: ProposalValidity;
      /**
       * @maxItems 30
       */
      validation_reasons: string[];
      authorization_id: string | null;
      created_at: string;
      version: number;
      kind: "create_work";
      payload: {
        title: string;
        description: string;
        /**
         * @maxItems 30
         */
        acceptance_criteria: string[];
        owner_id: string | null;
        capability_id: string | null;
        /**
         * @maxItems 100
         */
        dependency_ids: string[];
        connector: Connector;
      };
    }
  | {
      id: string;
      workspace_id: string;
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      validity: ProposalValidity;
      /**
       * @maxItems 30
       */
      validation_reasons: string[];
      authorization_id: string | null;
      created_at: string;
      version: number;
      kind: "escalate";
      payload: {
        reason: string;
        owner_id: string | null;
        team_id: string | null;
      };
    };
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RelationType".
 */
export type RelationType =
  | "SUBMITTED"
  | "ABOUT"
  | "IMPLEMENTED_BY"
  | "TRACKED_BY"
  | "DEPENDS_ON"
  | "OWNED_BY"
  | "RELATED_TO"
  | "SUPPORTED_BY"
  | "APPLIES_TO"
  | "SUPERSEDES"
  | "ATTEMPTED_FOR"
  | "OBSERVED_AS"
  | "VERIFIES";
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ProposalCreate".
 */
export type ProposalCreate =
  | {
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      kind: "answer";
      payload: PayloadAnswer;
    }
  | {
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      kind: "clarify";
      payload: PayloadClarify;
    }
  | {
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      kind: "link_work";
      payload: PayloadLinkWork;
    }
  | {
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      kind: "create_work";
      payload: PayloadCreateWork;
    }
  | {
      request_id: string;
      run_id: string;
      /**
       * @maxItems 100
       */
      evidence_ids: string[];
      explanation: string;
      /**
       * @maxItems 5
       */
      rejected_alternatives:
        | []
        | [Alternative]
        | [Alternative, Alternative]
        | [Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative]
        | [Alternative, Alternative, Alternative, Alternative, Alternative];
      based_on_revision: number;
      kind: "escalate";
      payload: PayloadEscalate;
    };
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ConnectorState".
 */
export type ConnectorState = "enabled" | "disabled" | "unconfigured" | "unverified" | "verified" | "unavailable";

export interface PromiseGraphContracts {}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Meta".
 */
export interface Meta {
  contract_version: "1.0.0";
  knowledge_revision: number;
  correlation_id: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Error".
 */
export interface Error {
  code: string;
  message: string;
  retryable: boolean;
  details: {};
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Customer".
 */
export interface Customer {
  id: string;
  workspace_id: string;
  name: string;
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Request".
 */
export interface Request {
  id: string;
  workspace_id: string;
  customer_id: string;
  subject: string;
  body: string;
  channel: "manual" | "import" | "connector";
  status: RequestStatus;
  owner_id: string | null;
  source_id: string;
  /**
   * @maxItems 100
   */
  capability_ids: string[];
  /**
   * @maxItems 100
   */
  linked_work_ids: string[];
  created_at: string;
  updated_at: string;
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "WorkItem".
 */
export interface WorkItem {
  id: string;
  workspace_id: string;
  title: string;
  kind: "feature" | "ticket" | "milestone";
  status: WorkStatus;
  owner_id: string | null;
  connector: Connector;
  external_id: string | null;
  external_url: string | null;
  forecast_at: string | null;
  observed_at: string;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "EvidenceRef".
 */
export interface EvidenceRef {
  id: string;
  source_id: string;
  source_revision: number;
  origin: Origin;
  excerpt: string;
  location: string;
  observed_at: string;
  ingested_at: string;
  stale: boolean;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "SourceRecord".
 */
export interface SourceRecord {
  id: string;
  workspace_id: string;
  revision: number;
  title: string;
  kind: "documentation" | "request" | "work" | "decision" | "observation";
  origin: Origin;
  body: string;
  sha256: string;
  observed_at: string;
  ingested_at: string;
  supersedes_revision: number | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "GraphNode".
 */
export interface GraphNode {
  id: string;
  label: string;
  title: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "GraphEdge".
 */
export interface GraphEdge {
  id: string;
  type: string;
  from_id: string;
  to_id: string;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  confirmed: boolean;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "EvidencePath".
 */
export interface EvidencePath {
  id: string;
  /**
   * @maxItems 20
   */
  nodes:
    | []
    | [GraphNode]
    | [GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode]
    | [GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode, GraphNode]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ]
    | [
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode,
        GraphNode
      ];
  /**
   * @maxItems 19
   */
  edges:
    | []
    | [GraphEdge]
    | [GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge, GraphEdge]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ]
    | [
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge,
        GraphEdge
      ];
  claim_kind: ClaimKind;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Scope".
 */
export interface Scope {
  entity_type: "customer" | "request" | "capability" | "work_item";
  entity_id: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Decision".
 */
export interface Decision {
  id: string;
  workspace_id: string;
  rule_type:
    | "forbidden_workaround"
    | "scope_reduction_allowed"
    | "date_extension_allowed"
    | "allowed_action_types"
    | "routing_owner";
  value:
    | string
    | boolean
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
  scope: Scope;
  status: DecisionStatus;
  rationale: string;
  author_id: string;
  effective_at: string;
  expires_at: string | null;
  supersedes_id: string | null;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Attempt".
 */
export interface Attempt {
  id: string;
  workspace_id: string;
  request_id: string;
  procedure_key: string;
  context_signature: string;
  scope: Scope;
  outcome: "succeeded" | "failed" | "unknown";
  verification: Verification;
  summary: string;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  observed_at: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ContextBundle".
 */
export interface ContextBundle {
  request_id: string;
  based_on_revision: number;
  /**
   * @maxItems 100
   */
  sources: EvidenceRef[];
  /**
   * @maxItems 20
   */
  paths:
    | []
    | [EvidencePath]
    | [EvidencePath, EvidencePath]
    | [EvidencePath, EvidencePath, EvidencePath]
    | [EvidencePath, EvidencePath, EvidencePath, EvidencePath]
    | [EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath]
    | [EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath]
    | [EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath]
    | [EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath, EvidencePath]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ]
    | [
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath,
        EvidencePath
      ];
  /**
   * @maxItems 30
   */
  related_work: WorkItem[];
  /**
   * @maxItems 30
   */
  decisions: Decision[];
  /**
   * @maxItems 30
   */
  attempts: Attempt[];
  /**
   * @maxItems 30
   */
  missing_information: string[];
  truncated: boolean;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Alternative".
 */
export interface Alternative {
  kind: ActionKind;
  reason: string;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Receipt".
 */
export interface Receipt {
  id: string;
  connector: Connector;
  target_id: string;
  target_url: string | null;
  observed_at: string;
  result_hash: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Action".
 */
export interface Action {
  id: string;
  workspace_id: string;
  request_id: string;
  run_id: string;
  proposal_id: string;
  kind: ActionKind;
  status: ActionStatus;
  verification: Verification;
  idempotency_key: string;
  connector: Connector;
  receipt: Receipt | null;
  error: Error | null;
  created_at: string;
  updated_at: string;
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Observation".
 */
export interface Observation {
  id: string;
  workspace_id: string;
  request_id: string;
  action_id: string | null;
  kind: "operator_confirmation" | "customer_confirmation" | "acceptance_check" | "connector_status";
  verification: Verification;
  body: string;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  actor_id: string;
  observed_at: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Budget".
 */
export interface Budget {
  max_tool_calls: number;
  max_replans: number;
  max_active_seconds: number;
  extensions_used: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Usage".
 */
export interface Usage {
  tool_calls: number;
  replans: number;
  active_ms: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Run".
 */
export interface Run {
  id: string;
  workspace_id: string;
  request_id: string;
  mode: PlannerMode;
  status: RunStatus;
  stage: RunStage;
  based_on_revision: number;
  event_seq: number;
  budget: Budget;
  usage: Usage;
  error: Error | null;
  created_at: string;
  updated_at: string;
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RunEvent".
 */
export interface RunEvent {
  id: string;
  run_id: string;
  seq: number;
  type: EventType;
  occurred_at: string;
  step_id: string | null;
  tool_name: string | null;
  payload: {};
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "JobLease".
 */
export interface JobLease {
  job_id: string;
  kind: JobKind;
  run_id: string | null;
  action_id: string | null;
  attempt: number;
  lease_token: string;
  fencing_token: number;
  expires_at: string;
  checkpoint: {} | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RequestCreate".
 */
export interface RequestCreate {
  customer_id: string;
  subject: string;
  body: string;
  channel: "manual" | "import" | "connector";
  /**
   * @maxItems 100
   */
  capability_ids: string[];
  external_ref: string | null;
  auto_start: boolean;
  planner_mode: PlannerMode;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RunCreate".
 */
export interface RunCreate {
  mode: PlannerMode;
  instruction: string;
  authorization_id: string | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RunCancel".
 */
export interface RunCancel {
  reason: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RunResume".
 */
export interface RunResume {
  expected_run_version: number;
  reason: string;
  additional_tool_calls: number;
  additional_active_seconds: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ReplyCreate".
 */
export interface ReplyCreate {
  body: string;
  external_ref: string | null;
  expected_request_version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ContextSearch".
 */
export interface ContextSearch {
  query: string;
  /**
   * @minItems 1
   * @maxItems 4
   */
  entity_types:
    | ["request" | "capability" | "work_item" | "source"]
    | ["request" | "capability" | "work_item" | "source", "request" | "capability" | "work_item" | "source"]
    | [
        "request" | "capability" | "work_item" | "source",
        "request" | "capability" | "work_item" | "source",
        "request" | "capability" | "work_item" | "source"
      ]
    | [
        "request" | "capability" | "work_item" | "source",
        "request" | "capability" | "work_item" | "source",
        "request" | "capability" | "work_item" | "source",
        "request" | "capability" | "work_item" | "source"
      ];
  customer_id: string | null;
  limit: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ContextExpand".
 */
export interface ContextExpand {
  /**
   * @minItems 1
   * @maxItems 10
   */
  entity_ids:
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string];
  /**
   * @minItems 1
   * @maxItems 8
   */
  relation_types:
    | [RelationType]
    | [RelationType, RelationType]
    | [RelationType, RelationType, RelationType]
    | [RelationType, RelationType, RelationType, RelationType]
    | [RelationType, RelationType, RelationType, RelationType, RelationType]
    | [RelationType, RelationType, RelationType, RelationType, RelationType, RelationType]
    | [RelationType, RelationType, RelationType, RelationType, RelationType, RelationType, RelationType]
    | [RelationType, RelationType, RelationType, RelationType, RelationType, RelationType, RelationType, RelationType];
  depth: number;
  limit: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Payload_answer".
 */
export interface PayloadAnswer {
  draft_text: string;
  /**
   * @maxItems 20
   */
  resolution_criteria:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Payload_clarify".
 */
export interface PayloadClarify {
  question: string;
  /**
   * @maxItems 20
   */
  missing_fields:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Payload_link_work".
 */
export interface PayloadLinkWork {
  work_item_id: string;
  comment_text: string | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Payload_create_work".
 */
export interface PayloadCreateWork {
  title: string;
  description: string;
  /**
   * @maxItems 30
   */
  acceptance_criteria: string[];
  owner_id: string | null;
  capability_id: string | null;
  /**
   * @maxItems 100
   */
  dependency_ids: string[];
  connector: Connector;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "Payload_escalate".
 */
export interface PayloadEscalate {
  reason: string;
  owner_id: string | null;
  team_id: string | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ProposalCommit".
 */
export interface ProposalCommit {
  expected_revision: number;
  authorization_id: string | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ObservationCreate".
 */
export interface ObservationCreate {
  action_id: string | null;
  kind: "operator_confirmation" | "customer_confirmation" | "acceptance_check" | "connector_status";
  verification: Verification;
  body: string;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  expected_request_version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RefreshCreate".
 */
export interface RefreshCreate {
  reason: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "WorkItemUpdate".
 */
export interface WorkItemUpdate {
  expected_version: number;
  status: WorkStatus;
  owner_id: string | null;
  forecast_at: string | null;
  reason: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "DecisionCommand".
 */
export interface DecisionCommand {
  operation: "record" | "supersede" | "revoke";
  decision_id: string | null;
  rule_type:
    | "forbidden_workaround"
    | "scope_reduction_allowed"
    | "date_extension_allowed"
    | "allowed_action_types"
    | "routing_owner";
  value:
    | string
    | boolean
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
  scope: Scope;
  rationale: string;
  effective_at: string;
  expires_at: string | null;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  expected_revision: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportPreview".
 */
export interface ImportPreview {
  format: "markdown" | "json" | "csv";
  origin: Origin;
  content: string;
  source_key: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportCommit".
 */
export interface ImportCommit {
  expected_revision: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ActionReconcile".
 */
export interface ActionReconcile {
  outcome: "succeeded" | "failed";
  target: {
    target_id: string;
    target_url: string | null;
  } | null;
  /**
   * @maxItems 100
   */
  evidence_ids: string[];
  expected_action_version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RequestClose".
 */
export interface RequestClose {
  reason: string;
  expected_request_version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "WorkspaceInfo".
 */
export interface WorkspaceInfo {
  workspace_id: string;
  name: string;
  clock_mode: "scenario" | "live";
  reference_time: string;
  planners: {
    scripted: "enabled" | "disabled" | "unconfigured" | "unverified" | "verified" | "unavailable";
    model: "enabled" | "disabled" | "unconfigured" | "unverified" | "verified" | "unavailable";
  };
  connectors: {
    local: "enabled" | "disabled" | "unconfigured" | "unverified" | "verified" | "unavailable";
    linear: "enabled" | "disabled" | "unconfigured" | "unverified" | "verified" | "unavailable";
  };
  /**
   * @maxItems 5
   */
  auto_commit_kinds:
    | []
    | [ActionKind]
    | [ActionKind, ActionKind]
    | [ActionKind, ActionKind, ActionKind]
    | [ActionKind, ActionKind, ActionKind, ActionKind]
    | [ActionKind, ActionKind, ActionKind, ActionKind, ActionKind];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "CapabilitySummary".
 */
export interface CapabilitySummary {
  id: string;
  name: string;
  /**
   * @maxItems 50
   */
  aliases: string[];
  status: "active" | "beta" | "deprecated" | "planned";
  product_version: string | null;
  owner_id: string | null;
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RequestCreateResult".
 */
export interface RequestCreateResult {
  request: Request;
  run: Run | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ReplyResult".
 */
export interface ReplyResult {
  source_id: string;
  source_revision: number;
  run: Run;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "SearchCandidate".
 */
export interface SearchCandidate {
  entity_type: "request" | "capability" | "work_item" | "source";
  id: string;
  title: string;
  score: number;
  /**
   * @maxItems 10
   */
  evidence:
    | []
    | [EvidenceRef]
    | [EvidenceRef, EvidenceRef]
    | [EvidenceRef, EvidenceRef, EvidenceRef]
    | [EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef]
    | [EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef]
    | [EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef]
    | [EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef]
    | [EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef, EvidenceRef]
    | [
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef
      ]
    | [
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef,
        EvidenceRef
      ];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ContextSearchResult".
 */
export interface ContextSearchResult {
  /**
   * @maxItems 20
   */
  candidates:
    | []
    | [SearchCandidate]
    | [SearchCandidate, SearchCandidate]
    | [SearchCandidate, SearchCandidate, SearchCandidate]
    | [SearchCandidate, SearchCandidate, SearchCandidate, SearchCandidate]
    | [SearchCandidate, SearchCandidate, SearchCandidate, SearchCandidate, SearchCandidate]
    | [SearchCandidate, SearchCandidate, SearchCandidate, SearchCandidate, SearchCandidate, SearchCandidate]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ]
    | [
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate,
        SearchCandidate
      ];
  truncated: boolean;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ContextExpandResult".
 */
export interface ContextExpandResult {
  /**
   * @maxItems 100
   */
  paths: EvidencePath[];
  /**
   * @maxItems 200
   */
  nodes: GraphNode[];
  truncated: boolean;
  visited_count: number;
  /**
   * @maxItems 500
   */
  edges: GraphEdge[];
  truncation: ("limit" | "depth") | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "PersonSummary".
 */
export interface PersonSummary {
  id: string;
  name: string;
  role: string;
  team_id: string | null;
  active: boolean;
  version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ProposalCommitResult".
 */
export interface ProposalCommitResult {
  action: Action;
  job_id: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ObservationResult".
 */
export interface ObservationResult {
  observation: Observation;
  request: Request;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RefreshResult".
 */
export interface RefreshResult {
  job_id: string;
  coalesced: boolean;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "WorkItemUpdateResult".
 */
export interface WorkItemUpdateResult {
  work_item: WorkItem;
  knowledge_revision: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "SourceSummary".
 */
export interface SourceSummary {
  id: string;
  revision: number;
  title: string;
  kind: "documentation" | "request" | "work" | "decision" | "observation";
  origin: Origin;
  sha256: string;
  observed_at: string;
  ingested_at: string;
  stale: boolean;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportChange".
 */
export interface ImportChange {
  entity_type: string;
  entity_id: string;
  change: "create" | "update" | "noop";
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportRejection".
 */
export interface ImportRejection {
  location: string;
  reason: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportPreviewResult".
 */
export interface ImportPreviewResult {
  preview_id: string;
  valid_count: number;
  rejected_count: number;
  /**
   * @maxItems 1000
   */
  changes: ImportChange[];
  /**
   * @maxItems 1000
   */
  rejected: ImportRejection[];
  based_on_revision: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "SourceRevisionRef".
 */
export interface SourceRevisionRef {
  source_id: string;
  revision: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportCommitResult".
 */
export interface ImportCommitResult {
  imported_count: number;
  rejected_count: number;
  noop_count: number;
  /**
   * @maxItems 1000
   */
  source_revisions: SourceRevisionRef[];
  invalidated_proposal_count: number;
  knowledge_revision: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "EventsPage".
 */
export interface EventsPage {
  /**
   * @maxItems 100
   */
  data: RunEvent[];
  next_after_seq: number;
  has_more: boolean;
  meta: Meta;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ErrorEnvelope".
 */
export interface ErrorEnvelope {
  error: Error;
  meta: Meta;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "JobClaim".
 */
export interface JobClaim {
  worker_id: string;
  /**
   * @minItems 1
   * @maxItems 4
   */
  kinds: [JobKind] | [JobKind, JobKind] | [JobKind, JobKind, JobKind] | [JobKind, JobKind, JobKind, JobKind];
  lease_seconds: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "LeaseBinding".
 */
export interface LeaseBinding {
  lease_token: string;
  fencing_token: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "NewRunEvent".
 */
export interface NewRunEvent {
  type: EventType;
  tool_name: string | null;
  payload: {};
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "JobCheckpoint".
 */
export interface JobCheckpoint {
  lease_token: string;
  fencing_token: number;
  step_id: string;
  expected_event_seq: number;
  event: NewRunEvent;
  checkpoint: {};
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "CheckpointResult".
 */
export interface CheckpointResult {
  event: RunEvent;
  next_event_seq: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "JobComplete".
 */
export interface JobComplete {
  lease_token: string;
  fencing_token: number;
  completion_id: string;
  outcome: JobCompletion;
  result: {};
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "CompletionReceipt".
 */
export interface CompletionReceipt {
  job_id: string;
  completion_id: string;
  outcome: JobCompletion;
  recorded_at: string;
  run: Run | null;
  next_job_id: string | null;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "RemotePrecondition".
 */
export interface RemotePrecondition {
  target_id: string;
  provider_version: string | null;
  observed_at: string;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "BeginDispatch".
 */
export interface BeginDispatch {
  job_id: string;
  lease_token: string;
  fencing_token: number;
  expected_action_version: number;
  expected_revision: number;
  /**
   * @maxItems 20
   */
  remote_preconditions:
    | []
    | [RemotePrecondition]
    | [RemotePrecondition, RemotePrecondition]
    | [RemotePrecondition, RemotePrecondition, RemotePrecondition]
    | [RemotePrecondition, RemotePrecondition, RemotePrecondition, RemotePrecondition]
    | [RemotePrecondition, RemotePrecondition, RemotePrecondition, RemotePrecondition, RemotePrecondition]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ]
    | [
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition,
        RemotePrecondition
      ];
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "DispatchPermit".
 */
export interface DispatchPermit {
  permit_id: string;
  expires_at: string;
  payload_hash: string;
  action_version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ExecuteLocal".
 */
export interface ExecuteLocal {
  job_id: string;
  lease_token: string;
  fencing_token: number;
  expected_action_version: number;
}
/**
 * This interface was referenced by `PromiseGraphContracts`'s JSON-Schema
 * via the `definition` "ImportDocument".
 */
export interface ImportDocument {
  workspace_id: string;
  observed_at: string;
  /**
   * @maxItems 1000
   */
  customers?: {
    id: string;
    name: string;
    plan: string | null;
    /**
     * @maxItems 20
     */
    constraints:
      | []
      | [string]
      | [string, string]
      | [string, string, string]
      | [string, string, string, string]
      | [string, string, string, string, string]
      | [string, string, string, string, string, string]
      | [string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ];
  }[];
  /**
   * @maxItems 1000
   */
  teams?: {
    id: string;
    name: string;
  }[];
  /**
   * @maxItems 1000
   */
  people?: {
    id: string;
    name: string;
    role: string;
    team_id: string | null;
    active: boolean;
  }[];
  /**
   * @maxItems 1000
   */
  capabilities?: {
    id: string;
    name: string;
    /**
     * @maxItems 50
     */
    aliases: string[];
    status: "active" | "beta" | "deprecated" | "planned";
    product_version: string | null;
    owner_id: string | null;
    /**
     * @maxItems 100
     */
    customer_ids: string[];
  }[];
  /**
   * @maxItems 1000
   */
  work_items?: {
    id: string;
    title: string;
    description: string;
    kind: "feature" | "ticket" | "milestone";
    status: WorkStatus;
    owner_id: string | null;
    connector: Connector;
    external_id: string | null;
    external_url: string | null;
    forecast_at: string | null;
    observed_at: string;
    /**
     * @maxItems 100
     */
    capability_ids: string[];
    /**
     * @maxItems 100
     */
    depends_on: string[];
    /**
     * @maxItems 30
     */
    acceptance_criteria: string[];
  }[];
  /**
   * @maxItems 1000
   */
  requests?: {
    id: string;
    customer_id: string;
    subject: string;
    body: string;
    status: RequestStatus;
    owner_id: string | null;
    /**
     * @maxItems 100
     */
    capability_ids: string[];
    /**
     * @maxItems 100
     */
    linked_work_ids: string[];
    /**
     * @maxItems 100
     */
    related_request_ids: string[];
    created_at: string;
  }[];
  /**
   * @maxItems 1000
   */
  decisions?: {
    id: string;
    rule_type:
      | "forbidden_workaround"
      | "scope_reduction_allowed"
      | "date_extension_allowed"
      | "allowed_action_types"
      | "routing_owner";
    value:
      | string
      | boolean
      | []
      | [string]
      | [string, string]
      | [string, string, string]
      | [string, string, string, string]
      | [string, string, string, string, string]
      | [string, string, string, string, string, string]
      | [string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ]
      | [
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string,
          string
        ];
    scope: Scope;
    status: DecisionStatus;
    rationale: string;
    author_id: string;
    effective_at: string;
    expires_at: string | null;
    supersedes_id: string | null;
  }[];
  /**
   * @maxItems 1000
   */
  attempts?: {
    id: string;
    request_id: string;
    work_item_id: string | null;
    procedure_key: string;
    context_signature: string;
    scope: Scope;
    outcome: "succeeded" | "failed" | "unknown";
    verification: Verification;
    summary: string;
    observed_at: string;
  }[];
}
