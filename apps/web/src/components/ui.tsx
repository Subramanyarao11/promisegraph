import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Icon, type IconName } from "./Icon";
import { spec, type StatusSpec } from "../lib/status";
import { formatTs } from "../lib/format";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, isUnavailable } from "../api/client";

export function Badge({ s, title, testId }: { s: StatusSpec; title?: string; testId?: string }) {
  return (
    <span className={`badge tone-${s.tone}`} title={title} data-testid={testId}>
      <Icon name={s.icon} size={12} />
      <span>{s.label}</span>
    </span>
  );
}

export function StatusBadge({ map, value, prefix, testId }: { map: Record<string, StatusSpec>; value: string | null | undefined; prefix?: string; testId?: string }) {
  const s = spec(map, value);
  return <Badge s={prefix ? { ...s, label: `${prefix}: ${s.label}` } : s} testId={testId} title={value ?? undefined} />;
}

export function Tag({ children, tone = "neutral", icon }: { children: ReactNode; tone?: string; icon?: IconName }) {
  return (
    <span className={`tag tone-${tone}`}>
      {icon && <Icon name={icon} size={12} />}
      {children}
    </span>
  );
}

export function Ts({ iso, label }: { iso: string | null | undefined; label?: string }) {
  if (!iso) return <span className="muted">—</span>;
  return (
    <time dateTime={iso} title={`${iso} (UTC)`} className="ts">
      {label ? `${label} ` : ""}
      {formatTs(iso)}
    </time>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="mono">{children}</code>;
}

/* ---------- live announcements ---------- */
const AnnounceCtx = createContext<(msg: string, assertive?: boolean) => void>(() => {});
export function AnnouncerProvider({ children }: { children: ReactNode }) {
  // Polite messages are appended to a log (not replaced), so a quick follow-up event such as
  // "run finished" cannot erase a mutation confirmation before assistive tech reads it.
  const [log, setLog] = useState<{ id: number; msg: string }[]>([]);
  const [assertive, setAssertive] = useState<{ id: number; msg: string } | null>(null);
  const [toast, setToast] = useState<{ msg: string; key: number; tone: string } | null>(null);
  const seq = useRef(0);
  const announce = useCallback((msg: string, isAssertive = false) => {
    const id = ++seq.current;
    if (isAssertive) setAssertive({ id, msg });
    else setLog((l) => [...l, { id, msg }].slice(-8));
    setToast({ msg, key: id, tone: isAssertive ? "danger" : "info" });
  }, []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);
  return (
    <AnnounceCtx.Provider value={announce}>
      {children}
      <div className="sr-only" role="log" aria-live="polite" aria-relevant="additions" data-testid="live-polite">
        {log.map((m) => <p key={m.id}>{m.msg}</p>)}
      </div>
      <div className="sr-only" role="alert" aria-live="assertive" data-testid="live-assertive">
        {assertive && <p key={assertive.id}>{assertive.msg}</p>}
      </div>
      {toast && (
        <div className={`toast tone-${toast.tone}`} key={toast.key} aria-hidden="true">
          <Icon name={toast.tone === "danger" ? "alert" : "info"} />
          {toast.msg}
        </div>
      )}
    </AnnounceCtx.Provider>
  );
}
export const useAnnounce = () => useContext(AnnounceCtx);

/* ---------- dialog (native modal dialog: focus trap, Esc, inert background) ---------- */
export function Dialog({ open, onClose, title, children, wide, testId }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean; testId?: string }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className={`dialog ${wide ? "dialog-wide" : ""}`} aria-labelledby={titleId} onClose={onClose} onCancel={(e) => { e.preventDefault(); onClose(); }} data-testid={testId}>
      {open && (
        <>
          <header className="dialog-head">
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close dialog">
              <Icon name="x" />
            </button>
          </header>
          <div className="dialog-body">{children}</div>
        </>
      )}
    </dialog>
  );
}

/* ---------- form fields ---------- */
export function Field({ label, hint, error, children, id }: { label: string; hint?: string; error?: string; children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode; id?: string }) {
  const auto = useId();
  const fid = id ?? auto;
  const hintId = hint ? `${fid}-hint` : undefined;
  const errId = error ? `${fid}-err` : undefined;
  const describedBy = [hintId, errId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`field ${error ? "field-error" : ""}`}>
      <label htmlFor={fid}>{label}</label>
      {children({ id: fid, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && <p className="hint" id={hintId}>{hint}</p>}
      {error && (
        <p className="error-text" id={errId}>
          <Icon name="alert" size={12} /> {error}
        </p>
      )}
    </div>
  );
}

/* ---------- states ---------- */
export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}…
    </div>
  );
}

export function Empty({ title, children, icon = "inbox" }: { title: string; children?: ReactNode; icon?: IconName }) {
  return (
    <div className="empty">
      <Icon name={icon} size={20} />
      <p className="empty-title">{title}</p>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function errorTitle(e: unknown): string {
  if (!(e instanceof ApiError)) return "Something went wrong";
  if (isUnavailable(e)) return e.code === "NETWORK" ? "Can't reach the server" : "Database unavailable";
  switch (e.code) {
    case "NOT_FOUND": return "Not found";
    case "UNAUTHORIZED":
    case "WORKSPACE_FORBIDDEN": return "You don't have access to this workspace";
    case "REVISION_CONFLICT": return "Something changed in the meantime. Refresh and try again";
    case "IDEMPOTENCY_CONFLICT": return "This looks like a duplicate of an earlier action";
    case "INVALID_TRANSITION": return "That isn't possible right now";
    case "EVIDENCE_INVALID": return "The supporting sources aren't valid";
    case "POLICY_VIOLATION": return "Not allowed by company rules";
    case "RATE_LIMITED": return "Too many requests, please wait a moment";
    case "CAPABILITY_DISABLED": return "That feature is turned off";
    case "INVALID_INPUT": return "Please check the highlighted fields";
    default: return e.code;
  }
}

export function ErrorPanel({ error, onRetry, what }: { error: unknown; onRetry?: () => void; what?: string }) {
  const e = error instanceof ApiError ? error : null;
  const unavailable = isUnavailable(error);
  return (
    <div className={`callout ${unavailable ? "tone-danger" : "tone-warn"}`} role="alert" data-testid={unavailable ? "state-unavailable" : "state-error"}>
      <Icon name={unavailable ? "alert" : "info"} size={16} />
      <div>
        <p className="callout-title">{errorTitle(error)}{what ? ` · ${what}` : ""}</p>
        <p>{e?.message ?? String((error as Error)?.message ?? error)}</p>
        {unavailable && <p className="muted">We don't show old or sample data in its place. Try again once it's back.</p>}
        {e?.correlationId && <p className="muted small">Reference for support: <Mono>{e.correlationId}</Mono></p>}
        {onRetry && (
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            <Icon name="refresh" /> Retry
          </button>
        )}
      </div>
    </div>
  );
}

export function Callout({ tone, icon, title, children, testId }: { tone: string; icon: IconName; title: string; children?: ReactNode; testId?: string }) {
  return (
    <div className={`callout tone-${tone}`} data-testid={testId}>
      <Icon name={icon} size={16} />
      <div>
        <p className="callout-title">{title}</p>
        {children}
      </div>
    </div>
  );
}

export function Section({ title, id, children, actions, count, testId }: { title: string; id?: string; children: ReactNode; actions?: ReactNode; count?: number; testId?: string }) {
  const hid = useId();
  return (
    <section className="panel" aria-labelledby={hid} id={id} data-testid={testId}>
      <header className="panel-head">
        <h2 id={hid}>
          {title}
          {count !== undefined && <span className="count">{count}</span>}
        </h2>
        {actions && <div className="panel-actions">{actions}</div>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

/* Owner with activity from GET /people/{id} (Rev 5 #33); falls back to the bare ID if the read is unavailable. */
interface PersonSummary { id: string; name: string; role: string; team_id: string | null; active: boolean; version: number }
export function Owner({ id }: { id: string | null }) {
  const q = useQuery({
    queryKey: ["person", id],
    enabled: Boolean(id),
    queryFn: ({ signal }) => api.get<PersonSummary>(`/people/${encodeURIComponent(id!)}`, signal),
    staleTime: 60_000,
    retry: false,
  });
  if (!id) return <Tag tone="warn" icon="alert">No owner assigned</Tag>;
  const p = q.data?.data;
  return (
    <span className="row" style={{ display: "inline-flex" }} data-testid="owner" title={id}>
      {p ? <span>{p.name}{p.role ? <span className="muted"> · {p.role}</span> : null}</span> : <Mono>{id}</Mono>}
      {p && !p.active && <Tag tone="warn" icon="alert">Inactive owner: no longer here</Tag>}
    </span>
  );
}
