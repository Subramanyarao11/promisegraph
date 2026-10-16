import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import type { RequestCreate, RequestCreateResult } from "@contracts";
import { useCapabilities, useCommand, useCustomerName, useCustomers, useInfo, useRequests, rememberRun } from "../api/hooks";
import { ApiError } from "../api/client";
import { Icon } from "../components/Icon";
import { Dialog, Empty, ErrorPanel, Field, Loading, StatusBadge, Ts, useAnnounce } from "../components/ui";
import { REQUEST_STATUS } from "../lib/status";

const STATUSES = Object.keys(REQUEST_STATUS);

export function RequestsPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const customer = params.get("customer") ?? "";
  const [open, setOpen] = useState(params.get("new") === "1");
  const customers = useCustomers();
  const customerName = useCustomerName();
  const q = useRequests({ status: status || undefined, customer_id: customer || undefined });
  const rows = useMemo(() => q.data?.pages.flatMap((p) => p.data) ?? [], [q.data]);

  const setFilter = (k: string, v: string) => {
    const n = new URLSearchParams(params);
    if (v) n.set(k, v);
    else n.delete(k);
    setParams(n, { replace: true });
  };

  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Requests</h1>
          <p>Every customer request and where it stands. Open one to see what PromiseGraph found and what it suggests.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(true)} data-testid="new-request">
          <Icon name="plus" /> New request
        </button>
      </div>
      <HowItWorks forceOpen={params.get("help") === "1"} onClose={() => { if (params.get("help")) setFilter("help", ""); }} />

      <div className="filters" role="search" aria-label="Filter requests">
        <Field label="Status">
          {(p) => (
            <select {...p} value={status} onChange={(e) => setFilter("status", e.target.value)}>
              <option value="">All statuses</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>{REQUEST_STATUS[s].label}</option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Customer">
          {(p) => (
            <select {...p} value={customer} onChange={(e) => setFilter("customer", e.target.value)}>
              <option value="">All customers</option>
              {customers.data?.items.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          )}
        </Field>
      </div>

      <section className="panel" aria-label="Request inbox">
        {q.isLoading ? (
          <div className="panel-body"><Loading label="Loading requests" /></div>
        ) : q.error ? (
          <div className="panel-body"><ErrorPanel error={q.error} onRetry={() => q.refetch()} what="requests" /></div>
        ) : rows.length === 0 ? (
          status || customer ? (
            <Empty title="No requests match these filters" icon="inbox">
              <button type="button" className="btn btn-sm" onClick={() => setParams({}, { replace: true })}>Clear filters</button>
            </Empty>
          ) : (
            <Empty title="No requests yet" icon="inbox">
              <p>Click <strong>New request</strong> and paste what a customer asked, or try one of the examples there.</p>
            </Empty>
          )
        ) : (
          <div className="table-wrap">
            <table className="table responsive" data-testid="request-table">
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Customer</th>
                  <th scope="col">Where it stands</th>
                  <th scope="col">Engineering work</th>
                  <th scope="col">Last update</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td data-label="Request">
                      <Link to={`/requests/${encodeURIComponent(r.id)}`} className="cell-title">{r.subject}</Link>
                      <div className="cell-sub">{r.body.length > 90 ? `${r.body.slice(0, 90)}…` : r.body}</div>
                    </td>
                    <td data-label="Customer">{customerName(r.customer_id)}</td>
                    <td data-label="Status"><StatusBadge map={REQUEST_STATUS} value={r.status} /></td>
                    <td data-label="Engineering work">{r.linked_work_ids.length ? <span>{r.linked_work_ids.length === 1 ? "Attached to 1 item" : `Attached to ${r.linked_work_ids.length} items`}</span> : <span className="muted">None</span>}</td>
                    <td data-label="Last update"><Ts iso={r.updated_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {q.hasNextPage && (
          <div className="panel-body">
            <button type="button" className="btn btn-sm" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>
              {q.isFetchingNextPage ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </section>

      <Dialog open={open} onClose={() => setOpen(false)} title="New customer request" wide testId="new-request-dialog">
        <NewRequestForm onDone={() => setOpen(false)} />
      </Dialog>
    </>
  );
}

function NewRequestForm({ onDone }: { onDone: () => void }) {
  const nav = useNavigate();
  const announce = useAnnounce();
  const customers = useCustomers();
  const caps = useCapabilities();
  const info = useInfo();
  const [form, setForm] = useState({ customer_id: "", subject: "", body: "", external_ref: "", auto_start: true, planner_mode: "scripted" as "scripted" | "model", capability_ids: [] as string[] });
  const [clientErr, setClientErr] = useState<Record<string, string>>({});
  const create = useCommand<RequestCreate, RequestCreateResult>("post", () => "/requests", () => [["requests"]]);
  const serverErr = create.error instanceof ApiError ? create.error.fieldErrors : {};
  const err = (k: string) => clientErr[k] ?? serverErr[k];
  const modelState = info.data?.data.planners.model;
  const modelUsable = modelState === "enabled" || modelState === "verified" || modelState === "unverified";

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const ce: Record<string, string> = {};
    if (!form.customer_id) ce.customer_id = "Choose the customer this request is from.";
    if (!form.subject.trim()) ce.subject = "Subject is required (1–200 characters).";
    if (!form.body.trim()) ce.body = "Describe the request (1–20,000 characters).";
    setClientErr(ce);
    if (Object.keys(ce).length) {
      announce("Request not submitted. Check the highlighted fields.", true);
      return;
    }
    const body: RequestCreate = {
      customer_id: form.customer_id,
      subject: form.subject.trim(),
      body: form.body.trim(),
      channel: "manual",
      capability_ids: form.capability_ids,
      external_ref: form.external_ref.trim() || null,
      auto_start: form.auto_start,
      planner_mode: form.planner_mode,
    };
    create.mutate(body, {
      onSuccess: (res) => {
        const { request, run } = res.data;
        if (run) rememberRun(request.id, run.id);
        announce(`Request created${run ? ". PromiseGraph is looking into it" : ""}.`);
        onDone();
        nav(`/requests/${encodeURIComponent(request.id)}`);
      },
      onError: (e) => announce(`Request not created: ${(e as Error).message}`, true),
    });
  };

  const tryExample = (ex: Example) => {
    const c = customers.data?.items.find((x) => x.name.toLowerCase().includes(ex.customer));
    setForm({ ...form, customer_id: c?.id ?? form.customer_id, subject: ex.subject, body: ex.body });
    setClientErr({});
    announce(`Example filled in: ${ex.label}. You can edit it before submitting.`);
  };

  return (
    <form onSubmit={submit} noValidate className="stack-lg" data-testid="new-request-form">
      <p className="small">Paste what the customer asked. PromiseGraph will look into it and suggest a next step for you to approve. Nothing is sent to the customer.</p>
      <fieldset data-testid="examples">
        <legend>Try an example</legend>
        <div className="row">
          {EXAMPLES.map((ex) => (
            <button key={ex.label} type="button" className="btn btn-sm" onClick={() => tryExample(ex)}>{ex.label}</button>
          ))}
        </div>
        <p className="hint">Examples only fill in the form. PromiseGraph decides the next step itself.</p>
      </fieldset>
      {create.error && !(create.error instanceof ApiError && create.error.code === "INVALID_INPUT") && <ErrorPanel error={create.error} />}
      <div className="form-grid">
        <Field label="Customer" error={err("customer_id")}>
          {(p) => (
            <select {...p} value={form.customer_id} onChange={(e) => setForm({ ...form, customer_id: e.target.value })} required>
              <option value="">{customers.isLoading ? "Loading customers…" : "Choose customer"}</option>
              {customers.data?.items.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          )}
        </Field>
        <Field label="Ticket or email reference (optional)" hint="So you can find it in your helpdesk later." error={err("external_ref")}>
          {(p) => <input {...p} type="text" value={form.external_ref} onChange={(e) => setForm({ ...form, external_ref: e.target.value })} maxLength={200} />}
        </Field>
        <div className="span-2">
          <Field label="Subject" error={err("subject")}>
            {(p) => <input {...p} type="text" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} maxLength={200} required />}
          </Field>
        </div>
        <div className="span-2">
          <Field label="Request body" hint="The customer's own words. PromiseGraph keeps this exact text as the source." error={err("body")}>
            {(p) => <textarea {...p} rows={6} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} maxLength={20000} required />}
          </Field>
        </div>
        {caps.data && caps.data.items.length > 0 && (
          <fieldset className="span-2">
            <legend>Which product areas is this about? (optional; PromiseGraph also works this out)</legend>
            <div className="radio-row" style={{ maxHeight: 120, overflowY: "auto" }}>
              {caps.data.items.map((c) => (
                <label key={c.id} className="check">
                  <input
                    type="checkbox"
                    checked={form.capability_ids.includes(c.id)}
                    onChange={(e) => setForm({ ...form, capability_ids: e.target.checked ? [...form.capability_ids, c.id] : form.capability_ids.filter((x) => x !== c.id) })}
                  />
                  {c.name}
                </label>
              ))}
            </div>
          </fieldset>
        )}
        <fieldset className="span-2">
          <legend>How should PromiseGraph think it through?</legend>
          <div className="radio-row">
            <label>
              <input type="radio" name="planner_mode" value="scripted" checked={form.planner_mode === "scripted"} onChange={() => setForm({ ...form, planner_mode: "scripted" })} />
              Rule-based mode
            </label>
            <label>
              <input type="radio" name="planner_mode" value="model" checked={form.planner_mode === "model"} disabled={!modelUsable} onChange={() => setForm({ ...form, planner_mode: "model" })} />
              AI model {modelUsable ? "" : "(not connected)"}
            </label>
          </div>
          <p className="hint">Rule-based mode follows fixed rules over your real data. It does not use an AI model.</p>
        </fieldset>
        <label className="check span-2">
          <input type="checkbox" checked={form.auto_start} onChange={(e) => setForm({ ...form, auto_start: e.target.checked })} />
          Start looking into it right away
        </label>
      </div>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={create.isPending} data-testid="submit-request">
          {create.isPending ? "Submitting…" : "Submit and look into it"}
        </button>
      </div>
    </form>
  );
}

interface Example { label: string; customer: string; subject: string; body: string }
/* Realistic requests from the demo scenarios (fixtures/scenarios t01, t02, t04, t06). They only prefill the form. */
const EXAMPLES: Example[] = [
  { label: "\"How do I…?\" question", customer: "corvid", subject: "Monthly PDF of the bed occupancy dashboard", body: "Could the Bed Occupancy dashboard be sent automatically as a PDF on the 1st of every month at 06:00 Berlin time to finance@corvid.example? Who on our side is allowed to set this up?" },
  { label: "Vague request", customer: "tamarind", subject: "Provisioning for store managers", body: "We need provisioning for our identity provider before 200 store managers join next month. Can you turn it on?" },
  { label: "Something broke", customer: "acme", subject: "Scheduled exports stopped working", body: "Our scheduled exports stopped working again this week. Finance needs the board pack exports before Friday. What is going on?" },
  { label: "New feature ask", customer: "corvid", subject: "Stream audit events to our SIEM", body: "Our security operations team needs admin and sign-in audit events streamed to our SIEM over syslog with TLS in near real time. A CSV download is not enough for continuous monitoring." },
];

const HOW_KEY = "pg.howItWorks.dismissed";
const STEPS: { icon: "inbox" | "graph" | "doc" | "check"; title: string; text: string }[] = [
  { icon: "inbox", title: "A customer asks", text: "Add their request, in their own words." },
  { icon: "graph", title: "It checks what we know", text: "Docs, open engineering work, what was tried before, and company rules." },
  { icon: "doc", title: "It suggests a next step", text: "Answer, ask a question, attach to existing work, create a task, or hand off — and says why." },
  { icon: "check", title: "You approve, then confirm", text: "Nothing happens without your approval. Mark it solved only once the customer confirms." },
];
function HowItWorks({ forceOpen, onClose }: { forceOpen: boolean; onClose: () => void }) {
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem(HOW_KEY) === "1"; } catch { return false; }
  });
  useEffect(() => { if (forceOpen) setDismissed(false); }, [forceOpen]);
  if (dismissed) return null;
  const dismiss = () => {
    try { localStorage.setItem(HOW_KEY, "1"); } catch { /* storage unavailable: dismiss for this visit only */ }
    setDismissed(true);
    onClose();
  };
  return (
    <section className="panel how" aria-labelledby="how-title" data-testid="how-it-works">
      <header className="panel-head">
        <h2 id="how-title">How it works</h2>
        <div className="panel-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={dismiss} data-testid="dismiss-how">Got it, hide this</button>
        </div>
      </header>
      <div className="panel-body">
        <p className="how-lead">PromiseGraph reads a customer request, checks what the company already knows, and suggests the best next step — which you approve.</p>
        <ol className="how-steps">
          {STEPS.map((st, i) => (
            <li key={st.title}>
              <span className="how-icon" aria-hidden="true"><Icon name={st.icon} size={18} /></span>
              <div>
                <p className="how-title"><span className="sr-only">Step {i + 1}: </span>{st.title}</p>
                <p className="small muted">{st.text}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
