import { useState } from "react";
import { useSearchParams } from "react-router";
import type { WorkItem, WorkItemUpdate, WorkItemUpdateResult, WorkStatus } from "@contracts";
import { oneOf } from "../lib/format";
import { useCommand, useWorkItems } from "../api/hooks";
import { ApiError } from "../api/client";
import { Icon } from "../components/Icon";
import { Dialog, Empty, ErrorPanel, Field, Loading, Mono, Owner, StatusBadge, Tag, Ts, useAnnounce } from "../components/ui";
import { CONNECTOR_LABEL, WORK_STATUS } from "../lib/status";

const WORK_STATUSES = ["open", "in_progress", "blocked", "done", "cancelled"] as const satisfies readonly WorkStatus[];

export function WorkPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const q = useWorkItems({ status: status || undefined });
  const [editing, setEditing] = useState<WorkItem | null>(null);
  const items = q.data?.items ?? [];
  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Work</h1>
          <p>Engineering tasks that requests are waiting on: who owns each one and when it was last checked. A task being done doesn't mean the customer is happy; you confirm that on the request.</p>
        </div>
      </div>
      <div className="filters" role="search" aria-label="Filter work">
        <Field label="Status">
          {(p) => (
            <select {...p} value={status} onChange={(e) => setParams(e.target.value ? { status: e.target.value } : {}, { replace: true })}>
              <option value="">All statuses</option>
              {Object.entries(WORK_STATUS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
          )}
        </Field>
      </div>
      <section className="panel" aria-label="Work items">
        {q.isLoading ? <div className="panel-body"><Loading label="Loading work" /></div> : q.error ? <div className="panel-body"><ErrorPanel error={q.error} onRetry={() => q.refetch()} what="work items" /></div> : items.length === 0 ? (
          <Empty title={status ? "No work with this status" : "No engineering work yet"} icon="work"><p>Tasks appear here when they're added to the knowledge base, or when you approve “Create a new task” on a request.</p></Empty>
        ) : (
          <div className="table-wrap">
            <table className="table responsive" data-testid="work-table">
              <thead>
                <tr><th scope="col">Task</th><th scope="col">Status</th><th scope="col">Where it lives</th><th scope="col">Owner</th><th scope="col">Expected</th><th scope="col">Last checked</th><th scope="col"><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {items.map((w) => (
                  <tr key={w.id} data-testid={`work-row-${w.id}`}>
                    <td data-label="Task"><span className="cell-title">{w.title}</span><div className="cell-sub">{w.kind === "feature" ? "Feature" : w.kind === "ticket" ? "Ticket" : "Milestone"}</div></td>
                    <td data-label="Status"><StatusBadge map={WORK_STATUS} value={w.status} /></td>
                    <td data-label="Where it lives">
                      <span className="row">
                        <Tag icon={w.connector === "linear" ? "external" : "work"} tone={w.connector === "linear" ? "info" : "neutral"}>{CONNECTOR_LABEL[w.connector]}</Tag>
                        {w.external_id && <Mono>{w.external_id}</Mono>}
                        {w.external_url && <a href={w.external_url} target="_blank" rel="noreferrer">Open<span className="sr-only"> {w.title} in Linear (new tab)</span></a>}
                      </span>
                    </td>
                    <td data-label="Owner"><Owner id={w.owner_id} /></td>
                    <td data-label="Expected">{w.forecast_at ? <Ts iso={w.forecast_at} /> : <span className="muted">No date</span>}</td>
                    <td data-label="Last checked"><Ts iso={w.observed_at} /></td>
                    <td>
                      {w.connector === "local" ? (
                        <button type="button" className="btn btn-sm" onClick={() => setEditing(w)} aria-label={`Update ${w.title}`}>Update</button>
                      ) : (
                        <span className="small muted">Managed in Linear</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {q.data?.truncated && <p className="panel-body small muted">List truncated at 500 items.</p>}
      </section>
      <Dialog open={Boolean(editing)} onClose={() => setEditing(null)} title={editing ? `Update ${editing.title}` : "Update work"}>
        {editing && <WorkUpdateForm w={editing} onDone={() => setEditing(null)} />}
      </Dialog>
    </>
  );
}

function WorkUpdateForm({ w, onDone }: { w: WorkItem; onDone: () => void }) {
  const announce = useAnnounce();
  const [status, setStatus] = useState(w.status);
  const [owner, setOwner] = useState(w.owner_id ?? "");
  const [forecast, setForecast] = useState(w.forecast_at ? w.forecast_at.slice(0, 16) : "");
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | undefined>();
  const upd = useCommand<WorkItemUpdate & { _id: string }, WorkItemUpdateResult>("patch", (b) => `/work-items/${encodeURIComponent(b._id)}`, () => [["work"], ["context"], ["proposal"]]);
  const fe = upd.error instanceof ApiError ? upd.error.fieldErrors : {};
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) { setErr("Say why it changed; this is kept in the history."); return; }
    setErr(undefined);
    upd.mutate({ _id: w.id, expected_version: w.version, status, owner_id: owner.trim() || null, forecast_at: forecast ? new Date(forecast).toISOString().replace(/\.\d{3}Z$/, "Z") : null, reason: reason.trim() }, {
      onSuccess: () => { announce("Task updated. Any suggestion that relied on the old details will be re-checked."); onDone(); },
      onError: (e) => announce(`Update refused: ${e.message}`, true),
    });
  };
  return (
    <form onSubmit={submit} className="stack" noValidate>
      {upd.error && !(upd.error instanceof ApiError && upd.error.code === "INVALID_INPUT") && <ErrorPanel error={upd.error} />}
      <Field label="Status" error={fe.status}>
        {(p) => <select {...p} value={status} onChange={(e) => setStatus(oneOf(WORK_STATUSES, e.target.value, status))}>{Object.entries(WORK_STATUS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>}
      </Field>
      <Field label="Owner (person ID)" hint="Leave empty if nobody owns it yet." error={fe.owner_id}>
        {(p) => <input {...p} type="text" value={owner} onChange={(e) => setOwner(e.target.value)} />}
      </Field>
      <Field label="Expected date (your local time)" error={fe.forecast_at}>
        {(p) => <input {...p} type="datetime-local" value={forecast} onChange={(e) => setForecast(e.target.value)} />}
      </Field>
      <Field label="Why the change?" error={err ?? fe.reason}>
        {(p) => <textarea {...p} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} />}
      </Field>
      <p className="small muted"><Icon name="info" size={12} /> Suggestions that relied on the old owner or status will be marked out of date.</p>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={upd.isPending}>Save update</button>
      </div>
    </form>
  );
}
