/**
 * Test-only scripted job driver for browser checks against the protocol mock.
 *
 * It plays the worker role through the *private* job protocol (worker token, leases, fencing, checkpoints,
 * idempotency keys) so the UI can be exercised end-to-end before Agent B's worker is integrated. It is not
 * product code: plans are chosen per test by request subject, and the API still assigns validity, runs
 * transitions, applies local effects and issues receipts. At G2/G5 the real worker replaces it.
 */
import { randomUUID } from "node:crypto";

export type Kind = "answer" | "clarify" | "link_work" | "create_work" | "escalate";
export interface PlanStep {
  kind: Kind;
  /** Inject the mock's stale_next_proposal fault before preparing this proposal. */
  stale?: boolean;
  /** Inject local_create_timeout_after_effect before the dispatch of this step's action. */
  dispatchTimeout?: boolean;
}
type Json = Record<string, any>;

export class ScriptedDriver {
  private plans: { match: string; steps: PlanStep[] }[] = [];
  private timer: NodeJS.Timeout | null = null;
  private busy = false;
  private pendingTimeout = new Set<string>();
  readonly log: string[] = [];
  constructor(private base = process.env.PG_MOCK_URL ?? "http://localhost:8100", private ws = "ws_demo", private token = process.env.WORKER_TOKEN ?? "dev-worker-token") {}

  plan(subjectContains: string, steps: PlanStep[]) {
    this.plans.push({ match: subjectContains, steps: [...steps] });
  }

  async fault(body: Json) {
    const r = await fetch(`${this.base}/__mock/faults`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`fault ${r.status}`);
  }
  async reset() {
    const r = await fetch(`${this.base}/__mock/reset`, { method: "POST" });
    if (!r.ok) throw new Error(`reset ${r.status}`);
  }

  start(intervalMs = 200) {
    this.timer = setInterval(() => {
      if (this.busy) return;
      this.busy = true;
      this.tick().catch((e) => this.log.push(`error: ${String(e).slice(0, 300)}`)).finally(() => (this.busy = false));
    }, intervalMs);
  }
  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  private async http(method: string, path: string, body?: unknown, headers: Record<string, string> = {}, priv = false) {
    const url = `${this.base}/${priv ? "internal" : "api"}/v1/workspaces/${this.ws}${path}`;
    const h: Record<string, string> = { Authorization: `Bearer ${this.token}`, ...headers };
    if (body !== undefined) {
      h["Content-Type"] = "application/json";
      if (!path.endsWith("/claim")) h["Idempotency-Key"] = `drv-${randomUUID()}`;
    }
    const r = await fetch(url, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
    if (r.status === 204) return { status: 204, json: null as any };
    const json = await r.json().catch(() => null);
    return { status: r.status, json };
  }

  private async tick() {
    const c = await this.http("POST", "/jobs/claim", { worker_id: "browser-test-driver", kinds: ["investigate", "dispatch", "reconcile", "refresh"], lease_seconds: 60 }, {}, true);
    if (c.status === 204) return;
    if (c.status !== 200) throw new Error(`claim ${c.status} ${JSON.stringify(c.json)}`);
    const lease = c.json.data;
    const bind = { lease_token: lease.lease_token, fencing_token: lease.fencing_token };
    const leaseHeaders = { "X-Job-Id": lease.job_id, "X-Lease-Token": lease.lease_token, "X-Fencing-Token": String(lease.fencing_token) };
    const complete = async (outcome: string, result: Json) => {
      const r = await this.http("POST", `/jobs/${lease.job_id}/complete`, { ...bind, completion_id: `cmp-${randomUUID()}`, outcome, result }, {}, true);
      if (r.status !== 200) throw new Error(`complete ${r.status} ${JSON.stringify(r.json)}`);
      this.log.push(`${lease.kind} ${lease.job_id} -> ${outcome}`);
    };

    if (lease.kind === "dispatch") {
      const a = (await this.http("GET", `/actions/${lease.action_id}`)).json.data;
      if (this.pendingTimeout.delete(a.proposal_id)) await this.fault({ local_create_timeout_after_effect: 1 });
      const r = await this.http("POST", `/actions/${a.id}/execute-local`, { job_id: lease.job_id, ...bind, expected_action_version: a.version }, {}, true);
      if (r.status === 200) return complete("succeeded", { action_id: a.id, execution_state: r.json.data.status, receipt: r.json.data.receipt, error: null });
      if (r.status === 503) return complete("unknown", { action_id: a.id, execution_state: "unknown", receipt: null, error: r.json?.error ?? null });
      return complete("failed", { action_id: a.id, execution_state: "failed", receipt: null, error: r.json?.error ?? null });
    }
    if (lease.kind === "reconcile") {
      // Deliberately unresolved: leaves the action unknown for operator reconciliation.
      return complete("succeeded", { action_id: lease.action_id, lookup_evidence: { method: "test-driver: no lookup performed" }, established: false, receipt: null });
    }
    if (lease.kind === "refresh") return complete("succeeded", { work_item_ids: [], observation_ids: [], affected_request_ids: [] });

    // investigate
    const run = (await this.http("GET", `/runs/${lease.run_id}`)).json.data;
    let seq: number = run.event_seq;
    let calls = 0;
    const cp = async (type: string, payload: Json, tool: string | null, stage: string) => {
      const r = await this.http("POST", `/jobs/${lease.job_id}/checkpoint`, {
        ...bind, step_id: `step-${randomUUID()}`, expected_event_seq: seq,
        event: { type, tool_name: tool, payload },
        checkpoint: { completed_call_ids: [], pending_action_id: null, stage, usage: { tool_calls: calls, replans: run.usage.replans, active_ms: 0 } },
      }, {}, true);
      if (r.status !== 200) throw new Error(`checkpoint ${r.status} ${JSON.stringify(r.json)}`);
      seq = r.json.data.next_event_seq;
    };
    const tool = async (name: string, args: Json, fn: () => Promise<{ count: number; ids: string[] }>) => {
      const call_id = `call-${randomUUID().slice(0, 8)}`;
      calls++;
      await cp("tool_started", { call_id, arguments: args }, name, "investigate");
      const t0 = Date.now();
      const out = await fn();
      await cp("tool_completed", { call_id, ok: true, result_count: out.count, ids: out.ids.slice(0, 20), duration_ms: Date.now() - t0, error_code: null }, name, "investigate");
    };

    if (lease.checkpoint?.resume_stage === "observe") {
      await tool("inspect_action", { action_id: lease.checkpoint.action_id }, async () => ({ count: 1, ids: [lease.checkpoint.action_id] }));
      return complete("succeeded", { proposal_ids: [], summary: "Observed the executed action.", stage: "observe" });
    }

    const req = (await this.http("GET", `/requests/${run.request_id}`)).json.data;
    const plan = this.plans.find((p) => req.subject.includes(p.match));
    const step: PlanStep = plan?.steps.shift() ?? { kind: "escalate" };
    await cp("run_started", { mode: run.mode, stage: "investigate" }, null, "investigate");
    let ctx: Json = {};
    let revision = 0;
    await tool("read_memory", { request_id: req.id }, async () => {
      const r = await this.http("GET", `/requests/${req.id}/context`);
      ctx = r.json.data;
      revision = r.json.meta.knowledge_revision;
      return { count: ctx.sources.length, ids: ctx.sources.map((s: Json) => s.id) };
    });
    const ev: string[] = ctx.sources.slice(0, 2).map((s: Json) => s.id);
    const work = (ctx.related_work as Json[]).find((w) => w.status !== "cancelled" && !req.linked_work_ids.includes(w.id));
    const payloads: Record<Kind, Json> = {
      answer: { draft_text: `Draft for ${req.subject}: the documented procedure applies (see cited source).`, resolution_criteria: ["Customer confirms the procedure works"] },
      clarify: { question: "Do you mean account creation or group synchronization?", missing_fields: ["provisioning_type"] },
      link_work: { work_item_id: work?.id ?? "missing_work", comment_text: null },
      create_work: { title: `Support: ${req.subject}`.slice(0, 200), description: "Requested capability is not covered by existing work.", acceptance_criteria: ["Customer can complete the workflow", "Documented in the help center"], owner_id: null, capability_id: null, dependency_ids: [], connector: "local" },
      escalate: { reason: "No supported disposition found by the test driver.", owner_id: null, team_id: null },
    };
    if (step.stale) await this.fault({ stale_next_proposal: 1 });
    const alt = step.kind === "create_work" ? [{ kind: "link_work", reason: "No existing work covers this requirement.", evidence_ids: ev.slice(0, 1) }] : [{ kind: "create_work", reason: "Creating work is unnecessary for this disposition.", evidence_ids: [] }];
    let proposal: Json = {};
    await tool("prepare_action", { kind: step.kind }, async () => {
      const r = await this.http("POST", "/proposals", { request_id: req.id, run_id: run.id, kind: step.kind, payload: payloads[step.kind], evidence_ids: ev, explanation: `Test-driver plan: ${step.kind} for “${req.subject}”.`, rejected_alternatives: alt, based_on_revision: revision }, leaseHeaders);
      if (r.status !== 201 && r.status !== 200) throw new Error(`proposal ${r.status} ${JSON.stringify(r.json)}`);
      proposal = r.json.data;
      return { count: 1, ids: [proposal.id] };
    });
    await cp("proposal_ready", { proposal_id: proposal.id, kind: proposal.kind, validity: proposal.validity }, null, "decide");
    if (step.dispatchTimeout) this.pendingTimeout.add(proposal.id);
    if ((step.kind === "answer" || step.kind === "clarify") && proposal.validity === "valid") {
      const cur = (await this.http("GET", `/requests/${req.id}`)).json.meta.knowledge_revision;
      const r = await this.http("POST", `/proposals/${proposal.id}/commit`, { expected_revision: cur, authorization_id: null }, leaseHeaders);
      if (r.status !== 202) throw new Error(`commit ${r.status} ${JSON.stringify(r.json)}`);
      return complete("succeeded", { proposal_ids: [proposal.id], action_ids: [r.json.data.action.id], summary: `Committed ${step.kind} draft.` });
    }
    return complete("waiting", { proposal_ids: [proposal.id], awaiting: "review" });
  }
}
