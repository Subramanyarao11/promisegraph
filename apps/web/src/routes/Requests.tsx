import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import type { RequestCreate, RequestCreateResult } from "@contracts";
import { useCapabilities, useCommand, useCustomerName, useCustomers, useInfo, useRequests, rememberRun } from "../api/hooks";
import { ApiError } from "../api/client";
import { Icon } from "../components/Icon";
import { Dialog, Empty, ErrorPanel, Field, Loading, StatusBadge, Ts, useAnnounce, Mono } from "../components/ui";
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
          <p>Customer requests and where each one stands. Status words are the server's request state.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(true)} data-testid="new-request">
          <Icon name="plus" /> New request
        </button>
      </div>

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
            <Empty title="No requests in this workspace yet" icon="inbox">
              <p>Enter a new request, or import sources and requests from the Sources page.</p>
            </Empty>
          )
        ) : (
          <div className="table-wrap">
            <table className="table responsive" data-testid="request-table">
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Customer</th>
                  <th scope="col">Status</th>
                  <th scope="col">Linked work</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td data-label="Request">
                      <Link to={`/requests/${encodeURIComponent(r.id)}`} className="cell-title">{r.subject}</Link>
                      <div className="cell-sub"><Mono>{r.id}</Mono> · {r.channel}</div>
                    </td>
                    <td data-label="Customer">{customerName(r.customer_id)}</td>
                    <td data-label="Status"><StatusBadge map={REQUEST_STATUS} value={r.status} /></td>
                    <td data-label="Linked work">{r.linked_work_ids.length ? r.linked_work_ids.map((w) => <Mono key={w}>{w}</Mono>) : <span className="muted">None</span>}</td>
                    <td data-label="Updated"><Ts iso={r.updated_at} /></td>
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
        announce(`Request ${request.id} created${run ? ` and run ${run.id} ${run.status}` : ""}.`);
        onDone();
        nav(`/requests/${encodeURIComponent(request.id)}`);
      },
      onError: (e) => announce(`Request not created: ${(e as Error).message}`, true),
    });
  };

  return (
    <form onSubmit={submit} noValidate className="stack-lg" data-testid="new-request-form">
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
        <Field label="External reference (optional)" hint="Ticket or thread ID from the source system." error={err("external_ref")}>
          {(p) => <input {...p} type="text" value={form.external_ref} onChange={(e) => setForm({ ...form, external_ref: e.target.value })} maxLength={200} />}
        </Field>
        <div className="span-2">
          <Field label="Subject" error={err("subject")}>
            {(p) => <input {...p} type="text" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} maxLength={200} required />}
          </Field>
        </div>
        <div className="span-2">
          <Field label="Request body" hint="Paste the customer's words. This becomes an immutable source revision." error={err("body")}>
            {(p) => <textarea {...p} rows={6} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} maxLength={20000} required />}
          </Field>
        </div>
        {caps.data && caps.data.items.length > 0 && (
          <fieldset className="span-2">
            <legend>Known capabilities (optional — the server verifies links; unconfirmed ones stay candidates)</legend>
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
          <legend>Planner</legend>
          <div className="radio-row">
            <label>
              <input type="radio" name="planner_mode" value="scripted" checked={form.planner_mode === "scripted"} onChange={() => setForm({ ...form, planner_mode: "scripted" })} />
              Scripted mode
            </label>
            <label>
              <input type="radio" name="planner_mode" value="model" checked={form.planner_mode === "model"} disabled={!modelUsable} onChange={() => setForm({ ...form, planner_mode: "model" })} />
              Live model {modelUsable ? "" : `(unavailable: ${modelState ?? "status unknown"})`}
            </label>
          </div>
          <p className="hint">Scripted mode runs the real tools with a deterministic planner and is labelled as such everywhere.</p>
        </fieldset>
        <label className="check span-2">
          <input type="checkbox" checked={form.auto_start} onChange={(e) => setForm({ ...form, auto_start: e.target.checked })} />
          Start an investigation run immediately
        </label>
      </div>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={create.isPending} data-testid="submit-request">
          {create.isPending ? "Submitting…" : "Submit request"}
        </button>
      </div>
    </form>
  );
}
