# PromiseGraph

PromiseGraph is a customer-operations agent. You give it a customer request, and it investigates the request using company knowledge stored in **FalkorDB**. It then chooses one of five next actions: **answer, clarify, link existing work, create new work, or escalate**. It carries out that action through validated tools, records the result, and remembers scoped outcomes for later requests.

Graph Hacks (FalkorDB) hackathon, Company Brain track. All data is synthetic.

## Status (honest)

| Area | Status |
| --- | --- |
| Full request loop: intake → graph investigation → proposal → durable action → observation → verification | Implemented and verified on the real stack |
| Acceptance scenarios (T01–T28 subset, 18 scenarios) | **18/18 pass** on a clean export of the committed tree ([evidence](docs/evidence/g2-scenarios.md)) |
| Restart durability (T19) | Verified: `scripts/check_persistence.sh` restarts FalkorDB and confirms state survives |
| Browser journeys (keyboard, 390 px, stale-blocked commit, clarification, closure, DB down, unknown dispatch) | Pass on the real stack (Playwright `PG_TARGET=real` and `real-fault`) |
| Planner | **Scripted mode** (deterministic and labelled in the UI). It uses the same tools as the live model. |
| Generalisation of the scripted planner | 8/12 acceptable on a sealed holdout set written without seeing the planner code. Several "acceptable" answers quote weakly relevant text ([evidence](docs/evidence/g4-holdouts-v2-scripted.md)). |
| Live model planner (OpenAI gpt-4o-mini) | **Live and working end to end** through the same validated tools, under a hard spend guard. On our tests it scores **2/18 scenarios and 1/12 holdouts** (vs 18/18 and 8/12 rule-based), for $0.28 total. Rule-based mode stays the default ([evidence](docs/evidence/g4-live-model.md)). |
| Linear connector | **Verified** against a real Linear test team: created issue PRO-5 after operator approval and read it back ([evidence](docs/evidence/g3-linear.md)). Timeout reconciliation against Linear is tested only with mocked HTTP. |
| Local task connector | Verified. Receipts are real records in the graph and are read back. |

We don't claim measured time savings or accuracy figures beyond the evidence linked above.

## Architecture

```
apps/web (React)  ──►  services/api (FastAPI) ──► FalkorDB (one graph per workspace: pg_ws_demo, pg_ws_other)
                         ▲  public /api/v1 + private /internal/v1 (worker-only)
services/worker ─────────┘  claims jobs (lease + fencing), runs the planner/tool loop, executes connectors
```

- **The API is the only service that touches the graph.** Every mutation is a single parameterized Cypher query that checks its own preconditions: revision, entity versions, lease fencing and idempotency. The state change, audit event and job therefore land together or not at all.
- **The worker has no inbound port and no database credentials.** Its planner picks among 8 typed tools: `search_context`, `expand_context`, `read_evidence`, `read_memory`, `prepare_action`, `commit_action`, `inspect_action` and `record_observation`. The API validates every proposal: evidence must exist and be current, and decision scope and policy are enforced. The planner cannot self-certify.
- **The graph drives behaviour:**
  - Request → Capability → WorkItem → DEPENDS_ON chains → owner paths, at most 4 hops by default and 6 at most.
  - Scoped decisions (forbidden workarounds, routing owners, allowed action types) and prior attempts change the chosen action.
  - Proposals become stale when the facts they consulted change.
  - Creating a task never resolves a request. Only an attributable verification does.

Contracts and specification: [`docs/spec/`](docs/spec/), [`contracts/PROTOCOL.md`](contracts/PROTOCOL.md) and the [`contracts/*.schema.json`](contracts/) files.

## Run it

Prerequisites: Docker, Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node 22 with pnpm.

```bash
cp .env.example .env           # dev tokens; AI and Linear are disabled by default
scripts/demo.sh up             # FalkorDB + API :8000 + worker + web :5173, seeded through the import API
open http://localhost:5173
scripts/demo.sh down           # stop (data kept)  ·  scripts/demo.sh reset  (fresh seed)
```

## Verify it

```bash
(cd services/api && uv run pytest -q)                       # API on real FalkorDB (throwaway graphs)
(cd services/worker && uv run pytest -q --ignore=tests/integration)
(cd contracts/mock && uv run pytest -q)                     # protocol mock used for parallel development
scripts/check_persistence.sh                                # T19 restart durability
uv run --no-project --with httpx==0.28.1 --with jsonschema==4.25.1 \
  python scripts/run_scenarios.py --manage --port 8050      # all scenarios on a fresh real stack
(cd tests/browser && PG_TARGET=real npx playwright test)    # browser journeys on a private real stack
```

## Enabling the live model (optional)

Set these in `.env`: `AI_ENABLED=true`, `AI_PROVIDER=openai`, `AI_MODEL`, `AI_API_KEY`, `AI_BUDGET_USD` (≤ 10), and `AI_PRICE_INPUT_PER_MTOK` / `AI_PRICE_OUTPUT_PER_MTOK`. Take the prices from the provider's official pricing page; there is no built-in price table.

Before each call, the worker reserves that call's worst-case cost. If the call would exceed the budget, it doesn't make the call. Spend is recorded in a ledger that survives restarts. Also set a budget limit in your provider account as a backstop.

## AI-assistance disclosure

This project was built with Claude Code (Anthropic, model Claude Opus 5.5). One coordinating session wrote the contracts, fixtures, scripts and integration evidence. Parallel sub-agents implemented the API (`services/api`), the worker (`services/worker`) and the web app (`apps/web`, `tests/browser`) under strict per-directory ownership and frozen contracts.

The human builder chose the product direction, approved the specification and makes the credential and budget decisions. All results in this README come from commands run in this repository, and the evidence files record the exact commits and commands.
