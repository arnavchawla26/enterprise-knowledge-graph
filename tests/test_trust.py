import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from conftest import FIX
from ekg import trust


def test_schema_diff_reports_added_removed_retyped():
    base = [{"name": "a", "type": "int"}, {"name": "b", "type": "str"}, {"name": "c", "type": "int"}]
    live = [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}, {"name": "d", "type": "str"}]
    d = trust.schema_diff(base, live)
    assert d == {"added": ["d"], "removed": ["c"], "retyped": ["b: str -> int"], "comparable": True}
    assert trust.schema_diff(None, live)["comparable"] is False


def test_hours_between_handles_both_source_formats_via_lake_parsing():
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert trust.hours_between("2026-06-30T00:00:00", as_of) == 24.0
    assert round(trust.hours_between("2026-06-30", as_of)) == 24


def test_evaluate_on_the_fixture_lake(tmp_path):
    """The fixture enterprise plays the lake; the SLA is written so that one completeness and one
    freshness check fail and the drift check sees one added column."""
    lake = trust.Lake(FIX.parent / "fixtures" / "lake_root")
    # build a lake root that mirrors Project 1's layout from the fixture CSVs
    root = tmp_path / "root"
    (root / "data" / "enterprise").mkdir(parents=True)
    for f in (FIX / "enterprise").glob("*.csv"):
        (root / "data" / "enterprise" / f.name).write_text(f.read_text())
    lake = trust.Lake(root)
    slas = {"data_products": {"dp": {"owner": "o", "freshness_sla_hours": 24, "freshness_columns": {"case": "opened_at"},
                                     "tables": {"case": {"min_rows": 10, "max_null_rate": {"case_id": 0.0, "closed_at": 0.1}},
                                                "site": {"min_rows": 2, "max_null_rate": {"site_id": 0.0}}}}}}
    baseline = {"lake": {"case": [c for c in lake.schema("case") if c["name"] != "source_system"], "site": lake.schema("site")}}
    docs = trust.evaluate(slas, lake, None, baseline, datetime(2026, 4, 10, tzinfo=timezone.utc), "run1")
    by = {(d["check"], d["table"]): d for d in docs}
    assert by[("completeness.rows", "case")]["status"] == "fail" and by[("completeness.rows", "case")]["value"] == 4
    assert by[("completeness.rows", "site")]["status"] == "pass"
    assert by[("completeness.nulls", "case.closed_at")]["status"] == "fail", "1 of 4 cases is open: null rate 0.25 > 0.1"
    assert by[("completeness.nulls", "case.case_id")]["status"] == "pass"
    assert by[("schema_drift", "case")]["status"] == "fail" and by[("schema_drift", "case")]["detail"]["added"] == ["source_system"]
    assert by[("schema_drift", "site")]["status"] == "pass"
    f = by[("freshness", "case.opened_at")]
    assert f["detail"]["newest"] == "2026-04-02T10:00:00" and f["status"] == "fail", "US and ISO timestamps both parsed; 8 days old vs 24 h"
    assert by[("summary", None)]["status"] == "fail" and by[("summary", None)]["value"] == 4


def test_sla_file_declares_every_project_1_data_product():
    slas = yaml.safe_load(trust.SLA_PATH.read_text())
    assert set(slas["data_products"]) == {"fleet_health", "parts_demand", "dispatch_quality"}
    for dp in slas["data_products"].values():
        assert dp["freshness_sla_hours"] > 0 and dp["tables"]
