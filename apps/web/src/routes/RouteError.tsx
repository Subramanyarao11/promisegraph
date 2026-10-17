import { isRouteErrorResponse, useRouteError } from "react-router";

/**
 * Router-level error boundary (react-router `errorElement`). It renders outside the app shell, so it is
 * self-contained, keeps the user's place recoverable, and shows no stack trace.
 */
export function RouteError() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : error instanceof Error ? error.message : "Unknown error";
  return (
    <main className="content" style={{ maxWidth: 640 }} role="alert" data-testid="route-error">
      <h1>Something went wrong on this page</h1>
      <p style={{ margin: "8px 0" }}>This was a display error, so nothing on the server was changed. Reload to try again.</p>
      <p className="small muted">Details: {message}</p>
      <p style={{ marginTop: 12 }}>
        <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>Reload</button>{" "}
        <a className="btn" href="/requests">Go to requests</a>
      </p>
    </main>
  );
}
