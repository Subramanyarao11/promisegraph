import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router";
import { Icon, type IconName } from "./components/Icon";
import { AnnouncerProvider, Badge } from "./components/ui";
import { useHealth, useInfo } from "./api/hooks";
import { WORKSPACE_ID, isUnavailable } from "./api/client";
import { CONNECTOR_STATE, spec } from "./lib/status";
import { formatTs, oneOf, viewerZone } from "./lib/format";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/requests", label: "Requests", icon: "inbox" },
  { to: "/work", label: "Work", icon: "work" },
  { to: "/memory", label: "Company rules & history", icon: "memory" },
  { to: "/sources", label: "Knowledge base", icon: "source" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

const THEMES = ["system", "light", "dark"] as const;
type Theme = (typeof THEMES)[number];
function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try { return oneOf(THEMES, localStorage.getItem("pg.theme") ?? "", "system"); } catch { return "system"; }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try { localStorage.setItem("pg.theme", theme); } catch { /* ignore */ }
  }, [theme]);
  const next = () => setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system"));
  return [theme, next];
}

/** Planner mode from /info, in plain words. Nothing is assumed when it cannot be read. */
export function ModeBadges() {
  const info = useInfo();
  if (info.isLoading) return null;
  if (info.error || !info.data) return <Badge s={{ label: "Mode unknown (couldn't reach the server)", tone: "neutral", icon: "question" }} />;
  const p = info.data.data.planners;
  const model = spec(CONNECTOR_STATE, p.model);
  const modelUsable = p.model === "enabled" || p.model === "verified" || p.model === "unverified";
  return (
    <span className="row" data-testid="mode-badges">
      {modelUsable ? (
        <>
          <Badge s={{ label: "Rule-based mode available", tone: "mode", icon: "doc" }} />
          <Badge s={{ label: `AI model connected (${model.label.toLowerCase()})`, tone: model.tone, icon: model.icon }} testId="model-state" />
        </>
      ) : (
        <Badge
          s={{ label: "Rule-based mode (no AI model connected)", tone: "mode", icon: "doc" }}
          title="PromiseGraph follows fixed rules over your real data. No AI model is used."
          testId="model-state"
        />
      )}
    </span>
  );
}

function HealthBanner() {
  const health = useHealth();
  const info = useInfo();
  const h = health.data;
  if (!h) return null;
  if (h.ok && !isUnavailable(info.error)) return null;
  const unreachable = h.status === null || h.status === 502 || h.status === 504 || (h.status === 500 && !h.body);
  return (
    <div className="banner tone-danger" role="alert" data-testid="banner-unavailable">
      <Icon name="alert" />
      {unreachable
        ? "Can't reach the PromiseGraph server. Nothing on this page is live until it reconnects. We never show sample data instead."
        : "Database unavailable. Reading and approving are paused until it's back. We never show old or sample data as if it were live."}
    </div>
  );
}

export function Shell() {
  const [theme, nextTheme] = useTheme();
  const info = useInfo();
  const loc = useLocation();
  useEffect(() => {
    const main = document.getElementById("main");
    main?.focus({ preventScroll: true });
  }, [loc.pathname]);
  const d = info.data?.data;
  return (
    <AnnouncerProvider>
      <a href="#main" className="skip-link">Skip to content</a>
      <div className="shell">
        <aside className="sidebar" aria-label="Primary">
          <div className="brand">
            <Icon name="graph" size={18} />
            PromiseGraph
          </div>
          <div className="ws" title="Workspace">
            {d?.name ?? "Workspace"}
            <div className="small muted" title="Workspace ID">{d?.workspace_id ?? WORKSPACE_ID}</div>
          </div>
          <nav className="nav" aria-label="Sections">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.to === "/requests"}>
                <Icon name={n.icon} />
                {n.label}
              </NavLink>
            ))}
            <Link to="/requests?help=1" data-testid="nav-how-it-works">
              <Icon name="info" />
              How it works
            </Link>
          </nav>
          <div className="sidebar-foot">
            <button type="button" className="btn btn-ghost btn-sm" onClick={nextTheme} aria-label={`Theme: ${theme}. Change theme`}>
              <Icon name={theme === "dark" ? "moon" : "sun"} /> Theme: {theme}
            </button>
          </div>
        </aside>
        <div className="main">
          <HealthBanner />
          <div className="topbar">
            <ModeBadges />
            <span className="spacer" />
            {d && (
              <span className="small muted" data-testid="clock">
                {d.clock_mode === "scenario" ? <>Demo clock: it is {formatTs(d.reference_time)} for this demo data</> : "Live clock"} · times shown in {viewerZone()}
              </span>
            )}
          </div>
          <main id="main" className="content" tabIndex={-1}>
            <Outlet />
          </main>
        </div>
      </div>
    </AnnouncerProvider>
  );
}
