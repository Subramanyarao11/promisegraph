import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import type { Attempt, ContextBundle, Decision, DecisionCommand, Request } from "@contracts";
import { useAttempts, useCommand, useDecisions } from "../api/hooks";
import { RuleValue, ScopeName } from "../components/Names";
import { api, ApiError, getLastMeta, qs } from "../api/client";
import { Icon } from "../components/Icon";
import { Dialog, Empty, ErrorPanel, Field, Loading, Mono, Owner, Section, StatusBadge, Tag, Ts, useAnnounce } from "../components/ui";
import { ATTEMPT_OUTCOME, DECISION_STATUS, RULE_LABEL, VERIFICATION } from "../lib/status";
import { humanize } from "../lib/format";

const RULES: DecisionCommand["rule_type"][] = ["forbidden_workaround", "scope_reduction_allowed", "date_extension_allowed", "allowed_action_types", "routing_owner"];
const RULE_HINT: Record<string, string> = {
  forbidden_workaround: "The name of the workaround not to suggest, e.g. recreate_schedule_in_utc.",
  scope_reduction_allowed: "Yes or no.",
  date_extension_allowed: "Yes or no.",
  allowed_action_types: "Comma-separated: answer, clarify, link_work, create_work, escalate.",
  routing_owner: "The person ID to send these requests to.",
};

export function MemoryPage() {
  const [tab, setTab] = useState<"decisions" | "attempts">("decisions");
  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Company rules &amp; history</h1>
          <p>The rules PromiseGraph follows for certain customers or products, and what was tried before. Changing a rule never erases the old one.</p>
        </div>
      </div>
      <div className="tabs" role="tablist" aria-label="Company rules and history">
        <button type="button" role="tab" id="tab-decisions" aria-selected={tab === "decisions"} aria-controls="panel-decisions" onClick={() => setTab("decisions")}>Company rules</button>
        <button type="button" role="tab" id="tab-attempts" aria-selected={tab === "attempts"} aria-controls="panel-attempts" onClick={() => setTab("attempts")}>What was tried before</button>
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "decisions" ? <Decisions /> : <Attempts />}
      </div>
    </>
  );
}

function Decisions() {
  const [activeOnly, setActiveOnly] = useState(false);
  const q = useDecisions(activeOnly ? { active: "true" } : {});
  const [dialog, setDialog] = useState<{ op: DecisionCommand["operation"]; target: Decision | null } | null>(null);
  const items = q.data?.items ?? [];
  const byId = useMemo(() => new Map(items.map((d) => [d.id, d])), [items]);
  // Chains: show each record with its predecessor link so history is readable.
  const sorted = [...items].sort((a, b) => b.effective_at.localeCompare(a.effective_at));
  return (
    <div className="stack-lg">
      <div className="row">
        <label className="check"><input type="checkbox" checked={activeOnly} onChange={(e) => setActiveOnly(e.target.checked)} /> Only rules in effect now</label>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn btn-primary" onClick={() => setDialog({ op: "record", target: null })}><Icon name="plus" /> Add a rule</button>
      </div>
      <Section title="Company rules" count={items.length} testId="decision-list">
        {q.isLoading ? <Loading label="Loading decisions" /> : q.error ? <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="decisions" /> : items.length === 0 ? (
          <Empty title="No company rules yet" icon="memory"><p>Rules tell PromiseGraph things like “don't suggest this workaround for this customer”.</p></Empty>
        ) : (
          <ul className="list">
            {sorted.map((d) => {
              const successor = items.find((x) => x.supersedes_id === d.id);
              return (
                <li key={d.id} className="stack" style={{ gap: 4 }} data-testid="decision">
                  <div className="row">
                    <strong>{RULE_LABEL[d.rule_type] ?? humanize(d.rule_type)}</strong>
                    <StatusBadge map={DECISION_STATUS} value={d.status} />
                    <Mono>{d.id}</Mono>
                    <span style={{ flex: 1 }} />
                    {d.status === "active" && (
                      <>
                        <button type="button" className="btn btn-sm" onClick={() => setDialog({ op: "supersede", target: d })} aria-label={`Replace rule ${d.id}`}>Replace</button>
                        <button type="button" className="btn btn-sm btn-danger" onClick={() => setDialog({ op: "revoke", target: d })} aria-label={`Withdraw rule ${d.id}`}>Withdraw</button>
                      </>
                    )}
                  </div>
                  <div className="small">Setting: <strong><RuleValue ruleType={d.rule_type} value={d.value} /></strong> · applies to <ScopeName scope={d.scope} /></div>
                  <div className="small">{d.rationale}</div>
                  <div className="small muted">
                    from <Ts iso={d.effective_at} /> · {d.expires_at ? <>until <Ts iso={d.expires_at} /></> : "no end date"} · added by <Owner id={d.author_id} />
                  </div>
                  {(d.supersedes_id || successor) && (
                    <div className="small row">
                      <Icon name="refresh" size={12} />
                      {d.supersedes_id && <span>replaces an earlier rule <Mono>{d.supersedes_id}</Mono>{byId.get(d.supersedes_id) ? ` (${byId.get(d.supersedes_id)!.status})` : ""}</span>}
                      {successor && <span>replaced by <Mono>{successor.id}</Mono></span>}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Section>
      <Dialog open={Boolean(dialog)} onClose={() => setDialog(null)} title={dialog?.op === "record" ? "Add a rule" : dialog?.op === "supersede" ? "Replace this rule" : "Withdraw this rule"}>
        {dialog && <DecisionForm op={dialog.op} target={dialog.target} revision={q.data ? (getLastMeta()?.knowledge_revision ?? 0) : 0} onDone={() => setDialog(null)} />}
      </Dialog>
    </div>
  );
}

function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 16);
}
function fromLocalInput(v: string): string {
  return new Date(v).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function DecisionForm({ op, target, revision, onDone }: { op: DecisionCommand["operation"]; target: Decision | null; revision: number; onDone: () => void }) {
  const announce = useAnnounce();
  const [rule, setRule] = useState<DecisionCommand["rule_type"]>(target?.rule_type ?? "forbidden_workaround");
  const [value, setValue] = useState(target ? (Array.isArray(target.value) ? (target.value as string[]).join(", ") : String(target.value)) : "");
  const [scopeType, setScopeType] = useState<DecisionCommand["scope"]["entity_type"]>(target?.scope.entity_type ?? "customer");
  const [scopeId, setScopeId] = useState(target?.scope.entity_id ?? "");
  const [rationale, setRationale] = useState("");
  const [effective, setEffective] = useState(toLocalInput(new Date().toISOString()));
  const [expires, setExpires] = useState("");
  const [errs, setErrs] = useState<Record<string, string>>({});
  const cmd = useCommand<DecisionCommand, Decision>("post", () => "/decisions", () => [["decisions"], ["context"], ["proposal"]]);
  const fe = cmd.error instanceof ApiError ? cmd.error.fieldErrors : {};
  const isBool = rule === "scope_reduction_allowed" || rule === "date_extension_allowed";
  const parsedValue = (): DecisionCommand["value"] => {
    if (isBool) return value === "true";
    if (rule === "allowed_action_types") return value.split(",").map((s) => s.trim()).filter(Boolean) as unknown as DecisionCommand["value"];
    return value.trim();
  };
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const ce: Record<string, string> = {};
    if (!rationale.trim()) ce.rationale = "Say why; this is kept with the rule.";
    if (op !== "revoke" && !isBool && !value.trim()) ce.value = "A value is required.";
    if (!scopeId.trim()) ce.scope = "Say which customer, request, product area or task it applies to.";
    setErrs(ce);
    if (Object.keys(ce).length) return;
    cmd.mutate({
      operation: op,
      decision_id: target?.id ?? null,
      rule_type: rule,
      value: op === "revoke" && target ? target.value : parsedValue(),
      scope: { entity_type: scopeType, entity_id: scopeId.trim() },
      rationale: rationale.trim(),
      effective_at: fromLocalInput(effective),
      expires_at: expires ? fromLocalInput(expires) : null,
      evidence_ids: [],
      expected_revision: revision,
    }, {
      onSuccess: () => { announce(op === "record" ? "Rule added." : op === "supersede" ? "Rule replaced. The old one is kept in the history." : "Rule withdrawn. It stays in the history."); onDone(); },
      onError: (e) => announce(`Rule not saved: ${(e as Error).message}`, true),
    });
  };
  const locked = op === "revoke";
  return (
    <form onSubmit={submit} className="stack" noValidate>
      {cmd.error && !(cmd.error instanceof ApiError && cmd.error.code === "INVALID_INPUT") && <ErrorPanel error={cmd.error} />}
      {op === "revoke" && <p className="small">The rule stops applying to new suggestions. It stays in the history.</p>}
      <div className="form-grid">
        <Field label="Kind of rule" error={fe.rule_type}>
          {(p) => <select {...p} value={rule} disabled={locked || op === "supersede"} onChange={(e) => { setRule(e.target.value as DecisionCommand["rule_type"]); setValue(""); }}>{RULES.map((r) => <option key={r} value={r}>{RULE_LABEL[r]}</option>)}</select>}
        </Field>
        <Field label="Setting" hint={RULE_HINT[rule]} error={errs.value ?? fe.value}>
          {(p) => isBool ? (
            <select {...p} value={value || "false"} disabled={locked} onChange={(e) => setValue(e.target.value)}><option value="true">Yes</option><option value="false">No</option></select>
          ) : (
            <input {...p} type="text" value={value} disabled={locked} onChange={(e) => setValue(e.target.value)} />
          )}
        </Field>
        <Field label="Applies to a" error={fe["scope/entity_type"]}>
          {(p) => <select {...p} value={scopeType} disabled={locked} onChange={(e) => setScopeType(e.target.value as DecisionCommand["scope"]["entity_type"])}>{["customer", "request", "capability", "work_item"].map((s) => <option key={s} value={s}>{humanize(s)}</option>)}</select>}
        </Field>
        <Field label="Which one (ID)" error={errs.scope ?? fe["scope/entity_id"]}>
          {(p) => <input {...p} type="text" value={scopeId} disabled={locked} onChange={(e) => setScopeId(e.target.value)} />}
        </Field>
        <Field label="Starts (your local time)" error={fe.effective_at}>
          {(p) => <input {...p} type="datetime-local" value={effective} onChange={(e) => setEffective(e.target.value)} />}
        </Field>
        <Field label="Ends (optional)" error={fe.expires_at}>
          {(p) => <input {...p} type="datetime-local" value={expires} disabled={locked} onChange={(e) => setExpires(e.target.value)} />}
        </Field>
        <div className="span-2">
          <Field label="Why?" error={errs.rationale ?? fe.rationale}>
            {(p) => <textarea {...p} rows={3} value={rationale} onChange={(e) => setRationale(e.target.value)} />}
          </Field>
        </div>
      </div>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
        <button type="submit" className={`btn ${op === "revoke" ? "btn-danger" : "btn-primary"}`} disabled={cmd.isPending}>{op === "record" ? "Add rule" : op === "supersede" ? "Replace rule" : "Withdraw rule"}</button>
      </div>
    </form>
  );
}

/* Attempts have no list route in contract 1.0.0; they are read from request contexts (bounded scan). */
function Attempts() {
  const direct = useAttempts();
  const q = useQuery({
    queryKey: ["attempts-scan"],
    enabled: direct.data?.supported === false,
    queryFn: async ({ signal }) => {
      const reqs = await api.list<Request>("/requests" + qs({ limit: 50 }), signal);
      const results = await Promise.allSettled(reqs.data.map((r) => api.get<ContextBundle>(`/requests/${encodeURIComponent(r.id)}/context`, signal)));
      const seen = new Map<string, Attempt>();
      let failed = 0;
      for (const r of results) {
        if (r.status === "fulfilled") r.value.data.attempts.forEach((a) => seen.set(a.id, a));
        else failed++;
      }
      return { attempts: [...seen.values()].sort((a, b) => b.observed_at.localeCompare(a.observed_at)), scanned: reqs.data.length, more: Boolean(reqs.page.next_cursor), failed };
    },
  });
  if (direct.isLoading) return <Loading label="Loading attempts" />;
  if (direct.error) return <ErrorPanel error={direct.error} onRetry={() => direct.refetch()} what="attempts" />;
  if (direct.data?.supported) return <AttemptList attempts={direct.data.items} note={direct.data.truncated ? "List truncated at 500 attempts." : null} />;
  if (q.isLoading) return <Loading label="Gathering attempts from request contexts" />;
  if (q.error) return <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="attempts" />;
  const d = q.data!;
  return <AttemptList attempts={d.attempts} note={`Gathered from the context of ${d.scanned} requests${d.more ? " (first page only — more requests exist)" : ""}${d.failed ? `; ${d.failed} could not be read` : ""}.`} />;
}

function AttemptList({ attempts, note }: { attempts: Attempt[]; note: string | null }) {
  const d = { attempts };
  return (
    <Section title="What was tried before" count={d.attempts.length} testId="attempt-list">
      <p className="small muted" style={{ marginBottom: 8 }}>{note ? `${note} ` : ""}Only results someone confirmed count as proof that something worked.</p>
      {d.attempts.length === 0 ? <Empty title="Nothing recorded yet" icon="memory"><p>When a fix or workaround is tried and someone confirms whether it worked, it shows up here.</p></Empty> : (
        <ul className="list">
          {d.attempts.map((a) => (
            <li key={a.id} className="stack" style={{ gap: 3 }}>
              <span className="row"><strong>{a.procedure_key.replace(/_/g, " ")}</strong> <StatusBadge map={ATTEMPT_OUTCOME} value={a.outcome} /> <StatusBadge map={VERIFICATION} value={a.verification} /></span>
              <span>{a.summary}</span>
              <span className="small muted"><Link to={`/requests/${encodeURIComponent(a.request_id)}`}>Open the request</Link> · for <ScopeName scope={a.scope} /> · <Ts iso={a.observed_at} /></span>
              {a.verification === "passed" && <span><Tag tone="success" icon="shield">Confirmed result</Tag></span>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
