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
          <p>Actual capability and connection states reported by the API. Nothing here is assumed by the browser.</p>
        </div>
      </div>
      <div className="stack-lg">
        <Callout tone="info" icon="lock" title="Credentials stay on the server">
          <p>Model and Linear keys are provisioned in the server environment. This browser never receives them; requests go through a proxy that adds the operator credential server-side.</p>
        </Callout>
        {info.isLoading ? <Loading label="Loading workspace info" /> : info.error ? <ErrorPanel error={info.error} onRetry={() => info.refetch()} what="workspace info" /> : d && (
          <div className="grid-2">
            <Section title="Workspace">
              <dl className="kv">
                <dt>ID</dt><dd><Mono>{d.workspace_id}</Mono></dd>
                <dt>Name</dt><dd>{d.name}</dd>
                <dt>Clock</dt><dd>{d.clock_mode === "scenario" ? <Tag tone="warn" icon="clock">Scenario clock (synthetic time)</Tag> : <Tag icon="clock">Live clock</Tag>}</dd>
                <dt>Reference time</dt><dd><Ts iso={d.reference_time} /></dd>
                <dt>Your time zone</dt><dd>{viewerZone()}</dd>
                <dt>Contract</dt><dd>{info.data?.meta.contract_version} (client expects {CONTRACT_VERSION})</dd>
                <dt>Knowledge rev</dt><dd>{info.data?.meta.knowledge_revision}</dd>
              </dl>
            </Section>
            <Section title="Planners">
              <dl className="kv" data-testid="planner-states">
                <dt>Scripted</dt><dd className="row"><StatusBadge map={CONNECTOR_STATE} value={d.planners.scripted} /> <span className="small muted">Labelled “Scripted mode”</span></dd>
                <dt>Live model</dt><dd className="row"><StatusBadge map={CONNECTOR_STATE} value={d.planners.model} testId="settings-model" /></dd>
              </dl>
              {d.planners.model !== "enabled" && d.planners.model !== "verified" && (
                <p className="small muted" style={{ marginTop: 8 }}>A run requested with the live model while it is unavailable fails with CAPABILITY_DISABLED; it is never silently run in Scripted mode.</p>
              )}
            </Section>
            <Section title="Connectors">
              <dl className="kv" data-testid="connector-states">
                <dt>Local task</dt><dd><StatusBadge map={CONNECTOR_STATE} value={d.connectors.local} /></dd>
                <dt>Linear</dt><dd className="row"><StatusBadge map={CONNECTOR_STATE} value={d.connectors.linear} testId="settings-linear" />{d.connectors.linear === "unverified" && <span className="small muted">Implemented; no verified readback yet</span>}</dd>
              </dl>
              {(d.connectors.linear === "disabled" || d.connectors.linear === "unconfigured") && (
                <p className="small muted" style={{ marginTop: 8 }}>Proposals targeting Linear are marked invalid with CAPABILITY_DISABLED. Local receipts are never labelled as Linear.</p>
              )}
            </Section>
            <Section title="Commit policy">
              <p className="small">Kinds the worker may commit without an operator (internal drafts only):</p>
              <div className="row" style={{ marginTop: 6 }}>{(d.auto_commit_kinds as string[]).length ? (d.auto_commit_kinds as string[]).map((k) => <Tag key={k}>{KIND_LABEL[k] ?? k}</Tag>) : <span className="muted">None</span>}</div>
              <p className="small muted" style={{ marginTop: 6 }}>Linking or creating work and escalation need your authorization. No policy sends messages.</p>
            </Section>
          </div>
        )}
        <Section title="Service readiness">
          {health.isLoading ? <Loading /> : (
            <dl className="kv">
              <dt>Readiness</dt>
              <dd>{health.data?.ok ? <Tag tone="success" icon="check">Ready</Tag> : <Tag tone="danger" icon="alert">Not ready{health.data?.status ? ` (HTTP ${health.data.status})` : " (unreachable)"}</Tag>}</dd>
              <dt>Workspace in use</dt><dd><Mono>{WORKSPACE_ID}</Mono></dd>
              <dt>API target (dev proxy)</dt><dd><Mono>{__API_TARGET__}</Mono></dd>
              {health.data?.body ? <><dt>Detail</dt><dd><Mono>{JSON.stringify(health.data.body).slice(0, 300)}</Mono></dd></> : null}
            </dl>
          )}
        </Section>
      </div>
    </>
  );
}
