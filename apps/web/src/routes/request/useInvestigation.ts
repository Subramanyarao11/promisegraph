import { useMemo, useRef } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import type { Action, Decision, EvidenceRef, Proposal, Run, RunCreate, RunEvent } from "@contracts";
import { api, isUnavailable } from "../../api/client";
import { typed } from "../../api/events";
import {
  keys, rememberRun, useActions, useCommand, useContextBundle, useInfo, useKnownRunIds, useObservations, useRequest,
  useRequestProposals, useRequestRuns, useRunTimelines, useSource,
} from "../../api/hooks";
import { useAnnounce } from "../../components/ui";
import { ACTIVE_RUN, KIND_LABEL, REQUEST_STATUS, RUN_STATUS, labelOf, spec } from "../../lib/status";

export interface ObservationRow {
  key: string;
  id: string;
  kind: string;
  verification: string;
  at: string;
  body: string | null;
}

/**
 * Everything the investigation page shows, from the public API only: request, context, runs (+ durable event
 * timelines), suggestions, actions and observations. Presentation lives in ./sections.tsx.
 */
export function useInvestigation(id: string) {
  const qc = useQueryClient();
  const announce = useAnnounce();
  const info = useInfo();
  const req = useRequest(id);
  const ctx = useContextBundle(id, Boolean(req.data));
  const actionsQ = useActions({ request_id: id }, { poll: 4000 });
  // Stable reference: it feeds the memoised proposal-ID set and the fallback run discovery.
  const actions = useMemo(() => actionsQ.data?.items ?? [], [actionsQ.data]);
  const obsQ = useObservations(id);
  const reqSource = useSource(req.data?.data.source_id ?? null, null);

  // Runs: GET /requests/{id}/runs (newest first); fallback for servers without it: remembered IDs + actions.
  const runsQ = useRequestRuns(id);
  const listedRuns = runsQ.data?.supported ? runsQ.data.runs : null;
  const fallbackIds = useKnownRunIds(id, actions);
  const runIds = listedRuns ? [...listedRuns].reverse().map((r) => r.id) : fallbackIds;
  const runQs = useQueries({
    queries: (listedRuns ? [] : runIds).map((rid) => ({
      queryKey: keys.run(rid),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<Run>(`/runs/${encodeURIComponent(rid)}`, signal),
      refetchInterval: (q: { state: { data?: { data: Run } } }) => (q.state.data && ACTIVE_RUN.has(q.state.data.data.status) ? 1500 : 6000),
    })),
  });
  // List routes are newest first; keep server order (timestamps can tie at one-second resolution).
  const runs: Run[] = listedRuns
    ? [...listedRuns].reverse()
    : runQs.flatMap((q) => (q.data && q.data.data.request_id === id ? [q.data.data] : [])).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const latestRun = runs.at(-1) ?? null;
  const activeRunIds = new Set(runs.filter((r) => ACTIVE_RUN.has(r.status)).map((r) => r.id));

  // Durable events drive refetches of the resources they mention, and announce milestones (not history).
  const lastAnnounced = useRef("");
  const onNewEvents = (evs: RunEvent[], initial: boolean) => {
    qc.invalidateQueries({ queryKey: keys.request(id) });
    qc.invalidateQueries({ queryKey: ["request-runs", id] });
    qc.invalidateQueries({ queryKey: ["request-proposals", id] });
    qc.invalidateQueries({ queryKey: ["actions"] });
    qc.invalidateQueries({ queryKey: ["proposal"] });
    for (const e of evs) qc.invalidateQueries({ queryKey: keys.run(e.run_id) });
    if (evs.some((e) => e.type === "tool_completed" || e.type === "run_finished" || e.type === "observation_recorded")) qc.invalidateQueries({ queryKey: keys.context(id) });
    if (initial) return;
    const milestone = evs.map(typed).find((e) => e.type === "run_finished" || e.type === "waiting_for_input" || e.type === "proposal_ready" || e.type === "error");
    if (!milestone) return;
    const key = `${milestone.run_id}:${milestone.seq}`;
    if (lastAnnounced.current === key) return;
    lastAnnounced.current = key;
    switch (milestone.type) {
      case "run_finished":
        announce(`Run status: ${spec(RUN_STATUS, milestone.payload.run_status).label}. Request status: ${spec(REQUEST_STATUS, milestone.payload.request_status).label}.`);
        break;
      case "waiting_for_input":
        announce("Run is waiting for the customer's input.");
        break;
      case "proposal_ready":
        announce(`Proposal ready: ${labelOf(KIND_LABEL, milestone.payload.kind)}.`);
        break;
      case "error":
        announce(`Run error: ${milestone.payload.code ?? "unknown"}.`, true);
        break;
    }
  };
  const timelines = useRunTimelines(runIds, activeRunIds, onNewEvents);
  const allEvents = useMemo(() => Object.values(timelines).flatMap((t) => t.events.map(typed)), [timelines]);

  // Suggestions: GET /proposals?request_id= ; fallback: IDs from proposal_ready events and actions.
  const proposalIds = useMemo(() => {
    const s = new Set<string>();
    for (const e of allEvents) if (e.type === "proposal_ready" && e.payload.proposal_id) s.add(e.payload.proposal_id);
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
  const proposals: Proposal[] = listedProps
    ? [...listedProps].reverse()
    : propQs.flatMap((q) => (q.data ? [q.data.data] : [])).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const proposal = proposals.at(-1) ?? null;
  const priorProposals = proposals.slice(0, -1).reverse();
  const actionFor = (p: Proposal | null): Action | null => (p ? actions.find((a) => a.proposal_id === p.id) ?? null : null);
  const action = actionFor(proposal);

  const startRun = useCommand<RunCreate, Run>("post", () => `/requests/${encodeURIComponent(id)}/runs`, () => [keys.request(id)]);
  const replan = () =>
    startRun.mutate({ mode: "scripted", instruction: "Look again: the earlier suggestion was out of date or blocked.", authorization_id: null }, {
      onSuccess: (r) => { rememberRun(id, r.data.id); announce("Looking into it again with the latest facts."); },
      onError: (e) => announce(`Couldn't start again: ${e.message}`, true),
    });

  const bundle = ctx.data?.data;
  const refs = new Map<string, EvidenceRef>((bundle?.sources ?? []).map((s) => [s.id, s]));
  const observations: ObservationRow[] = obsQ.data?.supported
    ? obsQ.data.items.map((o) => ({ key: o.id, id: o.id, kind: o.kind, verification: o.verification, at: o.observed_at, body: o.body }))
    : allEvents.flatMap((e) =>
        e.type === "observation_recorded"
          ? [{ key: `${e.run_id}:${e.seq}`, id: e.payload.observation_id ?? "", kind: e.payload.kind ?? "", verification: e.payload.verification ?? "", at: e.occurred_at, body: null }]
          : [],
      );
  const lastQuestion = allEvents.findLast((e) => e.type === "waiting_for_input");
  const waitingQuestion = lastQuestion?.type === "waiting_for_input" ? lastQuestion.payload.question ?? "" : proposal?.kind === "clarify" ? proposal.payload.question : null;

  // What shaped the recommendation: evidence it cites (incl. rejected options), its target work, IDs named in its reasons.
  const cited = {
    evidence: new Set<string>([...(proposal?.evidence_ids ?? []), ...(proposal?.rejected_alternatives ?? []).flatMap((a) => a.evidence_ids)]),
    work: new Set<string>([
      ...(proposal?.kind === "link_work" ? [proposal.payload.work_item_id] : []),
      ...(proposal?.kind === "create_work" ? proposal.payload.dependency_ids : []),
    ]),
    text: [proposal?.explanation ?? "", ...(proposal?.validation_reasons ?? []), ...(proposal?.rejected_alternatives ?? []).map((a) => a.reason)].join("\n"),
  };

  return {
    info, req, ctx, actionsQ, reqSource,
    bundle, refs, actions, runs, runIds, latestRun, timelines, allEvents,
    proposal, priorProposals, actionFor, action, observations,
    passed: observations.filter((o) => o.verification === "passed" && o.kind !== "connector_status"),
    waitingQuestion,
    cited,
    citedStale: (bundle?.sources ?? []).some((x) => x.stale && cited.evidence.has(x.id)),
    conflicts: findConflicts(bundle?.decisions ?? []),
    runActive: activeRunIds.size > 0,
    knowledgeRevision: ctx.data?.meta.knowledge_revision ?? req.data?.meta.knowledge_revision ?? null,
    unavailable: isUnavailable(ctx.error) || isUnavailable(actionsQ.error),
    startRun, replan,
  };
}
export type Investigation = ReturnType<typeof useInvestigation>;

/** Groups active decisions with the same rule type and scope but different values — a visible hint, not a resolution. */
function findConflicts(ds: readonly Decision[]): Decision[][] {
  const groups = new Map<string, Decision[]>();
  for (const d of ds) {
    if (d.status !== "active") continue;
    const k = `${d.rule_type}|${d.scope.entity_type}|${d.scope.entity_id}`;
    groups.set(k, [...(groups.get(k) ?? []), d]);
  }
  return [...groups.values()].filter((g) => g.length > 1 && new Set(g.map((d) => JSON.stringify(d.value))).size > 1);
}
