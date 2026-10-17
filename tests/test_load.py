import pandas as pd
import pytest

from ekg import load
from conftest import FIX


def test_case_timestamps_parse_both_formats():
    assert load.parse_case_ts("2026-04-02T12:11:06") == "2026-04-02T12:11:06"
    assert load.parse_case_ts("03/15/2026 18:48") == "2026-03-15T18:48:00"
    assert load.parse_case_ts("") is None and load.parse_case_ts(None) is None and load.parse_case_ts(float("nan")) is None
    assert load.parse_case_ts("not a date") is None


def test_serial_normalization_covers_rules_s1_s2():
    for raw, want in [("SG-ZA1000AB", "ZA1000AB"), ("HGST_za1000ad", "ZA1000AD"), ("za1000ac", "ZA1000AC"),
                      ("TOS-X", "X"), ("WD-X", "X"), ("HIT-X", "X"), ("OEM-X", "X"), ("ZA1000AA", "ZA1000AA")]:
        assert load.normalize_serial(raw) == want
    assert load.normalize_serial("SGX") == "SGX", "only a real prefix is stripped"


def test_name_normalization():
    n = load.normalize_name
    assert n("Westgate Retail Holdings") == n("Westgate Retail, Inc.") == n("WESTGATE RETAIL") == n("The Westgate  Retail") == "westgate retail"
    assert n("Northwind Cap. Holdings") == "northwind cap"


def test_asset_rows_keep_both_identifiers(enterprise):
    rows = load.asset_rows(enterprise["installed_asset"])
    r = {x["asset_id"]: x for x in rows}
    assert r["AST-0000002"]["erp_serial"] == "SG-ZA1000AB" and r["AST-0000002"]["serial_norm"] == "ZA1000AB"
    assert r["AST-0000003"]["warranty_end"] is None, "null warranty stays null (rule W1)"


def test_case_rows_parse_and_carry_truth(enterprise, truth_dir):
    truth = pd.read_csv(truth_dir / "case_truth.csv")
    rows = {r["case_id"]: r for r in load.case_rows(enterprise["case"], truth)}
    assert rows["CS-0000001"]["opened_at"] == "2026-03-15T18:48:00" and rows["CS-0000001"]["opened_at_raw"] == "03/15/2026 18:48"
    assert rows["CS-0000003"]["closed_at"] is None
    assert rows["CS-0000002"]["truth_deflectable"] is True and rows["CS-0000001"]["truth_hardware_fault"] is True
    assert rows["CS-0000001"]["failure_reported"] == 1


def test_pod_rows_flag_risky_pod():
    drives = pd.read_csv(FIX / "drives.csv", dtype={"vault_id": str, "pod_id": str, "cluster_id": str})
    load.RISKY_POD_MIN_DRIVES = 2
    try:
        pods = {p["pod_key"]: p for p in load.pod_rows(drives)}
    finally:
        load.RISKY_POD_MIN_DRIVES = 10
    assert set(pods) == {"sac2/1155/01", "phx1/2000/18", "sac2/1156/02"}
    assert pods["sac2/1155/01"]["expected_failures_30d"] == pytest.approx(0.901)
    assert pods["phx1/2000/18"]["scored_drives"] == 1 and pods["phx1/2000/18"]["risky"] is False, "below the minimum scored drives"
    assert pods["sac2/1155/01"]["risky"] is True and pods["sac2/1156/02"]["risky"] is False


def test_product_uses_part_is_derived_from_dispatches(enterprise):
    e = enterprise
    edges = load.product_uses_part(e["case"], e["dispatch"], e["dispatch_part"], e["installed_asset"])
    assert sorted((x["src"], x["dst"], x["dispatch_count"]) for x in edges) == [("PRD-SA300", "PRT-0001", 1), ("PRD-SA300", "PRT-0002", 1)]


class FakeSession:
    """Records the Cypher the loader would send, so the write plan is testable without Neo4j."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run(self, cypher, **params):
        self.calls.append((cypher, params))
        return self

    def consume(self):
        return None

    def single(self):
        return {"n": 0}

    def data(self):
        return []


def test_load_plan_covers_every_label_and_edge(enterprise, truth_dir):
    e = dict(enterprise)
    e["case_truth"] = pd.read_csv(truth_dir / "case_truth.csv")
    drives = pd.read_csv(FIX / "drives.csv", dtype={"vault_id": str, "pod_id": str, "cluster_id": str})
    s = FakeSession()
    counts = load.load(s, e, drives, "2026-05-31", log=lambda m: None)
    assert counts["Asset"] == 6 and counts["Drive"] == 6 and counts["Case"] == 4 and counts["AccountRecord"] == 8
    assert counts["HOSTS"] == 6 and counts["ABOUT"] == 4 and counts["USED"] == 2 and counts["COVERS"] == 2
    assert counts["Pod"] == 3 and counts["Vault"] == 3
    sent = " ".join(c for c, _ in s.calls)
    for rel in ("HOSTS", "IS_A", "ABOUT", "OPENED_BY", "RESOLVES", "PERFORMS", "USED", "SUPPLIED_BY", "USES", "COVERS", "OF_MODEL", "IN_POD", "IN_VAULT", "INSTANCE_OF"):
        assert f"[:{rel}]" in sent or f"[e:{rel}]" in sent, rel
    assert "CREATE CONSTRAINT asset_id IF NOT EXISTS" in sent
