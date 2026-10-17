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
        for ds in j.get("reads", []) + j.get("writes", []) + j.get("intermediates", []):
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


# ---------------------------------------------------------------- column-level lineage
def _columns_fixture():
    """Column names snapshotted from the real files and Postgres (tests/fixtures/columns.json)."""
    import json
    fix = json.loads((FIX / "columns.json").read_text())
    lake = {t: [{"name": c, "data_type": "x", "ordinal": i + 1} for i, c in enumerate(cols)] for t, cols in fix["lake"].items()}
    wh = {t: [{"name": c, "data_type": "x", "ordinal": i + 1} for i, c in enumerate(cols)] for t, cols in fix["warehouse"].items()}
    return lake, wh


def test_column_edges_cover_copy_cast_and_declared_derivations():
    pipe = lineage.read_pipeline()
    lake, wh = _columns_fixture()
    rows = lineage.build(pipe, _generated(), lake, wh, "sois")
    assert rows["column_unresolved"] == [], rows["column_unresolved"]
    edges = {(e["src"], e["dst"]): e for e in rows["column_derived"]}
    # Postgres copy job: 1:1 by column name, including the three renamed telemetry tables
    assert edges[("pg.installed_asset.warranty_end", "installed_asset.warranty_end")]["precision"] == "copy"
    assert edges[("pg.telemetry_drive_summary.failed", "drive_summary.failed")]["via"] == "JOB-003"
    assert ("pg.telemetry_daily_recent.smart_5_raw", "telemetry_daily.smart_5_raw") in edges
    # raw CSV -> Parquet is a cast per kept column; `day` has no source column
    assert edges[("telemetry_daily.smart_5_raw", "backblaze_daily_csv.smart_5_raw")]["precision"] == "cast"
    assert not any(src == "telemetry_daily.day" for src, _ in edges)
    # load_telemetry's two GROUP BYs
    assert edges[("drive_summary.failure_date", "telemetry_daily.failure")]["expr"].startswith("max(CASE WHEN failure = 1")
    assert ("drive_summary.failure_date", "telemetry_daily.date") in edges
    assert ("drive_model.vendor", "drive_summary.model") in edges
    assert ("drive_model.failure_count", "drive_summary.failed") in edges
    # the failure model's features: a delta feeds from the raw counter and the date, the score from every feature plus model
    assert {dst for src, dst in edges if src == "failure_model/features.smart_5_delta7"} == {"telemetry_daily.smart_5_raw", "telemetry_daily.date"}
    lgbm_inputs = {dst for src, dst in edges if src == "failure_model/features.lightgbm"}
    assert len(lgbm_inputs) == 26   # 25 numeric features + model
    assert "failure_model/features.model" in lgbm_inputs and "failure_model/features.smart_198_max7" in lgbm_inputs
    assert {dst for src, dst in edges if src == "failure_model/features.y"} == {"drive_summary.failure_date", "telemetry_daily.date"}
    # the report figure's first hop, and the join columns behind its site_id
    assert edges[("reports/fleet_risk_sites.csv.expected_failures_30d", "failure_model/features.p_fail_30d")]["expr"].startswith("sum(p_fail_30d)")
    assert ("reports/fleet_risk_sites.csv.site_id", "installed_asset.site_id") in edges
    assert ("reports/fleet_risk_top100.csv.p_fail_30d", "failure_model/features.p_fail_30d") in edges
    # no self loops, no dangling ends
    cols = {c["column_key"] for c in rows["columns"]}
    assert all(src in cols and dst in cols and src != dst for src, dst in edges)
    # the feature frame is an intermediate: produced by the job, between its reads and its writes
    assert {"src": "failure_model/features", "dst": "JOB-005"} in rows["produced"]
    assert any(d["src"] == "reports/fleet_risk_sites.csv" and d["dst"] == "failure_model/features" for d in rows["derived"])
    assert any(d["src"] == "failure_model/features" and d["dst"] == "telemetry_daily" for d in rows["derived"])


def test_unresolved_declared_columns_are_reported_not_written():
    pipe = lineage.read_pipeline()
    lake, wh = _columns_fixture()
    lake["drive_summary"] = [c for c in lake["drive_summary"] if c["name"] != "failed"]
    rows = lineage.build(pipe, _generated(), lake, wh, "sois")
    assert "drive_summary.failed" in rows["column_unresolved"]
    assert not any(e["dst"] == "drive_summary.failed" for e in rows["column_derived"])


@pytest.mark.skipif(not settings().sois_repo.exists(), reason="services-ops-intelligence checkout not present")
def test_feature_group_matches_project1_feature_columns():
    """numeric_features in pipeline.yaml must be exactly sois.failure_model.feature_columns(), and every
    telemetry column the feature frame reads must be a column load_telemetry keeps."""
    from ekg.gds import _import_sois
    fm = _import_sois(settings().sois_repo)
    import sois.load_telemetry as lt
    pipe = lineage.read_pipeline()
    assert pipe["column_groups"]["numeric_features"] == fm.feature_columns()
    assert set(pipe["column_groups"]["bb_five_raw"]) == {f"smart_{n}_raw" for n in fm.BB_FIVE}
    declared = set(pipe["datasets"]["failure_model/features"]["columns"])
    assert set(fm.NUMERIC_FEATURES) | {"model", "y", "censored", "serial_number", "date"} <= declared
    for block in pipe["column_lineage"]:
        if block["source"] == "telemetry_daily" and block.get("columns"):
            for col, spec in block["columns"].items():
                for f in spec["from"]:
                    if "." not in f:
                        assert f in lt.KEPT_COLUMNS, f"{block['target']}.{col} reads telemetry_daily.{f}, which load_telemetry does not keep"


def test_format_provenance_prints_column_path_before_job_path():
    d = {"column": "reports/fleet_risk_sites.csv.expected_failures_30d", "table": "reports/fleet_risk_sites.csv", "producers": ["failure_model"],
         "means": None, "definition": None,
         "columns": [{"column": "failure_model/features.p_fail_30d", "table": "failure_model/features", "source": "real_derived", "depth": 1,
                      "parent": "reports/fleet_risk_sites.csv.expected_failures_30d", "via": "failure_model", "precision": "expr", "expr": "sum(p_fail_30d) per site"},
                     {"column": "backblaze_daily_csv.smart_5_raw", "table": "backblaze_daily_csv", "source": "real", "depth": 2,
                      "parent": "failure_model/features.p_fail_30d", "via": "load_telemetry", "precision": "cast", "expr": None}],
         "upstream": [{"depth": 1, "node": "telemetry_daily", "kind": "parquet", "source": "real", "produced_by": ["load_telemetry"]}]}
    out = lineage.format_provenance(d)
    assert out.index("column path (2 upstream columns, 2 hops") < out.index("job path:")
    assert "<- failure_model/features.p_fail_30d  [failure_model: sum(p_fail_30d) per site]" in out
    assert "<- backblaze_daily_csv.smart_5_raw  [load_telemetry: cast]" in out
    assert "column path: none declared" in lineage.format_provenance({**d, "columns": []})


def test_format_column_impact():
    d = {"column": "telemetry_daily.smart_5_raw", "jobs": ["failure_model", "load_postgres"], "tables": ["failure_model/features", "pg.telemetry_daily_recent"],
         "downstream": [{"depth": 1, "column": "failure_model/features.smart_5_raw", "table": "failure_model/features", "kind": "dataframe"},
                        {"depth": 1, "column": "pg.telemetry_daily_recent.smart_5_raw", "table": "pg.telemetry_daily_recent", "kind": "postgres_table"},
                        {"depth": 2, "column": "failure_model/features.lightgbm", "table": "failure_model/features", "kind": "dataframe"}]}
    out = lineage.format_column_impact(d)
    assert out.startswith("telemetry_daily.smart_5_raw: 3 downstream columns in 2 tables")
    assert "    -> failure_model/features.lightgbm (dataframe)" in out and "jobs that must re-run: failure_model, load_postgres" in out
