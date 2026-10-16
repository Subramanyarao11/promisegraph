import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import type { Attempt, ContextBundle, Decision, DecisionCommand, Request } from "@contracts";
import { useAttempts, useCommand, useDecisions } from "../api/hooks";
import { api, ApiError, getLastMeta, qs } from "../api/client";
import { Icon } from "../components/Icon";
import { Dialog, Empty, ErrorPanel, Field, Loading, Mono, Section, StatusBadge, Tag, Ts, useAnnounce } from "../components/ui";
import { ATTEMPT_OUTCOME, DECISION_STATUS, VERIFICATION } from "../lib/status";
import { humanize } from "../lib/format";

const RULES: DecisionCommand["rule_type"][] = ["forbidden_workaround", "scope_reduction_allowed", "date_extension_allowed", "allowed_action_types", "routing_owner"];
const RULE_HINT: Record<string, string> = {
  forbidden_workaround: "Text: the procedure key that must not be repeated.",
  scope_reduction_allowed: "Yes/no.",
  date_extension_allowed: "Yes/no.",
  allowed_action_types: "Comma-separated action kinds, e.g. clarify, escalate.",
  routing_owner: "Text: the person or team ID to route to.",
};

export function MemoryPage() {
  const [tab, setTab] = useState<"decisions" | "attempts">("decisions");
  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Memory</h1>
          <p>Scoped decisions and observed outcomes the agent consults. History is retained: superseding or revoking never deletes a record.</p>
        </div>
      </div>
      <div className="tabs" role="tablist" aria-label="Memory">
        <button type="button" role="tab" id="tab-decisions" aria-selected={tab === "decisions"} aria-controls="panel-decisions" onClick={() => setTab("decisions")}>Decisions</button>
        <button type="button" role="tab" id="tab-attempts" aria-selected={tab === "attempts"} aria-controls="panel-attempts" onClick={() => setTab("attempts")}>Attempts &amp; outcomes</button>
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
        <label className="check"><input type="checkbox" checked={activeOnly} onChange={(e) => setActiveOnly(e.target.checked)} /> Active only</label>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn btn-primary" onClick={() => setDialog({ op: "record", target: null })}><Icon name="plus" /> Record decision</button>
      </div>
      <Section title="Decisions" count={items.length} testId="decision-list">
        {q.isLoading ? <Loading label="Loading decisions" /> : q.error ? <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="decisions" /> : items.length === 0 ? (
          <Empty title="No decisions recorded" icon="memory" />
        ) : (
          <ul className="list">
            {sorted.map((d) => {
              const successor = items.find((x) => x.supersedes_id === d.id);
              return (
                <li key={d.id} className="stack" style={{ gap: 4 }} data-testid="decision">
                  <div className="row">
                    <strong>{humanize(d.rule_type)}</strong>
                    <StatusBadge map={DECISION_STATUS} value={d.status} />
                    <Mono>{d.id}</Mono>
                    <span style={{ flex: 1 }} />
                    {d.status === "active" && (
                      <>
                        <button type="button" className="btn btn-sm" onClick={() => setDialog({ op: "supersede", target: d })} aria-label={`Supersede ${d.id}`}>Supersede</button>
                        <button type="button" className="btn btn-sm btn-danger" onClick={() => setDialog({ op: "revoke", target: d })} aria-label={`Revoke ${d.id}`}>Revoke</button>
                      </>
                    )}
                  </div>
                  <div className="small">value <Mono>{JSON.stringify(d.value)}</Mono> · scope {d.scope.entity_type} <Mono>{d.scope.entity_id}</Mono></div>
                  <div className="small">{d.rationale}</div>
                  <div className="small muted">
                    effective <Ts iso={d.effective_at} /> · {d.expires_at ? <>expires <Ts iso={d.expires_at} /></> : "no expiry"} · by <Mono>{d.author_id}</Mono> · v{d.version}
                  </div>
                  {(d.supersedes_id || successor) && (
                    <div className="small row">
                      <Icon name="refresh" size={12} />
                      {d.supersedes_id && <span>supersedes <Mono>{d.supersedes_id}</Mono>{byId.get(d.supersedes_id) ? ` (${byId.get(d.supersedes_id)!.status})` : ""}</span>}
                      {successor && <span>superseded by <Mono>{successor.id}</Mono></span>}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Section>
      <Dialog open={Boolean(dialog)} onClose={() => setDialog(null)} title={dialog?.op === "record" ? "Record decision" : dialog?.op === "supersede" ? `Supersede ${dialog.target?.id}` : `Revoke ${dialog?.target?.id ?? ""}`}>
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
    if (!rationale.trim()) ce.rationale = "Explain why; the rationale is kept with the history.";
    if (op !== "revoke" && !isBool && !value.trim()) ce.value = "A value is required.";
    if (!scopeId.trim()) ce.scope = "Scope entity ID is required.";
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
      onSuccess: (r) => { announce(`Decision ${r.data.id} ${op === "record" ? "recorded" : op === "supersede" ? "recorded; previous one superseded" : "revoked"}. Prior records are kept.`); onDone(); },
      onError: (e) => announce(`Decision not saved: ${(e as Error).message}`, true),
    });
  };
  const locked = op === "revoke";
  return (
    <form onSubmit={submit} className="stack" noValidate>
      {cmd.error && !(cmd.error instanceof ApiError && cmd.error.code === "INVALID_INPUT") && <ErrorPanel error={cmd.error} />}
      {op === "revoke" && <p className="small">Revoking keeps the record and its history; it stops applying to future plans.</p>}
      <div className="form-grid">
        <Field label="Rule type" error={fe.rule_type}>
          {(p) => <select {...p} value={rule} disabled={locked || op === "supersede"} onChange={(e) => { setRule(e.target.value as DecisionCommand["rule_type"]); setValue(""); }}>{RULES.map((r) => <option key={r} value={r}>{humanize(r)}</option>)}</select>}
        </Field>
        <Field label="Value" hint={RULE_HINT[rule]} error={errs.value ?? fe.value}>
          {(p) => isBool ? (
            <select {...p} value={value || "false"} disabled={locked} onChange={(e) => setValue(e.target.value)}><option value="true">Yes</option><option value="false">No</option></select>
          ) : (
            <input {...p} type="text" value={value} disabled={locked} onChange={(e) => setValue(e.target.value)} />
          )}
        </Field>
        <Field label="Scope type" error={fe["scope/entity_type"]}>
          {(p) => <select {...p} value={scopeType} disabled={locked} onChange={(e) => setScopeType(e.target.value as DecisionCommand["scope"]["entity_type"])}>{["customer", "request", "capability", "work_item"].map((s) => <option key={s} value={s}>{humanize(s)}</option>)}</select>}
        </Field>
        <Field label="Scope entity ID" error={errs.scope ?? fe["scope/entity_id"]}>
          {(p) => <input {...p} type="text" value={scopeId} disabled={locked} onChange={(e) => setScopeId(e.target.value)} />}
        </Field>
        <Field label="Effective (your local time)" error={fe.effective_at}>
          {(p) => <input {...p} type="datetime-local" value={effective} onChange={(e) => setEffective(e.target.value)} />}
        </Field>
        <Field label="Expires (optional)" error={fe.expires_at}>
          {(p) => <input {...p} type="datetime-local" value={expires} disabled={locked} onChange={(e) => setExpires(e.target.value)} />}
        </Field>
        <div className="span-2">
          <Field label="Rationale" error={errs.rationale ?? fe.rationale}>
            {(p) => <textarea {...p} rows={3} value={rationale} onChange={(e) => setRationale(e.target.value)} />}
          </Field>
        </div>
      </div>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
        <button type="submit" className={`btn ${op === "revoke" ? "btn-danger" : "btn-primary"}`} disabled={cmd.isPending}>{op === "record" ? "Record" : op === "supersede" ? "Supersede" : "Revoke"}</button>
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
    <Section title="Attempts & observed outcomes" count={d.attempts.length} testId="attempt-list">
      <p className="small muted" style={{ marginBottom: 8 }}>{note ? `${note} ` : ""}Only verified outcomes are evidence of resolution.</p>
      {d.attempts.length === 0 ? <Empty title="No attempts recorded" icon="memory" /> : (
        <ul className="list">
          {d.attempts.map((a) => (
            <li key={a.id} className="stack" style={{ gap: 3 }}>
              <span className="row"><Mono>{a.procedure_key}</Mono> <StatusBadge map={ATTEMPT_OUTCOME} value={a.outcome} /> <StatusBadge map={VERIFICATION} value={a.verification} /></span>
              <span>{a.summary}</span>
              <span className="small muted">request <Link to={`/requests/${encodeURIComponent(a.request_id)}`}>{a.request_id}</Link> · scope {a.scope.entity_type} <Mono>{a.scope.entity_id}</Mono> · context <Mono>{a.context_signature}</Mono> · <Ts iso={a.observed_at} /></span>
              {a.verification === "passed" && <span><Tag tone="success" icon="shield">Verified outcome</Tag></span>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
