import type { EventsPage, RunEvent } from "@contracts";
import { api, qs } from "./client";

export interface Timeline {
  events: RunEvent[];
  error: unknown;
  lastPolledAt: number | null;
}

/**
 * Durable event-timeline poller for one run (plain TS, no React).
 * Cursor = next_after_seq; 1.5 s while the run is active, exponential backoff (3→30 s) otherwise or on error.
 * Entries are deduplicated by (run_id, seq); a retried page adds nothing.
 */
export class RunPoller {
  private seen = new Map<string, RunEvent>();
  private cursor = 0;
  private active = false;
  private idleDelay = 3000;
  private initial = true;
  private stopped = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly ctrl = new AbortController();

  constructor(
    private readonly runId: string,
    private readonly onChange: (t: Timeline) => void,
    private readonly onNew: (events: RunEvent[], initial: boolean) => void,
  ) {
    void this.tick();
  }

  setActive(active: boolean): void {
    const wasActive = this.active;
    this.active = active;
    if (active && !wasActive) this.wake();
  }

  stop(): void {
    this.stopped = true;
    this.ctrl.abort();
    if (this.timer) clearTimeout(this.timer);
  }

  private wake(): void {
    this.idleDelay = 3000;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => void this.tick(), 0);
    }
  }

  private async tick(): Promise<void> {
    this.timer = null;
    let delay: number;
    try {
      const fresh: RunEvent[] = [];
      let more = true;
      while (more && !this.stopped) {
        const page = (await api.rawGet(`/runs/${encodeURIComponent(this.runId)}/events` + qs({ after_seq: this.cursor, limit: 100 }), this.ctrl.signal)) as EventsPage;
        for (const ev of page.data) {
          const k = `${ev.run_id}:${ev.seq}`;
          if (!this.seen.has(k)) {
            this.seen.set(k, ev);
            fresh.push(ev);
          }
        }
        if (page.next_after_seq > this.cursor) this.cursor = page.next_after_seq;
        more = page.has_more && page.data.length > 0;
      }
      if (this.stopped) return;
      this.onChange({ events: [...this.seen.values()].sort((a, b) => a.seq - b.seq), error: null, lastPolledAt: Date.now() });
      const firstLoad = this.initial;
      this.initial = false;
      if (fresh.length) {
        this.onNew(fresh, firstLoad);
        this.idleDelay = 3000;
      }
      delay = this.active ? 1500 : this.idleDelay;
      if (!this.active) this.idleDelay = Math.min(this.idleDelay * 2, 30_000);
    } catch (err) {
      if (this.stopped || (err instanceof DOMException && err.name === "AbortError")) return;
      this.onChange({ events: [...this.seen.values()].sort((a, b) => a.seq - b.seq), error: err, lastPolledAt: Date.now() });
      delay = this.idleDelay;
      this.idleDelay = Math.min(this.idleDelay * 2, 30_000);
    }
    if (!this.stopped) this.timer = setTimeout(() => void this.tick(), delay);
  }
}
