"""Data-trust job: completeness, freshness and schema drift per data product, against SLAs.

    uv run python -m ekg.trust baseline          # snapshot the live schemas -> trust/schema_baseline.json
    uv run python -m ekg.trust run [--as-of 2026-07-01T00:00:00] [--no-es] [--no-postgres]
                                                 # evaluate trust/slas.yaml -> reports/trust_results.json, Elasticsearch index ekg-trust

Sources. The lake (Project 1's CSV and Parquet files, read with DuckDB) and
the warehouse (Project 1's Postgres). Both are read-only inputs.

Checks (one document each in the index, status pass|fail|skip):
  completeness.rows        row count >= min_rows (lake)
  completeness.nulls       null rate of each required column <= max_null_rate (lake)
  completeness.warehouse   warehouse row count within max_row_count_gap of the lake count
  freshness                hours from the newest value of freshness_columns to --as-of <= freshness_sla_hours
  schema_drift             columns added / removed / retyped versus trust/schema_baseline.json

Freshness needs a clock. The data ends 2026-06-30 (the telemetry quarter), so
against the real clock every product is stale by months; --as-of lets the
job answer "was it fresh at the time". Both answers are honest and both are
in the README.

Timestamps in case.opened_at come in two formats (rule D1); the freshness
check parses both, the way ekg.load does.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from ekg.load import parse_case_ts

REPO = Path(__file__).resolve().parents[2]
SLA_PATH = REPO / "trust" / "slas.yaml"
BASELINE_PATH = REPO / "trust" / "schema_baseline.json"
INDEX = "ekg-trust"

LAKE_FILES = {"drive_summary": "data/parquet/drive_summary.parquet", "drive_model": "data/parquet/drive_model.parquet"}
WAREHOUSE_NAMES = {"drive_summary": "telemetry_drive_summary"}


# ---------------------------------------------------------------- sources
class Lake:
    def __init__(self, sois_repo: Path):
        self.repo = sois_repo
        self.con = duckdb.connect()

    def source(self, table: str) -> str | None:
        p = self.repo / LAKE_FILES.get(table, f"data/enterprise/{table}.csv")
        if not p.exists():
            return None
        if p.suffix == ".parquet":
            return f"read_parquet('{p}')"
        return f"read_csv('{p}', header=true, all_varchar=true, sample_size=2000)"

    def profile(self, table: str, columns: list[str]) -> dict | None:
        src = self.source(table)
        if src is None:
            return None
        cols = ", ".join(f"sum(CASE WHEN \"{c}\" IS NULL OR CAST(\"{c}\" AS VARCHAR) = '' THEN 1 ELSE 0 END) AS \"{c}\"" for c in columns)
        row = self.con.execute(f"SELECT count(*) AS n{', ' + cols if cols else ''} FROM {src}").fetchone()
        n = int(row[0])
        return {"rows": n, "null_rate": {c: (int(v) / n if n else 0.0) for c, v in zip(columns, row[1:])}}

    def max_value(self, table: str, column: str) -> str | None:
        src = self.source(table)
        if src is None:
            return None
        vals = [r[0] for r in self.con.execute(f"SELECT DISTINCT \"{column}\" FROM {src} WHERE \"{column}\" IS NOT NULL").fetchall()]
        parsed = [parse_case_ts(v) or (str(v) if re.match(r"^\d{4}-\d{2}-\d{2}", str(v)) else None) for v in vals]
        parsed = [p for p in parsed if p]
        return max(parsed) if parsed else None

    def schema(self, table: str) -> list[dict] | None:
        src = self.source(table)
        if src is None:
            return None
        if ".csv" in src:
            src = src.replace("all_varchar=true, ", "")
        rows = self.con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
        return [{"name": r[0], "type": str(r[1]).lower()} for r in rows]


class Warehouse:
    def __init__(self, con):
        self.con = con

    def rows(self, table: str) -> int | None:
        t = WAREHOUSE_NAMES.get(table, table)
        try:
            return int(self.con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
        except Exception:  # noqa: BLE001
            self.con.rollback()
            return None

    def schema(self, table: str) -> list[dict] | None:
        t = WAREHOUSE_NAMES.get(table, table)
        rows = self.con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (t,)).fetchall()
        return [{"name": r[0], "type": r[1]} for r in rows] or None


# ---------------------------------------------------------------- checks (pure given profiles)
def hours_between(newest_iso: str, as_of: datetime) -> float:
    newest = datetime.fromisoformat(newest_iso[:19])
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return (as_of - newest).total_seconds() / 3600


def schema_diff(baseline: list[dict] | None, live: list[dict] | None) -> dict:
    if baseline is None or live is None:
        return {"added": [], "removed": [], "retyped": [], "comparable": False}
    b = {c["name"]: c["type"] for c in baseline}
    l = {c["name"]: c["type"] for c in live}
    return {"added": sorted(set(l) - set(b)), "removed": sorted(set(b) - set(l)),
            "retyped": sorted(f"{k}: {b[k]} -> {l[k]}" for k in set(b) & set(l) if b[k] != l[k]), "comparable": True}


def evaluate(slas: dict, lake: Lake, warehouse: Warehouse | None, baseline: dict, as_of: datetime, run_id: str) -> list[dict]:
    docs: list[dict] = []
    stamp = {"run_id": run_id, "as_of": as_of.isoformat(), "evaluated_at": datetime.now(timezone.utc).isoformat()}

    def doc(dp: str, check: str, table: str | None, status: str, detail: dict, threshold=None, value=None) -> None:
        docs.append({**stamp, "data_product": dp, "owner": slas["data_products"][dp].get("owner"), "check": check, "table": table,
                     "status": status, "threshold": threshold, "value": value, "detail": detail})

    for dp, spec in slas["data_products"].items():
        newest_overall: str | None = None
        for table, rule in spec["tables"].items():
            cols = list(rule.get("max_null_rate", {}))
            prof = lake.profile(table, cols)
            if prof is None:
                doc(dp, "completeness.rows", table, "skip", {"reason": "lake file missing"})
                continue
            doc(dp, "completeness.rows", table, "pass" if prof["rows"] >= rule["min_rows"] else "fail", {"rows": prof["rows"]},
                threshold=rule["min_rows"], value=prof["rows"])
            for c, limit in rule.get("max_null_rate", {}).items():
                rate = prof["null_rate"][c]
                doc(dp, "completeness.nulls", f"{table}.{c}", "pass" if rate <= limit else "fail", {"null_rate": round(rate, 5), "rows": prof["rows"]},
                    threshold=limit, value=round(rate, 5))
            if warehouse is not None and "warehouse_vs_lake" in spec:
                wrows = warehouse.rows(table)
                if wrows is None:
                    doc(dp, "completeness.warehouse", table, "skip", {"reason": "table not in warehouse"})
                else:
                    gap = abs(wrows - prof["rows"]) / prof["rows"] if prof["rows"] else 0.0
                    doc(dp, "completeness.warehouse", table, "pass" if gap <= spec["warehouse_vs_lake"]["max_row_count_gap"] else "fail",
                        {"lake_rows": prof["rows"], "warehouse_rows": wrows, "gap": round(gap, 5)},
                        threshold=spec["warehouse_vs_lake"]["max_row_count_gap"], value=round(gap, 5))
            live = lake.schema(table)
            base = baseline.get("lake", {}).get(table)
            d = schema_diff(base, live)
            status = "skip" if not d["comparable"] else ("pass" if not (d["added"] or d["removed"] or d["retyped"]) else "fail")
            doc(dp, "schema_drift", table, status, d, threshold=0, value=len(d["added"]) + len(d["removed"]) + len(d["retyped"]))
            if warehouse is not None:
                dw = schema_diff(baseline.get("warehouse", {}).get(table), warehouse.schema(table))
                status = "skip" if not dw["comparable"] else ("pass" if not (dw["added"] or dw["removed"] or dw["retyped"]) else "fail")
                doc(dp, "schema_drift", f"pg.{WAREHOUSE_NAMES.get(table, table)}", status, dw, threshold=0,
                    value=len(dw["added"]) + len(dw["removed"]) + len(dw["retyped"]))
        for table, col in spec.get("freshness_columns", {}).items():
            newest = lake.max_value(table, col)
            if newest is None:
                doc(dp, "freshness", f"{table}.{col}", "skip", {"reason": "no parseable timestamps"})
                continue
            age = hours_between(newest, as_of)
            newest_overall = max(newest_overall or "", newest)
            doc(dp, "freshness", f"{table}.{col}", "pass" if age <= spec["freshness_sla_hours"] else "fail",
                {"newest": newest, "age_hours": round(age, 1)}, threshold=spec["freshness_sla_hours"], value=round(age, 1))
        checks = [d for d in docs if d["data_product"] == dp]
        failed = [d for d in checks if d["status"] == "fail"]
        doc(dp, "summary", None, "fail" if failed else "pass",
            {"checks": len(checks), "failed": len(failed), "failed_checks": sorted({f"{d['check']}:{d['table']}" for d in failed}),
             "newest_event": newest_overall}, threshold=0, value=len(failed))
    return docs


# ---------------------------------------------------------------- elasticsearch
MAPPING = {
    "mappings": {"properties": {
        "run_id": {"type": "keyword"}, "as_of": {"type": "date"}, "evaluated_at": {"type": "date"},
        "data_product": {"type": "keyword"}, "owner": {"type": "keyword"}, "check": {"type": "keyword"}, "table": {"type": "keyword"},
        "status": {"type": "keyword"}, "threshold": {"type": "float"}, "value": {"type": "float"}, "detail": {"type": "object", "enabled": False}}}}


def index_docs(es_url: str, docs: list[dict]) -> int:
    from elasticsearch import Elasticsearch, helpers
    es = Elasticsearch(es_url)
    if not es.indices.exists(index=INDEX):
        es.indices.create(index=INDEX, body=MAPPING)
    actions = [{"_index": INDEX, "_id": f"{d['run_id']}:{d['data_product']}:{d['check']}:{d['table']}", "_source": d} for d in docs]
    ok, _ = helpers.bulk(es, actions, refresh=True)
    return ok


# ---------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    from ekg.config import pg_connect, settings
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["baseline", "run"])
    ap.add_argument("--as-of", type=str, default=None, help="ISO timestamp; default now (UTC)")
    ap.add_argument("--no-es", action="store_true")
    ap.add_argument("--no-postgres", action="store_true")
    ap.add_argument("--reports", type=Path, default=Path("reports"))
    a = ap.parse_args(argv)
    s = settings()
    slas = yaml.safe_load(SLA_PATH.read_text())
    lake = Lake(s.sois_repo)
    warehouse = None
    pg = None
    if not a.no_postgres:
        try:
            pg = pg_connect()
            warehouse = Warehouse(pg)
        except Exception as e:  # noqa: BLE001
            print(f"postgres not available ({type(e).__name__}); warehouse checks skipped", file=sys.stderr)
    tables = sorted({t for spec in slas["data_products"].values() for t in spec["tables"]})
    if a.command == "baseline":
        base = {"captured_at": datetime.now(timezone.utc).isoformat(), "lake": {}, "warehouse": {}}
        for t in tables:
            base["lake"][t] = lake.schema(t)
            if warehouse is not None:
                base["warehouse"][t] = warehouse.schema(t)
        BASELINE_PATH.write_text(json.dumps(base, indent=2) + "\n")
        print(f"baseline for {len(tables)} tables -> {BASELINE_PATH}")
        return 0
    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else {}
    as_of = datetime.fromisoformat(a.as_of).replace(tzinfo=timezone.utc) if a.as_of else datetime.now(timezone.utc)
    run_id = as_of.strftime("%Y%m%dT%H%M%S") + ("-asof" if a.as_of else "-now")
    docs = evaluate(slas, lake, warehouse, baseline, as_of, run_id)
    if pg is not None:
        pg.close()
    a.reports.mkdir(parents=True, exist_ok=True)
    out = {"run_id": run_id, "as_of": as_of.isoformat(), "checks": len(docs),
           "by_status": {k: sum(1 for d in docs if d["status"] == k) for k in ("pass", "fail", "skip")},
           "summary": {d["data_product"]: {"status": d["status"], **d["detail"]} for d in docs if d["check"] == "summary"},
           "docs": docs}
    (a.reports / f"trust_results_{'asof' if a.as_of else 'now'}.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
    for dp, v in out["summary"].items():
        print(f"{dp}: {v['status']} ({v['failed']}/{v['checks']} checks failed) {', '.join(v['failed_checks'])}")
    if not a.no_es:
        n = index_docs(s.es_url, docs)
        print(f"indexed {n} documents into {INDEX} at {s.es_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
