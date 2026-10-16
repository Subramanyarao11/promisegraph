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
          <h1>Knowledge base</h1>
          <p>The docs, requests and engineering work PromiseGraph checks. Add more by importing a file: you'll see a preview before anything is saved.</p>
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
    if (!form.source_key.trim()) ce.source_key = "Give it a name (up to 200 characters).";
    if (!form.content.trim()) ce.content = "Paste content or choose a file.";
    setErrs(ce);
    if (Object.keys(ce).length) return;
    setResult(null);
    prev.mutate({ ...form, source_key: form.source_key.trim() }, {
      onSuccess: (r) => { setPreview(r.data); announce(`Preview ready: ${r.data.valid_count} ready to add, ${r.data.rejected_count} with problems.`); },
      onError: (e) => announce(`Preview failed: ${(e as Error).message}`, true),
    });
  };
  const doCommit = () => {
    if (!preview) return;
    commit.mutate({ _id: preview.preview_id, expected_revision: preview.based_on_revision }, {
      onSuccess: (r) => {
        setResult(r.data); setPreview(null);
        announce(`Saved: ${r.data.imported_count} added, ${r.data.noop_count} unchanged.`);
      },
      onError: (e) => announce(`Not saved: ${(e as Error).message}`, true),
    });
  };

  return (
    <Section title="Add to the knowledge base" testId="import-panel">
      <form onSubmit={doPreview} className="stack" noValidate>
        {prev.error && !(prev.error instanceof ApiError && prev.error.code === "INVALID_INPUT") && <ErrorPanel error={prev.error} />}
        <div className="form-grid">
          <Field label="Format" error={fe.format}>
            {(p) => <select {...p} value={form.format} onChange={(e) => setForm({ ...form, format: e.target.value as ImportPreview["format"] })}><option value="markdown">A document (Markdown)</option><option value="json">Structured data (JSON)</option><option value="csv">Engineering tasks (CSV)</option></select>}
          </Field>
          <Field label="Where it comes from" hint="Only use demo, public, or data you're allowed to use." error={fe.origin}>
            {(p) => <select {...p} value={form.origin} onChange={(e) => setForm({ ...form, origin: e.target.value as ImportPreview["origin"] })}><option value="synthetic">Demo data (made up)</option><option value="public">Public information</option><option value="owned_permitted">Our own data</option></select>}
          </Field>
          <Field label="Name" hint="Re-importing with the same name saves a new version and keeps the old one." error={errs.source_key ?? fe.source_key}>
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
          <button type="submit" className="btn" disabled={prev.isPending} data-testid="preview-import">{prev.isPending ? "Checking…" : "Preview"}</button>
        </div>
      </form>

      {preview && (
        <div className="stack" style={{ marginTop: 12 }} data-testid="import-preview">
          <hr className="divider" />
          <div className="row">
            <strong>Preview: nothing saved yet</strong>
            <Tag tone="success" icon="check">{preview.valid_count} ready to add</Tag>
            <Tag tone={preview.rejected_count ? "danger" : "neutral"} icon={preview.rejected_count ? "alert" : "check"}>{preview.rejected_count} with problems</Tag>
          </div>
          {preview.changes.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th scope="col">What</th><th scope="col">ID</th><th scope="col">Change</th></tr></thead>
                <tbody>{preview.changes.map((c, i) => <tr key={i}><td>{c.entity_type}</td><td><Mono>{c.entity_id}</Mono></td><td><Tag tone={c.change === "noop" ? "neutral" : c.change === "create" ? "success" : "info"}>{c.change === "noop" ? "no change" : c.change === "create" ? "new" : "updated"}</Tag></td></tr>)}</tbody>
              </table>
            </div>
          )}
          {preview.rejected.length > 0 && (
            <Callout tone="danger" icon="alert" title="These records have problems and won't be added">
              <ul className="small">{preview.rejected.map((r, i) => <li key={i}><Mono>{r.location}</Mono>: {r.reason}</li>)}</ul>
            </Callout>
          )}
          {commit.error && <ErrorPanel error={commit.error} />}
          <div className="form-actions">
            <button type="button" className="btn" onClick={() => setPreview(null)}>Cancel</button>
            <button type="button" className="btn btn-primary" onClick={doCommit} disabled={commit.isPending || preview.valid_count === 0} data-testid="commit-import">{commit.isPending ? "Saving…" : `Save ${preview.valid_count} to the knowledge base`}</button>
          </div>
        </div>
      )}
      {result && (
        <Callout tone="success" icon="check" title="Saved to the knowledge base" testId="import-result">
          <p>{result.imported_count} added · {result.noop_count} already there, unchanged · {result.rejected_count} skipped</p>
          {result.invalidated_proposal_count > 0 && <p><Icon name="alert" size={12} /> {result.invalidated_proposal_count} pending suggestions relied on facts that just changed. They're now out of date and need a fresh look.</p>}
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
    <Section title="What's in the knowledge base" count={items.length} testId="source-list">
      <div className="filters" role="search" aria-label="Filter sources">
        <Field label="Type">{(p) => <select {...p} value={kind} onChange={(e) => setKind(e.target.value)}><option value="">All types</option>{["documentation", "request", "work", "decision", "observation"].map((k) => <option key={k} value={k}>{k}</option>)}</select>}</Field>
        <Field label="Where it comes from">{(p) => <select {...p} value={origin} onChange={(e) => setOrigin(e.target.value)}><option value="">Anywhere</option><option value="synthetic">Demo data (made up)</option><option value="public">Public information</option><option value="owned_permitted">Our own data</option></select>}</Field>
      </div>
      {q.isLoading ? <Loading label="Loading sources" /> : q.error ? <ErrorPanel error={q.error} onRetry={() => q.refetch()} what="sources" /> : items.length === 0 ? (
        <Empty title="The knowledge base is empty" icon="source"><p>Add a document or a task list above to get started.</p></Empty>
      ) : (
        <div className="table-wrap">
          <table className="table responsive">
            <thead><tr><th scope="col">Source</th><th scope="col">Type</th><th scope="col">Version</th><th scope="col">Up to date?</th><th scope="col">True as of</th><th scope="col">Added</th></tr></thead>
            <tbody>
              {items.map((s: SourceSummary) => (
                <tr key={`${s.id}@${s.revision}`}>
                  <td data-label="Source">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => open({ source_id: s.id, revision: s.revision })} style={{ padding: 0, height: "auto" }}>
                      <span className="cell-title">{s.title}</span>
                    </button>
                    <div className="cell-sub">{s.origin === "synthetic" ? "Demo data" : s.origin === "public" ? "Public" : "Our own data"}</div>
                  </td>
                  <td data-label="Type">{s.kind === "work" ? "engineering task" : s.kind}</td>
                  <td data-label="Version">
                    <span className="row">version {s.revision}
                      {s.revision > 1 && (
                        <select aria-label={`Open an earlier revision of ${s.title}`} className="btn-sm" style={{ width: "auto", minHeight: 24, padding: "0 4px" }} value="" onChange={(e) => e.target.value && open({ source_id: s.id, revision: Number(e.target.value) })}>
                          <option value="">Older versions…</option>
                          {Array.from({ length: s.revision }, (_, i) => s.revision - i).map((r) => <option key={r} value={r}>version {r}</option>)}
                        </select>
                      )}
                    </span>
                  </td>
                  <td data-label="Up to date?">{s.stale ? <Tag tone="warn" icon="alert">May be out of date</Tag> : <Tag tone="success" icon="check">Up to date</Tag>}</td>
                  <td data-label="True as of"><Ts iso={s.observed_at} /></td>
                  <td data-label="Added"><Ts iso={s.ingested_at} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
