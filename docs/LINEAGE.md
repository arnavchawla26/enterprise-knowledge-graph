# Lineage and metadata graph (ekg.lineage)

Nodes: 81 Table (lake files, report files, Postgres copies prefixed `pg.`, the Project 2
outputs, and one declared in-memory frame), 777 Column (542 from Postgres `information_schema`
and DuckDB `DESCRIBE` over the real files, 197 from one raw Backblaze daily CSV, 38 declared for
the failure model's feature frame), 12 Job, 3 DataProduct, 300 Query. Edges:
Column-BELONGS_TO->Table (777), Job-READS->Table (101), Table-PRODUCED_BY->Job (81),
Table-DERIVED_FROM->Table (306), Column-DERIVED_FROM->Column (590), DataProduct-COMPOSED_OF->Table
(14), Query-READS->Table (520), Column-MEANS->Term (20), Term-STORED_IN->Table (37). Counts from
`reports/lineage_counts.json`, 2026-10-17.

Where the edges come from. `lineage/pipeline.yaml` declares the real pipeline (what each
Project 1 and Project 2 job reads and writes), written by reading the source;
`tests/test_lineage.py` fails if a declared file is not named in that job's module, so the
declaration cannot drift silently. The generated `data_product`, `job`, `lineage` and
`query_log` tables are loaded alongside and marked `source=generated`. Table-level `DERIVED_FROM`
is precise (1:1) for the Postgres copy job, row-level for the generated lineage table, and
coarse (every write from every read) for the analysis jobs; the `precision` property on the
edge says which. Column-level `DERIVED_FROM` is declared per job in the `column_lineage` block
of the same file, from reading each job's code (the aggregation, join or model behind every
output column), and generated for the Postgres copy job from the column names each `pg.*`
table shares with its lake source; see the section below.

## Where did this number come from

Two queries: the column path (Column-DERIVED_FROM->Column upstream, each column at its shortest
depth with the job and expression of the last hop) and the job path (tables upstream through
Table-DERIVED_FROM->Table with the job that produced each).

```
MATCH (c:Column {column_key: $column_key})
MATCH p = (c)-[:DERIVED_FROM*1..12]->(up:Column)
WITH up, p ORDER BY length(p)
WITH up, head(collect(p)) AS p
WITH up, length(p) AS depth, relationships(p)[-1] AS last, nodes(p)[-2] AS parent
MATCH (up)-[:BELONGS_TO]->(t:Table)
OPTIONAL MATCH (j:Job {job_id: last.via})
RETURN up.column_key AS column, t.name AS table, t.source AS source, depth, parent.column_key AS parent,
       j.name AS via, last.precision AS precision, last.expr AS expr
ORDER BY depth, column
```

```
MATCH (c:Column {column_key: $column_key})-[:BELONGS_TO]->(t:Table)
OPTIONAL MATCH (c)-[:MEANS]->(term:Term)
OPTIONAL MATCH (t)-[:PRODUCED_BY]->(j:Job)
WITH c, t, term, collect(DISTINCT j.name) AS producers
OPTIONAL MATCH p = (t)-[:DERIVED_FROM*1..12]->(up:Table)
WITH c, t, term, producers, up, min(length(p)) AS depth
OPTIONAL MATCH (up)-[:PRODUCED_BY]->(uj:Job)
WITH c, t, term, producers, up, depth, collect(DISTINCT uj.name) AS via
RETURN c.column_key AS column, t.name AS table, producers, term.label AS means, term.definition AS definition,
       [x IN collect({depth: depth, node: up.name, kind: up.kind, source: up.source, produced_by: via}) WHERE x.node IS NOT NULL] AS upstream
```

`uv run python -m ekg.lineage provenance reports/fleet_risk_sites.csv expected_failures_30d`
(the column path abridged to four lines per depth; the tool prints twelve):

```
reports/fleet_risk_sites.csv.expected_failures_30d  (table reports/fleet_risk_sites.csv, written by failure_model)
  means: Fleet Health: For a site, account or the whole fleet on the scoring date: the expected number of drive failures in the next 30 days, the sum of Project 1's calibrated p_fail_30d over the drives, together with the count the Backblaze SMART rule flags. Lower expected failures per drive is healthier.
  column path (66 upstream columns, 5 hops; the deepest 19 are in backblaze_daily_csv):
    <- failure_model/features.p_fail_30d  [failure_model: sum(p_fail_30d) per site, rounded to 2]
      <- failure_model/features.lightgbm  [failure_model: the calibrated lightgbm score at the fleet scoring date (score_fleet)]
        <- failure_model/features.capacity_bytes  [failure_model: Platt-calibrated LightGBM (params in reports/failure_model_metrics.json); per-feature share is a model property (gain), not lineage]
        <- failure_model/features.days_seen_7  [failure_model: ...]
        <- failure_model/features.model  [failure_model: ...]
        <- failure_model/features.smart_187_delta7  [failure_model: ...]
           ... and 22 more at this depth
          <- telemetry_daily.capacity_bytes  [failure_model: value at t]
          <- telemetry_daily.date  [failure_model: smart_187_raw at t minus at t-7]
          <- telemetry_daily.model  [failure_model: value at t; LightGBM categorical]
          <- telemetry_daily.smart_187_raw  [failure_model: smart_187_raw at t minus at t-7]
             ... and 15 more at this depth
            <- backblaze_daily_csv.capacity_bytes  [load_telemetry: cast]
            <- backblaze_daily_csv.date  [load_telemetry: cast]
            <- backblaze_daily_csv.smart_187_raw  [load_telemetry: cast]
            <- backblaze_daily_csv.smart_5_raw  [load_telemetry: cast]
               ... and 15 more at this depth
  job path:
  <- drive_model (parquet, real_derived, by load_telemetry)
  <- drive_summary (parquet, real_derived, by load_telemetry)
  <- failure_model/features (dataframe, real_derived, by failure_model)
  <- installed_asset (csv, generated_joined_to_real, by generate_enterprise)
  <- site (csv, generated, by generate_enterprise)
  <- telemetry_daily (parquet, real, by load_telemetry)
  <- truth/account_resolution (csv, truth, by generate_enterprise)
  <- truth/asset_serial_map (csv, truth, by generate_enterprise)
    <- backblaze_daily_csv (raw_csv, real)
```

So the number in a site's row of the fleet-risk report is the Fleet Health term, written by
`sois.failure_model`: a per-site sum of the calibrated LightGBM score, which is a function of
the 25 numeric features plus the model categorical, each of which is one SMART column (or a
7-day delta or max of one) from the telemetry Parquet, which is a cast of the same column in
the raw Backblaze CSV. The 66 upstream columns are the whole answer; the job path alone would
say "every input of failure_model", which includes `site.address` and is wrong at the column
level. The glossary definition rides along, which is the point of the semantic layer. A column
reached by several edges (`telemetry_daily.smart_187_raw` feeds `smart_187_raw`, `_delta7` and
`_max7`) is printed once, at its shortest depth, with the expression of the hop that reached it
first; the graph holds all three edges.

## Column-level lineage: what it covers and what it cannot

590 Column-DERIVED_FROM->Column edges: 214 `copy` (the Postgres copy job, generated from the
column names each of the 23 `pg.*` tables shares with its lake source, including the three
renamed telemetry tables), 53 `cast` (`sois.load_telemetry`'s TRY_CAST of every kept column
from the raw CSV into `telemetry_daily`), and 323 `expr` (declared per output column with the
expression behind it). The declared blocks cover `load_telemetry`'s two GROUP BYs
(`drive_summary` from `telemetry_daily`, `drive_model` from `drive_summary`), the failure
model's feature frame (the 25 numeric features from `sois.failure_model.feature_columns()`,
checked equal by a test, plus the label from `drive_summary.failure_date`), its scores and the
seven tabular reports they feed, `parts_forecast`'s three CSVs, `repeat_dispatch`'s CSV, and
`ekg.resolve`'s reviewer list. 38 tables have column edges as target. Every declared column
must exist in the discovered schema; `ekg.lineage load` prints any that do not (zero on this
run) and a test fails if one is missing from the fixture snapshot of the real column names
(`tests/fixtures/columns.json`).

What column lineage does not and cannot say:

- **The in-memory feature frame.** `failure_model/features` is never written to disk, so its
  38 columns are declared in `pipeline.yaml`, not discovered. The test checks the numeric
  features against Project 1's `feature_columns()`; the passthrough and label columns are
  checked by name only.
- **Inside the model.** `lightgbm` and `p_fail_30d` derive from all 25 features plus `model`.
  That is true and complete as lineage, but which features matter is a property of the fitted
  model (`lightgbm_feature_importance_gain` in `reports/failure_model_metrics.json`), not of
  the data flow; the graph does not pretend otherwise.
- **Non-tabular outputs.** The JSON reports (`failure_model_metrics.json`,
  `parts_forecast_summary.json`, `repeat_dispatch_summary.json`, `load_summary.json`), the
  figures (`*.png`), the model files and `ekg/graph` have no columns, so lineage stops at the
  table level for them. `asset_embeddings.parquet` has 32 embedding columns derived from graph
  structure, not from columns; its only column edge would be `serial_number`, which is omitted
  because the embedding job reads the graph, not a table.
- **The generated lineage table.** The 13 `declared_row` edges from Project 1's generated
  `lineage.csv` describe jobs that exist only as rows (`build_features`, planned jobs), so there
  is no code to read; they stay table-level.
- **`telemetry_daily.day`.** The hive partition column comes from the CSV file name, not from a
  column, so it has no source edge.
- **Constants.** Report columns that are constants or labels (`dispatch_budget.csv.model` is the
  score's name, `daily_budget` is the budget B) are declared with no inputs.
- **Expressions are text.** The `expr` on an edge is a declared string, checked only in that the
  columns it names exist. It is not parsed from the SQL or pandas; a change in Project 1's code
  that keeps the file names but changes an aggregation would pass the declaration test and
  leave the expression stale. Parsing the SQL and pandas would be the next step; it is not
  done, and the declaration is the honest substitute.

## What breaks if this table changes

```
MATCH (t:Table {name: $table})
OPTIONAL MATCH p = (t)<-[:DERIVED_FROM*1..12]-(d:Table)
WITH t, d, min(length(p)) AS depth
WITH t, [x IN collect({depth: depth, node: d.name, kind: d.kind}) WHERE x.node IS NOT NULL] AS downstream
WITH t, downstream, [t.name] + [x IN downstream | x.node] AS affected
OPTIONAL MATCH (j:Job)-[:READS]->(a:Table) WHERE a.name IN affected
WITH t, downstream, affected, collect(DISTINCT j.name) AS jobs
OPTIONAL MATCH (dp:DataProduct)-[:COMPOSED_OF]->(b:Table) WHERE b.name IN affected
WITH t, downstream, affected, jobs, collect(DISTINCT dp.name) AS data_products
OPTIONAL MATCH (q:Query)-[:READS]->(qt:Table) WHERE qt.name IN affected
RETURN t.name AS table, downstream, jobs, data_products, count(DISTINCT q) AS logged_queries, collect(DISTINCT q.user_name) AS query_users
```

`uv run python -m ekg.lineage impact installed_asset` (abridged; 35 downstream datasets, now including
the failure model's feature frame):

```
installed_asset
  -> case (csv)
  -> ekg/graph (neo4j)
  -> models/test_scores.parquet (model_output)
  -> pg.installed_asset (postgres_table)
  -> reports/failure_model_metrics.json (report)
  -> reports/fleet_risk_sites.csv (report)
  -> reports/parts_forecast_backtest.csv (report)
  -> reports/repeat_dispatch.csv (report)
  ...
    -> dispatch (csv)
    -> ekg/reports/asset_embeddings.parquet (model_output)
    -> pg.case (postgres_table)
    -> reports/dashboard.png (figure)
      -> dispatch_part (csv)
      -> pg.dispatch (postgres_table)
        -> pg.dispatch_part (postgres_table)
  jobs that must re-run: build_features, dashboard, ekg.gds, ekg.load, ekg.resolve, ekg.trust, failure_model, load_postgres, parts_forecast, repeat_dispatch
  data products affected: fleet_health, parts_demand, dispatch_quality
  logged queries affected: 182 (users: agent_triage, analyst_01, analyst_02, etl_nightly, ops_lead, svc_dashboard)
```

`impact pg.site` is smaller: no derived tables, three data products, 32 logged queries.

## What breaks if this column changes

The mirror of the column provenance query: downstream columns at their shortest depth, the
tables they belong to, and the jobs that produce those tables. `impact` takes a column key as
well as a table name.

```
MATCH (c:Column {column_key: $column_key})
OPTIONAL MATCH p = (c)<-[:DERIVED_FROM*1..12]-(d:Column)
WITH c, d, min(length(p)) AS depth
OPTIONAL MATCH (d)-[:BELONGS_TO]->(t:Table)
WITH c, [x IN collect({depth: depth, column: d.column_key, table: t.name, kind: t.kind}) WHERE x.column IS NOT NULL] AS downstream
WITH c, downstream, [x IN downstream | x.table] AS tables
OPTIONAL MATCH (t:Table)-[:PRODUCED_BY]->(j:Job) WHERE t.name IN tables
RETURN c.column_key AS column, downstream, collect(DISTINCT j.name) AS jobs
```

`uv run python -m ekg.lineage impact telemetry_daily smart_5_raw`:

```
telemetry_daily.smart_5_raw: 40 downstream columns in 9 tables
  -> failure_model/features.smart_5_delta7 (dataframe)
  -> failure_model/features.smart_5_max7 (dataframe)
  -> failure_model/features.smart_5_raw (dataframe)
  -> pg.telemetry_daily_recent.smart_5_raw (postgres_table)
    -> failure_model/features.heuristic (dataframe)
    -> failure_model/features.lightgbm (dataframe)
    -> failure_model/features.logistic (dataframe)
    -> reports/fleet_risk_top100.csv.smart_5_raw (report)
      -> failure_model/features.heuristic_flag (dataframe)
      -> failure_model/features.p_fail_30d (dataframe)
      -> models/test_scores.parquet.heuristic (model_output)
      -> models/test_scores.parquet.lightgbm (model_output)
      -> models/test_scores.parquet.logistic (model_output)
        -> models/test_scores.parquet.heuristic_flag (model_output)
        -> reports/dispatch_budget.csv.caught_per_day (report)
        -> reports/dispatch_budget.csv.caught_total (report)
        -> reports/dispatch_budget.csv.precision (report)
        -> reports/dispatch_budget.csv.recall (report)
        -> reports/dispatch_budget.csv.trucks_total (report)
        -> reports/dispatch_budget.csv.wasted_per_day (report)
        -> reports/dispatch_budget.csv.wasted_total (report)
        -> reports/fleet_risk_sites.csv.expected_failures_30d (report)
        -> reports/fleet_risk_sites.csv.heuristic_flagged (report)
        -> reports/fleet_risk_sites.csv.max_p (report)
        -> reports/fleet_risk_top100.csv.heuristic_flag (report)
           ... and 11 more at this depth
          -> reports/dispatch_budget_curve.csv.caught_per_day (report)
          -> reports/dispatch_budget_curve.csv.precision (report)
          -> reports/dispatch_budget_curve.csv.recall (report)
          -> reports/dispatch_budget_curve.csv.wasted_per_day (report)
  jobs that must re-run: failure_model, load_postgres
```

Caveats. Table-level impact over-approximates: `failure_model` writes eleven files from seven
inputs, and the table graph says each output depends on every input; the column graph is where
the precise answer lives, and the two answers differ (`impact site address` reaches one column, its
Postgres copy, where the table graph says every failure-model report depends on `site`). Logged
queries are counted only at the table level because the generated query log names tables, not
columns. The query log is generated (300 rows from ten templates), so "182 queries affected"
counts the shape of a query history, not a real one.
