import { useState } from "react";
import type { Action, EvidenceRef, Proposal, ProposalCommit, ProposalCommitResult, Request, WorkItem, WorkspaceInfo } from "@contracts";
import { useCommand, keys, useWorkItem } from "../api/hooks";
import { ReconcileForm } from "./Forms";
import { EntityName } from "./Names";
import { ApiError } from "../api/client";
import { Icon } from "./Icon";
import { EvidenceChip } from "./Evidence";
import { Badge, Callout, Dialog, ErrorPanel, Mono, Owner, StatusBadge, Tag, Ts, useAnnounce } from "./ui";
import { ACTION_STATUS, CONNECTOR_LABEL, CONNECTOR_STATE, KIND_LABEL, VALIDITY, VERIFICATION, WORK_STATUS, spec } from "../lib/status";

interface Props {
  request: Request;
  proposal: Proposal | null;
  action: Action | null;
  refs: Map<string, EvidenceRef>;
  work: WorkItem[];
  info: WorkspaceInfo | undefined;
  knowledgeRevision: number | null;
  runStatus: string | null;
  onReplan: () => void;
  replanPending: boolean;
}

/** What the server says the destination is; never guessed for kinds the server routes itself. */
function targetOf(p: Proposal, action: Action | null, work: WorkItem[]): { label: string; connector: string | null; note?: string } {
  if (p.kind === "create_work" || (action && action.kind === "create_work")) {
    const c = action?.connector ?? (p.kind === "create_work" ? p.payload.connector : "local");
    return { label: c === "linear" ? "A new issue in Linear" : "A new task in PromiseGraph's task list", connector: c };
  }
  if (p.kind === "link_work") {
    const w = work.find((x) => x.id === p.payload.work_item_id);
    return { label: "A link between this request and the existing work, saved in PromiseGraph", connector: action?.connector ?? "local", note: w?.connector === "linear" ? "Nothing is posted to Linear; comments stay drafts." : undefined };
  }
  if (p.kind === "answer" || p.kind === "clarify") return { label: "A saved draft in PromiseGraph. Nothing is sent to the customer", connector: action?.connector ?? null };
  return { label: "A hand-off record in PromiseGraph", connector: action?.connector ?? null };
}

function Payload({ p, work }: { p: Proposal; work: WorkItem[] }) {
  switch (p.kind) {
    case "answer":
      return (
        <div className="stack">
          <div>
            <div className="draft-label"><Icon name="doc" size={12} /> Draft reply: not sent. Copy it into your helpdesk when you’re ready</div>
            <div className="draft" data-testid="draft-text">{p.payload.draft_text}</div>
          </div>
          <Criteria title="It’s solved when" items={p.payload.resolution_criteria as string[]} />
        </div>
      );
    case "clarify":
      return (
        <div className="stack">
          <div>
            <div className="draft-label"><Icon name="question" size={12} /> Draft question for the customer: not sent</div>
            <div className="draft" data-testid="draft-question">{p.payload.question}</div>
          </div>
          {p.payload.missing_fields.length > 0 && (
            <div className="row"><span className="label">We need to know:</span>{(p.payload.missing_fields as string[]).map((f) => <Tag key={f} tone="warn">{f}</Tag>)}</div>
          )}
        </div>
      );
    case "link_work": {
      return <LinkWorkPayload p={p} work={work} />;
    }
    case "create_work":
      return (
        <div className="stack">
          <dl className="kv">
            <dt>Task</dt><dd><strong>{p.payload.title}</strong></dd>
            <dt>Owner</dt><dd data-testid="proposal-owner">{p.payload.owner_id ? <Owner id={p.payload.owner_id} /> : <Tag tone="warn" icon="alert">No owner yet: someone will need to pick this up</Tag>}</dd>
            <dt>Product area</dt><dd>{p.payload.capability_id ? <EntityName type="capability" id={p.payload.capability_id} /> : <span className="muted">Not specified</span>}</dd>
            <dt>Depends on</dt><dd>{p.payload.dependency_ids.length ? <span className="stack" style={{ gap: 2 }}>{p.payload.dependency_ids.map((d) => <EntityName key={d} type="work_item" id={d} />)}</span> : <span className="muted">Nothing else</span>}</dd>
          </dl>
          <p className="small" style={{ whiteSpace: "pre-wrap" }}>{p.payload.description}</p>
          <Criteria title="Done when" items={p.payload.acceptance_criteria} testId="acceptance-criteria" />
        </div>
      );
    case "escalate":
      return (
        <dl className="kv">
          <dt>Why</dt><dd>{p.payload.reason}</dd>
          <dt>Handed to</dt><dd><Owner id={p.payload.owner_id} /></dd>
          <dt>Team</dt><dd>{p.payload.team_id ? <span>{p.payload.team_id.replace(/^team_/, "").replace(/_/g, " ")}</span> : <span className="muted">Not specified</span>}</dd>
        </dl>
      );
  }
}

function LinkWorkPayload({ p, work }: { p: Extract<Proposal, { kind: "link_work" }>; work: WorkItem[] }) {
  const local = work.find((x) => x.id === p.payload.work_item_id);
  const fetched = useWorkItem(local ? null : p.payload.work_item_id);
  const w = local ?? fetched.data?.data;
  return (
    <div className="stack">
      <dl className="kv">
        <dt>Existing work</dt>
        <dd>
          {w ? <strong>{w.title}</strong> : <Mono>{p.payload.work_item_id}</Mono>}{" "}
          {w && <StatusBadge map={WORK_STATUS} value={w.status} />}
        </dd>
        {w && <><dt>Owner</dt><dd><Owner id={w.owner_id} /></dd></>}
        {w && <><dt>Expected</dt><dd>{w.forecast_at ? <Ts iso={w.forecast_at} /> : <span className="muted">No date we can stand behind, so don’t promise one</span>}</dd></>}
      </dl>
      {p.payload.comment_text && (
        <div>
          <div className="draft-label"><Icon name="doc" size={12} /> Draft comment: not posted</div>
          <div className="draft">{p.payload.comment_text}</div>
        </div>
      )}
    </div>
  );
}

function Criteria({ title, items, testId }: { title: string; items: string[]; testId?: string }) {
  if (!items.length) return <p className="small muted">{title}: not stated.</p>;
  return (
    <div data-testid={testId}>
      <span className="label">{title}</span>
      <ul className="criteria">
        {items.map((c, i) => (
          <li key={i}><Icon name="circle" size={12} /> <span>{c}</span></li>
        ))}
      </ul>
    </div>
  );
}

export function nextStep(request: Request, action: Action | null, proposal: Proposal | null): string {
  switch (request.status) {
    case "resolved": return "Solved and confirmed. If the customer says it’s back, record that below and PromiseGraph will look into it again.";
    case "awaiting_input": return "We’re waiting for the customer to answer the question. When they reply, paste their answer below and PromiseGraph picks up where it left off.";
    case "awaiting_review":
      if (!proposal) return "This is waiting for your approval, but there’s no suggestion to approve yet. Click “Look into it again” to get one.";
      return proposal.validity === "valid" ? "Review the suggestion above. If it looks right, click “Approve and do it”." : "The suggestion can’t be used any more. Click “Look into it again” for a fresh one.";
    case "awaiting_work": return "Engineering has the work. Creating or attaching a task doesn’t mean the customer’s problem is solved: once the work is done, check with the customer and confirm it below.";
    case "awaiting_verification": return "Check with the customer (or test it yourself), then confirm below whether it’s solved. A drafted reply on its own doesn’t count.";
    case "investigating": return action?.status === "unknown" ? "We’re not sure the last step went through. PromiseGraph is checking, and won’t do it twice." : "PromiseGraph is looking into it. This page updates by itself.";
    case "escalated": return "A person now owns this. Record what they decide below.";
    case "closed": return "This request was closed. It’s not marked as solved.";
    default: return "Click “Look into it” to have PromiseGraph investigate.";
  }
}

export function ActionArea({ request, proposal, action, refs, work, info, knowledgeRevision, runStatus, onReplan, replanPending }: Props) {
  const announce = useAnnounce();
  const [confirm, setConfirm] = useState(false);
  const commit = useCommand<ProposalCommit & { _pid: string }, ProposalCommitResult>(
    "post",
    (b) => `/proposals/${encodeURIComponent(b._pid)}/commit`,
    (b) => [keys.proposal(b._pid), ["actions"], keys.request(request.id), ["run"]],
  );

  if (!proposal) {
    return (
      <div className="stack" data-testid="no-proposal">
        <p className="muted">Nothing to approve yet. When PromiseGraph has a suggestion, it appears here.</p>
      </div>
    );
  }

  const target = targetOf(proposal, action, work);
  const linearState = info?.connectors.linear;
  const linearUnavailable = target.connector === "linear" && linearState !== undefined && !["enabled", "verified", "unverified"].includes(linearState);
  const committed = Boolean(action);
  const revisionMoved = knowledgeRevision !== null && knowledgeRevision > proposal.based_on_revision;
  const canCommit = proposal.validity === "valid" && !committed && !linearUnavailable && runStatus !== "cancelled";
  let blockedReason: string | null = null;
  if (committed) blockedReason = null;
  else if (proposal.validity === "stale") blockedReason = "Some facts changed after this was suggested (for example an owner or a task’s status), so it’s no longer safe to do. Get a fresh suggestion.";
  else if (proposal.validity === "invalid") blockedReason = "This suggestion breaks a company rule or points at something that no longer exists, so it can’t be done.";
  else if (linearUnavailable) blockedReason = `Linear isn’t connected (${spec(CONNECTOR_STATE, linearState).label.toLowerCase()}), so this can’t be created there.`;
  else if (runStatus === "cancelled") blockedReason = "The investigation was stopped. Look into it again to continue.";

  const doCommit = () => {
    const body = { _pid: proposal.id, expected_revision: knowledgeRevision ?? proposal.based_on_revision, authorization_id: proposal.authorization_id };
    commit.mutate(body, {
      onSuccess: (r) => {
        setConfirm(false);
        announce(`Approved. ${spec(ACTION_STATUS, r.data.action.status).label}.`);
      },
      onError: (e) => {
        announce(`Couldn’t do it: ${(e as Error).message}${e instanceof ApiError ? ` (${e.code})` : ""}`, true);
      },
    });
  };

  return (
    <div className="stack-lg" data-testid="action-area">
      <div className="stack">
        <div className="row">
          <strong style={{ fontSize: 14 }} data-testid="proposal-kind">{KIND_LABEL[proposal.kind]}</strong>
          <StatusBadge map={VALIDITY} value={proposal.validity} testId="proposal-validity" />
        </div>
        <p className="small muted">Suggested <Ts iso={proposal.created_at} /></p>
        {revisionMoved && proposal.validity === "valid" && !committed && (
          <p className="small" style={{ color: "var(--warn-fg)" }}><Icon name="info" size={12} /> Some information changed since this was suggested. PromiseGraph re-checks everything before doing it.</p>
        )}
        {proposal.validation_reasons.length > 0 && (
          <details className="small" data-testid="validation-details" open={proposal.validity !== "valid"}>
            <summary>Why it was flagged</summary>
            <ul data-testid="validation-reasons">{proposal.validation_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </details>
        )}
      </div>

      <Payload p={proposal} work={work} />

      <dl className="kv">
        <dt>What it will do</dt>
        <dd data-testid="execution-target">
          <span className="row">
            <Tag icon={target.connector === "linear" ? "external" : "work"} tone={target.connector === "linear" ? "info" : "neutral"}>{target.label}</Tag>
            {target.connector === "linear" && linearState && <StatusBadge map={CONNECTOR_STATE} value={linearState} prefix="Linear" />}
          </span>
          {target.note && <span className="small muted">{target.note}</span>}
        </dd>
        <dt>Based on</dt>
        <dd className="row">{proposal.evidence_ids.length ? proposal.evidence_ids.map((id) => <EvidenceChip key={id} id={id} refs={refs} />) : <Tag tone="warn" icon="alert">No sources cited</Tag>}</dd>
        <dt>Approval</dt>
        <dd>{proposal.authorization_id || committed ? <Tag tone="success" icon="lock">Approved</Tag> : <span className="muted">Not approved yet</span>}</dd>
      </dl>

      {blockedReason && (
        <Callout tone={proposal.validity === "stale" ? "warn" : "danger"} icon={proposal.validity === "stale" ? "alert" : "ban"} title={proposal.validity === "stale" ? "Out of date: facts changed since this was suggested" : "This can’t be done"} testId="commit-blocked">
          <p>{blockedReason}</p>
          <button type="button" className="btn btn-sm" onClick={onReplan} disabled={replanPending}><Icon name="refresh" /> Get a fresh suggestion</button>
        </Callout>
      )}

      {!committed && (
        <div className="row">
          <button type="button" className="btn btn-primary btn-lg" disabled={!canCommit} aria-disabled={!canCommit} onClick={() => setConfirm(true)} data-testid="authorize">
            <Icon name="check" /> Approve and do it
          </button>
          {!canCommit && <span className="small muted">Not available. See the note above.</span>}
        </div>
      )}

      {action && <Receipt action={action} runStatus={runStatus} />}

      <Dialog open={confirm} onClose={() => setConfirm(false)} title="Approve this action?" testId="authorize-dialog">
        <p>
          PromiseGraph will <strong>{KIND_LABEL[proposal.kind].toLowerCase()}</strong>. Result: <strong>{target.label}</strong>.
        </p>
        <ul className="small">
          <li>It double-checks the facts first. If anything changed, it stops instead of doing something out of date.</li>
          {(proposal.kind === "answer" || proposal.kind === "clarify") && <li>This only saves a draft. Nothing is sent to the customer.</li>}
          {(proposal.kind === "create_work" || proposal.kind === "link_work") && <li>This doesn’t solve the customer’s problem by itself. You’ll confirm that separately once it’s done.</li>}
        </ul>
        {commit.error && <ErrorPanel error={commit.error} />}
        <div className="form-actions">
          <button type="button" className="btn" onClick={() => setConfirm(false)}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={doCommit} disabled={commit.isPending || !canCommit} data-testid="confirm-authorize">
            {commit.isPending ? "Approving…" : "Approve and do it"}
          </button>
        </div>
      </Dialog>
    </div>
  );
}

const RESULT_LABEL: Record<string, string> = {
  answer: "Draft reply saved (not sent)",
  clarify: "Draft question saved (not sent)",
  link_work: "Request attached to the existing work",
  create_work: "New task created",
  escalate: "Handed off",
};

export function Receipt({ action, runStatus = null }: { action: Action; runStatus?: string | null }) {
  const autoReconciling = runStatus === "queued" || runStatus === "running";
  const r = action.receipt;
  return (
    <div className="card" data-testid="receipt">
      <div className="row">
        <StatusBadge map={ACTION_STATUS} value={action.status} testId="action-status" />
        <StatusBadge map={VERIFICATION} value={action.verification} />
      </div>
      {action.status === "unknown" && (
        <Callout tone="danger" icon="question" title="We’re not sure this went through" testId="dispatch-unknown">
          <p>The connection timed out, so the task may or may not have been created. PromiseGraph checks before doing anything, and nothing is created again automatically. If it can’t tell, it asks you.</p>
        </Callout>
      )}
      {action.status === "unknown" && autoReconciling && (
        <p className="small muted" data-testid="auto-reconciling"><Icon name="progress" size={12} /> Checking whether it went through…</p>
      )}
      {action.status === "unknown" && !autoReconciling && <ReconcileForm action={action} />}
      {action.error && (
        <p className="small" style={{ color: "var(--danger-fg)" }}><Icon name="alert" size={12} /> {action.error.message} <span className="muted">({action.error.code})</span></p>
      )}
      {r ? (
        <div className="stack">
          <p className="done-title"><Icon name="check" /> Done. Here’s the record</p>
          <dl className="kv small">
            <dt>Result</dt><dd>{RESULT_LABEL[action.kind] ?? action.kind}</dd>
            <dt>Saved in</dt>
            <dd className="row">
              <Badge s={{ label: CONNECTOR_LABEL[r.connector] ?? r.connector, tone: r.connector === "linear" ? "info" : "neutral", icon: r.connector === "linear" ? "external" : "work" }} testId="receipt-connector" />
              <span className="muted">record <Mono>{r.target_id}</Mono></span>
              {r.target_url && <a href={r.target_url} target="_blank" rel="noreferrer">Open <Icon name="external" size={12} /><span className="sr-only">(opens in new tab)</span></a>}
            </dd>
            <dt>When</dt><dd><Ts iso={r.observed_at} /></dd>
          </dl>
          <details className="small">
            <summary>Technical record</summary>
            <dl className="kv small">
              <dt>Action</dt><dd><Mono>{action.id}</Mono> · version {action.version}</dd>
              <dt>Receipt</dt><dd><Mono>{r.id}</Mono></dd>
              <dt>Content check</dt><dd><Mono>{r.result_hash.slice(0, 16)}…</Mono></dd>
            </dl>
          </details>
        </div>
      ) : (
        <p className="small muted">{action.status === "accepted" || action.status === "dispatching" ? "Working on it…" : "No record yet."}</p>
      )}
    </div>
  );
}
