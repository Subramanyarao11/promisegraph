import { useEffect, useEffectEvent, useRef, useState, useSyncExternalStore } from "react";
import { skipToken, useInfiniteQuery, useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { RunPoller, type Timeline } from "./poller";
import type {
  Action, Attempt, CapabilitySummary, ContextBundle, Customer, Decision, EvidenceRef, Observation, Proposal, Request, Run, RunEvent, SourceRecord,
  SourceSummary, WorkItem, WorkspaceInfo,
} from "@contracts";
import { api, ApiError, newIdempotencyKey, qs, WORKSPACE_ID } from "./client";
import { ACTIVE_RUN } from "../lib/status";

const noRetryCodes = new Set(["NOT_FOUND", "UNAUTHORIZED", "WORKSPACE_FORBIDDEN", "INVALID_INPUT", "CAPABILITY_DISABLED"]);
export function retryPolicy(count: number, err: unknown) {
  if (err instanceof ApiError && (noRetryCodes.has(err.code) || err.code === "DEPENDENCY_UNAVAILABLE")) return false;
  return count < 1;
}

export const keys = {
  info: ["info"] as const,
  health: ["health"] as const,
  customers: ["customers"] as const,
  capabilities: ["capabilities"] as const,
  requests: (f: object) => ["requests", f] as const,
  request: (id: string) => ["request", id] as const,
  context: (id: string) => ["context", id] as const,
  actions: (f: object) => ["actions", f] as const,
  run: (id: string) => ["run", id] as const,
  proposal: (id: string) => ["proposal", id] as const,
  source: (id: string, rev: number | null) => ["source", id, rev] as const,
  work: (f: object) => ["work", f] as const,
  decisions: (f: object) => ["decisions", f] as const,
  sources: (f: object) => ["sources", f] as const,
};

export function useInfo() {
  return useQuery({ queryKey: keys.info, queryFn: ({ signal }) => api.get<WorkspaceInfo>("/info", signal), staleTime: 15_000, refetchInterval: 30_000 });
}

export interface Health {
  ok: boolean;
  status: number | null;
  body: unknown;
}
export function useHealth() {
  return useQuery({
    queryKey: keys.health,
    queryFn: async (): Promise<Health> => {
      try {
        const r = await fetch("/health/ready", { headers: { Accept: "application/json" } });
        let body: unknown = null;
        try { body = await r.json(); } catch { body = null; }
        return { ok: r.ok, status: r.status, body };
      } catch {
        return { ok: false, status: null, body: null };
      }
    },
    refetchInterval: (q) => (q.state.data && !q.state.data.ok ? 3000 : 15000),
    retry: false,
  });
}

/** Fetches every page of a bounded list (max 100 per page) up to a ceiling. */
async function listAll<T>(path: string, params: Record<string, string | number | null | undefined>, signal?: AbortSignal, ceiling = 500): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = [];
  let cursor: string | null = null;
  do {
    const page: { data: T[]; page: { next_cursor: string | null } } = await api.list<T>(path + qs({ ...params, limit: 100, cursor }), signal);
    items.push(...page.data);
    cursor = page.page?.next_cursor ?? null;
  } while (cursor && items.length < ceiling);
  return { items, truncated: Boolean(cursor) };
}

export function useCustomers() {
  return useQuery({ queryKey: keys.customers, queryFn: ({ signal }) => listAll<Customer>("/customers", {}, signal), staleTime: 60_000 });
}
export function useCustomerName() {
  const q = useCustomers();
  return (id: string) => q.data?.items.find((c) => c.id === id)?.name ?? id;
}
export function useCapabilities() {
  return useQuery({ queryKey: keys.capabilities, queryFn: ({ signal }) => listAll<CapabilitySummary>("/capabilities", {}, signal), staleTime: 60_000 });
}

export interface RequestFilters {
  status?: string;
  customer_id?: string;
}
export function useRequests(f: RequestFilters) {
  return useInfiniteQuery({
    queryKey: keys.requests(f),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.list<Request>("/requests" + qs({ status: f.status, customer_id: f.customer_id, limit: 25, cursor: pageParam }), signal),
    getNextPageParam: (last) => last.page?.next_cursor ?? null,
    refetchInterval: 10_000,
  });
}

export function useRequest(id: string) {
  return useQuery({ queryKey: keys.request(id), queryFn: ({ signal }) => api.get<Request>(`/requests/${encodeURIComponent(id)}`, signal), refetchInterval: 5000 });
}
export function useContextBundle(id: string, enabled = true) {
  return useQuery({ queryKey: keys.context(id), queryFn: ({ signal }) => api.get<ContextBundle>(`/requests/${encodeURIComponent(id)}/context`, signal), enabled });
}
export function useActions(f: { request_id?: string; status?: string }, opts: { poll?: number | false } = {}) {
  return useQuery({ queryKey: keys.actions(f), queryFn: ({ signal }) => listAll<Action>("/actions", f, signal), refetchInterval: opts.poll ?? false });
}
export function useRun(id: string | null) {
  return useQuery({
    queryKey: keys.run(id ?? ""),
    queryFn: id ? ({ signal }) => api.get<Run>(`/runs/${encodeURIComponent(id)}`, signal) : skipToken,
    refetchInterval: (q) => (q.state.data && ACTIVE_RUN.has(q.state.data.data.status) ? 1500 : 8000),
  });
}
export function useProposal(id: string | null) {
  return useQuery({ queryKey: keys.proposal(id ?? ""), queryFn: id ? ({ signal }) => api.get<Proposal>(`/proposals/${encodeURIComponent(id)}`, signal) : skipToken, refetchInterval: 5000 });
}
export function useSource(id: string | null, revision: number | null) {
  return useQuery({
    queryKey: keys.source(id ?? "", revision),
    queryFn: id ? ({ signal }) => api.get<SourceRecord>(`/sources/${encodeURIComponent(id)}` + qs({ revision }), signal) : skipToken,
    staleTime: Infinity,
  });
}
export function useWorkItems(f: { status?: string; customer_id?: string }) {
  return useQuery({ queryKey: keys.work(f), queryFn: ({ signal }) => listAll<WorkItem>("/work-items", f, signal), refetchInterval: 15_000 });
}
export function useDecisions(f: { scope_entity_type?: string; scope_entity_id?: string; active?: string }) {
  return useQuery({ queryKey: keys.decisions(f), queryFn: ({ signal }) => listAll<Decision>("/decisions", f, signal) });
}
export function useSources(f: { kind?: string; origin?: string }) {
  return useQuery({ queryKey: keys.sources(f), queryFn: ({ signal }) => listAll<SourceSummary>("/sources", f, signal) });
}

/* ---------- known runs per request ----------
 * The public contract (1.0.0) has no "list runs for a request" read. Run IDs are learned from command
 * results (intake, start run, reply) and from actions (Action.run_id). The browser remembers IDs it has
 * seen so a refresh can re-read them from the API; all state shown is still re-fetched from the server. */
const runKey = (requestId: string) => `pg.runs.${WORKSPACE_ID}.${requestId}`;
export function rememberRun(requestId: string, runId: string) {
  try {
    const cur: string[] = JSON.parse(localStorage.getItem(runKey(requestId)) ?? "[]");
    if (!cur.includes(runId)) localStorage.setItem(runKey(requestId), JSON.stringify([...cur, runId]));
  } catch { /* storage unavailable: run IDs still come from actions */ }
  window.dispatchEvent(new CustomEvent("pg:runs", { detail: requestId }));
}
/* Browser-remembered run IDs are an external store (localStorage + "storage"/"pg:runs" events). */
function subscribeRuns(onChange: () => void): () => void {
  window.addEventListener("pg:runs", onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener("pg:runs", onChange);
    window.removeEventListener("storage", onChange);
  };
}
function readRunsRaw(requestId: string): string {
  try { return localStorage.getItem(runKey(requestId)) ?? "[]"; } catch { return "[]"; }
}
function parseRunIds(raw: string): string[] {
  try {
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch { return []; }
}
export function useKnownRunIds(requestId: string, actions: Action[] | undefined): string[] {
  // Snapshot is the raw string (stable between reads), parsed during render.
  const raw = useSyncExternalStore(subscribeRuns, () => readRunsRaw(requestId), () => "[]");
  const ids = parseRunIds(raw);
  for (const a of actions ?? []) if (!ids.includes(a.run_id)) ids.push(a.run_id);
  return ids;
}

/* ---------- durable event timelines for a set of runs ----------
 * One effect keeps a RunPoller per run ID in sync with the list (the pollers are the external system);
 * their callbacks write timeline state. No parent-notification effects. */
export type { Timeline } from "./poller";
export function useRunTimelines(runIds: string[], activeIds: ReadonlySet<string>, onNew: (events: RunEvent[], initial: boolean) => void): Record<string, Timeline> {
  const [timelines, setTimelines] = useState<Record<string, Timeline>>({});
  const pollers = useRef(new Map<string, RunPoller>());
  const handleNew = useEffectEvent(onNew);
  const idsKey = runIds.join("|");
  const activeKey = [...activeIds].sort().join("|");

  useEffect(function syncPollers() {
    const wanted = new Set(idsKey ? idsKey.split("|") : []);
    const map = pollers.current;
    for (const id of wanted) {
      if (!map.has(id)) map.set(id, new RunPoller(id, (t) => setTimelines((s) => ({ ...s, [id]: t })), (evs, initial) => handleNew(evs, initial)));
    }
    for (const [id, p] of map) {
      if (!wanted.has(id)) {
        p.stop();
        map.delete(id);
      }
    }
  }, [idsKey]);

  useEffect(function syncActivity() {
    const active = new Set(activeKey ? activeKey.split("|") : []);
    for (const [id, p] of pollers.current) p.setActive(active.has(id));
  }, [activeKey, idsKey]);

  useEffect(function stopAllOnUnmount() {
    const map = pollers.current;
    return () => {
      for (const p of map.values()) p.stop();
      map.clear();
    };
  }, []);

  return timelines;
}

/* ---------- commands with idempotency keys ----------
 * One key per intent: retrying the same body after a lost response reuses the key (server replays the
 * original result); any change to the body is a new intentional command with a new key. */
export function useCommand<TBody extends object, TResult>(method: "post" | "patch", pathFn: (b: TBody) => string, invalidate: (b: TBody, r: TResult | undefined) => QueryKey[]) {
  const qc = useQueryClient();
  const last = useRef<{ hash: string; key: string } | null>(null);
  return useMutation({
    mutationFn: async (body: TBody) => {
      // Keys prefixed with "_" are client-side routing params (e.g. path IDs), never part of the contract body.
      const wire = Object.fromEntries(Object.entries(body).filter(([k]) => !k.startsWith("_")));
      const hash = `${pathFn(body)}|${JSON.stringify(wire)}`;
      if (!last.current || last.current.hash !== hash) last.current = { hash, key: newIdempotencyKey() };
      const res = await api[method]<TResult>(pathFn(body), wire, last.current.key);
      last.current = null;
      return res;
    },
    onSettled: (res, _err, body) => {
      const ks = invalidate(body, res?.data);
      ks.forEach((k) => qc.invalidateQueries({ queryKey: k }));
      qc.invalidateQueries({ queryKey: ["request-runs"] });
      qc.invalidateQueries({ queryKey: ["request-proposals"] });
      qc.invalidateQueries({ queryKey: ["observations"] });
      qc.invalidateQueries({ queryKey: ["attempts"] });
    },
  });
}

/* ---------- G0 rev 2 list routes ----------
 * GET /requests/{id}/runs and GET /proposals?request_id= (newest first). Until a server implements them
 * (route missing → 404/405-style error), fall back to run IDs learned from command results and actions,
 * and proposal IDs from durable proposal_ready events. */
function routeMissing(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 404 || e.status === 405 || (e.code === "INVALID_INPUT" && /not allowed|not found/i.test(e.message)));
}
export function useRequestRuns(requestId: string) {
  return useQuery({
    queryKey: ["request-runs", requestId],
    queryFn: async ({ signal }) => {
      try {
        const r = await listAll<Run>(`/requests/${encodeURIComponent(requestId)}/runs`, {}, signal, 100);
        return { runs: r.items, supported: true };
      } catch (e) {
        if (routeMissing(e)) return { runs: [] as Run[], supported: false };
        throw e;
      }
    },
    refetchInterval: (q) => (q.state.data?.runs.some((r) => ACTIVE_RUN.has(r.status)) ? 1500 : 5000),
    retry: retryPolicy,
  });
}
export function useRequestProposals(requestId: string) {
  return useQuery({
    queryKey: ["request-proposals", requestId],
    queryFn: async ({ signal }) => {
      try {
        const r = await listAll<Proposal>(`/proposals`, { request_id: requestId }, signal, 200);
        return { proposals: r.items, supported: true };
      } catch (e) {
        if (routeMissing(e)) return { proposals: [] as Proposal[], supported: false };
        throw e;
      }
    },
    refetchInterval: 4000,
  });
}

/* ---------- Rev 3 additive reads ---------- */
export function useEvidence(id: string | null) {
  return useQuery({ queryKey: ["evidence", id], queryFn: id ? ({ signal }) => api.get<EvidenceRef>(`/evidence/${encodeURIComponent(id)}`, signal) : skipToken, staleTime: Infinity });
}
export function useWorkItem(id: string | null) {
  return useQuery({ queryKey: ["work-item", id], queryFn: id ? ({ signal }) => api.get<WorkItem>(`/work-items/${encodeURIComponent(id)}`, signal) : skipToken, refetchInterval: 10_000 });
}
export function useObservations(requestId: string) {
  return useQuery({
    queryKey: ["observations", requestId],
    queryFn: async ({ signal }) => {
      try {
        return { items: (await listAll<Observation>("/observations", { request_id: requestId }, signal)).items, supported: true };
      } catch (e) {
        if (routeMissing(e)) return { items: [] as Observation[], supported: false };
        throw e;
      }
    },
    refetchInterval: 5000,
  });
}
export function useAttempts(requestId?: string) {
  return useQuery({
    queryKey: ["attempts", requestId ?? "all"],
    queryFn: async ({ signal }) => {
      try {
        const r = await listAll<Attempt>("/attempts", { request_id: requestId }, signal);
        return { items: r.items, truncated: r.truncated, supported: true };
      } catch (e) {
        if (routeMissing(e) || (e instanceof ApiError && e.code === "INVALID_INPUT" && !requestId)) return { items: [] as Attempt[], truncated: false, supported: false };
        throw e;
      }
    },
  });
}
