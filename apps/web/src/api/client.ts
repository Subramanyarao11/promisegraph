import type { Meta } from "@contracts";

export const WORKSPACE_ID: string = import.meta.env.VITE_WORKSPACE_ID || "ws_demo";
export const CONTRACT_VERSION = "1.0.0";
const BASE = `/api/v1/workspaces/${encodeURIComponent(WORKSPACE_ID)}`;

export interface Envelope<T> {
  data: T;
  meta: Meta;
}
export interface ListEnvelope<T> {
  data: T[];
  page: { next_cursor: string | null };
  meta: Meta;
}

/** Error carrying the contract ErrorEnvelope fields, or a synthetic NETWORK code when the API was unreachable. */
export class ApiError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  details: Record<string, unknown>;
  correlationId: string | null;
  constructor(status: number, code: string, message: string, retryable: boolean, details: Record<string, unknown>, correlationId: string | null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details ?? {};
    this.correlationId = correlationId;
  }
  get fieldErrors(): Record<string, string> {
    const f = (this.details as { fields?: Record<string, string> }).fields;
    return f && typeof f === "object" ? f : {};
  }
}

export function isUnavailable(e: unknown): boolean {
  return e instanceof ApiError && (e.code === "DEPENDENCY_UNAVAILABLE" || e.code === "NETWORK" || (e.status >= 502 && e.status <= 504 && e.code !== "CAPABILITY_DISABLED"));
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** Last knowledge revision seen in any envelope; exposed for headers and stale checks. */
let lastMeta: Meta | null = null;
const metaListeners = new Set<(m: Meta) => void>();
export function onMeta(fn: (m: Meta) => void): () => void {
  metaListeners.add(fn);
  return () => metaListeners.delete(fn);
}
export function getLastMeta(): Meta | null {
  return lastMeta;
}

async function raw(method: string, path: string, opts: { body?: unknown; idempotencyKey?: string; absolute?: boolean; signal?: AbortSignal } = {}): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let res: Response;
  try {
    res = await fetch(opts.absolute ? path : BASE + path, {
      method,
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as { name?: string }).name === "AbortError") throw err;
    throw new ApiError(0, "NETWORK", "The API could not be reached.", true, {}, null);
  }
  let json: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }
  const meta = (json as { meta?: Meta } | null)?.meta;
  // Error envelopes carry knowledge_revision 0 when the database is unreachable ("unknown"): only trust successes.
  if (res.ok && meta && typeof meta.knowledge_revision === "number") {
    lastMeta = meta;
    metaListeners.forEach((fn) => fn(meta));
  }
  if (!res.ok) {
    const e = (json as { error?: { code: string; message: string; retryable: boolean; details: Record<string, unknown> } } | null)?.error;
    if (e) throw new ApiError(res.status, e.code, e.message, e.retryable, e.details, meta?.correlation_id ?? null);
    // Dev proxy reports an unreachable upstream as a bare 5xx without our envelope.
    throw new ApiError(res.status, res.status >= 500 ? "NETWORK" : `HTTP_${res.status}`, res.status >= 500 ? "The API could not be reached." : `Unexpected HTTP ${res.status}.`, res.status >= 500, {}, null);
  }
  return json;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => raw("GET", path, { signal }) as Promise<Envelope<T>>,
  list: <T>(path: string, signal?: AbortSignal) => raw("GET", path, { signal }) as Promise<ListEnvelope<T>>,
  absolute: (path: string, signal?: AbortSignal) => raw("GET", path, { absolute: true, signal }),
  post: <T>(path: string, body: unknown, idempotencyKey: string) => raw("POST", path, { body, idempotencyKey }) as Promise<Envelope<T>>,
  patch: <T>(path: string, body: unknown, idempotencyKey: string) => raw("PATCH", path, { body, idempotencyKey }) as Promise<Envelope<T>>,
  rawGet: (path: string, signal?: AbortSignal) => raw("GET", path, { signal }),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}
