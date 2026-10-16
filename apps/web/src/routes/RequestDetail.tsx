import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import type { Action, Decision, EvidenceRef, Proposal, Run, RunCreate, RunEvent } from "@contracts";
import { api, ApiError, isUnavailable } from "../api/client";
import { keys, useActions, useCommand, useContextBundle, useCustomerName, useInfo, useKnownRunIds, useRequest, useObservations, useRequestProposals, useRequestRuns, useRunEvents, useSource, rememberRun, type Timeline } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Badge, Callout, Empty, ErrorPanel, Loading, Mono, Section, StatusBadge, Tag, Ts, useAnnounce } from "../components/ui";
import { EvidenceChip, PathText, SourceDrawerProvider, useOpenSource } from "../components/Evidence";
import { ActionArea } from "../components/ActionArea";
import { RunTimeline } from "../components/Timeline";
import { CloseRequest, ObservationForm, ReplyForm, RunControls } from "../components/Forms";
import { ACTIVE_RUN, ATTEMPT_OUTCOME, DECISION_STATUS, KIND_LABEL, REQUEST_STATUS, RUN_STATUS, VERIFICATION, WORK_STATUS, CONNECTOR_LABEL, spec } from "../lib/status";
import { humanize } from "../lib/format";

export function RequestDetail() {
  const { id = "" } = useParams();
  return (
    <SourceDrawerProvider>
      <Workspace key={id} id={id} />
    </SourceDrawerProvider>
  );
}

/** Feeds one run's durable events up to the workspace; renders nothing itself. */
function RunFeed({ runId, active, onUpdate, onNew }: { runId: string; active: boolean; onUpdate: (id: string, t: Timeline) => void; onNew: (evs: RunEvent[], initial: boolean) => void }) {
  const t = useRunEvents(runId, active, onNew);
  useEffect(() => onUpdate(runId, t), [runId, t, onUpdate]);
  return null;
}

function Workspace({ id }: { id: string }) {
  const qc = useQueryClient();
  const announce = useAnnounce();
  const openSource = useOpenSource();
  const customerName = useCustomerName();
  const info = useInfo();
  const req = useRequest(id);
  const ctx = useContextBundle(id, Boolean(req.data));
  const actionsQ = useActions({ request_id: id }, { poll: 4000 });
  const actions = useMemo(() => actionsQ.data?.items ?? [], [actionsQ.data]);
  const runsQ = useRequestRuns(id);
  const listedRuns = runsQ.data?.supported ? runsQ.data.runs : null;
  const fallbackIds = useKnownRunIds(id, actions);
  const runIds = listedRuns ? [...listedRuns].reverse().map((r) => r.id) : fallbackIds;
  const obsQ = useObservations(id);
  const reqSource = useSource(req.data?.data.source_id ?? null, null);

  const runQs = useQueries({
    queries: (listedRuns ? [] : runIds).map((rid) => ({
      queryKey: keys.run(rid),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<Run>(`/runs/${encodeURIComponent(rid)}`, signal),
      refetchInterval: (q: { state: { data?: { data: Run } } }) => (q.state.data && ACTIVE_RUN.has(q.state.data.data.status) ? 1500 : 6000),
    })),
  });
  // List routes are newest first; keep server order (timestamps can tie at one-second resolution).
  const runs = listedRuns ? [...listedRuns].reverse() : runQs.map((q) => q.data?.data).filter((r): r is Run => Boolean(r) && r!.request_id === id).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const latestRun = runs.length ? runs[runs.length - 1] : null;

  const [showAllPaths, setShowAllPaths] = useState(false);
  const [showAllSources, setShowAllSources] = useState(false);
  const [timelines, setTimelines] = useState<Record<string, Timeline>>({});
  const onUpdate = useCallback((rid: string, t: Timeline) => setTimelines((s) => (s[rid] === t ? s : { ...s, [rid]: t })), []);
  const lastAnnounced = useRef<string>("");
  const onNew = useCallback(
    (evs: RunEvent[], initial: boolean) => {
      // Durable events drive refetches of the resources they mention.
      qc.invalidateQueries({ queryKey: keys.request(id) });
      qc.invalidateQueries({ queryKey: ["request-runs", id] });
      qc.invalidateQueries({ queryKey: ["request-proposals", id] });
      qc.invalidateQueries({ queryKey: ["actions"] });
      qc.invalidateQueries({ queryKey: ["proposal"] });
      for (const e of evs) qc.invalidateQueries({ queryKey: keys.run(e.run_id) });
      if (evs.some((e) => e.type === "tool_completed" || e.type === "run_finished" || e.type === "observation_recorded")) qc.invalidateQueries({ queryKey: keys.context(id) });
      const fin = evs.find((e) => e.type === "run_finished" || e.type === "waiting_for_input" || e.type === "proposal_ready" || e.type === "error");
      if (fin && !initial) {
        const key = `${fin.run_id}:${fin.seq}`;
        if (lastAnnounced.current !== key) {
          lastAnnounced.current = key;
          const p = fin.payload as Record<string, string>;
          const msg =
            fin.type === "run_finished" ? `Run status: ${spec(RUN_STATUS, p.run_status).label}. Request status: ${spec(REQUEST_STATUS, p.request_status).label}.`
            : fin.type === "waiting_for_input" ? "Run is waiting for the customer's input."
            : fin.type === "proposal_ready" ? `Proposal ready: ${KIND_LABEL[p.kind] ?? p.kind}.`
            : `Run error: ${p.code}.`;
          announce(msg, fin.type === "error");
        }
      }
    },
    [qc, id, announce],
  );

  // Proposal IDs come from durable events (proposal_ready) and committed actions.
  const allEvents = useMemo(() => Object.values(timelines).flatMap((t) => t.events), [timelines]);
  const proposalIds = useMemo(() => {
    const s = new Set<string>();
    for (const e of allEvents) if (e.type === "proposal_ready" && typeof (e.payload as { proposal_id?: unknown }).proposal_id === "string") s.add((e.payload as { proposal_id: string }).proposal_id);
    for (const a of actions) s.add(a.proposal_id);
    return [...s];
  }, [allEvents, actions]);
  const propsQ = useRequestProposals(id);
  const listedProps = propsQ.data?.supported ? propsQ.data.proposals : null;
  const propQs = useQueries({
    queries: (listedProps ? [] : proposalIds).map((pid) => ({
      queryKey: keys.proposal(pid),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<Proposal>(`/proposals/${encodeURIComponent(pid)}`, signal),
      refetchInterval: 5000,
    })),
  });
  const proposals = listedProps ? [...listedProps].reverse() : propQs.map((q) => q.data?.data).filter((p): p is Proposal => Boolean(p)).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const proposal = proposals.length ? proposals[proposals.length - 1] : null;
  const priorProposals = proposals.slice(0, -1).reverse();
  const actionFor = (p: Proposal | null): Action | null => (p ? actions.find((a) => a.proposal_id === p.id) ?? null : null);
  const action = actionFor(proposal);

  const startRun = useCommand<RunCreate, Run>("post", () => `/requests/${encodeURIComponent(id)}/runs`, () => [keys.request(id)]);
  const replan = () =>
    startRun.mutate({ mode: "scripted", instruction: "Replan: the prior proposal is stale or blocked.", authorization_id: null }, {
      onSuccess: (r) => { rememberRun(id, r.data.id); announce(`Replanning in run ${r.data.id} (Scripted mode).`); },
      onError: (e) => announce(`Replan refused: ${(e as Error).message}`, true),
    });

  if (req.isLoading) return <Loading label="Loading request" />;
  if (req.error) {
    if (req.error instanceof ApiError && req.error.code === "NOT_FOUND")
      return (
        <div data-testid="state-unknown-entity">
          <Empty title="Unknown request" icon="question">
            <p>No request <Mono>{id}</Mono> exists in this workspace. It may be in another workspace or the ID is wrong.</p>
            <p><Link to="/requests">Back to requests</Link></p>
          </Empty>
        </div>
      );
    return <ErrorPanel error={req.error} onRetry={() => req.refetch()} what="request" />;
  }
  const request = req.data!.data;
  const bundle = ctx.data?.data;
  const knowledgeRevision = ctx.data?.meta.knowledge_revision ?? req.data!.meta.knowledge_revision;
  const refs = new Map<string, EvidenceRef>((bundle?.sources ?? []).map((s) => [s.id, s]));
  const runActive = Boolean(latestRun && ACTIVE_RUN.has(latestRun.status));
  const waitingQuestion = (() => {
    for (let i = allEvents.length - 1; i >= 0; i--) if (allEvents[i].type === "waiting_for_input") return String((allEvents[i].payload as { question?: string }).question ?? "");
    return proposal?.kind === "clarify" ? proposal.payload.question : null;
  })();
  const obsEvents = allEvents.filter((e) => e.type === "observation_recorded");
  const observations = obsQ.data?.supported
    ? obsQ.data.items.map((o) => ({ key: o.id, id: o.id, kind: o.kind, verification: o.verification, at: o.observed_at, body: o.body as string | null, actor: o.actor_id as string | null }))
    : obsEvents.map((e) => { const p = e.payload as Record<string, string>; return { key: `${e.run_id}:${e.seq}`, id: p.observation_id, kind: p.kind, verification: p.verification, at: e.occurred_at, body: null, actor: null }; });
  const passed = observations.filter((o) => o.verification === "passed" && o.kind !== "connector_status");
  const conflicts = findConflicts(bundle?.decisions ?? []);
  const reqUnavailable = isUnavailable(ctx.error) || isUnavailable(actionsQ.error);

  return (
    <div data-testid="request-workspace">
      {runIds.map((rid) => (
        <RunFeed key={rid} runId={rid} active={Boolean(runs.find((r) => r.id === rid && ACTIVE_RUN.has(r.status)))} onUpdate={onUpdate} onNew={onNew} />
      ))}

      <header className="detail-head" aria-labelledby="req-title">
        <div className="row small muted">
          <Link to="/requests">Requests</Link> <Icon name="chevron" size={12} /> <Mono>{request.id}</Mono>
        </div>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div className="grow" style={{ flex: 1, minWidth: 0 }}>
            <h1 id="req-title">{request.subject}</h1>
            <p className="muted">{customerName(request.customer_id)}</p>
          </div>
          <div className="row">
            <RunControls request={request} run={latestRun} />
            <CloseRequest request={request} />
          </div>
        </div>
        <div className="status-strip" aria-label="Current state">
          <StatusBadge map={REQUEST_STATUS} value={request.status} prefix="Request" testId="request-status" />
          {latestRun ? <StatusBadge map={RUN_STATUS} value={latestRun.status} prefix="Run" testId="run-status" /> : <Tag>No run yet</Tag>}
          {latestRun && <Tag>stage {latestRun.stage}</Tag>}
          {latestRun && <Badge s={{ label: latestRun.mode === "scripted" ? "Scripted mode" : "Live model", tone: "mode", icon: "doc" }} testId="run-mode" />}
          {action && <StatusBadge map={VERIFICATION} value={action.verification} />}
        </div>
        <div className="meta">
          <span>Knowledge rev <strong data-testid="knowledge-revision">{knowledgeRevision}</strong></span>
          <span>Request v{request.version}</span>
          <span>Channel {request.channel}</span>
          <span>
            Source{" "}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => openSource({ source_id: request.source_id, revision: null })}>
              <Mono>{request.source_id}</Mono>
            </button>
            {reqSource.data && <Tag>{humanize(reqSource.data.data.origin)}</Tag>}
          </span>
          <span>Owner {request.owner_id ? <Mono>{request.owner_id}</Mono> : <Tag tone="warn" icon="alert">Missing owner</Tag>}</span>
          <span>Created <Ts iso={request.created_at} /></span>
          <span>Updated <Ts iso={request.updated_at} /></span>
        </div>
        {latestRun?.status === "failed" && latestRun.error && (
          <Callout tone="danger" icon="alert" title={`Run failed: ${latestRun.error.code}`} testId="run-failed">
            <p>{latestRun.error.message}</p>
            {latestRun.error.code === "CAPABILITY_DISABLED" && <p className="muted">The live model is unavailable; the run was not silently switched to Scripted mode.</p>}
          </Callout>
        )}
        {latestRun?.status === "cancelled" && (
          <Callout tone="neutral" icon="ban" title="Run cancelled" testId="run-cancelled"><p>Planning stopped. Any external operation already in flight is still observed and reconciled.</p></Callout>
        )}
        {request.status === "resolved" && (
          <Callout tone="success" icon="check" title="Resolved with evidence" testId="resolved-evidence">
            {passed.length ? (
              <ul className="small">
                {passed.map((o) => (
                  <li key={o.key}>{humanize(o.kind)} · passed · <Mono>{o.id}</Mono>{o.actor ? <> · by <Mono>{o.actor}</Mono></> : null} · <Ts iso={o.at} />{o.body ? <> — “{o.body}”</> : null}</li>
                ))}
              </ul>
            ) : (
              <p>The server records an attributable passed verification for this request.</p>
            )}
          </Callout>
        )}
      </header>

      {reqUnavailable && <ErrorPanel error={ctx.error ?? actionsQ.error} onRetry={() => { ctx.refetch(); actionsQ.refetch(); }} what="investigation" />}

      <div className="workspace-grid">
        <div className="col">
          <Section title="Finding" testId="finding">
            {proposal ? (
              <div className="stack">
                <p className="finding" data-testid="finding-text">{proposal.explanation}</p>
                <div className="row">
                  <span className="label">Selected disposition</span>
                  <Tag tone="info" icon="check">{KIND_LABEL[proposal.kind]}</Tag>
                </div>
                <div>
                  <span className="label">Rejected alternatives</span>
                  {proposal.rejected_alternatives.length === 0 ? (
                    <p className="small muted">None recorded.</p>
                  ) : (
                    <ul className="list" data-testid="alternatives">
                      {proposal.rejected_alternatives.map((alt, i) => (
                        <li key={i} className="stack" style={{ gap: 4 }}>
                          <span className="row"><Tag icon="x">{KIND_LABEL[alt.kind]}</Tag> <span>{alt.reason}</span></span>
                          {alt.evidence_ids.length > 0 && <span className="row">{alt.evidence_ids.map((eid) => <EvidenceChip key={eid} id={eid} refs={refs} />)}</span>}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ) : runActive ? (
              <p className="muted">Investigation in progress — a finding appears when the run prepares a proposal.</p>
            ) : (
              <p className="muted">No finding yet. {runIds.length === 0 ? "Start a run to investigate this request." : ""}</p>
            )}
          </Section>

          <Section title="Graph evidence path" count={bundle?.paths.length} testId="paths">
            {ctx.isLoading ? <Loading label="Loading context" /> : ctx.error ? <ErrorPanel error={ctx.error} onRetry={() => ctx.refetch()} what="context" /> : !bundle || bundle.paths.length === 0 ? (
              <p className="muted">No graph path was returned for this request.</p>
            ) : (
              <div className="stack-lg">
                {(showAllPaths ? bundle.paths : bundle.paths.slice(0, 3)).map((p, i) => <PathText key={p.id} path={p} refs={refs} index={i} />)}
                {bundle.paths.length > 3 && (
                  <button type="button" className="btn btn-sm" aria-expanded={showAllPaths} onClick={() => setShowAllPaths((v) => !v)}>
                    {showAllPaths ? "Show first 3 paths" : `Show all ${bundle.paths.length} paths`}
                  </button>
                )}
              </div>
            )}
            {bundle?.truncated && (
              <Callout tone="warn" icon="alert" title="Retrieval truncated" testId="truncated">
                <p>The context hit a size or depth bound. Coverage is incomplete; absence of a match below is not evidence of absence.</p>
              </Callout>
            )}
          </Section>

          <Section title="Sources" count={bundle?.sources.length} testId="sources">
            {!bundle ? (ctx.isLoading ? <Loading /> : null) : bundle.sources.length === 0 ? <p className="muted">No source evidence selected.</p> : (
              <ul className="list">
                {(showAllSources ? bundle.sources : bundle.sources.slice(0, 5)).map((s) => (
                  <li key={s.id} className="stack" style={{ gap: 4 }}>
                    <blockquote className="quote">{s.excerpt}</blockquote>
                    <div className="row small">
                      <button type="button" className="btn btn-sm" onClick={() => openSource({ source_id: s.source_id, revision: s.source_revision, ref: s })} data-testid="open-source">
                        <Icon name="doc" /> {s.source_id} · rev {s.source_revision}
                      </button>
                      <Tag>{humanize(s.origin)}</Tag>
                      <span className="muted">{s.location}</span>
                      {s.stale ? <Tag tone="warn" icon="alert">Stale</Tag> : <Tag tone="success" icon="check">Fresh</Tag>}
                      <span className="muted">observed <Ts iso={s.observed_at} /></span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {bundle && bundle.sources.length > 5 && (
              <button type="button" className="btn btn-sm" style={{ marginTop: 8 }} aria-expanded={showAllSources} onClick={() => setShowAllSources((v) => !v)}>
                {showAllSources ? "Show fewer sources" : `Show all ${bundle.sources.length} sources`}
              </button>
            )}
            {bundle && bundle.sources.some((s) => s.stale) && (
              <Callout tone="warn" icon="alert" title="Stale evidence present" testId="stale-evidence"><p>Some cited observations are older than the freshness window. Conclusions that rely on them are uncertain.</p></Callout>
            )}
          </Section>

          <Section title="Missing information" count={bundle?.missing_information.length} testId="missing">
            {!bundle ? null : bundle.missing_information.length === 0 ? <p className="muted">The server reported no missing information.</p> : (
              <ul>{bundle.missing_information.map((m, i) => <li key={i}>{m}</li>)}</ul>
            )}
          </Section>

          <div className="grid-2">
            <Section title="Owner & related work" count={bundle?.related_work.length} testId="work">
              {!bundle ? null : bundle.related_work.length === 0 ? <p className="muted">No related work found.</p> : (
                <ul className="list">
                  {bundle.related_work.map((w) => (
                    <li key={w.id} className="stack" style={{ gap: 3 }}>
                      <span className="row"><strong>{w.title}</strong> <StatusBadge map={WORK_STATUS} value={w.status} /></span>
                      <span className="row small">
                        <Mono>{w.id}</Mono>
                        <Tag icon={w.connector === "linear" ? "external" : "work"}>{CONNECTOR_LABEL[w.connector]}</Tag>
                        {w.owner_id ? <span>owner <Mono>{w.owner_id}</Mono></span> : <Tag tone="warn" icon="alert">Missing owner</Tag>}
                        {request.linked_work_ids.includes(w.id) && <Tag tone="info" icon="link">Linked</Tag>}
                      </span>
                      <span className="small muted">observed <Ts iso={w.observed_at} />{w.forecast_at ? <> · forecast <Ts iso={w.forecast_at} /></> : " · no forecast"}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
            <Section title="Decisions in scope" count={bundle?.decisions.length} testId="decisions">
              {conflicts.length > 0 && (
                <Callout tone="warn" icon="alert" title="Conflicting active decisions" testId="conflicting-decisions">
                  <p>{conflicts.map((c) => `${humanize(c[0].rule_type)} on ${c[0].scope.entity_id}: ${c.map((d) => d.id).join(" vs ")}`).join("; ")}. Expect clarification or escalation rather than a silent choice.</p>
                </Callout>
              )}
              {!bundle ? null : bundle.decisions.length === 0 ? <p className="muted">No scoped decisions apply.</p> : (
                <ul className="list">
                  {bundle.decisions.map((d) => (
                    <li key={d.id} className="stack" style={{ gap: 3 }}>
                      <span className="row"><strong>{humanize(d.rule_type)}</strong> <StatusBadge map={DECISION_STATUS} value={d.status} /></span>
                      <span className="small">value <Mono>{JSON.stringify(d.value)}</Mono> · scope {d.scope.entity_type} <Mono>{d.scope.entity_id}</Mono></span>
                      <span className="small muted">{d.rationale}</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="small"><Link to="/memory">Manage decisions in Memory</Link></p>
            </Section>
          </div>

          <Section title="Previous attempts & outcomes" count={bundle?.attempts.length} testId="attempts">
            {!bundle ? null : bundle.attempts.length === 0 ? <p className="muted">No previous attempts in scope.</p> : (
              <ul className="list">
                {bundle.attempts.map((a) => (
                  <li key={a.id} className="stack" style={{ gap: 3 }}>
                    <span className="row"><Mono>{a.procedure_key}</Mono> <StatusBadge map={ATTEMPT_OUTCOME} value={a.outcome} /> <StatusBadge map={VERIFICATION} value={a.verification} /></span>
                    <span>{a.summary}</span>
                    <span className="small muted">scope {a.scope.entity_type} <Mono>{a.scope.entity_id}</Mono> · context <Mono>{a.context_signature}</Mono> · <Ts iso={a.observed_at} /></span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Timeline" count={allEvents.length} testId="timeline-section">
            {runIds.length === 0 ? (
              <p className="muted">No runs known for this request. Start a run to investigate.</p>
            ) : (
              <div className="stack-lg">
                {[...runIds].reverse().map((rid) => {
                  const r = runs.find((x) => x.id === rid);
                  const t = timelines[rid];
                  return (
                    <div key={rid} className="stack">
                      <h3 className="row">Run <Mono>{rid}</Mono> {r && <Ts iso={r.created_at} />}</h3>
                      <RunTimeline run={r} runId={rid} events={t?.events ?? []} error={t?.error} lastPolledAt={t?.lastPolledAt ?? null} active={Boolean(r && ACTIVE_RUN.has(r.status))} />
                    </div>
                  );
                })}
              </div>
            )}
          </Section>
        </div>

        <div className="col action-rail">
          <Section title="Action" testId="action-section">
            <ActionArea
              request={request}
              proposal={proposal}
              action={action}
              refs={refs}
              work={bundle?.related_work ?? []}
              info={info.data?.data}
              knowledgeRevision={knowledgeRevision}
              runStatus={proposal ? runs.find((r) => r.id === proposal.run_id)?.status ?? null : null}
              onReplan={replan}
              replanPending={startRun.isPending}
            />
          </Section>

          {(request.status === "awaiting_input" || latestRun?.status === "waiting_for_input") && (
            <Section title="Clarification reply" testId="reply-section">
              <ReplyForm request={request} question={waitingQuestion} />
            </Section>
          )}

          <Section title="Outcome verification" testId="verify-section">
            {observations.length > 0 && (
              <ul className="list small" style={{ marginBottom: 10 }} data-testid="observations">
                {observations.map((o) => (
                  <li key={o.key} className="stack" style={{ gap: 2 }}>
                    <span className="row"><StatusBadge map={VERIFICATION} value={o.verification} /> {humanize(o.kind ?? "")} <Mono>{o.id}</Mono> <Ts iso={o.at} /></span>
                    {o.body && <span className="muted">{o.body}{o.actor ? ` — ${o.actor}` : ""}</span>}
                  </li>
                ))}
              </ul>
            )}
            {request.status === "resolved" || request.status === "closed" ? (
              <p className="small muted">Request is {spec(REQUEST_STATUS, request.status).label.toLowerCase()}. You can still record a new observation (e.g. a regression).</p>
            ) : null}
            <ObservationForm request={request} actions={actions} refs={bundle?.sources ?? []} />
          </Section>

          {(priorProposals.length > 0 || actions.length > 1) && (
            <Section title="Earlier proposals & actions" testId="history">
              <ul className="list small">
                {priorProposals.map((p) => {
                  const a = actionFor(p);
                  return (
                    <li key={p.id} className="row">
                      <Mono>{p.id}</Mono> {KIND_LABEL[p.kind]} <StatusBadge map={{ valid: { label: "Valid", tone: "success", icon: "check" }, stale: { label: "Stale", tone: "warn", icon: "alert" }, invalid: { label: "Invalid", tone: "danger", icon: "ban" } }} value={p.validity} />
                      {a && <span>→ action <Mono>{a.id}</Mono> {a.status}</span>}
                    </li>
                  );
                })}
              </ul>
            </Section>
          )}
        </div>
      </div>
    </div>
  );
}

/** Groups active decisions with the same rule type and scope but different values — a visible fact, not a resolution. */
function findConflicts(ds: Decision[]): Decision[][] {
  const groups = new Map<string, Decision[]>();
  for (const d of ds) {
    if (d.status !== "active") continue;
    const k = `${d.rule_type}|${d.scope.entity_type}|${d.scope.entity_id}`;
    groups.set(k, [...(groups.get(k) ?? []), d]);
  }
  return [...groups.values()].filter((g) => g.length > 1 && new Set(g.map((d) => JSON.stringify(d.value))).size > 1);
}

