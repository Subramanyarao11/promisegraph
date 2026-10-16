import { useState } from "react";
import type { ImportCommit, ImportCommitResult, ImportPreview, ImportPreviewResult, SourceSummary } from "@contracts";
import { useCommand, useSources } from "../api/hooks";
import { ApiError } from "../api/client";
import { Icon } from "../components/Icon";
import { SourceDrawerProvider, useOpenSource } from "../components/Evidence";
import { Callout, Empty, ErrorPanel, Field, Loading, Mono, Section, Tag, Ts, useAnnounce } from "../components/ui";

export function SourcesPage() {
  return (
    <SourceDrawerProvider>
      <div className="page-head">
        <div className="grow">
          <h1>Sources</h1>
          <p>Import permitted documentation, requests and work through preview → commit. Each record is an immutable revision; identical content is a no-op.</p>
        </div>
      </div>
      <div className="stack-lg">
        <ImportPanel />
        <SourceList />
      </div>
    </SourceDrawerProvider>
  );
}

function ImportPanel() {
  const announce = useAnnounce();
  const [form, setForm] = useState<ImportPreview>({ format: "markdown", origin: "synthetic", content: "", source_key: "" });
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<ImportPreviewResult | null>(null);
  const [result, setResult] = useState<ImportCommitResult | null>(null);
  const prev = useCommand<ImportPreview, ImportPreviewResult>("post", () => "/imports/preview", () => []);
  const commit = useCommand<ImportCommit & { _id: string }, ImportCommitResult>("post", (b) => `/imports/${encodeURIComponent(b._id)}/commit`, () => [["sources"], ["work"], ["requests"], ["context"], ["proposal"], ["decisions"]]);
  const fe = prev.error instanceof ApiError ? prev.error.fieldErrors : {};

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 1_000_000) { setErrs({ content: "File exceeds the 1 MB import limit." }); return; }
    const text = await f.text();
    const ext = f.name.split(".").pop()?.toLowerCase();
    const format = ext === "json" ? "json" : ext === "csv" ? "csv" : "markdown";
    setForm((s) => ({ ...s, content: text, format, source_key: s.source_key || f.name.replace(/\.[^.]+$/, "") }));
  };
  const doPreview = (e: React.FormEvent) => {
    e.preventDefault();
    const ce: Record<string, string> = {};
    if (!form.source_key.trim()) ce.source_key = "A source key is required (1–200 characters).";
    if (!form.content.trim()) ce.content = "Paste content or choose a file.";
    setErrs(ce);
    if (Object.keys(ce).length) return;
    setResult(null);
    prev.mutate({ ...form, source_key: form.source_key.trim() }, {
      onSuccess: (r) => { setPreview(r.data); announce(`Preview ready: ${r.data.valid_count} valid, ${r.data.rejected_count} rejected.`); },
      onError: (e) => announce(`Preview failed: ${(e as Error).message}`, true),
    });
  };
  const doCommit = () => {
    if (!preview) return;
    commit.mutate({ _id: preview.preview_id, expected_revision: preview.based_on_revision }, {
      onSuccess: (r) => {
        setResult(r.data); setPreview(null);
        announce(`Imported ${r.data.imported_count}, ${r.data.noop_count} unchanged, ${r.data.rejected_count} rejected. ${r.data.invalidated_proposal_count} proposals invalidated.`);
      },
      onError: (e) => announce(`Commit refused: ${(e as Error).message}`, true),
    });
  };

  return (
    <Section title="Import" testId="import-panel">
      <form onSubmit={doPreview} className="stack" noValidate>
        {prev.error && !(prev.error instanceof ApiError && prev.error.code === "INVALID_INPUT") && <ErrorPanel error={prev.error} />}
        <div className="form-grid">
          <Field label="Format" error={fe.format}>
            {(p) => <select {...p} value={form.format} onChange={(e) => setForm({ ...form, format: e.target.value as ImportPreview["format"] })}><option value="markdown">Markdown (one document)</option><option value="json">JSON import document</option><option value="csv">CSV (work items)</option></select>}
          </Field>
          <Field label="Origin" hint="Only synthetic, public, or owned/permitted data." error={fe.origin}>
            {(p) => <select {...p} value={form.origin} onChange={(e) => setForm({ ...form, origin: e.target.value as ImportPreview["origin"] })}><option value="synthetic">Synthetic</option><option value="public">Public</option><option value="owned_permitted">Owned / permitted</option></select>}
          </Field>
          <Field label="Source key" hint="Stable ID for this source; re-importing the same key creates a new revision." error={errs.source_key ?? fe.source_key}>
            {(p) => <input {...p} type="text" value={form.source_key} onChange={(e) => setForm({ ...form, source_key: e.target.value })} maxLength={200} />}
          </Field>
          <Field label="File (optional)" hint=".md, .json or .csv up to 1 MB">
            {(p) => <input {...p} type="file" accept=".md,.markdown,.json,.csv,text/*" onChange={(e) => onFile(e.target.files?.[0])} />}
          </Field>
          <div className="span-2">
            <Field label="Content" error={errs.content ?? fe.content}>
              {(p) => <textarea {...p} rows={8} className="mono" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />}
            </Field>
          </div>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn" disabled={prev.isPending} data-testid="preview-import">{prev.isPending ? "Previewing…" : "Preview import"}</button>
        </div>
      </form>

      {preview && (
        <div className="stack" style={{ marginTop: 12 }} data-testid="import-preview">
          <hr className="divider" />
          <div className="row">
            <strong>Preview</strong> <Mono>{preview.preview_id}</Mono>
            <Tag tone="success" icon="check">{preview.valid_count} valid</Tag>
            <Tag tone={preview.rejected_count ? "danger" : "neutral"} icon={preview.rejected_count ? "alert" : "check"}>{preview.rejected_count} rejected</Tag>
            <span className="small muted">against knowledge rev {preview.based_on_revision}</span>
          </div>
          {preview.changes.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th scope="col">Entity</th><th scope="col">ID</th><th scope="col">Change</th></tr></thead>
                <tbody>{preview.changes.map((c, i) => <tr key={i}><td>{c.entity_type}</td><td><Mono>{c.entity_id}</Mono></td><td><Tag tone={c.change === "noop" ? "neutral" : c.change === "create" ? "success" : "info"}>{c.change === "noop" ? "unchanged" : c.change}</Tag></td></tr>)}</tbody>
              </table>
            </div>
          )}
          {preview.rejected.length > 0 && (
            <Callout tone="danger" icon="alert" title="Rejected records">
              <ul className="small">{preview.rejected.map((r, i) => <li key={i}><Mono>{r.location}</Mono>: {r.reason}</li>)}</ul>
            </Callout>
          )}
          {commit.error && <ErrorPanel error={commit.error} />}
          <div className="form-actions">
            <button type="button" className="btn" onClick={() => setPreview(null)}>Discard preview</button>
            <button type="button" className="btn btn-primary" onClick={doCommit} disabled={commit.isPending || preview.valid_count === 0} data-testid="commit-import">{commit.isPending ? "Committing…" : `Commit ${preview.valid_count} records`}</button>
          </div>
        </div>
      )}
      {result && (
        <Callout tone="success" icon="check" title="Import committed" testId="import-result">
          <p>{result.imported_count} imported · {result.noop_count} unchanged (no-op) · {result.rejected_count} rejected · knowledge rev {result.knowledge_revision}</p>
          {result.invalidated_proposal_count > 0 && <p><Icon name="alert" size={12} /> {result.invalidated_proposal_count} prepared proposals were invalidated by changed facts and must be replanned.</p>}
          {result.source_revisions.length > 0 && <p className="small">Revisions: {result.source_revisions.map((s) => `${s.source_id}@${s.revision}`).join(", ")}</p>}
        </Callout>
      )}
    </Section>
  );
}

function SourceList() {
  const [kind, setKind] = useState("");
  const [origin, setOrigin] = useState("");
  const q = useSources({ kind: kind || undefined, origin: origin || undefined });
  const open = useOpenSource();
  const items = q.data?.items ?? [];
  return (
    <Section title="Source records" count={items.length} testId="source-list">
      <div className="filters" role="search" aria-label="Filter sources">
        <Field label="Kind">{(p) => <select {...p} value={kind} onChange={(e) => setKind(e.target.value)}><option value="">All kinds</option>{["documentation", "request", "work", "decision", "observation"].map((k) => <option key={k} value={k}>{k}</option>)}</select>}</Field>
        <Field label="Origin">{(p) => <select {...p} value={origin} onChange={(e) => setOrigin(e.target.value)}><option value="">All origins</option><option value="synthetic">Synthetic</option><option value="public">Public</option><option value="owned_permitted">Owned / permitted</option></select>}</Field>
      </div>
      {q.isLoading ? <Loading label="Loading sources" /> : q.error ? <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="sources" /> : items.length === 0 ? (
        <Empty title="No sources imported" icon="source"><p>Use the import form above, or run the seed script against the API.</p></Empty>
      ) : (
        <div className="table-wrap">
          <table className="table responsive">
            <thead><tr><th scope="col">Source</th><th scope="col">Kind</th><th scope="col">Revision</th><th scope="col">Freshness</th><th scope="col">Observed</th><th scope="col">Ingested</th></tr></thead>
            <tbody>
              {items.map((s: SourceSummary) => (
                <tr key={`${s.id}@${s.revision}`}>
                  <td data-label="Source">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => open({ source_id: s.id, revision: s.revision })} style={{ padding: 0, height: "auto" }}>
                      <span className="cell-title">{s.title}</span>
                    </button>
                    <div className="cell-sub"><Mono>{s.id}</Mono> · {s.origin.replace("_", " ")}</div>
                  </td>
                  <td data-label="Kind">{s.kind}</td>
                  <td data-label="Revision">
                    <span className="row">rev {s.revision}
                      {s.revision > 1 && (
                        <select aria-label={`Open an earlier revision of ${s.title}`} className="btn-sm" style={{ width: "auto", minHeight: 24, padding: "0 4px" }} value="" onChange={(e) => e.target.value && open({ source_id: s.id, revision: Number(e.target.value) })}>
                          <option value="">History…</option>
                          {Array.from({ length: s.revision }, (_, i) => s.revision - i).map((r) => <option key={r} value={r}>rev {r}</option>)}
                        </select>
                      )}
                    </span>
                  </td>
                  <td data-label="Freshness">{s.stale ? <Tag tone="warn" icon="alert">Stale</Tag> : <Tag tone="success" icon="check">Fresh</Tag>}</td>
                  <td data-label="Observed"><Ts iso={s.observed_at} /></td>
                  <td data-label="Ingested"><Ts iso={s.ingested_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
