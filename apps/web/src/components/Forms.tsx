import { useState } from "react";
import type { Action, ActionReconcile, EvidenceRef, ObservationCreate, ObservationResult, ReplyCreate, ReplyResult, Request, Run, RunCancel, RunCreate, RunResume } from "@contracts";
import { useCommand, keys, rememberRun, useInfo } from "../api/hooks";
import { ApiError } from "../api/client";
import { Icon } from "./Icon";
import { Dialog, ErrorPanel, Field, useAnnounce } from "./ui";
import { ACTION_STATUS, KIND_LABEL, REQUEST_STATUS, RUN_STATUS, spec } from "../lib/status";

/* ---------------- clarification reply ---------------- */
export function ReplyForm({ request, question }: { request: Request; question: string | null }) {
  const announce = useAnnounce();
  const [body, setBody] = useState("");
  const [ext, setExt] = useState("");
  const [err, setErr] = useState<string | undefined>();
  const reply = useCommand<ReplyCreate, ReplyResult>("post", () => `/requests/${encodeURIComponent(request.id)}/replies`, () => [keys.request(request.id), keys.context(request.id), ["run"], ["actions"]]);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!body.trim()) { setErr("Paste the customer's reply first."); return; }
    setErr(undefined);
    reply.mutate({ body: body.trim(), external_ref: ext.trim() || null, expected_request_version: request.version }, {
      onSuccess: (r) => {
        rememberRun(request.id, r.data.run.id);
        setBody(""); setExt("");
        announce(`Reply recorded. PromiseGraph is continuing (${spec(RUN_STATUS, r.data.run.status).label.toLowerCase()}).`);
      },
      onError: (e) => announce(`Reply not recorded: ${(e as Error).message}`, true),
    });
  };
  const fe = reply.error instanceof ApiError ? reply.error.fieldErrors : {};
  return (
    <form onSubmit={submit} className="stack" noValidate data-testid="reply-form">
      {question && <p className="small">We asked (as a draft for you to send): <em>“{question}”</em></p>}
      {reply.error && !(reply.error instanceof ApiError && reply.error.code === "INVALID_INPUT") && <ErrorPanel error={reply.error} />}
      <Field label="Customer reply" hint="Paste what they said. PromiseGraph continues the same investigation with it." error={err ?? fe.body}>
        {(p) => <textarea {...p} rows={4} value={body} onChange={(e) => setBody(e.target.value)} maxLength={20000} />}
      </Field>
      <Field label="Ticket or email reference (optional)" error={fe.external_ref}>
        {(p) => <input {...p} type="text" value={ext} onChange={(e) => setExt(e.target.value)} />}
      </Field>
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={reply.isPending} data-testid="submit-reply">{reply.isPending ? "Saving…" : "Save reply and continue"}</button>
      </div>
    </form>
  );
}

/* ---------------- outcome verification ---------------- */
const OBS_KINDS: { value: ObservationCreate["kind"]; label: string; hint: string }[] = [
  { value: "customer_confirmation", label: "The customer told us", hint: "The customer confirmed whether it works." },
  { value: "operator_confirmation", label: "I checked it myself", hint: "You tested it yourself." },
  { value: "acceptance_check", label: "We tested it against the criteria", hint: "Someone ran the agreed checks." },
  { value: "connector_status", label: "Status update from a tool (e.g. Linear)", hint: "Only a status update. This never marks a request solved." },
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
    if (!body.trim()) { setErr("Say what happened, e.g. who confirmed it and what they checked."); return; }
    setErr(undefined);
    obs.mutate({ action_id: actionId || null, kind, verification, body: body.trim(), evidence_ids: evidence, expected_request_version: request.version }, {
      onSuccess: (r) => {
        setBody(""); setEvidence([]);
        announce(`Saved. This request is now: ${spec(REQUEST_STATUS, r.data.request.status).label}.`);
      },
      onError: (e) => announce(`Observation not recorded: ${(e as Error).message}`, true),
    });
  };
  const fe = obs.error instanceof ApiError ? obs.error.fieldErrors : {};
  return (
    <form onSubmit={submit} className="stack" noValidate data-testid="observation-form">
      {obs.error && !(obs.error instanceof ApiError && obs.error.code === "INVALID_INPUT") && <ErrorPanel error={obs.error} />}
      <div className="form-grid">
        <Field label="How do you know?" hint={OBS_KINDS.find((k) => k.value === kind)?.hint} error={fe.kind}>
          {(p) => (
            <select {...p} value={kind} onChange={(e) => setKind(e.target.value as ObservationCreate["kind"])}>
              {OBS_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
            </select>
          )}
        </Field>
        <Field label="Is it solved?" error={fe.verification}>
          {(p) => (
            <select {...p} value={verification} onChange={(e) => setVerification(e.target.value as ObservationCreate["verification"])}>
              <option value="passed">Yes, it's solved</option>
              <option value="failed">No, still a problem (look into it again)</option>
              <option value="inconclusive">Not sure yet</option>
              <option value="pending">Still waiting to hear</option>
            </select>
          )}
        </Field>
        <div className="span-2">
          <Field label="Which action does this confirm?" error={fe.action_id}>
            {(p) => (
              <select {...p} value={actionId} onChange={(e) => setActionId(e.target.value)}>
                <option value="">Not about a specific action</option>
                {actions.map((a) => <option key={a.id} value={a.id}>{KIND_LABEL[a.kind] ?? a.kind} ({spec(ACTION_STATUS, a.status).label.toLowerCase()})</option>)}
              </select>
            )}
          </Field>
        </div>
        <div className="span-2">
          <Field label="What happened" error={err ?? fe.body}>
            {(p) => <textarea {...p} rows={3} value={body} onChange={(e) => setBody(e.target.value)} maxLength={4000} />}
          </Field>
        </div>
        {refs.length > 0 && (
          <details className="span-2 small">
            <summary>Attach supporting sources (optional)</summary>
          <fieldset>
            <legend className="sr-only">Supporting sources</legend>
            <div className="stack" style={{ maxHeight: 120, overflowY: "auto", gap: 4 }}>
              {refs.map((r) => (
                <label key={r.id} className="check small">
                  <input type="checkbox" checked={evidence.includes(r.id)} onChange={(e) => setEvidence(e.target.checked ? [...evidence, r.id] : evidence.filter((x) => x !== r.id))} />
                  “{r.excerpt.slice(0, 80)}{r.excerpt.length > 80 ? "…" : ""}”
                </label>
              ))}
            </div>
          </fieldset>
          </details>
        )}
      </div>
      {kind === "connector_status" && verification === "passed" && <p className="small muted"><Icon name="info" size={12} /> A tool’s status update can’t mark the request solved. It’s saved as background information.</p>}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={obs.isPending} data-testid="submit-observation">{obs.isPending ? "Saving…" : "Save"}</button>
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
      onSuccess: (r) => { rememberRun(request.id, r.data.id); onStarted?.(r.data); announce(`PromiseGraph is looking into it (${r.data.mode === "scripted" ? "rule-based mode" : "AI model"}).`); close(); },
      onError: (e) => announce(`Couldn't start: ${(e as Error).message}`, true),
    });
  const doCancel = () => run &&
    cancel.mutate({ _id: run.id, reason: reason.trim() || "Cancelled by operator" }, {
      onSuccess: () => { announce("Stopped. Anything already under way is still tracked."); close(); },
      onError: (e) => announce(`Couldn't stop: ${(e as Error).message}`, true),
    });
  const doResume = () => run &&
    resume.mutate({ _id: run.id, expected_run_version: run.version, reason: reason.trim() || "Operator resume", additional_tool_calls: 6, additional_active_seconds: 45 }, {
      onSuccess: () => { announce("Continuing from where it stopped."); close(); },
      onError: (e) => announce(`Couldn't continue: ${(e as Error).message}`, true),
    });

  return (
    <div className="row" role="group" aria-label="Investigation controls">
      <button type="button" className="btn btn-sm" onClick={() => setDialog("start")} disabled={Boolean(active)} data-testid="start-run" title={active ? "Already looking into it" : undefined}>
        <Icon name="play" /> {run ? "Look into it again" : "Look into it"}
      </button>
      {active && (
        <button type="button" className="btn btn-sm btn-danger" onClick={() => setDialog("cancel")} data-testid="cancel-run"><Icon name="stop" /> Stop</button>
      )}
      {run?.status === "failed" && (
        <button type="button" className="btn btn-sm" onClick={() => setDialog("resume")} data-testid="resume-run"><Icon name="refresh" /> Continue where it stopped</button>
      )}
      <Dialog open={dialog === "start"} onClose={close} title="Look into this request">
        <fieldset>
          <legend>How should PromiseGraph think it through?</legend>
          <div className="radio-row">
            <label><input type="radio" name="run-mode" checked={mode === "scripted"} onChange={() => setMode("scripted")} /> Rule-based mode</label>
            <label><input type="radio" name="run-mode" checked={mode === "model"} disabled={!modelUsable} onChange={() => setMode("model")} /> AI model{modelUsable ? "" : " (not connected)"}</label>
          </div>
        </fieldset>
        <p className="small">It re-reads the latest docs, work and rules before suggesting anything.</p>
        {start.error && <ErrorPanel error={start.error} />}
        <Field label="Anything it should know? (optional)" hint="For example, what changed since last time.">
          {(p) => <textarea {...p} rows={3} value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={4000} />}
        </Field>
        <div className="form-actions">
          <button type="button" className="btn" onClick={close}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={doStart} disabled={start.isPending} data-testid="confirm-start-run">{start.isPending ? "Starting…" : "Look into it"}</button>
        </div>
      </Dialog>
      <Dialog open={dialog === "cancel"} onClose={close} title="Stop looking into this?">
        <p className="small">PromiseGraph stops here. Anything already under way (like a task being created) still finishes and is recorded.</p>
        {cancel.error && <ErrorPanel error={cancel.error} />}
        <Field label="Reason">{(p) => <input {...p} type="text" value={reason} onChange={(e) => setReason(e.target.value)} />}</Field>
        <div className="form-actions">
          <button type="button" className="btn" onClick={close}>Keep going</button>
          <button type="button" className="btn btn-danger" onClick={doCancel} disabled={cancel.isPending} data-testid="confirm-cancel-run">Stop</button>
        </div>
      </Dialog>
      <Dialog open={dialog === "resume"} onClose={close} title="Continue where it stopped?">
        <p className="small">Gives PromiseGraph a little more time and a few more checks (+6 checks, +45 seconds), keeping everything it already did.</p>
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
    if (outcome === "succeeded" && !targetId.trim()) { setErr("Enter the ID of the task or issue you found."); return; }
    const target = outcome === "succeeded" ? { target_id: targetId.trim(), target_url: targetUrl.trim() || null } : null;
    setErr(undefined);
    rec.mutate({ _id: action.id, outcome, target, evidence_ids: [], expected_action_version: action.version }, {
      onSuccess: (r) => announce(`Thanks. Recorded as: ${spec(ACTION_STATUS, r.data.status).label}.`),
      onError: (e2) => announce(`Couldn't record that: ${(e2 as Error).message}`, true),
    });
  };
  return (
    <form onSubmit={submit} className="stack" noValidate data-testid="reconcile-form">
      <fieldset>
        <legend>Please check: was it actually created?</legend>
        <div className="radio-row">
          <label><input type="radio" name={`rec-${action.id}`} checked={outcome === "failed"} onChange={() => setOutcome("failed")} /> No, it wasn’t created</label>
          <label><input type="radio" name={`rec-${action.id}`} checked={outcome === "succeeded"} onChange={() => setOutcome("succeeded")} /> Yes, I found it</label>
        </div>
      </fieldset>
      {outcome === "succeeded" && (
        <>
          <Field label={`Its ID in ${action.connector === "linear" ? "Linear" : "the PromiseGraph task list"}`} error={err}>
            {(p) => <input {...p} type="text" value={targetId} onChange={(e) => setTargetId(e.target.value)} />}
          </Field>
          <Field label="Link (optional)">{(p) => <input {...p} type="text" value={targetUrl} onChange={(e) => setTargetUrl(e.target.value)} />}</Field>
        </>
      )}
      {rec.error && <ErrorPanel error={rec.error} />}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={rec.isPending}>Save what I found</button>
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
      onSuccess: () => { announce("Request closed."); setOpen(false); },
      onError: (e2) => announce(`Close refused: ${(e2 as Error).message}`, true),
    });
  };
  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => setOpen(true)} data-testid="close-request"><Icon name="x" /> Close request</button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Close this request?">
        <form onSubmit={submit} className="stack" noValidate>
          <p className="small">Use this when the request no longer needs work, for example if it was handled elsewhere. Closed isn't the same as solved, and the history is kept.</p>
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
