# Text-to-Cypher (ekg.nl2cypher)

Numbers from `reports/nl2cypher_eval_summary.json`, `reports/nl2cypher_hard_eval_summary.json`
and the two CSVs next to them, 2026-10-17, re-run after column-level lineage changed the schema
prompt (every cached response was regenerated; see Cache).

| Set | Backend | Model | Execution accuracy | Questions needing a retry | Model calls |
|---|---|---|---:|---:|---:|
| eval (20, one- and two-hop) | NVIDIA NIM | nvidia/nemotron-3-super-120b-a12b | 20/20 (1.00) | 1 (q03) | 21 |
| eval (20) | Claude | claude-sonnet-5-5 | 20/20 (1.00) | 0 | 20 |
| hard (10, three to six hops) | NVIDIA NIM | nvidia/nemotron-3-super-120b-a12b | 5/10 (0.50) | 2 (h06, h07; h07 failed all three) | 13 |
| hard (10) | Claude | claude-sonnet-5-5 | 10/10 (1.00) | 2 (h02 three attempts, h07 two) | 13 |

Execution accuracy: the generated query runs read-only and its result, hashed order-free
over rows and columns with floats rounded to four places, equals the hash of the gold
query's result (`questions/eval.yaml`, `questions/hard.yaml`, frozen by `ekg.nl2cypher
freeze` on the loaded graph). Column names are ignored; `DISTINCT` versus not is ignored when
the rows are the same. `make eval` reruns both sets from cache in a few seconds with zero
model calls and prints the same numbers.

## The two sets

The 20-question set is one- and two-hop lookups and counts over a schema the prompt spells out
in full, including the glossary that disambiguates "failure". That is the realistic shape of
an agent's first calls and both backends clear it, NIM with one slip caught by the loop (a
Cartesian product on `a.site_id = s.site_id`, a property that does not exist; the error named
it and the second attempt joined through `HOSTS`).

The 10-question hard set is what the first set lacked: three to six hops (h09 walks
Account-HAS-Site-HOSTS-Asset-IS_A-Product-USES-Part-SUPPLIED_BY-Supplier with a filter on the
edge), a per-group argmax (h03), a NOT pattern (h04), an argmax subquery feeding a second match
(h02), date arithmetic over the ISO timestamp strings (h06), a variable-length lineage walk
(h07) and the semantic layer (h08: data products with a column that MEANS a term). Questions
whose top-N cut fell on a tie were rephrased until the gold answer was unique, because a model
that breaks a tie differently is not wrong.

Claude answers all ten; two needed the loop (h02 returned no rows twice before it dropped a
wrong date comparison; h07 first walked the wrong relationship). NIM answers five. Its misses
are instructive and are in `reports/nl2cypher_hard_eval.csv` with the generated Cypher:

- h02 and h09: the `HOSTS` pattern written backwards (`(:Site)<-[:HOSTS]-(:Asset)`; h09 also
  reverses `SUPPLIED_BY`). Reversed directions match nothing, the query returns a count of 0,
  and a zero is a row, so the loop has nothing to retry on. The schema prompt lists every
  pattern with its direction; the model did not use it.
- h01: an invented condition (`EXISTS { (c:Case)-[:ABOUT]->(asset) WHERE c.resolution_code IS
  NOT NULL }`) the question never asked for. The answer has the right shape and the wrong rows.
- h06: a regex guard `=~ '\d{4}-\d{2}-\d{2}'` that a timestamp string never matches, so the
  count is of nothing; the prompt says `opened_at` is an ISO string and the gold query takes
  `left(opened_at, 10)`.
- h07: three syntax errors in a row while unrolling the variable-length walk into chained
  OPTIONAL MATCHes instead of writing `[:DERIVED_FROM*1..12]`.

The pattern across the five is that this model's errors on multi-hop questions are silent:
four of five executed and returned a plausible row, which an agent would pass on as the answer.
The loop only catches errors the database raises. The next layer, not built here, is a
result-shape check against the question (a count of 0 on a question about the account with the
most assets is suspicious) or a second model as a judge; both cost model calls per question.

## The loop

The schema prompt is built from the live graph (labels, properties, relationship patterns
with direction) plus the glossary, every Term with its one definition, and a few value facts
(`Drive.failed` is 1/0, `warranty_end` null means unknown). The model returns one fenced
query; the guard rejects anything that writes; the query runs as the reader account in a
READ transaction with a 30 s timeout and a LIMIT; an error or an empty result goes back to
the model verbatim, up to three attempts.

Two harness faults found by the hard set, both fixed and both worth stating because the
first run's numbers were wrong. NIM's Nemotron 3 Super reasons inside its output budget: at
`max_tokens=800` four of the ten hard questions came back with empty content three times each
(the reasoning used the budget), and the empty strings were cached and counted as misses. NIM
now gets 4,000 tokens, `max_tokens` is part of the cache key, and an empty answer is a failed
attempt that is never cached. Second, the NIM client had no request timeout, and one hung
request stalled an eval for ten minutes at the library default; it is 120 s with two retries.

The relationship patterns in the prompt come from `db.schema.visualization()` through the
driver's node objects; Cypher's `labels()` on its virtual nodes returned `[]` on Neo4j 5.26
after a restart and printed `(:None)-[:HOSTS]->(:None)`, which would have hidden every
direction from the model. The patterns include a few the visualization over-approximates
(`AccountRecord-SAME_AS->Asset` is listed because `SAME_AS` joins both pairs); the models were
not misled by them.

Read-only, honestly. Neo4j Community has users but no roles, so the `ekg_reader` account
can write in a default session (checked). Read-only is enforced by (1) the guard before
execution and (2) the driver's READ access mode, in which the server refuses writes
(`Neo.ClientError.Statement.AccessMode`, checked). On Enterprise the reader role would be
the third layer; `ekg.load` tries `GRANT ROLE reader` and records that it is unsupported.
One of Claude's broad lineage queries hit the 1 GB heap's transaction memory limit on the
offline rerun and succeeded on the driver's transient retry; a 30 s timeout and a LIMIT bound
the cost of a generated query, not its memory.

## Cache

`cache/nl2cypher/<sha256>.json`, keyed by backend, model, `max_tokens` and the full message
list, committed, so `make eval` reruns in seconds with zero model calls. The schema prompt is
part of the messages, so a change to the graph schema (column-level lineage added a
`(:Column)-[:DERIVED_FROM]->(:Column)` pattern) invalidates every key; the 66 responses from
the first prompt were deleted and the 67 in the repository are from the final prompt. Keys
come from the environment and are never written anywhere.
