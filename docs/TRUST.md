# Data-trust job (ekg.trust) and dashboard

`trust/slas.yaml` declares, per data product, the required tables, minimum row counts,
maximum null rates per column, an event column for freshness with an SLA in hours, and a
tolerated row-count gap between the lake copy and the warehouse copy. `ekg.trust run`
evaluates every check against the lake (Project 1's CSV and Parquet, via DuckDB) and the
warehouse (Project 1's Postgres), writes one document per check to the Elasticsearch
index `ekg-trust`, and a JSON copy to `reports/trust_results_{now,asof}.json`. Schema drift
compares live columns and types to `trust/schema_baseline.json`.

Results, 2026-10-17, 111 checks per run, lake and warehouse both holding the full data:

| Clock | pass | fail | fleet_health | parts_demand | dispatch_quality |
|---|---:|---:|---|---|---|
| Real clock (2026-10-17) | 104 | 7 | fail, 1/32 | fail, 1/38 | fail, 2/38 |
| As of 2026-07-01 | 111 | 0 | pass | pass | pass |

(The fail counts include the per-product summary documents.)

What failed and why:

- `freshness` against the real clock, and nothing else: the newest telemetry day is
  2026-06-30 (age 2,625 h against a 24 h SLA), the newest dispatch 2026-07-11, the newest
  case 2026-07-03. The data is a finished quarter. As of 2026-07-01 every product passes.
  A first draft measured freshness on `installed_asset.install_date`, which is historical by
  construction, and was removed because it is not an event stream.
- `completeness.warehouse` passes on every table (gap 0.0 against a tolerance of 0.01).
  Earlier the same day, before Project 1's Postgres was reloaded, it held that repository's
  6-row test fixture and this job reported 18 failures against the real clock and 14 as of
  2026-07-01, every warehouse-vs-lake check among them (gap 0.99998 on `installed_asset`).
  That run is the demonstration that the check catches a warehouse that disagrees with the
  lake; it was superseded, not hidden.
- `schema_drift`: 0 on every table, because the baseline was captured from the same files.
  `tests/test_trust.py` shows the check firing on an added column.

Dashboard: `kibana/trust_dashboard.ndjson` (a data view, six Lens panels, one dashboard),
imported with the saved-objects API and captured headlessly with Playwright by
`scripts/screenshot_kibana.py` into `reports/trust_dashboard.png`.
