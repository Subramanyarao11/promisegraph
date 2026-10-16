import type { Run, RunEvent } from "@contracts";
import { Icon, type IconName } from "./Icon";
import { Badge, Mono, StatusBadge, Tag, Ts } from "./ui";
import { ACTION_STATUS, CONNECTOR_LABEL, KIND_LABEL, REQUEST_STATUS, RUN_STATUS, VERIFICATION } from "../lib/status";
import { durationMs, humanize } from "../lib/format";

type P = Record<string, unknown>;
const str = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));

const TOOL: Record<string, string> = {
  search_context: "searched the knowledge base",
  expand_context: "followed connections",
  read_evidence: "read a source",
  read_memory: "checked rules and past attempts",
  prepare_action: "prepared a suggestion",
  commit_action: "saved the approved action",
  inspect_action: "checked the result",
  record_observation: "recorded a result",
};
const EV: Record<string, { label: string; icon: IconName }> = {
  run_started: { label: "Started looking into it", icon: "play" },
  tool_started: { label: "Checking", icon: "chevron" },
  tool_completed: { label: "Checked", icon: "check" },
  proposal_ready: { label: "Suggested an action", icon: "doc" },
  action_updated: { label: "Action update", icon: "work" },
  waiting_for_input: { label: "Waiting for the customer", icon: "question" },
  observation_recorded: { label: "Result recorded", icon: "eye" },
  run_finished: { label: "Investigation finished", icon: "stop" },
  error: { label: "Problem", icon: "alert" },
};

function Args({ args }: { args: unknown }) {
  if (!args || typeof args !== "object") return null;
  const entries = Object.entries(args as P).slice(0, 6);
  return (
    <span className="ev-detail">
      {entries.map(([k, v]) => (
        <span key={k}>
          {k}=<Mono>{str(v).slice(0, 80)}</Mono>{" "}
        </span>
      ))}
    </span>
  );
}

function EventBody({ ev }: { ev: RunEvent }) {
  const p = (ev.payload ?? {}) as P;
  switch (ev.type) {
    case "run_started":
      return (
        <span className="row">
          <Tag tone={p.mode === "scripted" ? "mode" : "info"} icon="doc">{p.mode === "scripted" ? "Rule-based mode" : p.mode === "model" ? "AI model" : str(p.mode)}</Tag>
          {p.stage ? <span className="ev-detail">stage {str(p.stage)}</span> : null}
        </span>
      );
    case "tool_started":
      return (
        <span className="row">
          <span>{TOOL[ev.tool_name ?? ""] ?? ev.tool_name ?? "a tool"}</span>
          <Args args={p.arguments} />
        </span>
      );
    case "tool_completed":
      return (
        <span className="row">
          <span>{TOOL[ev.tool_name ?? ""] ?? ev.tool_name ?? "a tool"}</span>
          {p.ok === false ? <Tag tone="danger" icon="alert">didn't work ({str(p.error_code)})</Tag> : <Tag tone="success" icon="check">ok</Tag>}
          {typeof p.result_count === "number" && <span className="ev-detail">{p.result_count} found</span>}
          {typeof p.duration_ms === "number" && <span className="ev-detail">{durationMs(p.duration_ms)}</span>}
          {Array.isArray(p.ids) && p.ids.length > 0 && <span className="ev-detail">ids: {(p.ids as string[]).slice(0, 5).join(", ")}{p.ids.length > 5 ? ` +${p.ids.length - 5}` : ""}</span>}
        </span>
      );
    case "proposal_ready":
      return (
        <span className="row">
          <span className="ev-detail">{KIND_LABEL[str(p.kind)] ?? humanize(str(p.kind))}</span>
          <Mono>{str(p.proposal_id)}</Mono>
          {p.validity ? <Tag tone={p.validity === "valid" ? "success" : p.validity === "stale" ? "warn" : "danger"}>{str(p.validity)}</Tag> : null}
        </span>
      );
    case "action_updated":
      return (
        <span className="row">
          <Mono>{str(p.action_id)}</Mono>
          <StatusBadge map={ACTION_STATUS} value={str(p.status)} />
          {p.connector ? <Tag icon={p.connector === "linear" ? "external" : "work"}>{CONNECTOR_LABEL[str(p.connector)] ?? str(p.connector)}</Tag> : null}
        </span>
      );
    case "waiting_for_input":
      return <span className="ev-detail">Draft question (not sent): “{str(p.question)}”</span>;
    case "observation_recorded":
      return (
        <span className="row">
          <span className="ev-detail">{humanize(str(p.kind))}</span>
          <StatusBadge map={VERIFICATION} value={str(p.verification)} />
          <Mono>{str(p.observation_id)}</Mono>
        </span>
      );
    case "run_finished":
      return (
        <span className="row">
          <StatusBadge map={RUN_STATUS} value={str(p.run_status)} prefix="Investigation" />
          {p.request_status ? <StatusBadge map={REQUEST_STATUS} value={str(p.request_status)} prefix="Request" /> : null}
        </span>
      );
    case "error":
      return (
        <span className="row">
          <Tag tone="danger" icon="alert">{str(p.code)}</Tag>
          <span className="ev-detail">{str(p.message)}</span>
        </span>
      );
    default:
      return <span className="ev-detail">{str(p).slice(0, 200)}</span>;
  }
}

export function RunTimeline({ run, events, error, lastPolledAt, active }: { run: Run | undefined; runId: string; events: RunEvent[]; error: unknown; lastPolledAt: number | null; active: boolean }) {
  return (
    <div className="stack">
      <div className="row small muted">
        {run && <StatusBadge map={RUN_STATUS} value={run.status} />}
        {run && <span>step: {run.stage}</span>}
        {run && <Badge s={{ label: run.mode === "scripted" ? "Rule-based mode" : "AI model", tone: "mode", icon: "doc" }} />}
        {run && <span>{run.usage.tool_calls} of {run.budget.max_tool_calls} checks used</span>}
        <span aria-live="off">{active ? "Updating every 1.5 s" : "Updating less often while idle"}{lastPolledAt ? ` · last ${new Date(lastPolledAt).toLocaleTimeString()}` : ""}</span>
      </div>
      {Boolean(error) && <p className="small" role="status" style={{ color: "var(--warn-fg)" }}><Icon name="alert" /> Couldn't refresh the log; trying again. What's shown is what we already had.</p>}
      {events.length === 0 ? (
        <p className="muted">Nothing logged yet{active ? ". It’s just starting." : "."}</p>
      ) : (
        <ol className="timeline" aria-label={`Run ${run?.id ?? ""} events`} data-testid="timeline">
          {events.map((ev) => (
            <li key={`${ev.run_id}:${ev.seq}`} data-testid="timeline-event" data-type={ev.type}>
              <span className="seq">#{ev.seq}</span>
              <div className="stack" style={{ gap: 2 }}>
                <div className="ev-line">
                  <Icon name={EV[ev.type]?.icon ?? "dot"} />
                  <span className="ev-type">{EV[ev.type]?.label ?? ev.type}</span>
                  <Ts iso={ev.occurred_at} />
                </div>
                <EventBody ev={ev} />
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
