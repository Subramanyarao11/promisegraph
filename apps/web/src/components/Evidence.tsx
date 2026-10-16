import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import type { EvidencePath, EvidenceRef, GraphEdge, GraphNode } from "@contracts";
import { useEvidence, useSource } from "../api/hooks";
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
      <Dialog open={Boolean(target)} onClose={() => setTarget(null)} title={target ? (target.source_id ? `Source ${target.source_id}` : `Evidence ${target.evidence_id}`) : "Source"} wide testId="source-drawer">
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
      {evq.isLoading && <Loading label="Resolving evidence" />}
      {evq.error ? <ErrorPanel error={evq.error} onRetry={() => evq.refetch()} what="evidence" /> : null}
      {ref && (
        <div className="stack">
          <span className="label">Exact excerpt cited</span>
          <blockquote className="quote" data-testid="drawer-excerpt">{ref.excerpt}</blockquote>
          <dl className="kv">
            <dt>Evidence</dt><dd><Mono>{ref.id}</Mono></dd>
            <dt>Location</dt><dd><Mono>{ref.location}</Mono></dd>
            <dt>Revision</dt><dd data-testid="drawer-revision">rev {ref.source_revision}</dd>
            <dt>Origin</dt><dd>{ref.origin.replace("_", " ")}</dd>
            <dt>Observed</dt><dd><Ts iso={ref.observed_at} /></dd>
            <dt>Ingested</dt><dd><Ts iso={ref.ingested_at} /></dd>
            <dt>Freshness</dt>
            <dd data-testid="drawer-freshness">
              {ref.stale ? <Tag tone="warn" icon="alert">Stale — observed too long ago to rely on</Tag> : <Tag tone="success" icon="check">Fresh</Tag>}
            </dd>
          </dl>
        </div>
      )}
      {!ref && !target.source_id ? null : q.isLoading ? (
        <Loading label="Loading source revision" />
      ) : q.error ? (
        <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="source" />
      ) : q.data ? (
        <div className="stack">
          <div className="row">
            <strong>{q.data.data.title}</strong>
            <Tag>{q.data.data.kind}</Tag>
            <Tag>rev {q.data.data.revision}</Tag>
            {q.data.data.supersedes_revision !== null && <Tag tone="info">supersedes rev {q.data.data.supersedes_revision}</Tag>}
          </div>
          {ref && q.data.data.revision !== ref.source_revision && (
            <Callout tone="warn" icon="alert" title="Showing a different revision than the one cited">
              <p>The evidence cites rev {ref.source_revision}; the server returned rev {q.data.data.revision}.</p>
            </Callout>
          )}
          {ref && !q.data.data.body.includes(ref.excerpt) && (
            <p className="muted small">The excerpt is a normalized quotation of this record and is not highlighted verbatim.</p>
          )}
          <pre className="source-body" tabIndex={0} aria-label="Source body">{highlight(q.data.data.body, ref?.excerpt)}</pre>
          <p className="small muted">
            sha256 <Mono>{q.data.data.sha256.slice(0, 16)}…</Mono> · observed <Ts iso={q.data.data.observed_at} /> · ingested <Ts iso={q.data.data.ingested_at} />
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
      <button type="button" className="btn btn-sm" onClick={() => open({ source_id: null, revision: null, evidence_id: id })} title="Open cited evidence">
        <Icon name="doc" /> {id}
      </button>
    );
  return (
    <button type="button" className="btn btn-sm" onClick={() => open({ source_id: ref.source_id, revision: ref.source_revision, ref })} title={ref.excerpt}>
      <Icon name="doc" /> {ref.source_id} · rev {ref.source_revision}
      {ref.stale && <span className="sr-only"> (stale)</span>}
      {ref.stale && <Icon name="alert" />}
    </button>
  );
}

/* ---------------- graph path: text first ---------------- */
function edgeBetween(path: EvidencePath, a: GraphNode, b: GraphNode): GraphEdge | undefined {
  const edges = path.edges as GraphEdge[];
  return edges.find((e) => (e.from_id === a.id && e.to_id === b.id) || (e.from_id === b.id && e.to_id === a.id));
}

export function PathText({ path, refs, index }: { path: EvidencePath; refs: Map<string, EvidenceRef>; index: number }) {
  const nodes = path.nodes as GraphNode[];
  const [showVisual, setShowVisual] = useState(false);
  const desc = useMemo(() => {
    const parts: string[] = [];
    nodes.forEach((n, i) => {
      parts.push(`${n.label} ${n.title}`);
      if (i < nodes.length - 1) {
        const e = edgeBetween(path, n, nodes[i + 1]);
        parts.push(e ? `${e.from_id === n.id ? "—" : "←"}${e.type}${e.from_id === n.id ? "→" : "—"}` : "→");
      }
    });
    return parts.join(" ");
  }, [path, nodes]);
  return (
    <div className="stack" data-testid="evidence-path">
      <div className="row small muted">
        <span>Path {index + 1} · {nodes.length} nodes · {Math.max(nodes.length - 1, 0)} hops ·</span> <Mono>{path.id}</Mono>
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
                        {e.confirmed ? <Tag tone="success" icon="check">confirmed</Tag> : <Tag tone="warn" icon="question">candidate · unconfirmed</Tag>}
                        {e.evidence_ids.map((id) => <EvidenceChip key={id} id={id} refs={refs} />)}
                      </>
                    ) : (
                      <Tag tone="danger" icon="alert">no edge returned between these nodes</Tag>
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
          <Icon name="graph" /> {showVisual ? "Hide diagram" : "Show diagram"}
        </button>
      </div>
      {showVisual && <PathVisual path={path} />}
    </div>
  );
}

/** Supplementary diagram; the ordered list above is the authoritative text equivalent. */
function PathVisual({ path }: { path: EvidencePath }) {
  const nodes = path.nodes as GraphNode[];
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

const CLAIM: Record<string, { label: string; tone: string }> = {
  source_assertion: { label: "Supported by source", tone: "success" },
  derived: { label: "Derived from sources", tone: "info" },
  inference: { label: "Inference · not directly stated", tone: "warn" },
  unknown: { label: "Support unknown", tone: "neutral" },
};
export function ClaimTag({ kind }: { kind: string }) {
  const c = CLAIM[kind] ?? { label: kind, tone: "neutral" };
  return <Tag tone={c.tone} icon={c.tone === "success" ? "check" : c.tone === "warn" ? "question" : "info"}>{c.label}</Tag>;
}
