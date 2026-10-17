# Text-to-Cypher (ekg.nl2cypher)

Numbers from `reports/nl2cypher_eval_summary.json` and `reports/nl2cypher_eval.csv`,
2026-10-17.

| Backend | Model | Execution accuracy | Questions needing a retry | Model calls | From cache on rerun |
|---|---|---:|---:|---:|---:|
| NVIDIA NIM | nvidia/nemotron-3-super-120b-a12b | 20/20 (1.00) | 2 (q03, q10) | 22 | 22 |
| Claude | claude-sonnet-5-5 | 20/20 (1.00) | 0 | 20 | 20 |

Execution accuracy: the generated query runs read-only and its result, hashed order-free
over rows and columns with floats rounded to four places, equals the hash of the gold
query's result (`questions/eval.yaml`, frozen by `ekg.nl2cypher freeze` on the loaded
graph). Column names are ignored; `DISTINCT` versus not is ignored when the rows are the
same.

The loop. The schema prompt is built from the live graph (labels, properties, relationship
patterns) plus the glossary, every Term with its one definition, and a few value facts
(`Drive.failed` is 1/0, `warranty_end` null means unknown). The model returns one fenced
query; the guard rejects anything that writes; the query runs as the reader account in a
READ transaction with a 30 s timeout and a LIMIT; an error or an empty result goes back to
the model verbatim, up to three attempts. NIM's two retries were a Cartesian-product join
on `a.site_id = s.site_id` (no such property; the error pointed at it) and an undirected
`SUPPLIED_BY` pattern that double-counted; both were fixed on the second attempt.

Why 20/20 is not the headline. The 20 questions are one- and two-hop lookups and counts
over a schema the prompt spells out in full, including the glossary that disambiguates
"failure". That is the realistic shape of an agent's first calls, but it is not a hard
benchmark: no temporal reasoning over the two timestamp formats, no path questions longer
than three hops, no questions that need a definition the prompt does not carry. A harder
set would score lower, and the cache makes adding one cheap. Both backends are strong
models; the interesting result is that an NVIDIA-hosted open model is at parity with Claude
on this set, with the loop catching its two slips.

Read-only, honestly. Neo4j Community has users but no roles, so the `ekg_reader` account
can write in a default session (checked). Read-only is enforced by (1) the guard before
execution and (2) the driver's READ access mode, in which the server refuses writes
(`Neo.ClientError.Statement.AccessMode`, checked). On Enterprise the reader role would be
the third layer; `ekg.load` tries `GRANT ROLE reader` and records that it is unsupported.

Cache. `cache/nl2cypher/<sha256>.json`, keyed by backend, model and the full message
list, committed, so `make eval` reruns in 0.2 s with zero model calls. Keys come from the
environment and are never written anywhere.
