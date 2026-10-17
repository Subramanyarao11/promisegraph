import { createContext, useContext, useState, type ReactNode } from "react";
import type { EvidencePath, EvidenceRef, GraphEdge, GraphNode } from "@contracts";
import { useEvidence, useSource } from "../api/hooks";
import { friendlyExcerpt } from "../lib/format";
import { Icon } from "./Icon";
import { Callout, Dialog, ErrorPanel, Loading, Mono, Tag, Ts } from "./ui";

/* ---------------- source drawer ---------------- */
export interface SourceTarget {
  source_id: string | null;
  revision: number | null;
  ref?: EvidenceRef;
  /** A cited evidence ID not present in the loaded context; resolved with GET /evidence/{id}. */
  evidence_id?: string;
}
const DrawerCtx = createContext<(t: SourceTarget) => void>(() => {});
export const useOpenSource = () => useContext(DrawerCtx);

export function SourceDrawerProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<SourceTarget | null>(null);
  return (
    <DrawerCtx.Provider value={setTarget}>
      {children}
      <Dialog open={Boolean(target)} onClose={() => setTarget(null)} title="Source" wide testId="source-drawer">
        {target && <SourceView target={target} />}
      </Dialog>
    </DrawerCtx.Provider>
  );
}

function highlight(body: string, excerpt: string | undefined): ReactNode {
  if (!excerpt) return body;
  const i = body.indexOf(excerpt);
  if (i < 0) return body;
  return (
    <>
      {body.slice(0, i)}
      <mark data-testid="excerpt-mark">{excerpt}</mark>
      {body.slice(i + excerpt.length)}
    </>
  );
}

function SourceView({ target }: { target: SourceTarget }) {
  const evq = useEvidence(target.ref || !target.evidence_id ? null : target.evidence_id);
  const ref = target.ref ?? evq.data?.data;
  const q = useSource(target.source_id ?? ref?.source_id ?? null, target.revision ?? ref?.source_revision ?? null);
  return (
    <div className="stack-lg">
      {evq.isLoading && <Loading label="Finding the source" />}
      {evq.error ? <ErrorPanel error={evq.error} onRetry={() => evq.refetch()} what="evidence" /> : null}
      {ref && (
        <div className="stack">
          <span className="label">The exact words we relied on</span>
          <blockquote className="quote" data-testid="drawer-excerpt">{ref.excerpt}</blockquote>
          <dl className="kv">
            <dt>Where in the source</dt><dd><Mono>{ref.location}</Mono></dd>
            <dt>Version</dt><dd data-testid="drawer-revision">version {ref.source_revision}</dd>
            <dt>Kind of data</dt><dd>{ORIGIN[ref.origin] ?? ref.origin}</dd>
            <dt>True as of</dt><dd><Ts iso={ref.observed_at} /></dd>
            <dt>Added to PromiseGraph</dt><dd><Ts iso={ref.ingested_at} /></dd>
            <dt>Still current?</dt>
            <dd data-testid="drawer-freshness">
              {ref.stale ? <Tag tone="warn" icon="alert">May be out of date: last checked a while ago</Tag> : <Tag tone="success" icon="check">Up to date</Tag>}
            </dd>
          </dl>
        </div>
      )}
      {!ref && !target.source_id ? null : q.isLoading ? (
        <Loading label="Loading the source" />
      ) : q.error ? (
        <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="source" />
      ) : q.data ? (
        <div className="stack">
          <div className="row">
            <strong>{q.data.data.title}</strong>
            <Tag>{q.data.data.kind}</Tag>
            <Tag>version {q.data.data.revision}</Tag>
            {q.data.data.supersedes_revision !== null && <Tag tone="info">replaces version {q.data.data.supersedes_revision}</Tag>}
          </div>
          {ref && q.data.data.revision !== ref.source_revision && (
            <Callout tone="warn" icon="alert" title="This is a different version from the one we relied on">
              <p>We relied on version {ref.source_revision}; this is version {q.data.data.revision}.</p>
            </Callout>
          )}
          {ref && !q.data.data.body.includes(ref.excerpt) && (
            <p className="muted small">The quote above is a tidied-up version of this record, so it isn't highlighted word for word.</p>
          )}
          <pre className="source-body" tabIndex={0} aria-label="Source body">{highlight(q.data.data.body, ref?.excerpt)}</pre>
          <p className="small muted">
            true as of <Ts iso={q.data.data.observed_at} /> · added <Ts iso={q.data.data.ingested_at} /> · content check <Mono>{q.data.data.sha256.slice(0, 12)}…</Mono>
          </p>
        </div>
      ) : null}
    </div>
  );
}

export function EvidenceChip({ id, refs }: { id: string; refs: Map<string, EvidenceRef> }) {
  const open = useOpenSource();
  const ref = refs.get(id);
  if (!ref)
    return (
      <button type="button" className="btn btn-sm" onClick={() => open({ source_id: null, revision: null, evidence_id: id })} title={id}>
        <Icon name="doc" /> Cited source
      </button>
    );
  return (
    <button type="button" className="btn btn-sm" onClick={() => open({ source_id: ref.source_id, revision: ref.source_revision, ref })} title={ref.excerpt}>
      <Icon name="doc" /> “{friendlyExcerpt(ref.excerpt, 40)}”
      {ref.stale && <span className="sr-only"> (may be out of date)</span>}
      {ref.stale && <Icon name="alert" />}
    </button>
  );
}

/* ---------------- graph path: text first ---------------- */
function edgeBetween(path: EvidencePath, a: GraphNode, b: GraphNode | undefined): GraphEdge | undefined {
  if (!b) return undefined;
  const edges: readonly GraphEdge[] = path.edges;
  return edges.find((e) => (e.from_id === a.id && e.to_id === b.id) || (e.from_id === b.id && e.to_id === a.id));
}

export function PathText({ path, refs, index }: { path: EvidencePath; refs: Map<string, EvidenceRef>; index: number }) {
  const nodes: readonly GraphNode[] = path.nodes;
  const [showVisual, setShowVisual] = useState(false);
  // Text equivalent for assistive tech: "Request X —ABOUT→ Capability Y …" (cheap; computed per render).
  const desc = nodes
    .map((n, i) => {
      const next = nodes[i + 1];
      const e = edgeBetween(path, n, next);
      const arrow = !next ? "" : e ? ` ${e.from_id === n.id ? "—" : "←"}${e.type}${e.from_id === n.id ? "→" : "—"} ` : " → ";
      return `${n.label} ${n.title}${arrow}`;
    })
    .join("");
  return (
    <div className="stack" data-testid="evidence-path">
      <div className="row small muted">
        <span>Connection {index + 1} · {Math.max(nodes.length - 1, 0)} steps ·</span> <Mono>{path.id}</Mono>
        <ClaimTag kind={path.claim_kind} />
        {path.evidence_ids.map((id) => <EvidenceChip key={id} id={id} refs={refs} />)}
      </div>
      <ol className="path" aria-label={`Graph path ${index + 1}: ${desc}`}>
        {nodes.map((n, i) => {
          const e = i < nodes.length - 1 ? edgeBetween(path, n, nodes[i + 1]) : undefined;
          return (
            <li key={`${n.id}-${i}`}>
              <span className="path-dot" aria-hidden="true">{i + 1}</span>
              <div>
                <div className="path-node">
                  <span className="node-label">{n.label}</span>
                  <strong>{n.title}</strong> <Mono>{n.id}</Mono>
                </div>
                {i < nodes.length - 1 && (
                  <div className="path-edge">
                    {e ? (
                      <>
                        <span>{e.from_id === n.id ? "↓" : "↑"}</span>
                        <span className="rel">{e.type}</span>
                        <span className="sr-only">{e.from_id === n.id ? "points to next node" : "points from next node to this one"}</span>
                        {e.confirmed ? <Tag tone="success" icon="check">confirmed</Tag> : <Tag tone="warn" icon="question">possible link, not confirmed</Tag>}
                        {e.evidence_ids.map((id) => <EvidenceChip key={id} id={id} refs={refs} />)}
                      </>
                    ) : (
                      <Tag tone="danger" icon="alert">no link found between these two</Tag>
                    )}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <div>
        <button type="button" className="btn btn-ghost btn-sm" aria-expanded={showVisual} onClick={() => setShowVisual((v) => !v)}>
          <Icon name="graph" /> {showVisual ? "Hide picture" : "Show as a picture"}
        </button>
      </div>
      {showVisual && <PathVisual path={path} />}
    </div>
  );
}

/** Supplementary diagram; the ordered list above is the authoritative text equivalent. */
function PathVisual({ path }: { path: EvidencePath }) {
  const nodes: readonly GraphNode[] = path.nodes;
  const W = 150, gap = 46, H = 64;
  const width = nodes.length * W + (nodes.length - 1) * gap + 8;
  return (
    <div className="path-visual" aria-hidden="true">
      <svg width={width} height={H + 8} viewBox={`0 0 ${width} ${H + 8}`}>
        {nodes.map((n, i) => {
          const x = 4 + i * (W + gap);
          const e = i < nodes.length - 1 ? edgeBetween(path, n, nodes[i + 1]) : undefined;
          return (
            <g key={`${n.id}-${i}`}>
              <rect className="n" x={x} y={4} width={W} height={H - 8} rx={6} />
              <text x={x + 8} y={22}>{n.label.toUpperCase()}</text>
              <text x={x + 8} y={40} style={{ fontWeight: 600 }}>{n.title.length > 20 ? n.title.slice(0, 19) + "…" : n.title}</text>
              {i < nodes.length - 1 && (
                <>
                  <line className={`e ${e && !e.confirmed ? "unconfirmed" : ""}`} x1={x + W} y1={H / 2} x2={x + W + gap} y2={H / 2} strokeWidth={1.5} />
                  <text x={x + W + 3} y={H / 2 - 5} style={{ fontSize: 9 }}>{e?.type.slice(0, 9) ?? "?"}</text>
                </>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

const ORIGIN: Record<string, string> = { synthetic: "Demo data (made up)", public: "Public information", owned_permitted: "Our own data" };
const CLAIM: Record<string, { label: string; tone: string }> = {
  source_assertion: { label: "Supported by source", tone: "success" },
  derived: { label: "Derived from sources", tone: "info" },
  inference: { label: "Our inference, not stated in a source", tone: "warn" },
  unknown: { label: "Not sure how well supported", tone: "neutral" },
};
export function ClaimTag({ kind }: { kind: string }) {
  const c = CLAIM[kind] ?? { label: kind, tone: "neutral" };
  return <Tag tone={c.tone} icon={c.tone === "success" ? "check" : c.tone === "warn" ? "question" : "info"}>{c.label}</Tag>;
}
