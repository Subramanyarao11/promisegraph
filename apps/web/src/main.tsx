import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router";
import "./styles.css";
import { retryPolicy } from "./api/hooks";
import { Shell } from "./App";
import { RequestsPage } from "./routes/Requests";
import { RequestDetail } from "./routes/RequestDetail";
import { WorkPage } from "./routes/Work";
import { MemoryPage } from "./routes/Memory";
import { SourcesPage } from "./routes/Sources";
import { SettingsPage } from "./routes/Settings";
import { NotFoundPage } from "./routes/NotFound";
import { RouteError } from "./routes/RouteError";

const qc = new QueryClient({
  defaultOptions: {
    queries: { retry: retryPolicy, refetchOnWindowFocus: true, staleTime: 2000 },
    mutations: { retry: false },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    // Router-level error boundary: a render error shows a recoverable page instead of a blank screen.
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/requests" replace /> },
      { path: "requests", element: <RequestsPage /> },
      { path: "requests/:id", element: <RequestDetail /> },
      { path: "work", element: <WorkPage /> },
      { path: "memory", element: <MemoryPage /> },
      { path: "sources", element: <SourcesPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("index.html is missing the #root element");
createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
