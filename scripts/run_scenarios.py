#!/usr/bin/env python3
"""PromiseGraph scenario runner (G2 acceptance evidence). Coordinator-owned; test-only.

Executes fixtures/scenarios/t*.json (and optionally holdouts) against the REAL stack: the real API
(services/api on FalkorDB) and the real worker (services/worker). It never talks to the protocol mock.

Run:
  uv run --no-project --with httpx==0.28.1 --with jsonschema==4.25.1 python scripts/run_scenarios.py \
      --manage --port 8020 [--only t04,t12] [--holdouts] [--json out.json]
  ... --base-url http://127.0.0.1:8020          (use an already running stack; see "Stacks" below)

Stacks:
  --manage   the runner owns its stacks through scripts/stack.sh: one shared freshly seeded stack on --port
             for scenarios without preconditions, and a dedicated freshly seeded stack on --secondary-port
             (default --port+1; e.g. --port 8040 --secondary-port 8041 to run beside other users) for every
             scenario with `fresh_seed` or a `server env K=V` precondition (env applied, e.g. FAULTS) and for
             the holdout set. Every stack is torn down (graphs deleted) afterwards unless --keep.
  --base-url run everything against one existing stack. `fresh_seed` scenarios run first; scenarios that need
             server env (FAULTS) are SKIPPED unless --fault-base-url points at a stack started with that env.
             `stable_after_restart` is only evaluated with --manage (it restarts the API process).

Semantics (fixtures/scenarios/_conventions.json, plus the interpretations documented here):
  * `${sN.path}` resolves against step sN's capture; `${knowledge_revision}` = GET /info meta.knowledge_revision.
  * Captures: create_request {request, run}; await_run {run, proposal (latest for the run), latest_action
    (latest action of the run), actions (all actions of the request), request, context, run_ids};
    commit {action} or {error}; reply {run, ...}; record_observation {observation, request}; etc.
  * Context assertions on an await_run step (context_includes/excludes/...) evaluate the context the run
    actually used: GET /requests/{id}/context at capture time (typed fields) UNION the ids returned by the
    run's tool_completed events (classified by id prefix: attempt_ -> attempts, decision_ -> decisions,
    work_ -> related_work). On a get_request_context step they evaluate that bundle only.
  * work_item_count_delta = GET /work-items count after the step minus the count at scenario start
    (scenario start == post-seed baseline on a fresh stack).
  * Every response is validated with contracts/tools/validate.py; violations fail the scenario (`contract`).
  * optional=true expectations are reported but never fail the scenario.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from contracts.tools.validate import errors as schema_errors  # noqa: E402

SCEN_DIR = ROOT / "fixtures" / "scenarios"
TERMINAL = {"succeeded", "failed", "cancelled"}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update(os.environ)
    return env


ENV = load_env()
OPERATOR_TOKEN = ENV.get("OPERATOR_TOKEN", "dev-operator-token")


class StepError(Exception):
    pass


# ------------------------------------------------------------------------------------------------ HTTP
class Api:
    def __init__(self, base_url: str, log: list[str], contract_errors: list[str]):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=30.0)
        self.log = log
        self.contract_errors = contract_errors

    def url(self, ws: str, path: str) -> str:
        return f"{self.base}/api/v1/workspaces/{ws}{path}"

    def call(self, method: str, ws: str, path: str, body: Any = None, params: dict | None = None,
             idem: str | None = None, schema: str | None = None, list_schema: str | None = None,
             whole_schema: str | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Accept": "application/json",
                   "X-Correlation-Id": f"scen-{uuid.uuid4().hex[:12]}"}
        if method in ("POST", "PATCH") and not path.startswith("/context/"):
            headers["Idempotency-Key"] = idem or f"scen-{uuid.uuid4().hex}"
        for attempt in range(5):
            try:
                r = self.http.request(method, self.url(ws, path), json=body, params=params, headers=headers)
                break
            except httpx.TransportError as exc:  # e.g. during an API restart
                if attempt == 4:
                    raise StepError(f"{method} {path}: transport error {exc!r}")
                time.sleep(1.0)
        try:
            data = r.json()
        except ValueError:
            data = {"_raw": r.text[:500]}
        self.log.append(f"{method} /workspaces/{ws}{path} -> {r.status_code}")
        where = f"{method} {path} [{r.status_code}]"
        if r.status_code >= 400:
            self._check("ErrorEnvelope", data, where)
        elif whole_schema:
            self._check(whole_schema, data, where)
        else:
            if isinstance(data, dict) and "meta" in data:
                self._check("Meta", data["meta"], where + " meta")
            if schema and isinstance(data, dict):
                self._check(schema, data.get("data"), where)
            if list_schema and isinstance(data, dict):
                for i, item in enumerate(data.get("data") or []):
                    self._check(list_schema, item, f"{where} data[{i}]")
        return r.status_code, data

    def _check(self, name: str, value: Any, where: str) -> None:
        errs = schema_errors(name, value)
        for e in errs[:5]:
            self.contract_errors.append(f"{where}: {name}: {e}")

    def ok(self, *a, **kw) -> Any:
        status, data = self.call(*a, **kw)
        if status >= 400:
            raise StepError(f"{a[0]} {a[2]} -> HTTP {status}: {json.dumps(data.get('error', data))[:400]}")
        return data.get("data") if isinstance(data, dict) and "data" in data and "next_after_seq" not in data else data

    def list_all(self, ws: str, path: str, params: dict | None = None, schema: str | None = None) -> list:
        out, cursor = [], None
        for _ in range(50):
            p = dict(params or {})
            p["limit"] = 100
            if cursor:
                p["cursor"] = cursor
            status, data = self.call("GET", ws, path, params=p, list_schema=schema)
            if status >= 400:
                raise StepError(f"GET {path} -> HTTP {status}: {json.dumps(data)[:300]}")
            out.extend(data.get("data") or [])
            cursor = (data.get("page") or {}).get("next_cursor")
            if not cursor:
                break
        return out


# ------------------------------------------------------------------------------------------- helpers
def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        else:
            return None
    return cur


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()
REF = re.compile(r"\$\{([^}]+)\}")


def resolve(value: Any, caps: dict, api: Api, ws: str) -> Any:
    if isinstance(value, dict):
        return {k: resolve(v, caps, api, ws) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, caps, api, ws) for v in value]
    if isinstance(value, str) and "${" in value:
        def one(expr: str) -> Any:
            if expr == "knowledge_revision":
                _, d = api.call("GET", ws, "/info", schema="WorkspaceInfo")
                return d["meta"]["knowledge_revision"]
            step, _, path = expr.partition(".")
            if step not in caps:
                raise StepError(f"reference ${{{expr}}}: step {step} has no capture")
            v = get_path(caps[step], path)
            if v is None or v is _MISSING:
                raise StepError(f"reference ${{{expr}}} resolved to nothing")
            return v
        m = REF.fullmatch(value)
        if m:
            return one(m.group(1))
        return REF.sub(lambda mm: str(one(mm.group(1))), value)
    return value


def soft_resolve(value: Any, caps: dict, api: Api, ws: str) -> Any:
    try:
        return resolve(value, caps, api, ws)
    except StepError as exc:
        return f"<unresolved: {exc}>"


def classify(i: str) -> str | None:
    for pre, field in (("attempt_", "attempts"), ("decision_", "decisions"), ("work_", "related_work")):
        if i.startswith(pre):
            return field
    return None


# ------------------------------------------------------------------------------------------ the runner
class ScenarioRun:
    def __init__(self, api: Api, scen: dict, restart_cb=None):
        self.api = api
        self.s = scen
        self.ws = scen.get("workspace_id", "ws_demo")
        self.caps: dict[str, dict] = {}
        self.step_ws: dict[str, str] = {}
        self.restart_cb = restart_cb
        self.baseline: dict[str, int] = {}

    # -- reads used by captures
    def work_count(self, ws: str) -> int:
        return len(self.api.list_all(ws, "/work-items", schema="WorkItem"))

    def run_snapshot(self, ws: str, run: dict) -> dict:
        rid = run["id"]
        props = self.api.list_all(ws, "/proposals", {"run_id": rid}, schema="Proposal")
        actions = self.api.list_all(ws, "/actions", {"request_id": run["request_id"]}, schema="Action")
        run_actions = [a for a in actions if a.get("run_id") == rid]
        request = self.api.ok("GET", ws, f"/requests/{run['request_id']}", schema="Request")
        events = self.events(ws, rid)
        ctx = self.api.ok("GET", ws, f"/requests/{run['request_id']}/context", schema="ContextBundle")
        tool_ids: list[str] = []
        for e in events:
            if e["type"] == "tool_completed":
                tool_ids.extend(x for x in (e["payload"].get("ids") or []) if isinstance(x, str))
        return {"run": run, "proposal": props[0] if props else None, "proposals": props,
                "latest_action": run_actions[0] if run_actions else None, "actions": actions,
                "request": request, "events": events, "context": ctx, "tool_ids": tool_ids}

    def events(self, ws: str, run_id: str, after: int = 0) -> list[dict]:
        out = []
        for _ in range(100):
            page = self.api.ok("GET", ws, f"/runs/{run_id}/events", params={"after_seq": after, "limit": 100},
                               whole_schema="EventsPage")
            out.extend(page["data"])
            after = page["next_after_seq"]
            if not page["has_more"]:
                break
        return out

    # -- steps
    def do_step(self, step: dict) -> dict:
        a = step["action"]
        ws = step.get("workspace_id", self.ws)
        self.step_ws[step["id"]] = ws
        inp = resolve(step.get("input", {}), self.caps, self.api, ws)
        api = self.api
        if a == "create_request":
            st, d = api.call("POST", ws, "/requests", inp, schema="RequestCreateResult")
            return self.http_capture(st, d, lambda x: {"request": x["request"], "run": x["run"]})
        if a == "await_run":
            return self.await_run(ws, inp)
        if a in ("commit_proposal", "commit_proposal_if_awaiting_review"):
            pid = inp["proposal_id"]
            if a == "commit_proposal_if_awaiting_review":
                prop = api.ok("GET", ws, f"/proposals/{pid}", schema="Proposal")
                existing = [x for x in api.list_all(ws, "/actions", {"request_id": prop["request_id"]}, schema="Action")
                            if x.get("proposal_id") == pid]
                if existing:
                    return {"skipped": "proposal already committed (auto_commit_kinds)", "action": existing[0]}
            st, d = api.call("POST", ws, f"/proposals/{pid}/commit", inp["body"], idem=inp.get("idempotency_key"),
                             schema="ProposalCommitResult")
            return self.http_capture(st, d, lambda x: {"action": x["action"], "job_id": x.get("job_id")})
        if a == "reply":
            st, d = api.call("POST", ws, f"/requests/{inp['request_id']}/replies", inp["body"], schema="ReplyResult")
            return self.http_capture(st, d, lambda x: dict(x))
        if a == "record_observation":
            st, d = api.call("POST", ws, f"/requests/{inp['request_id']}/observations", inp["body"],
                             schema="ObservationResult")
            return self.http_capture(st, d, lambda x: dict(x))
        if a == "patch_work_item":
            st, d = api.call("PATCH", ws, f"/work-items/{inp['work_item_id']}", inp["body"],
                             schema="WorkItemUpdateResult")
            return self.http_capture(st, d, lambda x: dict(x))
        if a == "decision_command":
            st, d = api.call("POST", ws, "/decisions", inp, schema="Decision")
            return self.http_capture(st, d, lambda x: {"decision": x})
        if a == "start_run":
            st, d = api.call("POST", ws, f"/requests/{inp['request_id']}/runs", inp["body"], schema="Run")
            return self.http_capture(st, d, lambda x: {"run": x})
        if a == "get_request":
            st, d = api.call("GET", ws, f"/requests/{inp['request_id']}", schema="Request")
            return self.http_capture(st, d, lambda x: {"request": x})
        if a == "get_run_events":
            return {"events": self.events(ws, inp["run_id"], inp.get("after_seq", 0))}
        if a == "get_action":
            st, d = api.call("GET", ws, f"/actions/{inp['action_id']}", schema="Action")
            return self.http_capture(st, d, lambda x: {"action": x})
        if a == "get_work_item":
            st, d = api.call("GET", ws, f"/work-items/{inp['work_item_id']}", schema="WorkItem")
            return self.http_capture(st, d, lambda x: {"work_item": x})
        if a == "get_proposal":
            st, d = api.call("GET", ws, f"/proposals/{inp['proposal_id']}", schema="Proposal")
            return self.http_capture(st, d, lambda x: {"proposal": x})
        if a == "get_source":
            params = {"revision": inp["revision"]} if inp.get("revision") is not None else None
            st, d = api.call("GET", ws, f"/sources/{inp['source_id']}", params=params, schema="SourceRecord")
            return self.http_capture(st, d, lambda x: {"source": x})
        if a == "get_request_context":
            params = {"depth": inp["depth"]} if inp.get("depth") else None
            st, d = api.call("GET", ws, f"/requests/{inp['request_id']}/context", params=params,
                             schema="ContextBundle")
            return self.http_capture(st, d, lambda x: {"context": x})
        if a == "list_actions":
            return {"actions": api.list_all(ws, "/actions", {"request_id": inp["request_id"]}, schema="Action")}
        if a == "list_runs":
            runs = api.list_all(ws, f"/requests/{inp['request_id']}/runs", schema="Run")
            return {"runs": runs, "latest_run": runs[0] if runs else None}
        if a == "list_decisions":
            params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in inp.items() if v is not None}
            return {"decisions": api.list_all(ws, "/decisions", params, schema="Decision")}
        if a == "context_search":
            st, d = api.call("POST", ws, "/context/search", inp, schema="ContextSearchResult")
            return self.http_capture(st, d, lambda x: {"result": x, "ids": [c["id"] for c in x["candidates"]]})
        if a == "context_expand":
            st, d = api.call("POST", ws, "/context/expand", inp, schema="ContextExpandResult")
            return self.http_capture(st, d, lambda x: {"result": x})
        if a in ("import_file", "import_content"):
            if a == "import_file":
                content = (ROOT / inp["path"]).read_text(encoding="utf-8")
            else:
                content = (ROOT / inp["content_from"]).read_text(encoding="utf-8") + inp.get("append", "")
            body = {"format": inp["format"], "origin": inp["origin"], "source_key": inp["source_key"],
                    "content": content}
            prev = api.ok("POST", ws, "/imports/preview", body, schema="ImportPreviewResult")
            st, d = api.call("POST", ws, f"/imports/{prev['preview_id']}/commit",
                             {"expected_revision": prev["based_on_revision"]}, schema="ImportCommitResult")
            return self.http_capture(st, d, lambda x: {"preview": prev, "commit": x})
        raise StepError(f"unsupported step action {a}")

    @staticmethod
    def http_capture(status: int, d: dict, ok_fn) -> dict:
        if status >= 400:
            return {"http_status": status, "error": (d or {}).get("error"), "body": d}
        cap = ok_fn(d["data"])
        cap["http_status"] = status
        return cap

    def await_run(self, ws: str, inp: dict) -> dict:
        rid, until = inp["run_id"], set(inp["until_status_in"])
        deadline = time.monotonic() + float(inp.get("timeout_seconds", 90))
        seen = []
        while True:
            run = self.api.ok("GET", ws, f"/runs/{rid}", schema="Run")
            if not seen or seen[-1] != (run["status"], run["stage"]):
                seen.append((run["status"], run["stage"]))
            if run["status"] in until:
                break
            if time.monotonic() > deadline:
                snap = self.run_snapshot(ws, run)
                snap.update(timed_out=True, status_trace=seen)
                return snap
            time.sleep(0.5)
        snap = self.run_snapshot(ws, run)
        snap.update(timed_out=False, status_trace=seen)
        return snap

    # -- execution
    def execute(self) -> dict:
        result: dict[str, Any] = {"id": self.s["id"], "acceptance_ids": self.s.get("acceptance_ids", []),
                                  "steps": [], "expectations": [], "error": None}
        wss = {self.ws} | {st.get("workspace_id", self.ws) for st in self.s.get("steps", [])}
        need_counts = {e["step"] for e in self.s.get("expectations", []) if e["assert"] == "work_item_count_delta"}
        for w in wss:
            self.baseline[w] = self.work_count(w)
        t0 = time.monotonic()
        for step in self.s.get("steps", []):
            ts = time.monotonic()
            try:
                cap = self.do_step(step)
            except StepError as exc:
                cap = {"step_error": str(exc)}
            if step["id"] in need_counts:
                w = step.get("workspace_id", self.ws)
                cap["work_item_count"] = self.work_count(w)
            self.caps[step["id"]] = cap
            result["steps"].append({"id": step["id"], "action": step["action"], "seconds": round(time.monotonic() - ts, 2),
                                    "summary": summarize(cap)})
        if any(e["assert"] == "stable_after_restart" for e in self.s.get("expectations", [])):
            self.restart_snapshots = self.stable_after_restart_prepare()
        for exp in self.s.get("expectations", []):
            try:
                ok, detail = self.evaluate(exp)
            except StepError as exc:
                ok, detail = False, f"error: {exc}"
            except Exception as exc:  # noqa: BLE001 - report runner bugs per expectation
                ok, detail = False, f"runner exception: {type(exc).__name__}: {exc}"
            result["expectations"].append({"assert": exp["assert"], "step": exp.get("step"),
                                           "optional": bool(exp.get("optional")), "ok": ok, "detail": detail,
                                           "spec": {k: v for k, v in exp.items() if k not in ("assert",)}})
        result["seconds"] = round(time.monotonic() - t0, 1)
        result["ids"] = self.ids_summary()
        return result

    def ids_summary(self) -> dict:
        out: dict[str, Any] = {}
        for sid, cap in self.caps.items():
            for key in ("request", "run", "proposal", "latest_action", "action"):
                v = cap.get(key)
                if isinstance(v, dict) and v.get("id"):
                    out.setdefault(sid, {})[key] = v["id"]
        return out

    # -- stable_after_restart
    def stable_after_restart_prepare(self) -> dict:
        snaps = {}
        if not self.restart_cb:
            return {"_skipped": "API restart only available with --manage"}
        for exp in self.s["expectations"]:
            if exp["assert"] == "stable_after_restart":
                rid = soft_resolve(exp["id"], self.caps, self.api, self.ws)
                snaps[rid] = {"before": self.api.ok("GET", self.ws, f"/{exp['resource']}s/{rid}", schema="Action")}
        self.restart_cb()
        for rid, snap in snaps.items():
            res = next(e["resource"] for e in self.s["expectations"] if e["assert"] == "stable_after_restart")
            snap["after"] = self.api.ok("GET", self.ws, f"/{res}s/{rid}", schema="Action")
        return snaps

    # -- assertions
    def cap(self, step: str) -> dict:
        if step not in self.caps:
            raise StepError(f"no capture for step {step}")
        c = self.caps[step]
        if "step_error" in c:
            raise StepError(f"step {step} failed: {c['step_error']}")
        return c

    def proposal(self, step: str) -> dict:
        c = self.cap(step)
        p = c.get("proposal")
        if not p:
            raise StepError(f"step {step} has no proposal (run {get_path(c, 'run.id')} status "
                            f"{get_path(c, 'run.status')})")
        return p

    def action(self, step: str) -> dict:
        c = self.cap(step)
        a = c.get("action") or c.get("latest_action")
        if not a:
            raise StepError(f"step {step} has no action (http {c.get('http_status')}, error {c.get('error')})")
        return a

    def request(self, step: str) -> dict:
        c = self.cap(step)
        r = c.get("request")
        if not r:
            raise StepError(f"step {step} has no request (http {c.get('http_status')}, error {c.get('error')})")
        return r

    def context_view(self, step: str) -> dict[str, set[str]]:
        """Ids per context field for a step (bundle + ids actually returned to the run's tools)."""
        c = self.cap(step)
        ctx = c.get("context") or {}
        view: dict[str, set[str]] = {k: set() for k in ("attempts", "decisions", "related_work", "sources", "all")}
        for f in ("attempts", "decisions", "related_work"):
            view[f] |= {x["id"] for x in ctx.get(f, [])}
        for s in ctx.get("sources", []):
            view["sources"] |= {s["source_id"], s["id"]}
        for p in ctx.get("paths", []):
            view["all"] |= {n["id"] for n in p["nodes"]}
        for i in c.get("tool_ids", []):
            f = classify(i)
            if f:
                view[f].add(i)
            view["all"].add(i)
        for f in ("attempts", "decisions", "related_work", "sources"):
            view["all"] |= view[f]
        return view

    def texts(self, step: str, fields: list[str]) -> list[tuple[str, str]]:
        p = self.proposal(step)
        out = []
        for f in fields:
            v = get_path(p, f)
            if isinstance(v, str) and v:
                out.append((f, v))
        return out

    def evaluate(self, e: dict) -> tuple[bool, str]:
        k = e["assert"]
        step = e.get("step")
        fn = getattr(self, "a_" + k, None)
        if fn is None:
            return False, f"assertion {k} not implemented by runner"
        return fn(e, step)

    # simple status assertions
    def a_run_status(self, e, step):
        c = self.cap(step)
        st = c["run"]["status"]
        extra = f" (timed out; trace {c.get('status_trace')})" if c.get("timed_out") else ""
        return st in e["values"], f"run {c['run']['id']} status={st} stage={c['run']['stage']}{extra}"

    def a_request_status(self, e, step):
        r = self.request(step)
        return r["status"] in e["values"], f"request {r['id']} status={r['status']}"

    def a_action_status(self, e, step):
        a = self.action(step)
        return a["status"] in e["values"], f"action {a['id']} ({a['kind']}) status={a['status']} error={a.get('error')}"

    def a_action_verification(self, e, step):
        a = self.action(step)
        return a["verification"] in e["values"], f"action {a['id']} verification={a['verification']}"

    def a_receipt_present(self, e, step):
        a = self.action(step)
        r = a.get("receipt")
        ok = bool(r) and r.get("connector") == e["connector"] and bool(r.get("target_id"))
        return ok, f"action {a['id']} receipt={json.dumps(r)[:300]}"

    def a_proposal_kind(self, e, step):
        p = self.proposal(step)
        return p["kind"] in e["values"], f"proposal {p['id']} kind={p['kind']} validity={p['validity']}"

    def a_proposal_validity(self, e, step):
        p = self.proposal(step)
        return p["validity"] in e["values"], (f"proposal {p['id']} validity={p['validity']} "
                                              f"reasons={p.get('validation_reasons')}")

    def _payload(self, e, step):
        p = self.proposal(step)
        if e.get("when_kind") and p["kind"] != e["when_kind"]:
            return p, None, True
        return p, get_path(p.get("payload") or {}, e["path"]), False

    def a_proposal_payload_equals(self, e, step):
        p, v, skip = self._payload(e, step)
        if skip:
            return True, f"n/a: kind {p['kind']} != {e['when_kind']}"
        return v == e["value"], f"proposal {p['id']} ({p['kind']}) payload.{e['path']}={v!r}"

    def a_proposal_payload_in(self, e, step):
        p, v, skip = self._payload(e, step)
        if skip:
            return True, f"n/a: kind {p['kind']} != {e['when_kind']}"
        return v in e["values"], f"proposal {p['id']} ({p['kind']}) payload.{e['path']}={v!r}"

    def a_proposal_payload_not_in(self, e, step):
        p, v, skip = self._payload(e, step)
        if skip:
            return True, f"n/a: kind {p['kind']}"
        vals = v if isinstance(v, list) else [v]
        bad = [x for x in vals if x in e["values"]]
        return not bad, f"proposal {p['id']} ({p['kind']}) payload.{e['path']}={v!r}"

    def a_proposal_payload_matches(self, e, step):
        p, v, _ = self._payload(e, step)
        ok = isinstance(v, str) and re.search(e["regex"], v) is not None
        return ok, f"proposal {p['id']} ({p['kind']}) payload.{e['path']}={v!r}"

    def a_proposal_payload_nonempty(self, e, step):
        p, v, _ = self._payload(e, step)
        ok = v not in (None, "", [], {}) and v is not _MISSING
        return ok, f"proposal {p['id']} ({p['kind']}) payload.{e['path']}={v!r}"

    def a_proposal_payload_min_items(self, e, step):
        p, v, _ = self._payload(e, step)
        ok = isinstance(v, list) and len(v) >= e["min"]
        return ok, f"proposal {p['id']} payload.{e['path']} has {len(v) if isinstance(v, list) else v!r} items"

    def a_rejected_alternative_kind(self, e, step):
        p = self.proposal(step)
        kinds = [a["kind"] for a in p.get("rejected_alternatives", [])]
        return all(k in kinds for k in e["kinds"]), f"proposal {p['id']} rejected kinds={kinds}"

    def a_rejected_alternative(self, e, step):
        p = self.proposal(step)
        alts = [a for a in p.get("rejected_alternatives", []) if a["kind"] == e["kind"]]
        for a in alts:
            blob = a["reason"] + " " + " ".join(a.get("evidence_ids", []))
            if any(m in blob for m in e["mentions_any"]) and (not e.get("evidence_required") or a.get("evidence_ids")):
                return True, f"proposal {p['id']} alternative: {a['reason'][:200]} evidence={a.get('evidence_ids')}"
        return False, (f"proposal {p['id']} ({p['kind']}) rejected_alternatives="
                       f"{json.dumps(p.get('rejected_alternatives'))[:700]}")

    def a_proposal_cites_ids(self, e, step):
        p = self.proposal(step)
        blob = json.dumps(p) + " " + " ".join(self.evidence_sources(step, p))
        missing = [i for i in e["ids"] if i not in blob]
        return not missing, f"proposal {p['id']} evidence_ids={p['evidence_ids']} missing={missing}"

    def evidence_sources(self, step, p) -> list[str]:
        ws = self.step_ws.get(step, self.ws)
        out = []
        for ev in p.get("evidence_ids", []):
            st, d = self.api.call("GET", ws, f"/evidence/{ev}", schema="EvidenceRef")
            if st < 400:
                out.append(d["data"]["source_id"])
        return out

    def a_evidence_sources_include_any(self, e, step):
        p = self.proposal(step)
        srcs = self.evidence_sources(step, p)
        return any(s in srcs for s in e["source_ids"]), f"proposal {p['id']} ({p['kind']}) evidence sources={srcs}"

    def a_text_matches(self, e, step):
        t = self.texts(step, e["fields"])
        hits = [f for f, v in t if re.search(e["regex"], v)]
        return bool(hits), f"matched in {hits}; texts={[(f, v[:300]) for f, v in t]}"

    def a_text_not_matches(self, e, step):
        t = self.texts(step, e["fields"])
        hits = [(f, re.search(e["regex"], v).group(0)) for f, v in t if re.search(e["regex"], v)]
        return not hits, f"inspected {[f for f, _ in t]}; hits={hits}"

    # context
    def a_context_includes(self, e, step):
        v = self.context_view(step)[e["field"]]
        missing = [i for i in e["ids"] if i not in v]
        return not missing, f"{e['field']} missing={missing}; present={sorted(v)[:25]}"

    def a_context_excludes(self, e, step):
        v = self.context_view(step)[e["field"]]
        present = [i for i in e["ids"] if i in v]
        return not present, f"{e['field']} unexpectedly present={present}"

    def a_context_excludes_active(self, e, step):
        c = self.cap(step)
        ws = self.step_ws.get(step, self.ws)
        bad = [d["id"] for d in (c.get("context") or {}).get("decisions", []) if d["id"] in e["ids"] and d["status"] == "active"]
        listed = {d["id"]: d for d in self.api.list_all(ws, "/decisions", schema="Decision")}
        for i in e["ids"]:
            if i in c.get("tool_ids", []) and listed.get(i, {}).get("status") == "active":
                bad.append(i)
        status = {i: listed.get(i, {}).get("status") for i in e["ids"]}
        return not bad, f"active in context={bad}; decision status={status}"

    def a_context_ids_exclude_pattern(self, e, step):
        v = self.context_view(step)["all"]
        bad = sorted(i for i in v if re.search(e["regex"], i))
        return not bad, f"matching ids={bad[:20]}"

    def a_context_missing_information_or_explanation(self, e, step):
        c = self.cap(step)
        texts = list((c.get("context") or {}).get("missing_information", []))
        p = c.get("proposal")
        if p:
            texts.append(p["explanation"])
        hits = [t[:200] for t in texts if re.search(e["regex"], t)]
        return bool(hits), f"hits={hits[:3]}; texts={[t[:150] for t in texts]}"

    def a_candidate_capabilities_include(self, e, step):
        c = self.cap(step)
        v = set(c.get("tool_ids", [])) | set(c["request"].get("capability_ids", []))
        v |= self.context_view(step)["all"]
        missing = [i for i in e["ids"] if i not in v]
        return not missing, f"missing={missing}; caps seen={sorted(i for i in v if i.startswith('cap_'))}"

    def a_context_attempt_recorded(self, e, step):
        ctx = self.cap(step)["context"]
        rid = soft_resolve(e["request_id"], self.caps, self.api, self.ws)
        hits = [a for a in ctx.get("attempts", []) if a.get("request_id") == rid and a["outcome"] in e["outcome_in"]
                and a["verification"] == e["verification"]]
        if hits:
            return True, f"attempt {hits[0]['id']} outcome={hits[0]['outcome']} verification={hits[0]['verification']}"
        listed = self.api.list_all(self.ws, "/attempts", {"request_id": rid}, schema="Attempt")
        return False, (f"context attempts={[(a['id'], a.get('request_id'), a['outcome'], a['verification']) for a in ctx.get('attempts', [])]}; "
                       f"GET /attempts?request_id={rid} -> {[(a['id'], a['outcome'], a['verification']) for a in listed]}")

    def a_evidence_marked_stale(self, e, step):
        ctx = self.cap(step)["context"]
        out, ok = [], True
        for ent in e["entity_ids"]:
            refs = [s for s in ctx.get("sources", []) if ent in s["source_id"] or ent in s["id"]]
            out.append((ent, [(s["id"], s["stale"]) for s in refs]))
            ok = ok and any(s["stale"] for s in refs)
        return ok, f"evidence refs={out}"

    # expand / paths
    def a_expand_truncated(self, e, step):
        r = self.cap(step)["result"]
        return r["truncated"] == e["value"], f"truncated={r['truncated']} truncation={r.get('truncation')} visited={r.get('visited_count')}"

    def _paths_and_edges(self, step):
        c = self.cap(step)
        src = c.get("result") or c.get("context") or {}
        paths = src.get("paths", [])
        edges = list(src.get("edges", []) or [])
        for p in paths:
            edges.extend(p["edges"])
        return paths, edges

    def a_path_contains_ordered(self, e, step):
        ids, mh = e["ids"], e.get("max_hops", 99)
        paths, edges = self._paths_and_edges(step)
        for p in paths:
            nodes = [n["id"] for n in p["nodes"]]
            pos, j = [], 0
            for idx, n in enumerate(nodes):
                if j < len(ids) and n == ids[j]:
                    pos.append(idx)
                    j += 1
            if j == len(ids) and pos[-1] - pos[0] <= mh:
                return True, f"path {p['id']} nodes={nodes}"
        adj = {(x["from_id"], x["to_id"]) for x in edges}
        if len(ids) - 1 <= mh and all((ids[i], ids[i + 1]) in adj for i in range(len(ids) - 1)):
            return True, f"edge chain present among {len(edges)} edges"
        return False, (f"{len(paths)} paths, {len(edges)} edges; paths={[[n['id'] for n in p['nodes']] for p in paths][:8]}; "
                       f"edges={sorted({(x['type'], x['from_id'], x['to_id']) for x in edges})[:20]}")

    def a_path_claim_kind_not(self, e, step):
        paths, _ = self._paths_and_edges(step)
        bad = [(p["id"], p["claim_kind"]) for p in paths
               if any(n["id"] in e["involving_ids"] for n in p["nodes"]) and p["claim_kind"] in e["claim_kinds"]]
        inv = [(p["id"], p["claim_kind"]) for p in paths if any(n["id"] in e["involving_ids"] for n in p["nodes"])]
        return not bad, f"paths involving ids={inv}; violating={bad}"

    def a_no_confirmed_edge(self, e, step):
        _, edges = self._paths_and_edges(step)
        f = soft_resolve(e["from_id"], self.caps, self.api, self.ws)
        t = soft_resolve(e["to_id"], self.caps, self.api, self.ws)
        bad = [x for x in edges if x["type"] == e["type"] and {x["from_id"], x["to_id"]} == {f, t} and x.get("confirmed")]
        near = [(x["type"], x["from_id"], x["to_id"], x.get("confirmed")) for x in edges if x["type"] == e["type"]]
        return not bad, f"{e['type']} edges={near[:10]}"

    # search
    def a_result_ids_exclude_pattern(self, e, step):
        ids = self.cap(step)["ids"]
        bad = [i for i in ids if re.search(e["regex"], i)]
        return not bad, f"ids={ids}"

    def a_result_ids_exclude(self, e, step):
        ids = self.cap(step)["ids"]
        bad = [i for i in e["ids"] if i in ids]
        return not bad, f"present={bad}; ids={ids}"

    def a_result_ids_include_any(self, e, step):
        ids = self.cap(step)["ids"]
        return any(i in ids for i in e["ids"]), f"ids={ids}"

    # errors / identity / revisions
    def a_http_error(self, e, step):
        c = self.caps.get(step) or {}
        st, err = c.get("http_status"), c.get("error") or {}
        ok = st in e["status"] and err.get("code") in e["codes"]
        extra = ""
        if not ok and c.get("action"):
            extra = f"; action={c['action']['id']} status={c['action']['status']}"
        return ok, f"http {st} code={err.get('code')} message={str(err.get('message'))[:200]}{extra}"

    def a_same_id(self, e, step):
        a = soft_resolve(e["a"], self.caps, self.api, self.ws)
        b = soft_resolve(e["b"], self.caps, self.api, self.ws)
        return a == b and not str(a).startswith("<unresolved"), f"a={a} b={b}"

    def _revs(self, e):
        a = soft_resolve(e["a"], self.caps, self.api, self.ws)
        b = soft_resolve(e["b"], self.caps, self.api, self.ws)
        return a, b

    def a_revision_gt(self, e, step):
        a, b = self._revs(e)
        return isinstance(a, int) and isinstance(b, int) and a > b, f"a={a} b={b}"

    def a_revision_gte(self, e, step):
        a, b = self._revs(e)
        return isinstance(a, int) and isinstance(b, int) and a >= b, f"a={a} b={b}"

    # actions / requests / work
    def a_no_action_kinds(self, e, step):
        acts = self.cap(step).get("actions", [])
        bad = [(a["id"], a["kind"]) for a in acts if a["kind"] in e["kinds"]]
        return not bad, f"actions={[(a['id'], a['kind'], a['status']) for a in acts]}"

    def a_no_action_status(self, e, step):
        acts = self.cap(step).get("actions", [])
        bad = [(a["id"], a["status"]) for a in acts if a["status"] in e["statuses"]]
        return not bad, f"actions={[(a['id'], a['kind'], a['status']) for a in acts]}"

    def a_count_actions(self, e, step):
        acts = [a for a in self.cap(step).get("actions", []) if a["kind"] == e["kind"]]
        return len(acts) == e["value"], f"{e['kind']} actions={[(a['id'], a['status']) for a in acts]}"

    def a_count_runs(self, e, step):
        runs = self.cap(step).get("runs", [])
        return len(runs) >= e["min"], f"runs={[(r['id'], r['status']) for r in runs]}"

    def a_no_link_to(self, e, step):
        c = self.cap(step)
        linked = c["request"].get("linked_work_ids", [])
        p = c.get("proposal") or {}
        bad = [w for w in e["work_item_ids"] if w in linked or
               (p.get("kind") == "link_work" and (p.get("payload") or {}).get("work_item_id") == w)]
        return not bad, f"linked_work_ids={linked}; proposal kind={p.get('kind')} payload={json.dumps(p.get('payload'))[:200]}"

    def a_request_linked_work_equals(self, e, step):
        r = self.request(step)
        ids = [soft_resolve(i, self.caps, self.api, self.ws) for i in e["ids"]]
        return sorted(r["linked_work_ids"]) == sorted(ids), f"linked_work_ids={r['linked_work_ids']}"

    def a_request_linked_work_includes(self, e, step):
        r = self.request(step)
        ids = [soft_resolve(i, self.caps, self.api, self.ws) for i in e["ids"]]
        return all(i in r["linked_work_ids"] for i in ids), f"expected {ids}; linked_work_ids={r['linked_work_ids']}"

    def a_work_item_field_nonempty(self, e, step):
        w = self.cap(step).get("work_item")
        if not w:
            raise StepError(f"step {step} has no work item")
        v = w.get(e["field"])
        return bool(v), f"work item {w['id']} {e['field']}={v!r}"

    def a_work_item_count_delta(self, e, step):
        c = self.cap(step)
        ws = self.step_ws.get(step, self.ws)
        n = c.get("work_item_count")
        if n is None:
            raise StepError("work item count not captured")
        d = n - self.baseline[ws]
        return d == e["value"], f"baseline={self.baseline[ws]} after {step}={n} delta={d}"

    # decisions
    def a_decision_state(self, e, step):
        did = soft_resolve(e["id"], self.caps, self.api, self.ws)
        ds = {d["id"]: d for d in self.cap(step).get("decisions", [])}
        d = ds.get(did)
        if not d:
            return False, f"decision {did} not in list {sorted(ds)}"
        ok = d["status"] == e["status"] and ("supersedes_id" not in e or d.get("supersedes_id") == e["supersedes_id"])
        return ok, f"decision {did} status={d['status']} supersedes_id={d.get('supersedes_id')}"

    # events
    def a_events_include_types_in_order(self, e, step):
        types = [x["type"] for x in self.cap(step)["events"]]
        j = 0
        for t in types:
            if j < len(e["types"]) and t == e["types"][j]:
                j += 1
        return j == len(e["types"]), f"event types={types}"

    def a_events_seq_contiguous(self, e, step):
        seqs = [x["seq"] for x in self.cap(step)["events"]]
        return seqs == list(range(1, len(seqs) + 1)), f"{len(seqs)} events, seqs={seqs[:5]}..{seqs[-3:]}"

    def a_event_payload_seen(self, e, step):
        evs = [x for x in self.cap(step)["events"] if x["type"] == e["type"]]
        hit = [x for x in evs if all(x["payload"].get(k) == v for k, v in e["payload"].items())]
        return bool(hit), f"{e['type']} payloads={[x['payload'] for x in evs]}"

    # imports / sources
    def a_import_counts(self, e, step):
        c = self.cap(step)
        r = c.get("commit")
        if not r:
            raise StepError(f"import failed: http {c.get('http_status')} {c.get('error')}")
        ok = r["imported_count"] == e["imported"] and r["rejected_count"] == e["rejected"]
        if "noop_min" in e:
            ok = ok and r["noop_count"] >= e["noop_min"]
        return ok, (f"imported={r['imported_count']} noop={r['noop_count']} rejected={r['rejected_count']} "
                    f"preview changes={[x['change'] for x in c['preview']['changes']]}")

    def a_source_revision(self, e, step):
        r = self.cap(step).get("commit") or {}
        revs = [(x["source_id"], x["revision"]) for x in r.get("source_revisions", [])]
        return (e["source_id"], e["revision"]) in revs, f"source_revisions={revs}"

    def a_source_body_unchanged(self, e, step):
        s = self.cap(step).get("source")
        if not s:
            raise StepError(f"no source captured: {self.cap(step).get('error')}")
        want = (ROOT / e["content_from"]).read_text(encoding="utf-8")
        if e.get("strip_front_matter"):
            want = strip_front_matter(want)
        return s["body"] == want, f"source {s['id']}@{s['revision']} body length {len(s['body'])} vs file {len(want)}"

    def a_stable_after_restart(self, e, step):
        snaps = getattr(self, "restart_snapshots", {})
        if "_skipped" in snaps:
            return False, "not evaluated: " + snaps["_skipped"]
        rid = soft_resolve(e["id"], self.caps, self.api, self.ws)
        s = snaps.get(rid)
        if not s:
            return False, f"no snapshot for {rid}"
        diffs = [(f, get_path(s["before"], f), get_path(s["after"], f)) for f in e["fields"]
                 if get_path(s["before"], f) != get_path(s["after"], f)]
        vals = {f: get_path(s["after"], f) for f in e["fields"]}
        return not diffs and all(v not in (None, _MISSING) for v in vals.values()), f"after API restart {vals}; diffs={diffs}"


def strip_front_matter(text: str) -> str:
    """PROTOCOL #36: markdown front matter (`---` ... `---` at the top) is metadata, not body."""
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def summarize(cap: dict) -> str:
    if "step_error" in cap:
        return "ERROR " + cap["step_error"][:300]
    if cap.get("skipped"):
        return "skipped: " + cap["skipped"]
    parts = []
    if cap.get("http_status", 0) >= 400:
        parts.append(f"HTTP {cap['http_status']} {(cap.get('error') or {}).get('code')}")
    for key in ("request", "run", "proposal", "latest_action", "action", "decision", "work_item"):
        v = cap.get(key)
        if isinstance(v, dict) and v.get("id"):
            extra = v.get("status") or v.get("kind") or ""
            if key == "proposal":
                extra = f"{v['kind']}/{v['validity']}"
            parts.append(f"{key}={v['id']}({extra})")
    if cap.get("timed_out"):
        parts.append(f"TIMED OUT trace={cap.get('status_trace')}")
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------- holdouts
PROMISE_DATE = r"(?i)(will|should) (be )?(fixed|restored|ready|delivered|available|working|ship|land)[^.]*\b(by|on|before)\b"
MUST_NOT_TEXT = {
    "promise_date": PROMISE_DATE,
    # any mention of public/no-login links in customer-facing text is flagged for review (t04 uses the same
    # strict regex on link_work text); guest accounts / authenticated share are the acceptable alternative
    "recommend_public_link": r"(?i)public (\(no-login\) )?(share )?links?|no-login",
    "grant_extension": r"(?i)(we (can|will|have) (extend|extended|delay|delayed|postpone|postponed|pushed))|(extension (is |has been )?(granted|approved))",
}


def run_holdout(api: Api, h: dict) -> dict:
    ws = h.get("workspace_id", "ws_demo")
    sr = ScenarioRun(api, {"id": h["id"], "workspace_id": ws})
    res: dict[str, Any] = {"id": h["id"], "checks": [], "error": None}
    base = sr.work_count(ws)
    try:
        created = api.ok("POST", ws, "/requests", h["request"], schema="RequestCreateResult")
        snap = sr.await_run(ws, {"run_id": created["run"]["id"], "timeout_seconds": 90,
                                 "until_status_in": ["succeeded", "awaiting_review", "waiting_for_input", "failed", "cancelled"]})
    except StepError as exc:
        res["error"] = str(exc)
        return res
    p = snap.get("proposal") or {}
    res.update(request_id=created["request"]["id"], run_id=snap["run"]["id"], run_status=snap["run"]["status"],
               proposal_id=p.get("id"), kind=p.get("kind"), payload=p.get("payload"),
               explanation=p.get("explanation"))
    disp_ok = False
    for d in h.get("acceptable_dispositions", []):
        if p.get("kind") == d["kind"] and all((p.get("payload") or {}).get(k) == v for k, v in d.items() if k != "kind"):
            disp_ok = True
    res["checks"].append({"check": "acceptable_disposition", "ok": disp_ok,
                          "detail": f"kind={p.get('kind')} payload={json.dumps(p.get('payload'))[:300]}"})
    text = " ".join(str(x) for x in [p.get("explanation"), *((p.get("payload") or {}).values())] if isinstance(x, str))
    for m in h.get("must_not", []):
        if m in MUST_NOT_TEXT:
            hit = re.search(MUST_NOT_TEXT[m], text)
            res["checks"].append({"check": f"must_not {m} (text heuristic)", "ok": not hit,
                                  "detail": f"hit={hit.group(0) if hit else None}"})
        else:
            kind, _, target = m.partition(":")
            bad = p.get("kind") == kind and (not target or (p.get("payload") or {}).get("work_item_id") == target)
            bad = bad or any(a["kind"] == kind and not target for a in snap.get("actions", []))
            res["checks"].append({"check": f"must_not {m}", "ok": not bad, "detail": f"kind={p.get('kind')}"})
    if p.get("kind") != "create_work":
        n = sr.work_count(ws) - base
        res["checks"].append({"check": "no work item created", "ok": n == 0, "detail": f"delta={n}"})
    res["contract_ok"] = True
    return res


# --------------------------------------------------------------------------------------------- stacks
class Stacks:
    def __init__(self, port: int, prefix: str, keep: bool):
        self.port, self.prefix, self.keep = port, prefix, keep
        self.up_ports: list[int] = []

    def sh(self, *args: str) -> None:
        cmd = [str(ROOT / "scripts" / "stack.sh"), *args]
        print("  $", " ".join(cmd), flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write("".join("    " + ln + "\n" for ln in r.stdout.splitlines()))
        if r.returncode != 0:
            sys.stderr.write(r.stderr)
            raise SystemExit(f"stack.sh {' '.join(args)} failed")

    def up(self, port: int, prefix: str, faults: str = "") -> str:
        args = ["up", "--port", str(port), "--prefix", prefix]
        if faults:
            args += ["--faults", faults]
        self.sh(*args)
        self.up_ports.append(port)
        return f"http://127.0.0.1:{port}"

    def down(self, port: int) -> None:
        if port in self.up_ports:
            self.sh("down", "--port", str(port), *(["--keep-graphs"] if self.keep else []))
            self.up_ports.remove(port)

    def restart_api(self, port: int) -> None:
        self.sh("restart-api", "--port", str(port))

    def down_all(self) -> None:
        for p in list(self.up_ports):
            self.down(p)


def scenario_env(s: dict) -> dict[str, str]:
    env = {}
    for pre in s.get("preconditions", []):
        m = re.match(r"server env (\w+)=(\S+)", pre)
        if m:
            env[m.group(1)] = m.group(2)
    return env


def needs_fresh(s: dict) -> bool:
    return "fresh_seed" in s.get("preconditions", []) or bool(scenario_env(s))


def print_result(r: dict) -> None:
    status = r["status"]
    print(f"[{status}] {r['id']} ({', '.join(r.get('acceptance_ids', []))}) {r.get('seconds', 0)}s "
          f"on {r.get('base_url')}", flush=True)
    for st in r.get("steps", []):
        print(f"    {st['id']} {st['action']:<36} {st['summary']}")
    for x in r.get("expectations", []):
        mark = "ok  " if x["ok"] else ("OPT " if x["optional"] else "FAIL")
        print(f"    {mark} {x['assert']}@{x['step']}: {x['detail'][:400]}")
    for c in r.get("contract_errors", [])[:10]:
        print(f"    CONTRACT {c[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url")
    ap.add_argument("--fault-base-url", help="stack started with the FAULTS a scenario requires (with --base-url)")
    ap.add_argument("--manage", action="store_true", help="bring up/tear down private stacks via scripts/stack.sh")
    ap.add_argument("--port", type=int, default=8020, help="managed shared stack port")
    ap.add_argument("--secondary-port", type=int, default=None,
                    help="managed port for fresh_seed/env/holdout stacks (default --port + 1)")
    ap.add_argument("--prefix", default=None, help="graph prefix stem for managed stacks (default pgeval_<ts>)")
    ap.add_argument("--keep", action="store_true", help="keep managed stacks' graphs (processes still stop)")
    ap.add_argument("--only", help="comma list of scenario id prefixes, e.g. t04,t12")
    ap.add_argument("--holdouts", action="store_true")
    ap.add_argument("--no-scenarios", action="store_true", help="only holdouts")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()
    if not args.manage and not args.base_url:
        ap.error("--base-url or --manage is required")
    if args.secondary_port is None:
        args.secondary_port = args.port + 1
    if args.manage and args.secondary_port == args.port:
        ap.error("--secondary-port must differ from --port")

    scen = [json.loads(p.read_text()) for p in sorted(SCEN_DIR.glob("t*.json"))]
    if args.only:
        wanted = [w.strip() for w in args.only.split(",") if w.strip()]
        scen = [s for s in scen if any(s["id"].startswith(w) for w in wanted)]
    if args.no_scenarios:
        scen = []
    holdouts = [json.loads(p.read_text()) for p in sorted((SCEN_DIR / "holdouts").glob("*.json"))] if args.holdouts else []

    stem = args.prefix or f"pgeval_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    stacks = Stacks(args.port, stem, args.keep) if args.manage else None
    started = datetime.now(timezone.utc)
    results: list[dict] = []
    holdout_results: list[dict] = []

    def run_one(s: dict, base: str, restart_cb=None) -> dict:
        log, cerrs = [], []
        api = Api(base, log, cerrs)
        try:
            r = ScenarioRun(api, s, restart_cb).execute()
        except Exception as exc:  # noqa: BLE001
            r = {"id": s["id"], "acceptance_ids": s.get("acceptance_ids", []), "steps": [], "expectations": [],
                 "error": f"{type(exc).__name__}: {exc}"}
        r["base_url"] = base
        r["contract_errors"] = cerrs
        req_fail = [x for x in r["expectations"] if not x["ok"] and not x["optional"]]
        r["status"] = "FAIL" if (req_fail or cerrs or r.get("error")) else "PASS"
        r["optional_misses"] = [x["assert"] for x in r["expectations"] if not x["ok"] and x["optional"]]
        r["http_calls"] = len(log)
        print_result(r)
        if r.get("error"):
            print("    ERROR", r["error"])
        return r

    try:
        shared = [s for s in scen if not needs_fresh(s)]
        fresh = [s for s in scen if needs_fresh(s)]
        if args.manage:
            if shared:
                base = stacks.up(args.port, f"{stem}s_")
                for s in shared:
                    results.append(run_one(s, base, lambda: stacks.restart_api(args.port)))
                stacks.down(args.port)
            for s in fresh:
                env = scenario_env(s)
                p = args.secondary_port
                tag = s["id"].split("-")[0]
                base = stacks.up(p, f"{stem}{tag}_", env.get("FAULTS", ""))
                results.append(run_one(s, base, lambda p=p: stacks.restart_api(p)))
                stacks.down(p)
            if holdouts:
                p = args.secondary_port
                base = stacks.up(p, f"{stem}h_")
                for h in holdouts:
                    holdout_results.append(run_hold(base, h))
                stacks.down(p)
        else:
            for s in fresh + shared:
                env = scenario_env(s)
                if env:
                    if not args.fault_base_url:
                        r = {"id": s["id"], "acceptance_ids": s.get("acceptance_ids", []), "status": "SKIP",
                             "steps": [], "expectations": [], "base_url": None,
                             "error": f"needs server env {env}; pass --fault-base-url or use --manage"}
                        print_result(r)
                        results.append(r)
                        continue
                    results.append(run_one(s, args.fault_base_url))
                else:
                    results.append(run_one(s, args.base_url))
            for h in holdouts:
                holdout_results.append(run_hold(args.base_url, h))
    finally:
        if stacks:
            stacks.down_all()

    npass = sum(r["status"] == "PASS" for r in results)
    print(f"\nscenarios: {npass}/{len(results)} PASS; holdouts: "
          f"{sum(all(c['ok'] for c in h['checks']) and not h.get('error') for h in holdout_results)}/{len(holdout_results)} acceptable")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
            "stem": stem, "results": results, "holdouts": holdout_results}, indent=2, default=str))
    return 0 if npass == len(results) else 1


def run_hold(base: str, h: dict) -> dict:
    log, cerrs = [], []
    api = Api(base, log, cerrs)
    r = run_holdout(api, h)
    r["contract_errors"] = cerrs
    ok = not r.get("error") and all(c["ok"] for c in r["checks"]) and not cerrs
    print(f"[{'OK' if ok else 'MISS'}] holdout {h['id']}: kind={r.get('kind')} run={r.get('run_status')} "
          f"{r.get('error') or ''}")
    for c in r["checks"]:
        print(f"    {'ok  ' if c['ok'] else 'FAIL'} {c['check']}: {c['detail'][:300]}")
    for c in cerrs[:5]:
        print(f"    CONTRACT {c[:300]}")
    return r


if __name__ == "__main__":
    sys.exit(main())
