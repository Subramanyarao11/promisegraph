import { useState } from "react";
import type { Action, ActionReconcile, EvidenceRef, ObservationCreate, ObservationResult, ReplyCreate, ReplyResult, Request, Run, RunCancel, RunCreate, RunResume } from "@contracts";
import { useCommand, keys, rememberRun, useInfo } from "../api/hooks";
import { ApiError } from "../api/client";
import { Icon } from "./Icon";
import { Dialog, ErrorPanel, Field, useAnnounce } from "./ui";
import { REQUEST_STATUS, RUN_STATUS, spec } from "../lib/status";

/* ---------------- clarification reply ---------------- */
export function ReplyForm({ request, question }: { request: Request; question: string | null }) {
  const announce = useAnnounce();
  const [body, setBody] = useState("");
  const [ext, setExt] = useState("");
  const [err, setErr] = useState<string | undefined>();
  const reply = useCommand<ReplyCreate, ReplyResult>("post", () => `/requests/${encodeURIComponent(request.id)}/replies`, () => [keys.request(request.id), keys.context(request.id), ["run"], ["actions"]]);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!body.trim()) { setErr("Enter the customer's reply."); return; }
    setErr(undefined);
    reply.mutate({ body: body.trim(), external_ref: ext.trim() || null, expected_request_version: request.version }, {
      onSuccess: (r) => {
        rememberRun(request.id, r.data.run.id);
        setBody(""); setExt("");
        announce(`Reply recorded as source ${r.data.source_id} rev ${r.data.source_revision}. Run ${r.data.run.id} is ${spec(RUN_STATUS, r.data.run.status).label.toLowerCase()}.`);
      },
      onError: (e) => announce(`Reply not recorded: ${(e as Error).message}`, true),
    });
  };
  const fe = reply.error instanceof ApiError ? reply.error.fieldErrors : {};
  return (
    <form onSubmit={submit} className="stack" noValidate data-testid="reply-form">
      {question && <p className="small">Question drafted for the customer: <em>“{question}”</em></p>}
      {reply.error && !(reply.error instanceof ApiError && reply.error.code === "INVALID_INPUT") && <ErrorPanel error={reply.error} />}
      <Field label="Customer reply" hint="Recorded as a new immutable source; the waiting run resumes with refreshed context." error={err ?? fe.body}>
        {(p) => <textarea {...p} rows={4} value={body} onChange={(e) => setBody(e.target.value)} maxLength={20000} />}
      </Field>
      <Field label="External reference (optional)" error={fe.external_ref}>
        {(p) => <input {...p} type="text" value={ext} onChange={(e) => setExt(e.target.value)} />}
      </Field>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={reply.isPending} data-testid="submit-reply">{reply.isPending ? "Recording…" : "Record reply and resume"}</button>
      </div>
    </form>
  );
}

/* ---------------- outcome verification ---------------- */
const OBS_KINDS: { value: ObservationCreate["kind"]; label: string; hint: string }[] = [
  { value: "customer_confirmation", label: "Customer confirmation", hint: "The customer confirmed the outcome." },
  { value: "operator_confirmation", label: "Operator confirmation", hint: "You checked the outcome yourself." },
  { value: "acceptance_check", label: "Acceptance check", hint: "The stated criteria were tested." },
  { value: "connector_status", label: "Connector status", hint: "External work status only — never resolves a request." },
];

export function ObservationForm({ request, actions, refs }: { request: Request; actions: Action[]; refs: EvidenceRef[] }) {
  const announce = useAnnounce();
  const [kind, setKind] = useState<ObservationCreate["kind"]>("customer_confirmation");
  const [verification, setVerification] = useState<ObservationCreate["verification"]>("passed");
  const [actionId, setActionId] = useState<string>(actions[0]?.id ?? "");
  const [body, setBody] = useState("");
  const [evidence, setEvidence] = useState<string[]>([]);
  const [err, setErr] = useState<string | undefined>();
  const obs = useCommand<ObservationCreate, ObservationResult>("post", () => `/requests/${encodeURIComponent(request.id)}/observations`, () => [keys.request(request.id), keys.context(request.id), ["actions"], ["run"], ["requests"]]);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!body.trim()) { setErr("Describe what was observed (who confirmed what, against which criteria)."); return; }
    setErr(undefined);
    obs.mutate({ action_id: actionId || null, kind, verification, body: body.trim(), evidence_ids: evidence, expected_request_version: request.version }, {
      onSuccess: (r) => {
        setBody(""); setEvidence([]);
        announce(`Observation ${r.data.observation.id} recorded (${r.data.observation.verification}). Request is now ${spec(REQUEST_STATUS, r.data.request.status).label}.`);
      },
      onError: (e) => announce(`Observation not recorded: ${(e as Error).message}`, true),
    });
  };
  const fe = obs.error instanceof ApiError ? obs.error.fieldErrors : {};
  return (
    <form onSubmit={submit} className="stack" noValidate data-testid="observation-form">
      {obs.error && !(obs.error instanceof ApiError && obs.error.code === "INVALID_INPUT") && <ErrorPanel error={obs.error} />}
      <div className="form-grid">
        <Field label="Observation kind" hint={OBS_KINDS.find((k) => k.value === kind)?.hint} error={fe.kind}>
          {(p) => (
            <select {...p} value={kind} onChange={(e) => setKind(e.target.value as ObservationCreate["kind"])}>
              {OBS_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </select>
          )}
        </Field>
        <Field label="Verification result" error={fe.verification}>
          {(p) => (
            <select {...p} value={verification} onChange={(e) => setVerification(e.target.value as ObservationCreate["verification"])}>
              <option value="passed">Passed</option>
              <option value="failed">Failed — reopen investigation</option>
              <option value="inconclusive">Inconclusive</option>
              <option value="pending">Pending</option>
            </select>
          )}
        </Field>
        <div className="span-2">
          <Field label="Related action" error={fe.action_id}>
            {(p) => (
              <select {...p} value={actionId} onChange={(e) => setActionId(e.target.value)}>
                <option value="">None</option>
                {actions.map((a) => <option key={a.id} value={a.id}>{a.id} · {a.kind} · {a.status}</option>)}
              </select>
            )}
          </Field>
        </div>
        <div className="span-2">
          <Field label="What was observed" error={err ?? fe.body}>
            {(p) => <textarea {...p} rows={3} value={body} onChange={(e) => setBody(e.target.value)} maxLength={4000} />}
          </Field>
        </div>
        {refs.length > 0 && (
          <fieldset className="span-2">
            <legend>Supporting evidence (optional)</legend>
            <div className="stack" style={{ maxHeight: 120, overflowY: "auto", gap: 4 }}>
              {refs.map((r) => (
                <label key={r.id} className="check small">
                  <input type="checkbox" checked={evidence.includes(r.id)} onChange={(e) => setEvidence(e.target.checked ? [...evidence, r.id] : evidence.filter((x) => x !== r.id))} />
                  {r.source_id} rev {r.source_revision} — {r.excerpt.slice(0, 70)}
                </label>
              ))}
            </div>
          </fieldset>
        )}
      </div>
      {kind === "connector_status" && verification === "passed" && <p className="small muted"><Icon name="info" size={12} /> A connector status cannot resolve the request; it is stored as external evidence.</p>}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={obs.isPending} data-testid="submit-observation">{obs.isPending ? "Recording…" : "Record observation"}</button>
      </div>
    </form>
  );
}

/* ---------------- run controls ---------------- */
export function RunControls({ request, run, onStarted }: { request: Request; run: Run | null; onStarted?: (r: Run) => void }) {
  const announce = useAnnounce();
  const [dialog, setDialog] = useState<null | "start" | "cancel" | "resume">(null);
  const [instruction, setInstruction] = useState("");
  const [reason, setReason] = useState("");
  const [mode, setMode] = useState<"scripted" | "model">("scripted");
  const info = useInfo();
  const modelState = info.data?.data.planners.model;
  const modelUsable = modelState === "enabled" || modelState === "verified" || modelState === "unverified";
  const start = useCommand<RunCreate, Run>("post", () => `/requests/${encodeURIComponent(request.id)}/runs`, () => [keys.request(request.id), ["run"]]);
  const cancel = useCommand<RunCancel & { _id: string }, Run>("post", (b) => `/runs/${encodeURIComponent(b._id)}/cancel`, (b) => [keys.run(b._id), keys.request(request.id)]);
  const resume = useCommand<RunResume & { _id: string }, Run>("post", (b) => `/runs/${encodeURIComponent(b._id)}/resume`, (b) => [keys.run(b._id), keys.request(request.id)]);
  const active = run && ["queued", "running", "waiting_for_input", "awaiting_review"].includes(run.status);
  const close = () => { setDialog(null); setReason(""); setInstruction(""); };

  const doStart = () =>
    start.mutate({ mode, instruction: instruction.trim(), authorization_id: null }, {
      onSuccess: (r) => { rememberRun(request.id, r.data.id); onStarted?.(r.data); announce(`Run ${r.data.id} ${r.data.status} (${r.data.mode === "scripted" ? "Scripted mode" : "live model"}).`); close(); },
      onError: (e) => announce(`Run not started: ${(e as Error).message}`, true),
    });
  const doCancel = () => run &&
    cancel.mutate({ _id: run.id, reason: reason.trim() || "Cancelled by operator" }, {
      onSuccess: () => { announce(`Run ${run.id} cancellation recorded. In-flight external operations are still observed.`); close(); },
      onError: (e) => announce(`Cancel refused: ${(e as Error).message}`, true),
    });
  const doResume = () => run &&
    resume.mutate({ _id: run.id, expected_run_version: run.version, reason: reason.trim() || "Operator resume", additional_tool_calls: 6, additional_active_seconds: 45 }, {
      onSuccess: () => { announce(`Run ${run.id} resumed from its checkpoint.`); close(); },
      onError: (e) => announce(`Resume refused: ${(e as Error).message}`, true),
    });

  return (
    <div className="row" role="group" aria-label="Run controls">
      <button type="button" className="btn btn-sm" onClick={() => setDialog("start")} disabled={Boolean(active)} data-testid="start-run" title={active ? "A run is already active" : undefined}>
        <Icon name="play" /> {run ? "New run" : "Start run"}
      </button>
      {active && (
        <button type="button" className="btn btn-sm btn-danger" onClick={() => setDialog("cancel")} data-testid="cancel-run"><Icon name="stop" /> Cancel run</button>
      )}
      {run?.status === "failed" && (
        <button type="button" className="btn btn-sm" onClick={() => setDialog("resume")} data-testid="resume-run"><Icon name="refresh" /> Resume from checkpoint</button>
      )}
      <Dialog open={dialog === "start"} onClose={close} title="Start an investigation run">
        <fieldset>
          <legend>Planner</legend>
          <div className="radio-row">
            <label><input type="radio" name="run-mode" checked={mode === "scripted"} onChange={() => setMode("scripted")} /> Scripted mode</label>
            <label><input type="radio" name="run-mode" checked={mode === "model"} disabled={!modelUsable} onChange={() => setMode("model")} /> Live model{modelUsable ? "" : ` (unavailable: ${modelState ?? "status unknown"})`}</label>
          </div>
        </fieldset>
        <p className="small">The run uses the real tools and re-reads current knowledge.</p>
        {start.error && <ErrorPanel error={start.error} />}
        <Field label="Instruction (optional)" hint="Up to 4,000 characters, e.g. what changed.">
          {(p) => <textarea {...p} rows={3} value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={4000} />}
        </Field>
        <div className="form-actions">
          <button type="button" className="btn" onClick={close}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={doStart} disabled={start.isPending} data-testid="confirm-start-run">{start.isPending ? "Starting…" : "Start run"}</button>
        </div>
      </Dialog>
      <Dialog open={dialog === "cancel"} onClose={close} title="Cancel this run?">
        <p className="small">Cancelling stops future planning. It does not cancel an external operation already in flight; receipts and reconciliation are still recorded.</p>
        {cancel.error && <ErrorPanel error={cancel.error} />}
        <Field label="Reason">{(p) => <input {...p} type="text" value={reason} onChange={(e) => setReason(e.target.value)} />}</Field>
        <div className="form-actions">
          <button type="button" className="btn" onClick={close}>Keep running</button>
          <button type="button" className="btn btn-danger" onClick={doCancel} disabled={cancel.isPending} data-testid="confirm-cancel-run">Cancel run</button>
        </div>
      </Dialog>
      <Dialog open={dialog === "resume"} onClose={close} title="Resume failed run">
        <p className="small">Resumes from the stored checkpoint with one budget extension (+6 tool calls, +45 s). Usage so far is preserved.</p>
        {run?.error && <p className="small"><strong>{run.error.code}</strong>: {run.error.message}</p>}
        {resume.error && <ErrorPanel error={resume.error} />}
        <Field label="Reason">{(p) => <input {...p} type="text" value={reason} onChange={(e) => setReason(e.target.value)} />}</Field>
        <div className="form-actions">
          <button type="button" className="btn" onClick={close}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={doResume} disabled={resume.isPending}>Resume</button>
        </div>
      </Dialog>
    </div>
  );
}

/* ---------------- operator reconciliation of an unknown dispatch (Rev 3 #20) ---------------- */
/** Rev 4 #28: the operator reports only what was found; the API reads the target back and issues the receipt. */
type ReconcileBody = ActionReconcile & { _id: string };
export function ReconcileForm({ action }: { action: Action }) {
  const announce = useAnnounce();
  const [outcome, setOutcome] = useState<"succeeded" | "failed">("failed");
  const [targetId, setTargetId] = useState("");
  const [targetUrl, setTargetUrl] = useState("");
  const [err, setErr] = useState<string | undefined>();
  const rec = useCommand<ReconcileBody, Action>("post", (b) => `/actions/${encodeURIComponent(b._id)}/reconcile`, () => [["actions"], ["run"], ["request"]]);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (outcome === "succeeded" && !targetId.trim()) { setErr("Enter the target ID you found (e.g. the task or issue identifier)."); return; }
    const target = outcome === "succeeded" ? { target_id: targetId.trim(), target_url: targetUrl.trim() || null } : null;
    setErr(undefined);
    rec.mutate({ _id: action.id, outcome, target, evidence_ids: [], expected_action_version: action.version }, {
      onSuccess: (r) => announce(`Action ${r.data.id} reconciled as ${r.data.status}.`),
      onError: (e2) => announce(`Reconciliation refused: ${(e2 as Error).message}`, true),
    });
  };
  return (
    <form onSubmit={submit} className="stack" noValidate data-testid="reconcile-form">
      <fieldset>
        <legend>Operator reconciliation — what did you find at the target?</legend>
        <div className="radio-row">
          <label><input type="radio" name={`rec-${action.id}`} checked={outcome === "failed"} onChange={() => setOutcome("failed")} /> Not created (confirmed failure)</label>
          <label><input type="radio" name={`rec-${action.id}`} checked={outcome === "succeeded"} onChange={() => setOutcome("succeeded")} /> Created — I found it</label>
        </div>
      </fieldset>
      {outcome === "succeeded" && (
        <>
          <Field label={`Target ID found on ${action.connector === "linear" ? "Linear" : "the local task list"}`} error={err}>
            {(p) => <input {...p} type="text" value={targetId} onChange={(e) => setTargetId(e.target.value)} />}
          </Field>
          <Field label="Target URL (optional)">{(p) => <input {...p} type="text" value={targetUrl} onChange={(e) => setTargetUrl(e.target.value)} />}</Field>
        </>
      )}
      {rec.error && <ErrorPanel error={rec.error} />}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={rec.isPending}>Record reconciliation</button>
      </div>
    </form>
  );
}

/* ---------------- explicit closure (Rev 3 #21) ---------------- */
export function CloseRequest({ request }: { request: Request }) {
  const announce = useAnnounce();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | undefined>();
  const close = useCommand<{ _id: string; reason: string; expected_request_version: number }, Request>("post", (b) => `/requests/${encodeURIComponent(b._id)}/close`, () => [keys.request(request.id), ["requests"]]);
  if (request.status === "closed") return null;
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) { setErr("A reason is required."); return; }
    close.mutate({ _id: request.id, reason: reason.trim(), expected_request_version: request.version }, {
      onSuccess: () => { announce(`Request ${request.id} closed.`); setOpen(false); },
      onError: (e2) => announce(`Close refused: ${(e2 as Error).message}`, true),
    });
  };
  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => setOpen(true)} data-testid="close-request"><Icon name="x" /> Close request</button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Close this request">
        <form onSubmit={submit} className="stack" noValidate>
          <p className="small">Closing is an explicit operator decision. It is not a verified resolution and history is kept.</p>
          {close.error && <ErrorPanel error={close.error} />}
          <Field label="Reason" error={err}>{(p) => <textarea {...p} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} />}</Field>
          <div className="form-actions">
            <button type="button" className="btn" onClick={() => setOpen(false)}>Cancel</button>
            <button type="submit" className="btn btn-danger" disabled={close.isPending}>Close request</button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
