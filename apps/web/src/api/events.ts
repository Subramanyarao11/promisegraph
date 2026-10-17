import type { EventType, RunEvent } from "@contracts";

/**
 * RunEvent.payload is `{}` in the contract (PROTOCOL.md "Event payload conventions" fixes its keys per type).
 * Payloads are not schema-validated on the wire, so every field is optional here and consumers handle absence.
 */
export interface EventPayloads {
  run_started: { mode?: string; stage?: string };
  tool_started: { call_id?: string; arguments?: Record<string, unknown> };
  tool_completed: { call_id?: string; ok?: boolean; result_count?: number; ids?: string[]; duration_ms?: number; error_code?: string | null };
  proposal_ready: { proposal_id?: string; kind?: string; validity?: string };
  action_updated: { action_id?: string; status?: string; connector?: string };
  waiting_for_input: { action_id?: string; question?: string };
  observation_recorded: { observation_id?: string; kind?: string; verification?: string };
  run_finished: { run_status?: string; request_status?: string; action_id?: string | null };
  error: { code?: string; message?: string };
}

/** Discriminated union over `type`: narrowing on `ev.type` gives the matching payload shape. */
export type TypedEvent = { [K in EventType]: Omit<RunEvent, "type" | "payload"> & { type: K; payload: EventPayloads[K] } }[EventType];

/** The single trust-boundary cast for event payloads. */
export function typed(ev: RunEvent): TypedEvent {
  return ev as unknown as TypedEvent;
}
