import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import type { Action, Decision, EvidenceRef, Proposal, Run, RunCreate, RunEvent } from "@contracts";
import { api, ApiError, isUnavailable } from "../api/client";
import { keys, useActions, useCommand, useContextBundle, useCustomerName, useInfo, useKnownRunIds, useRequest, useObservations, useRequestProposals, useRequestRuns, useRunEvents, useSource, rememberRun, type Timeline } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Badge, Callout, Empty, ErrorPanel, Loading, Mono, Owner, Section, StatusBadge, Tag, Ts, useAnnounce } from "../components/ui";
import { EvidenceChip, PathText, SourceDrawerProvider, useOpenSource } from "../components/Evidence";
import { ActionArea, nextStep } from "../components/ActionArea";
import { RuleValue, ScopeName } from "../components/Names";
import { RunTimeline } from "../components/Timeline";
import { CloseRequest, ObservationForm, ReplyForm, RunControls } from "../components/Forms";
import { ACTION_STATUS, ACTIVE_RUN, ATTEMPT_OUTCOME, DECISION_STATUS, KIND_EXPLAIN, KIND_LABEL, REQUEST_STATUS, RULE_LABEL, RUN_STATUS, VALIDITY, VERIFICATION, WORK_STATUS, spec } from "../lib/status";
import { friendlyExcerpt, humanize, plural } from "../lib/format";

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
    startRun.mutate({ mode: "scripted", instruction: "Look again: the earlier suggestion was out of date or blocked.", authorization_id: null }, {
      onSuccess: (r) => { rememberRun(id, r.data.id); announce("Looking into it again with the latest facts."); },
      onError: (e) => announce(`Couldn't start again: ${(e as Error).message}`, true),
    });

  if (req.isLoading) return <Loading label="Loading request" />;
  if (req.error) {
    if (req.error instanceof ApiError && req.error.code === "NOT_FOUND")
      return (
        <div data-testid="state-unknown-entity">
          <Empty title="Unknown request" icon="question">
            <p>We couldn't find this request (<Mono>{id}</Mono>). The link may be wrong, or it belongs to another workspace.</p>
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

  const kindOf = (k: string) => KIND_LABEL[k] ?? humanize(k);
  const firstPath = bundle?.paths[0];
  const chain = firstPath ? (firstPath.nodes as { title: string }[]).map((n) => n.title).join(" → ") : null;
  // What actually shaped the recommendation: evidence it cites (incl. rejected options), its target work, and IDs named in its reasons.
  const cited = {
    evidence: new Set<string>([...(proposal?.evidence_ids ?? []), ...(proposal?.rejected_alternatives ?? []).flatMap((a) => a.evidence_ids)]),
    work: new Set<string>([
      ...(proposal?.kind === "link_work" ? [proposal.payload.work_item_id] : []),
      ...(proposal?.kind === "create_work" ? proposal.payload.dependency_ids : []),
    ]),
    text: [proposal?.explanation ?? "", ...(proposal?.validation_reasons ?? []), ...(proposal?.rejected_alternatives ?? []).map((a) => a.reason)].join("\n"),
  };
  const citedStale = (bundle?.sources ?? []).some((x) => x.stale && cited.evidence.has(x.id));

  return (
    <div data-testid="request-workspace" className="story">
      {runIds.map((rid) => (
        <RunFeed key={rid} runId={rid} active={Boolean(runs.find((r) => r.id === rid && ACTIVE_RUN.has(r.status)))} onUpdate={onUpdate} onNew={onNew} />
      ))}

      <header className="detail-head" aria-labelledby="req-title">
        <nav className="row small muted" aria-label="Breadcrumb">
          <Link to="/requests">Requests</Link> <Icon name="chevron" size={12} /> <span>{request.subject}</span>
        </nav>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div className="grow" style={{ flex: "1 1 260px", minWidth: 0 }}>
            <h1 id="req-title">{request.subject}</h1>
            <p className="muted">From <strong>{customerName(request.customer_id)}</strong> · received <Ts iso={request.created_at} /></p>
          </div>
          <div className="row">
            <RunControls request={request} run={latestRun} />
            <CloseRequest request={request} />
          </div>
        </div>
        <div className="status-strip" aria-label="Where it stands">
          <StatusBadge map={REQUEST_STATUS} value={request.status} testId="request-status" />
          {latestRun ? <StatusBadge map={RUN_STATUS} value={latestRun.status} prefix="Investigation" testId="run-status" /> : <Tag>Not looked into yet</Tag>}
          {latestRun && <Badge s={{ label: latestRun.mode === "scripted" ? "Rule-based mode" : "AI model", tone: "mode", icon: "doc" }} testId="run-mode" />}
          {action && <StatusBadge map={VERIFICATION} value={action.verification} />}
          <span className="small muted">Owner: <Owner id={request.owner_id} /></span>
        </div>
        {latestRun?.status === "failed" && latestRun.error && (
          <Callout tone="danger" icon="alert" title="The investigation stopped with a problem" testId="run-failed">
            <p>{latestRun.error.message} <span className="muted">({latestRun.error.code})</span></p>
            {latestRun.error.code === "CAPABILITY_DISABLED" && <p className="muted">The AI model isn't connected. We didn't quietly switch to rule-based mode; start again in rule-based mode if you want.</p>}
          </Callout>
        )}
        {latestRun?.status === "cancelled" && (
          <Callout tone="neutral" icon="ban" title="Investigation stopped" testId="run-cancelled"><p>PromiseGraph stopped looking into this. Anything already under way is still tracked.</p></Callout>
        )}
        {request.status === "resolved" && (
          <Callout tone="success" icon="check" title="Solved, and confirmed" testId="resolved-evidence">
            {passed.length ? (
              <ul className="small">
                {passed.map((o) => (
                  <li key={o.key}>{OBS_KIND[o.kind] ?? humanize(o.kind)} · <Ts iso={o.at} />{o.body ? <> — “{o.body}”</> : null}</li>
                ))}
              </ul>
            ) : (
              <p>Someone confirmed this request is solved.</p>
            )}
          </Callout>
        )}
      </header>

      {reqUnavailable && <ErrorPanel error={ctx.error ?? actionsQ.error} onRetry={() => { ctx.refetch(); actionsQ.refetch(); }} what="investigation" />}

      <Section title="1. What the customer asked" testId="asked">
        <blockquote className="quote">{request.body}</blockquote>
        <p className="small muted" style={{ marginTop: 6 }}>
          Received <Ts iso={request.created_at} /> · {request.channel === "manual" ? "entered by hand" : request.channel === "import" ? "imported" : "from a connected tool"}
          {reqSource.data && <> · <button type="button" className="btn btn-ghost btn-sm" onClick={() => openSource({ source_id: request.source_id, revision: null })}>View original</button></>}
        </p>
      </Section>

      <Section title="2. What we found" testId="found">
        {ctx.isLoading ? <Loading label="Checking what we know" /> : ctx.error ? <ErrorPanel error={ctx.error} onRetry={() => ctx.refetch()} what="what we know" /> : !bundle ? null : (
          <div className="stack">
            <p className="finding">
              PromiseGraph checked {plural(bundle.sources.length, "source")}, {plural(bundle.related_work.length, "related engineering item")}, {plural(bundle.decisions.length, "company rule")} and {plural(bundle.attempts.length, "past attempt")}.
            </p>
            {chain && (
              <div>
                <h3 className="sub">How we know this</h3>
                <p data-testid="chain">{chain}</p>
              </div>
            )}
            {citedStale ? (
              <Callout tone="warn" icon="alert" title="This suggestion relies on information that may be out of date" testId="stale-evidence">
                <p>At least one source it is based on hasn't been updated recently. Double-check before approving.</p>
              </Callout>
            ) : (bundle.truncated || bundle.sources.some((x) => x.stale)) ? (
              <p className="small muted note" data-testid="coverage-note">
                <Icon name="info" size={12} />{" "}
                {bundle.truncated && <span data-testid="truncated">We didn't have time to check everything. </span>}
                {bundle.sources.some((x) => x.stale) && <span data-testid="stale-evidence">Some background sources may be out of date (none that this suggestion relies on).</span>}
              </p>
            ) : null}
            {bundle.missing_information.length > 0 && (
              <div data-testid="missing">
                <h3 className="sub">What we still don't know</h3>
                <ul>{bundle.missing_information.map((m, i) => <li key={i}>{m}</li>)}</ul>
              </div>
            )}
            <FoundGroup testId="sources" items={bundle.sources} cited={(x) => cited.evidence.has(x.id)} count={(n) => `${plural(n, "source")} checked`} render={(x) => (
              <>
                <blockquote className="quote">{friendlyExcerpt(x.excerpt)}</blockquote>
                <div className="row small">
                  <button type="button" className="btn btn-sm" onClick={() => openSource({ source_id: x.source_id, revision: x.source_revision, ref: x })} data-testid="open-source">
                    <Icon name="doc" /> Open source
                  </button>
                  {x.stale ? <Tag tone="warn" icon="alert">May be out of date</Tag> : <Tag tone="success" icon="check">Up to date</Tag>}
                  <span className="muted">as of <Ts iso={x.observed_at} /></span>
                </div>
              </>
            )} />
            <FoundGroup testId="work" items={bundle.related_work} cited={(w) => cited.work.has(w.id) || w.evidence_ids.some((e) => cited.evidence.has(e)) || cited.text.includes(w.id)} count={(n) => `${plural(n, "related engineering item")}`} render={(w) => (
              <>
                <span className="row"><strong>{w.title}</strong> <StatusBadge map={WORK_STATUS} value={w.status} />{request.linked_work_ids.includes(w.id) && <Tag tone="info" icon="link">Attached to this request</Tag>}</span>
                <span className="row small">Owner: <Owner id={w.owner_id} /> · {w.forecast_at ? <>expected <Ts iso={w.forecast_at} /></> : "no expected date"}</span>
              </>
            )} />
            <FoundGroup testId="decisions" items={bundle.decisions} cited={(d) => d.evidence_ids.some((e) => cited.evidence.has(e)) || cited.text.includes(d.id)} count={(n) => `${plural(n, "company rule")} ${n === 1 ? "applies" : "apply"}`} render={(d) => (
              <>
                <span className="row"><strong>{RULE_LABEL[d.rule_type] ?? humanize(d.rule_type)}</strong>: <RuleValue ruleType={d.rule_type} value={d.value} /> <StatusBadge map={DECISION_STATUS} value={d.status} /></span>
                <span className="small muted">{d.rationale} · applies to <ScopeName scope={d.scope} currentRequestId={request.id} /></span>
              </>
            )} before={conflicts.length > 0 ? (
              <Callout tone="warn" icon="alert" title="Hint: some rules may conflict" testId="conflicting-decisions">
                <p>{conflicts.map((c) => `${RULE_LABEL[c[0].rule_type] ?? humanize(c[0].rule_type)}: ${c.length} rules say different things`).join("; ")}.</p>
                <p className="muted small">This is only a hint from the screen. What counts is whether the suggested action below is allowed, or whether it was handed off.</p>
                {proposal && proposal.validation_reasons.length > 0 && <ul className="small" data-testid="conflict-authoritative">{proposal.validation_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
                {proposal?.kind === "escalate" && <p className="small"><strong>Handed off:</strong> {proposal.payload.reason}</p>}
              </Callout>
            ) : null} />
            <FoundGroup testId="attempts" items={bundle.attempts} cited={(t) => t.evidence_ids.some((e) => cited.evidence.has(e)) || cited.text.includes(t.id) || cited.text.includes(t.procedure_key)} count={(n) => `${plural(n, "past attempt")}`} render={(t) => (
              <>
                <span className="row"><StatusBadge map={ATTEMPT_OUTCOME} value={t.outcome} /> <StatusBadge map={VERIFICATION} value={t.verification} /></span>
                <span>{t.summary}</span>
                <span className="small muted"><Ts iso={t.observed_at} /> · for <ScopeName scope={t.scope} currentRequestId={request.id} /></span>
              </>
            )} />
          </div>
        )}
      </Section>

      <Section title="3. Recommended next step" testId="finding">
        {proposal ? (
          <div className="stack">
            <p className="recommend"><Icon name="check" size={16} /> <strong>{kindOf(proposal.kind)}</strong></p>
            <p className="small muted">{KIND_EXPLAIN[proposal.kind]}</p>
            <h3 className="sub">Why</h3>
            <p className="finding" data-testid="finding-text">{proposal.explanation}</p>
            <h3 className="sub">Other options we considered</h3>
            {proposal.rejected_alternatives.length === 0 ? (
              <p className="small muted">None recorded.</p>
            ) : (
              <ul className="list" data-testid="alternatives">
                {proposal.rejected_alternatives.map((alt, i) => (
                  <li key={i} className="stack" style={{ gap: 4 }}>
                    <span><Tag icon="x">Not chosen: {kindOf(alt.kind)}</Tag> {alt.reason}</span>
                    {alt.evidence_ids.length > 0 && <span className="row">{alt.evidence_ids.map((eid) => <EvidenceChip key={eid} id={eid} refs={refs} />)}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : runActive ? (
          <Loading label="PromiseGraph is still looking into it" />
        ) : (
          <p className="muted">No suggestion yet. {runIds.length === 0 ? "Click “Look into it” above to start." : ""}</p>
        )}
      </Section>

      <Section title="4. The action" testId="action-section">
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

      <Section title="5. What happens next" testId="verify-section">
        <div className="next-step" data-testid="next-step"><Icon name="info" /><p>{nextStep(request, action, proposal)}</p></div>
        {(request.status === "awaiting_input" || latestRun?.status === "waiting_for_input") && (
          <div className="subsection" data-testid="reply-section">
            <h3 className="sub">The customer replied?</h3>
            <ReplyForm request={request} question={waitingQuestion} />
          </div>
        )}
        <div className="subsection">
          <h3 className="sub">Confirm it's solved</h3>
          {observations.length > 0 && (
            <ul className="list small" style={{ marginBottom: 10 }} data-testid="observations">
              {observations.map((o) => (
                <li key={o.key} className="stack" style={{ gap: 2 }}>
                  <span className="row"><StatusBadge map={VERIFICATION} value={o.verification} /> {OBS_KIND[o.kind] ?? humanize(o.kind ?? "")} · <Ts iso={o.at} /></span>
                  {o.body && <span className="muted">{o.body}</span>}
                </li>
              ))}
            </ul>
          )}
          {request.status === "resolved" || request.status === "closed" ? (
            <p className="small muted">This request is {spec(REQUEST_STATUS, request.status).label.toLowerCase()}. If the problem comes back, record that here.</p>
          ) : (
            <p className="small muted">Only mark it solved once the customer (or a check you ran) confirms it. Creating or attaching a task doesn't count.</p>
          )}
          <ObservationForm request={request} actions={actions} refs={bundle?.sources ?? []} />
        </div>
      </Section>

      <details className="tech panel" data-testid="technical-details">
        <summary>Technical details</summary>
        <div className="panel-body stack-lg">
          <dl className="kv small">
            <dt>Request ID</dt><dd><Mono>{request.id}</Mono> (version {request.version})</dd>
            <dt>Knowledge revision</dt><dd data-testid="knowledge-revision">{knowledgeRevision}</dd>
            <dt>Original source</dt><dd><Mono>{request.source_id}</Mono>{reqSource.data && <> · {humanize(reqSource.data.data.origin)}</>}</dd>
            {proposal && <><dt>Suggested action</dt><dd><Mono>{proposal.id}</Mono> · based on revision {proposal.based_on_revision}</dd></>}
            {action && <><dt>Action</dt><dd><Mono>{action.id}</Mono></dd></>}
          </dl>
          <div data-testid="paths">
            <h3 className="sub">How we know this (all connections)</h3>
            {!bundle || bundle.paths.length === 0 ? <p className="muted">No connections returned.</p> : (
              <div className="stack-lg">
                {(showAllPaths ? bundle.paths : bundle.paths.slice(0, 3)).map((p, i) => <PathText key={p.id} path={p} refs={refs} index={i} />)}
                {bundle.paths.length > 3 && (
                  <button type="button" className="btn btn-sm" aria-expanded={showAllPaths} onClick={() => setShowAllPaths((v) => !v)}>
                    {showAllPaths ? "Show first 3" : `Show all ${bundle.paths.length} connections`}
                  </button>
                )}
              </div>
            )}
          </div>
          <div data-testid="timeline-section">
            <h3 className="sub">Investigation log</h3>
            {runIds.length === 0 ? <p className="muted">No investigations yet.</p> : (
              <div className="stack-lg">
                {[...runIds].reverse().map((rid) => {
                  const r = runs.find((x) => x.id === rid);
                  const t = timelines[rid];
                  return (
                    <div key={rid} className="stack">
                      <h3 className="row" data-testid="run-heading">Investigation <Mono>{rid}</Mono> {r && <Ts iso={r.created_at} />}</h3>
                      <RunTimeline run={r} runId={rid} events={t?.events ?? []} error={t?.error} lastPolledAt={t?.lastPolledAt ?? null} active={Boolean(r && ACTIVE_RUN.has(r.status))} />
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          {(priorProposals.length > 0 || actions.length > 1) && (
            <div data-testid="history">
              <h3 className="sub">Earlier suggestions</h3>
              <ul className="list small">
                {priorProposals.map((p) => {
                  const a = actionFor(p);
                  return (
                    <li key={p.id} className="row">
                      {kindOf(p.kind)} <StatusBadge map={VALIDITY} value={p.validity} /> <Mono>{p.id}</Mono>
                      {a && <span>→ <StatusBadge map={ACTION_STATUS} value={a.status} /></span>}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}

/**
 * One collapsed line per kind of finding ("3 company rules apply · Show"). Items that shaped the recommendation
 * (cited in its evidence, target or reasons) are shown expanded, labelled; everything else sits behind the toggle.
 */
function FoundGroup<T extends { id: string }>({ items, cited, count, render, testId, before }: { items: T[]; cited: (t: T) => boolean; count: (n: number) => string; render: (t: T) => React.ReactNode; testId: string; before?: React.ReactNode }) {
  const [all, setAll] = useState(false);
  const used = items.filter(cited);
  const shown = all ? [...used, ...items.filter((t) => !cited(t))] : used;
  if (items.length === 0) return <p className="small muted found-line" data-testid={testId}>{count(0)}.</p>;
  return (
    <div className="found-group" data-testid={testId}>
      <p className="found-line">
        <span>{count(items.length)}{used.length > 0 && <span className="muted"> · {used.length} used in this suggestion</span>}</span>
        {items.length > used.length && (
          <button type="button" className="btn btn-ghost btn-sm" aria-expanded={all} onClick={() => setAll((v) => !v)} data-testid={`toggle-${testId}`}>
            {all ? "Hide" : used.length ? "Show all" : "Show"}
          </button>
        )}
      </p>
      {before}
      {shown.length > 0 && (
        <ul className="list found-list">
          {shown.map((t) => (
            <li key={t.id} className="stack" style={{ gap: 3 }}>
              {cited(t) && <span className="used-tag"><Tag tone="info" icon="check">Used in this suggestion</Tag></span>}
              {render(t)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const OBS_KIND: Record<string, string> = {
  customer_confirmation: "Customer confirmed",
  operator_confirmation: "Confirmed by our team",
  acceptance_check: "Checked against the criteria",
  connector_status: "Status update from a connected tool",
};

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

