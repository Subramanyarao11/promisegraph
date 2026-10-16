export type IconName =
  | "circle" | "progress" | "question" | "eye" | "clock" | "shield" | "check" | "up" | "x" | "alert"
  | "pause" | "play" | "stop" | "dot" | "link" | "plus" | "doc" | "graph" | "inbox" | "work" | "memory"
  | "source" | "settings" | "chevron" | "refresh" | "lock" | "ban" | "info" | "sun" | "moon" | "external";

const P: Record<IconName, string> = {
  circle: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z",
  progress: "M8 2.5a5.5 5.5 0 1 0 5.5 5.5M8 2.5V8l3.5 2",
  question: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM6.3 6.4a1.8 1.8 0 1 1 2.4 1.7c-.5.2-.7.6-.7 1.1M8 11h.01",
  eye: "M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8ZM8 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z",
  clock: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM8 5v3l2 1.5",
  shield: "M8 1.8 13 3.6v4c0 3-2.2 5.3-5 6.6-2.8-1.3-5-3.6-5-6.6v-4L8 1.8Z",
  check: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM5.5 8.2l1.7 1.7 3.3-3.6",
  up: "M8 13V3M4 7l4-4 4 4",
  x: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM6 6l4 4M10 6l-4 4",
  alert: "M8 2 14.5 13.5h-13L8 2ZM8 6.5v3M8 11.5h.01",
  pause: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM6.7 6v4M9.3 6v4",
  play: "M5 3.5v9l7-4.5-7-4.5Z",
  stop: "M4 4h8v8H4z",
  dot: "M8 6.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z",
  link: "M6.5 9.5l3-3M5.3 7.7 4 9a2.1 2.1 0 0 0 3 3l1.3-1.3M10.7 8.3 12 7a2.1 2.1 0 0 0-3-3L7.7 5.3",
  plus: "M8 3v10M3 8h10",
  doc: "M4 1.8h5.5L12.5 5v9.2H4V1.8ZM9.5 1.8V5h3M6 8h4.5M6 10.5h4.5",
  graph: "M4 8a1.5 1.5 0 1 0 0 .01M12 4a1.5 1.5 0 1 0 0 .01M12 12a1.5 1.5 0 1 0 0 .01M5.4 7.3l5.2-2.6M5.4 8.7l5.2 2.6",
  inbox: "M2 9h3.5l1 1.5h3l1-1.5H14M2 9l1.8-5.5h8.4L14 9v4.5H2V9Z",
  work: "M2.5 5h11v8h-11zM6 5V3.2h4V5",
  memory: "M3 3.5h10v9H3zM5.5 6h5M5.5 8h5M5.5 10h3",
  source: "M3 2.5h7l3 3v8H3zM5.5 7h5M5.5 9.5h5",
  settings: "M8 5.8a2.2 2.2 0 1 0 0 4.4 2.2 2.2 0 0 0 0-4.4ZM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4",
  chevron: "M6 4l4 4-4 4",
  refresh: "M13 8a5 5 0 1 1-1.5-3.5M13 2.5v3h-3",
  lock: "M4 7h8v6.5H4zM5.8 7V5a2.2 2.2 0 1 1 4.4 0v2",
  ban: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM4.1 4.1l7.8 7.8",
  info: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM8 7.3v3.5M8 5.2h.01",
  sun: "M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5ZM8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1",
  moon: "M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5Z",
  external: "M9 2.5h4.5V7M13.5 2.5 7.5 8.5M11.5 9.5v4h-9v-9h4",
};

export function Icon({ name, size = 14, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg className={`icon ${className ?? ""}`} width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={P[name]} />
    </svg>
  );
}
