import { useState } from "react";
import type { Action, EvidenceRef, Proposal, ProposalCommit, ProposalCommitResult, Request, WorkItem, WorkspaceInfo } from "@contracts";
import { useCommand, keys, useWorkItem } from "../api/hooks";
import { ReconcileForm } from "./Forms";
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
  if (action) return { label: CONNECTOR_LABEL[action.connector] ?? action.connector, connector: action.connector };
  if (p.kind === "create_work") return { label: CONNECTOR_LABEL[p.payload.connector] ?? p.payload.connector, connector: p.payload.connector };
  if (p.kind === "link_work") {
    const w = work.find((x) => x.id === p.payload.work_item_id);
    if (w) return { label: `Link record on ${CONNECTOR_LABEL[w.connector] ?? w.connector} work item`, connector: w.connector, note: "Connector comments stay drafts in this release." };
    return { label: "Determined by the server at commit", connector: null };
  }
  if (p.kind === "answer" || p.kind === "clarify") return { label: "Internal draft record (nothing is sent)", connector: null };
  return { label: "Determined by the server at commit", connector: null };
}

function Payload({ p, work }: { p: Proposal; work: WorkItem[] }) {
  switch (p.kind) {
    case "answer":
      return (
        <div className="stack">
          <div>
            <div className="draft-label"><Icon name="doc" size={12} /> Draft answer — not sent to the customer</div>
            <div className="draft" data-testid="draft-text">{p.payload.draft_text}</div>
          </div>
          <Criteria title="Resolution criteria" items={p.payload.resolution_criteria as string[]} />
        </div>
      );
    case "clarify":
      return (
        <div className="stack">
          <div>
            <div className="draft-label"><Icon name="question" size={12} /> Draft clarification question — not sent</div>
            <div className="draft" data-testid="draft-question">{p.payload.question}</div>
          </div>
          {p.payload.missing_fields.length > 0 && (
            <div className="row"><span className="label">Missing:</span>{(p.payload.missing_fields as string[]).map((f) => <Tag key={f} tone="warn">{f}</Tag>)}</div>
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
            <dt>Title</dt><dd><strong>{p.payload.title}</strong></dd>
            <dt>Owner</dt><dd data-testid="proposal-owner">{p.payload.owner_id ? <Owner id={p.payload.owner_id} /> : <Tag tone="warn" icon="alert">Missing owner — assign before or after creation</Tag>}</dd>
            <dt>Capability</dt><dd>{p.payload.capability_id ? <Mono>{p.payload.capability_id}</Mono> : <span className="muted">None linked</span>}</dd>
            <dt>Depends on</dt><dd>{p.payload.dependency_ids.length ? p.payload.dependency_ids.map((d) => <Mono key={d}>{d}</Mono>) : <span className="muted">None</span>}</dd>
          </dl>
          <p className="small" style={{ whiteSpace: "pre-wrap" }}>{p.payload.description}</p>
          <Criteria title="Acceptance criteria" items={p.payload.acceptance_criteria} testId="acceptance-criteria" />
        </div>
      );
    case "escalate":
      return (
        <dl className="kv">
          <dt>Reason</dt><dd>{p.payload.reason}</dd>
          <dt>Owner</dt><dd>{p.payload.owner_id ? <Mono>{p.payload.owner_id}</Mono> : <Tag tone="warn" icon="alert">Missing owner</Tag>}</dd>
          <dt>Team</dt><dd>{p.payload.team_id ? <Mono>{p.payload.team_id}</Mono> : <span className="muted">None</span>}</dd>
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
        <dt>Work item</dt>
        <dd>
          {w ? <strong>{w.title}</strong> : null} <Mono>{p.payload.work_item_id}</Mono>{" "}
          {w && <StatusBadge map={WORK_STATUS} value={w.status} />}
        </dd>
        {w && <><dt>Owner</dt><dd>{w.owner_id ? <Mono>{w.owner_id}</Mono> : <Tag tone="warn" icon="alert">Missing owner</Tag>}</dd></>}
        {w && <><dt>Forecast</dt><dd>{w.forecast_at ? <Ts iso={w.forecast_at} /> : <span className="muted">No supported forecast</span>}</dd></>}
      </dl>
      {p.payload.comment_text && (
        <div>
          <div className="draft-label"><Icon name="doc" size={12} /> Draft comment — not posted</div>
          <div className="draft">{p.payload.comment_text}</div>
        </div>
      )}
    </div>
  );
}

function Criteria({ title, items, testId }: { title: string; items: string[]; testId?: string }) {
  if (!items.length) return <p className="small muted">{title}: none stated.</p>;
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
    case "resolved": return "Resolved with an attributable passed verification. Record a new observation if the customer reports a regression.";
    case "awaiting_input": return "Waiting for the customer's reply. Record it below to resume the same run.";
    case "awaiting_review": if (!proposal) return "Marked as waiting for review, but no proposal is recorded for it. Start a run to prepare one.";
      return proposal.validity === "valid" ? "Review the proposal and authorize it, or start a new run to replan." : "The proposal cannot be committed. Start a new run to replan with current knowledge.";
    case "awaiting_work": return "Waiting on linked or created work. Creating or linking work does not resolve the request — once the work is done, record a verification against the resolution criteria.";
    case "awaiting_verification": return "Verify the outcome with the customer or an acceptance check, then record it below. A drafted answer alone is not a resolution.";
    case "investigating": return action?.status === "unknown" ? "Dispatch outcome unknown — wait for reconciliation; do not create the work again." : "Investigation in progress. The timeline updates as durable events arrive.";
    case "escalated": return "Escalated for internal review. Record the reviewer's outcome as an observation.";
    case "closed": return "Closed by an operator. Reopening requires a new observation or explicit action.";
    default: return "Start an investigation run.";
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
        <p className="muted">No proposal has been prepared for this request yet.</p>
        <div className="next-step"><Icon name="info" /><p>{nextStep(request, action, null)}</p></div>
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
  else if (proposal.validity === "stale") blockedReason = "Stale: knowledge used by this proposal changed. It cannot be executed; replan to get a revised proposal.";
  else if (proposal.validity === "invalid") blockedReason = "Invalid: the server rejected this proposal. It cannot be executed.";
  else if (linearUnavailable) blockedReason = `Linear connector is ${spec(CONNECTOR_STATE, linearState).label.toLowerCase()}; this cannot be dispatched to Linear.`;
  else if (runStatus === "cancelled") blockedReason = "The run was cancelled. Start a new run to continue.";

  const doCommit = () => {
    const body = { _pid: proposal.id, expected_revision: knowledgeRevision ?? proposal.based_on_revision, authorization_id: proposal.authorization_id };
    commit.mutate(body, {
      onSuccess: (r) => {
        setConfirm(false);
        announce(`Authorized. Action ${r.data.action.id} is ${spec(ACTION_STATUS, r.data.action.status).label.toLowerCase()}.`);
      },
      onError: (e) => {
        announce(`Commit refused: ${e instanceof ApiError ? e.code : ""} ${(e as Error).message}`, true);
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
        <p className="small muted">
          Proposal <Mono>{proposal.id}</Mono> · based on knowledge rev {proposal.based_on_revision}
          {knowledgeRevision !== null && <> · current rev {knowledgeRevision}</>} · <Ts iso={proposal.created_at} />
        </p>
        {revisionMoved && proposal.validity === "valid" && !committed && (
          <p className="small" style={{ color: "var(--warn-fg)" }}><Icon name="info" size={12} /> Knowledge has changed since this was prepared. The server revalidates at commit and again before dispatch.</p>
        )}
        {proposal.validation_reasons.length > 0 && (
          <ul className="small" data-testid="validation-reasons">{proposal.validation_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        )}
      </div>

      <Payload p={proposal} work={work} />

      <dl className="kv">
        <dt>Execution target</dt>
        <dd data-testid="execution-target">
          <span className="row">
            <Tag icon={target.connector === "linear" ? "external" : "work"} tone={target.connector === "linear" ? "info" : "neutral"}>{target.label}</Tag>
            {target.connector === "linear" && linearState && <StatusBadge map={CONNECTOR_STATE} value={linearState} prefix="Linear" />}
          </span>
          {target.note && <span className="small muted">{target.note}</span>}
        </dd>
        <dt>Evidence</dt>
        <dd className="row">{proposal.evidence_ids.length ? proposal.evidence_ids.map((id) => <EvidenceChip key={id} id={id} refs={refs} />) : <Tag tone="warn" icon="alert">No evidence cited</Tag>}</dd>
        <dt>Authorization</dt>
        <dd>{proposal.authorization_id ? <span className="row"><Tag tone="success" icon="lock">Authorized</Tag><Mono>{proposal.authorization_id}</Mono></span> : committed ? <Tag tone="success" icon="lock">Authorized by commit</Tag> : <span className="muted">Not yet authorized</span>}</dd>
      </dl>

      {blockedReason && (
        <Callout tone={proposal.validity === "stale" ? "warn" : "danger"} icon={proposal.validity === "stale" ? "alert" : "ban"} title={proposal.validity === "stale" ? "Stale proposal — execution blocked" : "Execution blocked"} testId="commit-blocked">
          <p>{blockedReason}</p>
          <button type="button" className="btn btn-sm" onClick={onReplan} disabled={replanPending}><Icon name="refresh" /> Replan with a new run</button>
        </Callout>
      )}

      {!committed && (
        <div className="row">
          <button type="button" className="btn btn-primary" disabled={!canCommit} aria-disabled={!canCommit} onClick={() => setConfirm(true)} data-testid="authorize">
            <Icon name="lock" /> Review &amp; authorize
          </button>
          {!canCommit && <span className="small muted">Commit unavailable</span>}
        </div>
      )}

      {action && <Receipt action={action} runStatus={runStatus} />}

      <div className="next-step" data-testid="next-step"><Icon name="info" /><p><strong>Next: </strong>{nextStep(request, action, proposal)}</p></div>

      <Dialog open={confirm} onClose={() => setConfirm(false)} title="Authorize this action" testId="authorize-dialog">
        <p>
          You are authorizing <strong>{KIND_LABEL[proposal.kind]}</strong> for request <Mono>{request.id}</Mono>, targeting <strong>{target.label}</strong>.
        </p>
        <ul className="small">
          <li>The server revalidates against current knowledge before dispatch; stale intent is never executed.</li>
          {(proposal.kind === "answer" || proposal.kind === "clarify") && <li>This persists a draft. Nothing is sent to the customer.</li>}
          {(proposal.kind === "create_work" || proposal.kind === "link_work") && <li>Creating or linking work does not resolve the request; verification is a separate step.</li>}
        </ul>
        {commit.error && <ErrorPanel error={commit.error} />}
        <div className="form-actions">
          <button type="button" className="btn" onClick={() => setConfirm(false)}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={doCommit} disabled={commit.isPending || !canCommit} data-testid="confirm-authorize">
            {commit.isPending ? "Authorizing…" : "Authorize and commit"}
          </button>
        </div>
      </Dialog>
    </div>
  );
}

export function Receipt({ action, runStatus = null }: { action: Action; runStatus?: string | null }) {
  const autoReconciling = runStatus === "queued" || runStatus === "running";
  const r = action.receipt;
  return (
    <div className="card" data-testid="receipt">
      <div className="row">
        <strong>Action</strong> <Mono>{action.id}</Mono>
        <StatusBadge map={ACTION_STATUS} value={action.status} testId="action-status" />
        <StatusBadge map={VERIFICATION} value={action.verification} />
      </div>
      {action.status === "unknown" && (
        <Callout tone="danger" icon="question" title="Dispatch outcome unknown" testId="dispatch-unknown">
          <p>The connector may or may not have created the target. Reconciliation looks it up; nothing is created again automatically. If it cannot be established, operator reconciliation is required.</p>
        </Callout>
      )}
      {action.status === "unknown" && autoReconciling && (
        <p className="small muted" data-testid="auto-reconciling"><Icon name="progress" size={12} /> Automatic reconciliation is in progress. Operator reconciliation opens if it cannot establish the outcome.</p>
      )}
      {action.status === "unknown" && !autoReconciling && (
        <ReconcileForm action={action} />
      )}
      {action.error && (
        <p className="small" style={{ color: "var(--danger-fg)" }}><Icon name="alert" size={12} /> {action.error.code}: {action.error.message}</p>
      )}
      {r ? (
        <dl className="kv small">
          <dt>Receipt</dt><dd><Mono>{r.id}</Mono></dd>
          <dt>Target</dt>
          <dd className="row">
            <Badge s={{ label: CONNECTOR_LABEL[r.connector] ?? r.connector, tone: r.connector === "linear" ? "info" : "neutral", icon: r.connector === "linear" ? "external" : "work" }} testId="receipt-connector" />
            <Mono>{r.target_id}</Mono>
            {r.target_url && <a href={r.target_url} target="_blank" rel="noreferrer">Open <Icon name="external" size={12} /><span className="sr-only">(opens in new tab)</span></a>}
          </dd>
          <dt>Observed</dt><dd><Ts iso={r.observed_at} /></dd>
          <dt>Result hash</dt><dd><Mono>{r.result_hash.slice(0, 16)}…</Mono></dd>
        </dl>
      ) : (
        <p className="small muted">No receipt yet{action.status === "accepted" || action.status === "dispatching" ? " — dispatch in progress." : "."}</p>
      )}
      <p className="small muted">Updated <Ts iso={action.updated_at} /> · v{action.version}</p>
    </div>
  );
}
