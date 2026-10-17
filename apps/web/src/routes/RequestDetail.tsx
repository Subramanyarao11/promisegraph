import { Link, useParams } from "react-router";
import { ApiError } from "../api/client";
import { Empty, ErrorPanel, Loading, Mono } from "../components/ui";
import { SourceDrawerProvider } from "../components/Evidence";
import { useInvestigation } from "./request/useInvestigation";
import { ActionSection, AskedSection, FoundSection, NextSection, RecommendationSection, RequestHeader, TechnicalDetails } from "./request/sections";

export function RequestDetail() {
  const { id = "" } = useParams();
  return (
    <SourceDrawerProvider>
      {/* key: a different request gets fresh state (toggles, timelines) without reset effects. */}
      <Workspace key={id} id={id} />
    </SourceDrawerProvider>
  );
}

/** Reads top-down as a story: asked → found → recommended → action → what next; technical detail last. */
function Workspace({ id }: { id: string }) {
  const inv = useInvestigation(id);
  const { req, ctx, actionsQ } = inv;

  if (req.isLoading) return <Loading label="Loading request" />;
  if (req.error) {
    if (req.error instanceof ApiError && req.error.code === "NOT_FOUND")
      return (
        <div data-testid="state-unknown-entity">
          <Empty title="Unknown request" icon="question">
            <p>We couldn't find this request (<Mono>{id}</Mono>). The link may be wrong, or it belongs to another workspace.</p>
            <p><Link to="/requests">Back to requests</Link></p>
          </Empty>
        </div>
      );
    return <ErrorPanel error={req.error} onRetry={() => req.refetch()} what="request" />;
  }
  if (!req.data) return null;
  const request = req.data.data;

  return (
    <div data-testid="request-workspace" className="story">
      <RequestHeader inv={inv} request={request} />
      {inv.unavailable && <ErrorPanel error={ctx.error ?? actionsQ.error} onRetry={() => { void ctx.refetch(); void actionsQ.refetch(); }} what="investigation" />}
      <AskedSection inv={inv} request={request} />
      <FoundSection inv={inv} request={request} />
      <RecommendationSection inv={inv} request={request} />
      <ActionSection inv={inv} request={request} />
      <NextSection inv={inv} request={request} />
      <TechnicalDetails inv={inv} request={request} />
    </div>
  );
}
