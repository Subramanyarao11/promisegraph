const tzName = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return "UTC";
  }
})();

const local = new Intl.DateTimeFormat("en-GB", {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZoneName: "short",
});

/** Absolute timestamp in the viewer's zone with an explicit zone label. */
export function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return local.format(d).replace(",", "");
}

export function viewerZone(): string {
  return tzName;
}

export function shortId(id: string): string {
  return id;
}

export function humanize(s: string): string {
  return s.replace(/_/g, " ");
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function durationMs(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Source excerpts can be whole structured records; show their human-readable field instead of raw JSON. */
export function friendlyExcerpt(text: string, max = 220): string {
  let out = text;
  const t = text.trim();
  if (t.startsWith("{")) {
    try {
      const o = JSON.parse(t) as Record<string, unknown>;
      const pick = ["summary", "title", "subject", "body", "description", "rationale", "name"].map((k) => o[k]).find((v) => typeof v === "string" && v.trim());
      if (typeof pick === "string") out = pick;
    } catch { /* truncated or not JSON: fall back to the raw text */ }
  }
  return out.length > max ? `${out.slice(0, max)}…` : out;
}


/** Narrows an untyped string (e.g. a <select> value) to one of the allowed literals; keeps `fallback` otherwise. */
export function oneOf<T extends string>(allowed: readonly T[], value: string, fallback: T): T {
  return (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}
