# G4 — Live model evaluation (OpenAI gpt-4o-mini)

Date: 2026-10-18 · real stack (API + worker + FalkorDB), Linear off, same tools/policies as rule-based mode.
Command: `LINEAR_ENABLED=false scripts/run_scenarios.py --manage --port 8094 --holdout-set v2 --planner-mode model`.
Raw output: `g4-live-model-gpt-4o-mini.json` / `.log`. Spend guard: $5 hard cap.

| Planner | Acceptance scenarios | Sealed holdouts (v2) | Cost |
| --- | --- | --- | --- |
| Rule-based (scripted) | **18/18** | 8/12 (several weak answers) | $0 |
| Live model, gpt-4o-mini | **2/18** | **1/12** | **$0.27** for the run (317 calls, 1.66 M in / 31 k out tokens); $0.28 total incl. smoke tests |

What the live run proves:
- The live model drives the real tool loop end to end; proposals are validated by the server (no unsupported
  citations reach the API), actions execute, and the spend guard/ledger work against the real provider.

What it shows honestly:
- gpt-4o-mini is not good enough for this agent loop with the current prompt: many runs used all 12 model calls
  without proposing (`BUDGET_EXHAUSTED`), several linked work where an answer, clarification, new task or escalation
  was expected, and it under-used the graph memory the rule-based planner relies on.
- No tuning against the holdouts was done. A stronger model and prompt/tool-loop work are the obvious next steps.

Product default: **rule-based mode** stays the default for the demo; live mode is selectable per request and labelled.

## Follow-up: gpt-4.1 (2026-10-18, 22:56 IST)

- Found and fixed an adapter bug: models that reply in prose before calling tools ended the run immediately
  (`NO_PROPOSAL`). The loop now reminds the model to use the tools (max 2 reminders); unit-tested.
- gpt-4.1 with a 20-call limit on 6 key scenarios (t01, t02, t04, t06, t09, t11): **0/6 pass** ($0.49). Runs now reach
  proposals, but dispositions/evidence still miss the scenario expectations. Raw log: `g4-live-model-gpt-4.1-subset.log`.
- Conclusion for this submission: the live-model loop is connected and safe, but not yet competitive with the
  rule-based planner; improving it needs prompt and tool-loop work beyond the deadline.
