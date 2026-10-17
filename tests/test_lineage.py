import re
from pathlib import Path

import pandas as pd
import pytest

from conftest import FIX
from ekg import lineage
from ekg.config import settings


def _generated():
    return {n: pd.read_csv(FIX / "enterprise" / f"{n}.csv", dtype=str) for n in ("data_product", "job", "lineage", "query_log")}


def test_build_links_columns_jobs_products_queries_and_terms():
    pipe = lineage.read_pipeline()
    lake = {"installed_asset": lineage.columns_from_file(FIX / "enterprise" / "installed_asset.csv"),
            "case": lineage.columns_from_file(FIX / "enterprise" / "case.csv")}
    wh = {"installed_asset": [{"name": "asset_id", "data_type": "character varying", "ordinal": 1},
                              {"name": "warranty_end", "data_type": "date", "ordinal": 6}]}
    rows = lineage.build(pipe, _generated(), lake, wh, "sois")
    names = {t["name"] for t in rows["tables"]}
    assert {"installed_asset", "pg.installed_asset", "telemetry_daily", "reports/fleet_risk_sites.csv"} <= names
    assert {"src": "installed_asset.warranty_end", "dst": "WarrantyStatus"} in rows["means"]
    assert {"src": "pg.installed_asset.warranty_end", "dst": "WarrantyStatus"} in rows["means"]
    assert {"src": "case.failure_reported", "dst": "ServiceFailure"} in rows["means"]
    assert {"src": "reports/fleet_risk_sites.csv", "dst": "JOB-005"} in rows["produced"]
    assert {"src": "JOB-005", "dst": "telemetry_daily"} in rows["reads"]
    assert {"src": "pg.installed_asset", "dst": "JOB-003"} in rows["produced"], "load_postgres produces every warehouse copy"
    assert {"src": "DP-001", "dst": "pg.installed_asset"} in rows["composed"], "data products are served from the warehouse"
    assert {"src": "Q-00003", "dst": "pg.case"} in rows["query_reads"]
    planned = [j for j in rows["jobs"] if j["job_id"] == "JOB-002"]
    assert planned and planned[0]["status"] == "real" and planned[0]["source"] == "declared"
    assert any(c["column_key"] == "installed_asset.serial_number" for c in rows["columns"])
    assert {"src": "pg.installed_asset", "dst": "installed_asset", "via": "JOB-003", "precision": "copy"} in rows["derived"]
    assert {"src": "JOB-002", "dst": "installed_asset"} not in rows["reads"], "a job does not read its own output"
    assert any(d["src"] == "case" and d["dst"] == "installed_asset" and d["precision"] == "declared_row" for d in rows["derived"])
    assert any(d["src"] == "reports/fleet_risk_sites.csv" and d["dst"] == "telemetry_daily" and d["precision"] == "job" for d in rows["derived"])


def test_columns_from_file_reads_csv_header():
    cols = lineage.columns_from_file(FIX / "enterprise" / "case.csv")
    assert [c["name"] for c in cols][:3] == ["case_id", "opened_at", "closed_at"]
    assert lineage.columns_from_file(FIX / "nope.csv") == []


@pytest.mark.skipif(not settings().sois_repo.exists(), reason="services-ops-intelligence checkout not present")
def test_declared_reads_and_writes_are_named_in_each_jobs_source():
    """A stale declaration fails here: every file a job is declared to read or write must appear in its module."""
    pipe = lineage.read_pipeline()
    repos = {"services-ops-intelligence": settings().sois_repo, "enterprise-knowledge-graph": Path(__file__).resolve().parents[1]}
    for j in pipe["jobs"]:
        src = (repos[j["repo"]] / j["module"]).read_text()
        if j.get("config"):
            src += (repos[j["repo"]] / j["config"]).read_text()
        for ds in j.get("reads", []) + j.get("writes", []):
            d = pipe["datasets"][ds]
            if d["kind"] in ("neo4j", "elasticsearch_index"):
                continue
            base = Path(d["path"]).name
            stem = base.split(".")[0]
            assert stem in src, f"{j['job_id']} {j['name']}: '{base}' not in {j['module']}"


def test_provenance_and_impact_formatting():
    d = {"column": "reports/fleet_risk_sites.csv.expected_failures_30d", "table": "reports/fleet_risk_sites.csv", "producers": ["failure_model"],
         "means": "Fleet Health", "definition": "x",
         "upstream": [{"depth": 1, "node": "telemetry_daily", "kind": "parquet", "source": "real", "produced_by": ["load_telemetry"]}]}
    out = lineage.format_provenance(d)
    assert "written by failure_model" in out and "<- telemetry_daily (parquet, real, by load_telemetry)" in out
    i = {"table": "installed_asset", "downstream": [{"depth": 1, "node": "pg.installed_asset", "kind": "postgres_table"}], "jobs": ["load_postgres"],
         "data_products": ["fleet_health"], "logged_queries": 3, "query_users": ["analyst_01"]}
    out = lineage.format_impact(i)
    assert "jobs that must re-run: load_postgres" in out and "data products affected: fleet_health" in out
