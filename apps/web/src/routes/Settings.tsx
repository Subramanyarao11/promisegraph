import { useHealth, useInfo } from "../api/hooks";
import { CONTRACT_VERSION, WORKSPACE_ID } from "../api/client";
import { Callout, ErrorPanel, Loading, Mono, Section, StatusBadge, Tag, Ts } from "../components/ui";
import { CONNECTOR_STATE, KIND_LABEL } from "../lib/status";
import { viewerZone } from "../lib/format";

export function SettingsPage() {
  const info = useInfo();
  const health = useHealth();
  const d = info.data?.data;
  return (
    <>
      <div className="page-head">
        <div className="grow">
          <h1>Settings</h1>
          <p>What's switched on and connected right now, as reported by the PromiseGraph server.</p>
        </div>
      </div>
      <div className="stack-lg">
        <Callout tone="info" icon="lock" title="Passwords and keys stay on the server">
          <p>Keys for the AI model and Linear are set up on the server by an administrator. They're never sent to your browser.</p>
        </Callout>
        {info.isLoading ? <Loading label="Loading workspace info" /> : info.error ? <ErrorPanel error={info.error} onRetry={() => info.refetch()} what="workspace info" /> : d && (
          <div className="grid-2">
            <Section title="Workspace">
              <dl className="kv">
                <dt>Name</dt><dd>{d.name}</dd>
                <dt>ID</dt><dd><Mono>{d.workspace_id}</Mono></dd>
                <dt>Clock</dt><dd>{d.clock_mode === "scenario" ? <Tag tone="warn" icon="clock">Demo clock: dates are judged as of a fixed demo time</Tag> : <Tag icon="clock">Real time</Tag>}</dd>
                <dt>“Now” for this workspace</dt><dd><Ts iso={d.reference_time} /></dd>
                <dt>Your time zone</dt><dd>{viewerZone()}</dd>
                <dt>Technical</dt><dd className="small muted">API contract {info.data?.meta.contract_version} (app expects {CONTRACT_VERSION}) · knowledge revision {info.data?.meta.knowledge_revision}</dd>
              </dl>
            </Section>
            <Section title="How PromiseGraph thinks">
              <dl className="kv" data-testid="planner-states">
                <dt>Rule-based mode</dt><dd className="row"><StatusBadge map={CONNECTOR_STATE} value={d.planners.scripted} /> <span className="small muted">Fixed rules over your real data</span></dd>
                <dt>AI model</dt><dd className="row"><StatusBadge map={CONNECTOR_STATE} value={d.planners.model} testId="settings-model" /></dd>
              </dl>
              {d.planners.model !== "enabled" && d.planners.model !== "verified" && (
                <p className="small muted" style={{ marginTop: 8 }}>No AI model is connected. If someone asks for the AI model anyway, PromiseGraph says so instead of quietly using rule-based mode.</p>
              )}
            </Section>
            <Section title="Where tasks are created">
              <dl className="kv" data-testid="connector-states">
                <dt>PromiseGraph task list</dt><dd><StatusBadge map={CONNECTOR_STATE} value={d.connectors.local} /></dd>
                <dt>Linear</dt><dd className="row"><StatusBadge map={CONNECTOR_STATE} value={d.connectors.linear} testId="settings-linear" />{d.connectors.linear === "unverified" && <span className="small muted">Switched on, but we haven't confirmed it works yet</span>}</dd>
              </dl>
              {(d.connectors.linear === "disabled" || d.connectors.linear === "unconfigured") && (
                <p className="small muted" style={{ marginTop: 8 }}>Linear isn't connected, so new tasks go to PromiseGraph's own task list. We never label those as Linear issues.</p>
              )}
            </Section>
            <Section title="What happens without your approval">
              <p className="small">PromiseGraph may save these drafts by itself (nothing is sent):</p>
              <div className="row" style={{ marginTop: 6 }}>{(d.auto_commit_kinds as string[]).length ? (d.auto_commit_kinds as string[]).map((k) => <Tag key={k}>{KIND_LABEL[k] ?? k}</Tag>) : <span className="muted">None</span>}</div>
              <p className="small muted" style={{ marginTop: 6 }}>Attaching to work, creating tasks and handing off always need your approval. PromiseGraph never sends messages to customers.</p>
            </Section>
          </div>
        )}
        <Section title="System status">
          {health.isLoading ? <Loading /> : (
            <dl className="kv">
              <dt>Server</dt>
              <dd>{health.data?.ok ? <Tag tone="success" icon="check">Working</Tag> : <Tag tone="danger" icon="alert">Not working{health.data?.status ? ` (HTTP ${health.data.status})` : " (can't reach it)"}</Tag>}</dd>
              <dt>Workspace in use</dt><dd><Mono>{WORKSPACE_ID}</Mono></dd>
              <dt>Server address (dev)</dt><dd><Mono>{__API_TARGET__}</Mono></dd>
              {health.data?.body ? <><dt>Detail</dt><dd><Mono>{JSON.stringify(health.data.body).slice(0, 300)}</Mono></dd></> : null}
            </dl>
          )}
        </Section>
      </div>
    </>
  );
}
