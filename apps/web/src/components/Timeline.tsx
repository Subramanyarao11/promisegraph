import type { EventType, Run, RunEvent } from "@contracts";
import { typed, type TypedEvent } from "../api/events";
import { Icon, type IconName } from "./Icon";
import { Badge, Mono, StatusBadge, Tag, Ts } from "./ui";
import { ACTION_STATUS, CONNECTOR_LABEL, KIND_LABEL, REQUEST_STATUS, RUN_STATUS, VERIFICATION, labelOf } from "../lib/status";
import { durationMs, humanize } from "../lib/format";

const str = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));

const TOOL = {
  search_context: "searched the knowledge base",
  expand_context: "followed connections",
  read_evidence: "read a source",
  read_memory: "checked rules and past attempts",
  prepare_action: "prepared a suggestion",
  commit_action: "saved the approved action",
  inspect_action: "checked the result",
  record_observation: "recorded a result",
} satisfies Record<string, string>;
const EV = {
  run_started: { label: "Started looking into it", icon: "play" },
  tool_started: { label: "Checking", icon: "chevron" },
  tool_completed: { label: "Checked", icon: "check" },
  proposal_ready: { label: "Suggested an action", icon: "doc" },
  action_updated: { label: "Action update", icon: "work" },
  waiting_for_input: { label: "Waiting for the customer", icon: "question" },
  observation_recorded: { label: "Result recorded", icon: "eye" },
  run_finished: { label: "Investigation finished", icon: "stop" },
  error: { label: "Problem", icon: "alert" },
} satisfies Record<EventType, { label: string; icon: IconName }>;

/** Labels are exhaustive over the contract's EventType; anything unexpected on the wire is shown as-is. */
function eventMeta(type: string): { label: string; icon: IconName } {
  return Object.hasOwn(EV, type) ? EV[type as EventType] : { label: type, icon: "dot" };
}

function Args({ args }: { args: Record<string, unknown> | undefined }) {
  if (!args) return null;
  return (
    <span className="ev-detail">
      {Object.entries(args).slice(0, 6).map(([k, v]) => (
        <span key={k}>
          {k}=<Mono>{str(v).slice(0, 80)}</Mono>{" "}
        </span>
      ))}
    </span>
  );
}

const toolLabel = (name: string | null) => (name ? labelOf(TOOL, name) : "a tool");

/** Narrowing on the event's `type` gives the matching payload shape (see api/events.ts). */
function EventBody({ ev }: { ev: TypedEvent }) {
  switch (ev.type) {
    case "run_started": {
      const { mode, stage } = ev.payload;
      return (
        <span className="row">
          <Tag tone={mode === "scripted" ? "mode" : "info"} icon="doc">{mode === "scripted" ? "Rule-based mode" : mode === "model" ? "AI model" : str(mode)}</Tag>
          {stage ? <span className="ev-detail">stage {stage}</span> : null}
        </span>
      );
    }
    case "tool_started":
      return (
        <span className="row">
          <span>{toolLabel(ev.tool_name)}</span>
          <Args args={ev.payload.arguments} />
        </span>
      );
    case "tool_completed": {
      const { ok, error_code, result_count, duration_ms, ids } = ev.payload;
      return (
        <span className="row">
          <span>{toolLabel(ev.tool_name)}</span>
          {ok === false ? <Tag tone="danger" icon="alert">didn't work ({str(error_code)})</Tag> : <Tag tone="success" icon="check">ok</Tag>}
          {typeof result_count === "number" && <span className="ev-detail">{result_count} found</span>}
          {typeof duration_ms === "number" && <span className="ev-detail">{durationMs(duration_ms)}</span>}
          {ids && ids.length > 0 && <span className="ev-detail">ids: {ids.slice(0, 5).join(", ")}{ids.length > 5 ? ` +${ids.length - 5}` : ""}</span>}
        </span>
      );
    }
    case "proposal_ready": {
      const { kind, proposal_id, validity } = ev.payload;
      return (
        <span className="row">
          <span className="ev-detail">{labelOf(KIND_LABEL, kind)}</span>
          <Mono>{str(proposal_id)}</Mono>
          {validity ? <Tag tone={validity === "valid" ? "success" : validity === "stale" ? "warn" : "danger"}>{validity}</Tag> : null}
        </span>
      );
    }
    case "action_updated": {
      const { action_id, status, connector } = ev.payload;
      return (
        <span className="row">
          <Mono>{str(action_id)}</Mono>
          <StatusBadge map={ACTION_STATUS} value={status} />
          {connector ? <Tag icon={connector === "linear" ? "external" : "work"}>{labelOf(CONNECTOR_LABEL, connector)}</Tag> : null}
        </span>
      );
    }
    case "waiting_for_input":
      return <span className="ev-detail">Draft question (not sent): “{str(ev.payload.question)}”</span>;
    case "observation_recorded": {
      const { kind, verification, observation_id } = ev.payload;
      return (
        <span className="row">
          <span className="ev-detail">{humanize(str(kind))}</span>
          <StatusBadge map={VERIFICATION} value={verification} />
          <Mono>{str(observation_id)}</Mono>
        </span>
      );
    }
    case "run_finished": {
      const { run_status, request_status } = ev.payload;
      return (
        <span className="row">
          <StatusBadge map={RUN_STATUS} value={run_status} prefix="Investigation" />
          {request_status ? <StatusBadge map={REQUEST_STATUS} value={request_status} prefix="Request" /> : null}
        </span>
      );
    }
    case "error":
      return (
        <span className="row">
          <Tag tone="danger" icon="alert">{str(ev.payload.code)}</Tag>
          <span className="ev-detail">{str(ev.payload.message)}</span>
        </span>
      );
  }
}

export function RunTimeline({ run, events, error, lastPolledAt, active }: { run: Run | undefined; events: RunEvent[]; error: unknown; lastPolledAt: number | null; active: boolean }) {
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
                  <Icon name={eventMeta(ev.type).icon} />
                  <span className="ev-type">{eventMeta(ev.type).label}</span>
                  <Ts iso={ev.occurred_at} />
                </div>
                <EventBody ev={typed(ev)} />
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
