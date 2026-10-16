import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router";
import { Icon, type IconName } from "./components/Icon";
import { AnnouncerProvider, Badge } from "./components/ui";
import { useHealth, useInfo } from "./api/hooks";
import { WORKSPACE_ID, isUnavailable } from "./api/client";
import { CONNECTOR_STATE, spec } from "./lib/status";
import { formatTs, viewerZone } from "./lib/format";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/requests", label: "Requests", icon: "inbox" },
  { to: "/work", label: "Work", icon: "work" },
  { to: "/memory", label: "Memory", icon: "memory" },
  { to: "/sources", label: "Sources", icon: "source" },
  { to: "/settings", label: "Settings", icon: "settings" },
];

type Theme = "system" | "light" | "dark";
function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try { return (localStorage.getItem("pg.theme") as Theme) || "system"; } catch { return "system"; }
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

/** Planner mode labels come from /info; nothing is assumed when it cannot be read. */
export function ModeBadges() {
  const info = useInfo();
  if (info.isLoading) return null;
  if (info.error || !info.data) return <Badge s={{ label: "Planner status unknown", tone: "neutral", icon: "question" }} />;
  const p = info.data.data.planners;
  const scripted = spec(CONNECTOR_STATE, p.scripted);
  const model = spec(CONNECTOR_STATE, p.model);
  const modelUsable = p.model === "enabled" || p.model === "verified" || p.model === "unverified";
  return (
    <span className="row" data-testid="mode-badges">
      <Badge s={{ label: "Scripted mode", tone: "mode", icon: "doc" }} title={`Scripted planner: ${scripted.label}`} />
      <Badge s={{ label: modelUsable ? `Live model: ${model.label}` : `Live model unavailable (${model.label.toLowerCase()})`, tone: modelUsable ? model.tone : "neutral", icon: modelUsable ? model.icon : "ban" }} testId="model-state" />
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
        ? "API unreachable. Nothing below is live until it reconnects; no sample data is substituted."
        : "Database unavailable. Reads and actions are paused; no cached or sample data is shown as live."}
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
            Workspace <code className="mono">{d?.workspace_id ?? WORKSPACE_ID}</code>
            {d?.name ? <div>{d.name}</div> : null}
          </div>
          <nav className="nav" aria-label="Sections">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to}>
                <Icon name={n.icon} />
                {n.label}
              </NavLink>
            ))}
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
                {d.clock_mode === "scenario" ? "Scenario clock" : "Live clock"} · reference {formatTs(d.reference_time)} · times shown in {viewerZone()}
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
