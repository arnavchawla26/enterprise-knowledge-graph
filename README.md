# enterprise-knowledge-graph

Project 2 of 3: a governed knowledge graph over one small enterprise, built so that an
agent gets trusted, provenance-backed context instead of raw tables.

## Why this exists

On October 6, 2026 Dell announced the next release of the Dell AI Data Platform. Three of
its pieces are what this repository is a small working version of: an Enterprise
Knowledge Graph built from metadata, lineage and query history, which adds the related
tables, columns and documents to an agent's context when it asks a question; a Unified
Semantic Layer, in which every business term has one meaning; and the idea that agents
should receive "trusted context", scoped by permission and backed by lineage and quality
evidence, rather than whatever a search returns. Arthur Lewis's line from the
announcement, "In many cases, the model is not the next constraint, the data is", is the
premise.

This repository is the knowledge layer over the enterprise that
[services-ops-intelligence](https://github.com/arnavchawla26/services-ops-intelligence)
(Project 1) generates and models: a Neo4j graph of accounts, sites, assets, drives,
products, parts, suppliers, cases, dispatches, technicians and runbooks, plus a second
layer that describes the data itself (tables, columns, data products, jobs, lineage, query
history), an ontology with one definition per term loaded as nodes, measured entity
resolution, graph algorithms, text-to-Cypher with a self-correcting loop, a
context-assembly API with per-role permissions, and a data-trust dashboard. Project 3,
`knowledge-agents-pilot`, puts agents on top.

## What is real and what is generated

The same table as Project 1, because the inputs are Project 1's files, read-only.

| Data | Source | Status |
|---|---|---|
| Per-drive telemetry summary, last SMART 5/187/197 values, failure dates | Backblaze Hard Drive Data 2026 Q2 via Project 1's Parquet | Real |
| 30-day failure probability per drive (`Drive.p_fail_30d`) | Project 1's calibrated LightGBM, last scored date 2026-05-31 | Real telemetry, Project 1 model |
| Accounts, sites, assets, products, parts, suppliers, cases, dispatches, technicians, runbooks | Project 1's seeded generator; every asset is a real drive | Generated (assets joined to real; case timing follows real failures) |
| Data products, jobs, lineage rows, 300 query-log rows | Project 1's generator | Generated |
| The real pipeline (`lineage/pipeline.yaml`) | Declared from reading Project 1 and this repository's source; checked by a test | Real |
| Column metadata | Postgres `information_schema` and DuckDB `DESCRIBE` over the real files | Real |
| Ground truth for resolution | Project 1's `truth/` | Generated, reproducible from the seed |

The mess the resolver removes (three spellings, duplicate ids, vendor-prefixed and
lowercased serials) was put there on purpose by rules in Project 1's `docs/MESS_RULES.md`.
That is why resolution can be measured, and also why its numbers are a ceiling.

## What is in the graph

Loaded by `make load` in about 50 s: 359,101 Asset and 359,101 Drive nodes, 6,199 Pod,
312 Vault, 81 DriveModel, 701 Site, 335 AccountRecord (239 Account after resolution),
8,281 Case, 4,108 Dispatch, 80 Technician, 30 Part, 8 Supplier, 10 Product, 22 Runbook,
25 Term, and the metadata layer: 80 Table, 542 Column, 12 Job, 3 DataProduct, 300 Query.
Every entity node has an `INSTANCE_OF` edge to its Term; every column that carries a
glossary meaning has a `MEANS` edge to it. Daily telemetry stays out of the graph.

## Measured results (2026-10-17)

Every number below was printed by code in this repository and is in `reports/`. The
caveats are part of the result.

### Ontology and semantic layer
`ontology/enterprise.ttl` (SKOS/OWL-lite): 25 terms with exactly one definition each,
18 relationships, rendered to `ontology/glossary.md` and loaded as `:Term` nodes.
`ekg.ontology check` fails on a term with zero or two definitions, and `load` refuses to run
until it passes. The two failure definitions are copied from Project 1's rule F1: telemetry
failure is `failure = 1` on a drive-day; service failure is `case.failure_reported = 1`,
set on `hardware_replaced`. They overlap and do not match, and the glossary says so.

### Entity resolution ([docs/RESOLUTION.md](docs/RESOLUTION.md))
Accounts, pairwise on the held-out half of companies (thresholds set on the other half):
precision 1.000, recall 1.000, canonical-id accuracy 1.000; 24 pairs in the uncertain band
for a reviewer (`reports/uncertain_matches.csv`), 295 if industry and region are not used
as evidence. Assets: 359,101 of 359,101 drives linked to their ERP asset, precision 1.000,
recall 1.000, 0 collisions, after a first version that lost 505 assets because 430 real
serials are natively lowercase and 86 really start with `WD-`. What the numbers prove: the
matcher inverts the generator's mess rules on companies and assets it was not tuned on.
What they do not: anything about a real CRM, where variants cross regions and typos hit
the first word.

### Lineage ([docs/LINEAGE.md](docs/LINEAGE.md))
Two questions answered with Cypher and shown with their outputs: where
`reports/fleet_risk_sites.csv.expected_failures_30d` comes from (the Fleet Health term, written
by `sois.failure_model` from real telemetry through the serial map), and what breaks if
`installed_asset` changes (37 downstream datasets, 10 jobs, all 3 data products, 182 of the
300 logged queries). Job-level lineage over-approximates: column-level lineage is not done.

### Graph data science ([docs/GDS.md](docs/GDS.md))
Co-failure communities (same site, within 14 days): 233 Louvain communities over 1,306 of
1,522 failed drives, modularity 0.978, 191 communities inside one vault. Part centrality on
a 48-node graph: FAN-80MM, TPM-MOD, NIC-25G top betweenness; 8 of 30 parts single-sourced.
FastRP embeddings for 359,101 drives in 16 s.

The lift test is negative. Re-running Project 1's failure model on its own leak-free split
with the same parameters, three seeds each: baseline PR-AUC 0.1002, plus a vault-id
categorical 0.0623, plus the 32-dim embedding 0.0818. The embedding lowers PR-AUC by 0.018
(seed std 0.002) and recall at 1% by 1.7 points. The baseline reproduces Project 1's 0.0985
at its seed, so the harness is faithful. Location identity overfits 17 training dates; the
graph adds no signal the SMART attributes lack at this horizon. This README does not claim
graph features improve the model.

### Text-to-Cypher ([docs/NL2CYPHER.md](docs/NL2CYPHER.md))
20 questions with gold Cypher and frozen result hashes. Execution accuracy: NVIDIA NIM
(`nvidia/nemotron-3-super-120b-a12b`) 20/20 with two questions fixed on a retry by the
loop; Claude (`claude-sonnet-5-5`) 20/20 first try. The set is one- and two-hop lookups
over a fully spelled-out schema plus the glossary; a harder set would score lower.
Responses are cached on disk and committed, so `make eval` reruns with zero model calls.
Writes are refused by a guard before execution and by the server's read access mode; Neo4j
Community has no roles, so the separate reader account is hygiene, not enforcement.

### Context-assembly API ([docs/API.md](docs/API.md))
`POST /context {question, caller_role}` returns terms, tables, columns, runbooks and a graph
neighbourhood filtered by `policy/roles.yaml` (ops_lead, technician, analyst), lists what
was denied, and writes an audit line. For "longest lead time for the failed drives at
account ACC-00009", a technician gets parts and assets but not accounts, prices or the
account's neighbourhood; the ops lead gets all of it. No model call; keyword matching only.

### Data trust ([docs/TRUST.md](docs/TRUST.md), `reports/trust_dashboard.png`)
111 checks per run against `trust/slas.yaml`. Against the real clock: 93 pass, 18 fail; as of
2026-07-01: 97 pass, 14 fail. The warehouse-vs-lake completeness checks fail on every table
because Project 1's Postgres on this machine holds its 6-row test fixture while the lake
holds 359,101 assets; the job reports it and nothing here modifies that database.
Freshness fails against today because the quarter ended 2026-06-30, and passes as of
2026-07-01 for telemetry and support. Schema drift is 0 because the baseline was captured
from the same files; the test shows it firing. The Kibana dashboard is a committed
saved-objects export and a headless screenshot.

![trust dashboard](reports/trust_dashboard.png)

## What this does not yet support

Words a reader might expect that this repository does not earn yet: column-level lineage
(job-level only); a learned entity matcher (the shipped one is scored rules, measured on
generated mess); any lift from graph features (measured negative); a hard text-to-Cypher
benchmark (the eval set is easy); enterprise role enforcement in Neo4j (Community edition);
and a real query history (300 generated rows). The ontology is governed by a test, not by a
review process.

## How to run

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker, and a checkout of
services-ops-intelligence next to this one with its Parquet, enterprise CSVs, truth and
reports built (`make load generate analyses` there). About 6 GB of RAM for the three
containers (each JVM is capped at 1 GB).

```
cp .env.example .env                     # Neo4j and Elasticsearch settings; model keys optional
uv sync                                  # pinned environment
uv run pytest                            # 60 tests on fixtures; live tests skip without Neo4j
make up                                  # Neo4j 5.26 community (APOC, GDS), Elasticsearch 8.17, Kibana 8.17
make load                                # ontology check + render, graph load (about a minute)
make resolve                             # entity resolution, metrics, review list, SAME_AS edges
make lineage                             # metadata layer and the two documented queries
make gds                                 # communities, centrality, embeddings, lift test (about 3 minutes)
make eval                                # text-to-Cypher eval from cache (no keys needed)
make trust                               # trust checks, both clocks, into Elasticsearch
make screenshot                          # import the Kibana dashboard and capture it
make api                                 # context API on :8008
```

`ekg.load --postgres` reads the drive summary from Project 1's Postgres instead of Parquet;
on this machine that database holds a 6-row fixture, which is why Parquet is the default.
`make eval-live` calls the model backends for uncached prompts (`NVIDIA_API_KEY`,
`ANTHROPIC_API_KEY`). The Kibana screenshot needs `uv run playwright install chromium` once.

## Layout

```
ontology/     enterprise.ttl (SKOS/OWL-lite), glossary.md (rendered)
lineage/      pipeline.yaml (the real pipeline, declared and tested)
policy/       roles.yaml (per-role allow-lists for the API)
questions/    eval.yaml (20 questions, gold Cypher, frozen result hashes)
trust/        slas.yaml, schema_baseline.json
kibana/       trust_dashboard.ndjson (saved objects)
cache/        nl2cypher model responses (committed; reruns are free)
src/ekg/      config, graph, ontology, load, resolve, lineage, gds, nl2cypher, api, trust
scripts/      screenshot_kibana.py
reports/      metrics JSON/CSV, communities, centrality, eval, trust results, dashboard PNG
docs/         RESOLUTION, LINEAGE, GDS, NL2CYPHER, API, TRUST
tests/        60 tests on hand-made fixtures (6 assets, 8 account rows, every mess rule once)
```

## Honesty

Commit dates are real; nothing is backdated. Every number in this README was printed by
code in this repository on the stated date and lives in `reports/`. Negative results
(the embedding lift, the stale warehouse, the stale freshness) are reported as measured.
The generated data is labelled generated everywhere it appears. Project 1's repository and
database were never modified.

## License

MIT, see [LICENSE](LICENSE). The Backblaze data is subject to Backblaze's terms, not this
license.
