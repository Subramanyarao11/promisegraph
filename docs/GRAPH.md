# Graph data model and Cypher queries

FalkorDB is PromiseGraph's only database. The API process is the only code that holds the connection. Each workspace
is a separate graph (`pg_ws_demo`, `pg_ws_other`), chosen through a server-side mapping, never from user input.
If you take FalkorDB out, nothing works: the knowledge, the agent's memory, the run/job queue and the action log all
live in the graph.

## Nodes

Every domain node also carries the shared label `E` and a stable `id`, so the API can look anything up by id.

| Label | What it is | Key properties |
| --- | --- | --- |
| `Workspace` | One company's graph | `knowledge_revision` (bumped on every consequential change) |
| `Customer` | A (fictional) customer | name, plan, constraints |
| `Request` | A customer request | subject, body, status, owner, `search_text`, version |
| `Capability` | A product area, e.g. "Scheduled exports" | name, aliases, status, product version |
| `WorkItem` | An engineering task/feature (local or Linear) | title, status, owner, forecast, `observed_at`, connector, external id/url |
| `Person`, `Team` | Owners | name, role, `active` |
| `Source`, `SourceRev`, `Evidence` | Immutable source revisions and the exact excerpts cited as evidence | sha256, revision, excerpt, location |
| `Decision` | A scoped company rule (forbidden workaround, routing owner, allowed actions…) | rule_type, value, scope, status, effective/expiry |
| `Attempt` | Something tried before, and what actually happened | procedure, context signature, scope, outcome, verification |
| `Observation` | An attributable result (customer/operator confirmation, connector status) | kind, verification |
| `Run`, `RunEvent`, `Job` | The agent's durable runs, ordered event log and lease-fenced job queue | status, stage, checkpoint, lease and fencing tokens |
| `Proposal`, `Action`, `Receipt` | Suggested action, executed action, proof of its effect | basis revision, validity, idempotency hash, target id/url |

## Relationships

| Edge | Meaning |
| --- | --- |
| `(Customer)-[:SUBMITTED]->(Request)` | Who raised the request |
| `(Request)-[:ABOUT]->(Capability)` | Confirmed topic (unconfirmed candidates are marked `confirmed:false`) |
| `(Capability)-[:IMPLEMENTED_BY]->(WorkItem)` | Work that implements or maintains a product area |
| `(Request)-[:TRACKED_BY]->(WorkItem)` | The request is linked to this work |
| `(WorkItem)-[:DEPENDS_ON]->(WorkItem)` | Blocked by / needs |
| `(WorkItem\|Capability)-[:OWNED_BY]->(Person\|Team)` | Accountable owner |
| `(Request)-[:RELATED_TO]->(Request)` | Similar request (a candidate, never a duplicate claim by itself) |
| `(E)-[:SUPPORTED_BY]->(Source)` | Where a fact comes from |
| `(Decision)-[:APPLIES_TO]->(…)`, `(Decision)-[:SUPERSEDES]->(Decision)` | Rule scope and history |
| `(Attempt)-[:ATTEMPTED_FOR]->(Request\|WorkItem)` | Context in which something was tried |
| `(Action)-[:EXECUTES]->(Proposal)`, `(Action)-[:HAS_RECEIPT]->(Receipt)`, `(Observation)-[:VERIFIES]->(Request)` | The action ledger and verification |

Every evidence-bearing edge has an `id`, `evidence_ids` and `confirmed`.

The typical path the agent walks (4 hops):
`Request -ABOUT-> Capability -IMPLEMENTED_BY-> WorkItem -DEPENDS_ON-> WorkItem -OWNED_BY-> Person (inactive)`.
From that path it decides "link the existing work, can't promise a date, route to the active owner from the routing rule".

## The Cypher the product depends on

All queries are fixed templates with parameters. Labels and relationship types are inserted only from server-side
allowlists, and the model never writes Cypher. Source: `services/api/promisegraph_api/`.

**1. Bounded multi-hop traversal** (`context.py`, the `expand_context` tool; depth defaults to 4, max 6):
```cypher
MATCH p=(s:E {id: $id})-[:ABOUT|IMPLEMENTED_BY|DEPENDS_ON|OWNED_BY*1..4]-(t:E)
RETURN [n IN nodes(p) | [n.id, n.label, n.title]],
       [r IN relationships(p) | [r.id, type(r), startNode(r).id, endNode(r).id, r.evidence_ids, r.confirmed]]
LIMIT $cap
```

**2. All allowlisted edges among the visited nodes** (so the agent sees shared neighbours, not just a tree):
```cypher
MATCH (a:E)-[r]->(b:E) WHERE a.id IN $ids AND b.id IN $ids AND type(r) IN $types
RETURN r.id, type(r), a.id, b.id, r.evidence_ids, r.confirmed ORDER BY r.id LIMIT 501
```

**3. Candidate search** (the `search_context` tool, one fixed template per label):
```cypher
MATCH (n:Request) WHERE any(t IN $toks WHERE n.search_text CONTAINS t)
  AND ($cust IS NULL OR n.customer_id = $cust)
RETURN n.id, n.title, n.search_text LIMIT 500
```

**4. Scoped memory: company rules and past attempts** (`read_memory`):
```cypher
UNWIND $scopes AS sc MATCH (d:Decision) WHERE d.scope_type = sc[0] AND d.scope_id = sc[1] RETURN DISTINCT d.doc

MATCH (a:Attempt)-[:ATTEMPTED_FOR]->(w:WorkItem) WHERE w.id IN $w
OPTIONAL MATCH (r:Request {id: a.scope_id}) RETURN DISTINCT a.doc, r.customer_id
```
Attempts scoped to another customer are filtered out, so one customer's failed workaround never changes another
customer's plan.

**5. Atomic guarded writes** (`store.py`). FalkorDB runs each query atomically, so every mutation is compiled into
**one** query whose first clauses are guards. A failed guard divides by zero, which aborts the whole query: the state
change, its audit event, the revision bump, the queued job and the idempotency record land together or not at all.
```cypher
OPTIONAL MATCH (w:E {id: $ws_node})
WITH 1 / (CASE WHEN w IS NOT NULL AND w.knowledge_revision = $rev_guard THEN 1 ELSE 0 END) AS _c1
OPTIONAL MATCH (lj:E {id: $lease.id})
WITH 1 / (CASE WHEN lj.status = 'leased' AND lj.lease_token = $lease.lease_token
              AND lj.fencing_token = $lease.fencing_token AND lj.expires_epoch >= $lease.now THEN 1 ELSE 0 END) AS _c2
UNWIND $vguards AS vg OPTIONAL MATCH (vx:E {id: vg.id})
WITH 1 / (CASE WHEN vx.version = vg.version THEN 1 ELSE 0 END) AS g WITH count(g) AS _c3
// … then the CREATE/MERGE/SET clauses of the mutation itself
```
This gives optimistic concurrency (stale proposals are rejected), lease fencing for the worker (an expired worker
can't write) and exactly-once-recorded effects, all inside the graph.

**6. Indexes** (created on startup, one per frequently filtered property):
```cypher
CREATE INDEX FOR (n:E) ON (n.id)
CREATE INDEX FOR (n:Job) ON (n.status)
CREATE INDEX FOR (n:RunEvent) ON (n.run_id)
// … Proposal/Action/Run.request_id, Evidence/SourceRev.source_id, Decision.status, Request.status
```
