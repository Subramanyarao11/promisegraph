import { skipToken, useQuery } from "@tanstack/react-query";
import type { Request, WorkItem } from "@contracts";
import { api } from "../api/client";
import { useCapabilities, useCustomers } from "../api/hooks";
import { Mono } from "./ui";

/**
 * Display name for an entity ID, resolved through the public API:
 * customers (/customers), capabilities (/capabilities), people (/people/{id}), work items (/work-items/{id}),
 * requests (/requests/{id}). Falls back to the raw ID (monospace, never guessed) when a lookup fails.
 */
export type EntityType = "customer" | "capability" | "person" | "work_item" | "request";

interface PersonSummary { id: string; name: string; active: boolean }

export function useEntityName(type: EntityType | string, id: string | null | undefined): { name: string | null; loading: boolean } {
  const customers = useCustomers();
  const caps = useCapabilities();
  const single = useQuery({
    queryKey: ["entity-name", type, id],
    staleTime: 60_000,
    retry: false,
    queryFn:
      id && (type === "person" || type === "work_item" || type === "request")
        ? async ({ signal }) => {
            const path = encodeURIComponent(id);
            if (type === "person") return (await api.get<PersonSummary>(`/people/${path}`, signal)).data.name;
            if (type === "work_item") return (await api.get<WorkItem>(`/work-items/${path}`, signal)).data.title;
            return (await api.get<Request>(`/requests/${path}`, signal)).data.subject;
          }
        : skipToken,
  });
  if (!id) return { name: null, loading: false };
  if (type === "customer") return { name: customers.data?.items.find((c) => c.id === id)?.name ?? null, loading: customers.isLoading };
  if (type === "capability") return { name: caps.data?.items.find((c) => c.id === id)?.name ?? null, loading: caps.isLoading };
  return { name: single.data ?? null, loading: single.isLoading };
}

const TYPE_WORD: Record<string, string> = { customer: "customer", capability: "product area", person: "person", work_item: "task", request: "request" };

/** Inline name; shows "the <type> <id>" in monospace only if the name can't be resolved. */
export function EntityName({ type, id, withType = false }: { type: EntityType | string; id: string; withType?: boolean }) {
  const { name, loading } = useEntityName(type, id);
  const word = TYPE_WORD[type] ?? type.replace(/_/g, " ");
  if (name) return <span title={id} data-testid="entity-name">{withType ? `${word} “${name}”` : name}</span>;
  if (loading) return <span className="muted">…</span>;
  return <span>{withType ? `${word} ` : ""}<Mono>{id}</Mono></span>;
}

/** "applies to <name>" for a decision/attempt scope. */
export function ScopeName({ scope, currentRequestId }: { scope: { entity_type: string; entity_id: string }; currentRequestId?: string }) {
  if (scope.entity_type === "request" && scope.entity_id === currentRequestId) return <span>this request</span>;
  return <EntityName type={scope.entity_type} id={scope.entity_id} withType={scope.entity_type !== "customer"} />;
}

/** Rule values that are IDs get names (routing_owner → the person's name). */
export function RuleValue({ ruleType, value }: { ruleType: string; value: unknown }) {
  if (ruleType === "routing_owner" && typeof value === "string") return <EntityName type="person" id={value} />;
  if (typeof value === "boolean") return <span>{value ? "yes" : "no"}</span>;
  if (Array.isArray(value)) return <span>{value.map((v) => KIND[String(v)] ?? String(v)).join(", ")}</span>;
  return <span>{String(value).replace(/_/g, " ")}</span>;
}
const KIND: Record<string, string> = { answer: "reply with an answer", clarify: "ask a question", link_work: "attach to existing work", create_work: "create a new task", escalate: "hand off to a person" };
