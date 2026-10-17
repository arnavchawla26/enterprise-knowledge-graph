# Data-trust job (ekg.trust) and dashboard

`trust/slas.yaml` declares, per data product, the required tables, minimum row counts,
maximum null rates per column, an event column for freshness with an SLA in hours, and a
tolerated row-count gap between the lake copy and the warehouse copy. `ekg.trust run`
evaluates every check against the lake (Project 1's CSV and Parquet, via DuckDB) and the
warehouse (Project 1's Postgres), writes one document per check to the Elasticsearch
index `ekg-trust`, and a JSON copy to `reports/trust_results_{now,asof}.json`. Schema drift
compares live columns and types to `trust/schema_baseline.json`.

Results, 2026-10-17, 111 checks per run:

| Clock | pass | fail | fleet_health | parts_demand | dispatch_quality |
|---|---:|---:|---|---|---|
| Real clock (2026-10-17) | 93 | 18 | fail, 5/32 | fail, 4/38 | fail, 6/38 |
| As of 2026-07-01 | 97 | 14 | fail, 4/32 | fail, 3/38 | fail, 4/38 |

(The fail counts include the three per-product summary documents.)

What failed and why, both true:

- `completeness.warehouse` on every table: Project 1's Postgres on this machine holds its
  6-row test fixture (its live loader test reloads the database), while the lake holds
  359,101 assets. Gap 0.99998 against a tolerance of 0.01. The warehouse is wrong and the
  job says so; nothing in this repository modifies Project 1's database. This is also why
  `ekg.load` reads the drive summary from Parquet by default.
- `freshness` against the real clock: the newest telemetry day is 2026-06-30 (age 2,625 h
  against a 24 h SLA), the newest dispatch 2026-07-11, the newest case 2026-07-03. The data
  is a finished quarter. As of 2026-07-01 the telemetry is fresh and the support tables pass
  their SLAs; a first draft measured freshness on `installed_asset.install_date`, which is
  historical by construction, and was removed because it is not an event stream.
- `schema_drift`: 0 on every table, because the baseline was captured from the same files.
  `tests/test_trust.py` shows the check firing on an added column.

Dashboard: `kibana/trust_dashboard.ndjson` (a data view, six Lens panels, one dashboard),
imported with the saved-objects API and captured headlessly with Playwright by
`scripts/screenshot_kibana.py` into `reports/trust_dashboard.png`.
