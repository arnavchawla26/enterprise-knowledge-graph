# Lineage and metadata graph (ekg.lineage)

Nodes: 80 Table (lake files, report files, Postgres copies prefixed `pg.`, and the Project 2
outputs), 542 Column (from Postgres `information_schema` and DuckDB `DESCRIBE` over the real
files), 12 Job, 3 DataProduct, 300 Query. Edges: Column-BELONGS_TO->Table (542),
Job-READS->Table (101), Table-PRODUCED_BY->Job (80), Table-DERIVED_FROM->Table (289),
DataProduct-COMPOSED_OF->Table (14), Query-READS->Table (520), Column-MEANS->Term (20),
Term-STORED_IN->Table (37).

Where the edges come from. `lineage/pipeline.yaml` declares the real pipeline (what each
Project 1 and Project 2 job reads and writes), written by reading the source;
`tests/test_lineage.py` fails if a declared file is not named in that job's module, so the
declaration cannot drift silently. The generated `data_product`, `job`, `lineage` and
`query_log` tables are loaded alongside and marked `source=generated`. `DERIVED_FROM` is
precise (1:1) for the Postgres copy job, row-level for the generated lineage table, and
coarse (every write from every read) for the analysis jobs; the `precision` property on the
edge says which.

## Where did this number come from

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

`uv run python -m ekg.lineage provenance reports/fleet_risk_sites.csv expected_failures_30d`:

```
reports/fleet_risk_sites.csv.expected_failures_30d  (table reports/fleet_risk_sites.csv, written by failure_model)
  means: Fleet Health: For a site, account or the whole fleet on the scoring date: the expected number of drive failures in the next 30 days, the sum of Project 1's calibrated p_fail_30d over the drives, together with the count the Backblaze SMART rule flags. Lower expected failures per drive is healthier.
  <- drive_model (parquet, real_derived, by load_telemetry)
  <- drive_summary (parquet, real_derived, by load_telemetry)
  <- installed_asset (csv, generated_joined_to_real, by generate_enterprise)
  <- site (csv, generated, by generate_enterprise)
  <- telemetry_daily (parquet, real, by load_telemetry)
  <- truth/account_resolution (csv, truth, by generate_enterprise)
  <- truth/asset_serial_map (csv, truth, by generate_enterprise)
    <- backblaze_daily_csv (raw_csv, real)
```

So the number in a site's row of the fleet-risk report is the Fleet Health term, written by
`sois.failure_model`, from real telemetry (Backblaze CSV -> Parquet) joined to generated
assets and sites through the serial map. The glossary definition rides along with the
provenance, which is the point of the semantic layer.

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

`uv run python -m ekg.lineage impact installed_asset` (abridged; 37 downstream datasets):

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

Caveats. Job-level lineage over-approximates: `failure_model` writes eleven files from
seven inputs, and the graph says each output depends on every input, which is true at the
job level and false at the column level. Column-level lineage would need the SQL and
pandas in each job parsed; that is not done. The query log is generated (300 rows from ten
templates), so "182 queries affected" counts the shape of a query history, not a real one.
