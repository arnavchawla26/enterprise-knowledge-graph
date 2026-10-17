import pandas as pd

from conftest import FIX
from ekg import resolve
from ekg.load import account_record_rows


def _records():
    acc = pd.read_csv(FIX / "enterprise" / "account.csv", dtype=str, keep_default_na=False, na_values=[""])
    acc["account_row_id"] = acc["account_row_id"].astype(int)
    return account_record_rows(acc)


def test_token_overlap_handles_abbreviation_and_typo():
    assert resolve.token_overlap("westgate retail", "westgate retail") == 1.0
    assert resolve.token_overlap("northwind cap", "northwind capital") == 1.0
    assert resolve.token_overlap("maplewood anlytcs", "maplewood analytics") == 1.0
    assert resolve.token_overlap("northwind media", "northwind capital") == 0.5


def test_three_spellings_and_duplicate_ids_become_one_cluster():
    res = resolve.resolve_accounts(_records())
    c = res.clusters_with_id
    assert c[1] == c[4] == c[5], "Westgate: canonical, 'Westgate Retail, Inc.' (dup id), 'WESTGATE RETAIL' (dup row)"
    assert c[2] == c[6] == c[7], "Northwind Capital cluster"
    assert c[8] not in (c[1], c[2]), "Northwind Media is a different company (shares the block with Northwind Capital)"
    assert c[3] not in (c[1], c[2], c[8])
    canon = {v["account_id"] for v in res.canonical.values()}
    assert canon == {"ACC-00001", "ACC-00002", "ACC-00003", "ACC-00004"}


def test_canonical_record_is_the_cleanest_crm_cloud_row():
    res = resolve.resolve_accounts(_records())
    names = {v["name"] for v in res.canonical.values()}
    assert "Westgate Retail Holdings" in names and "WESTGATE RETAIL" not in names


def test_uncertain_band_goes_to_review_not_to_a_merge():
    recs = _records()
    res = resolve.resolve_accounts(recs, match=1.01, grey=0.5)
    assert (res.pairs["band"] == "uncertain").sum() > 0
    assert len(set(res.clusters.values())) == len(recs), "nothing merged by the name matcher above the top score"


def test_pairwise_metrics_against_truth():
    recs = _records()
    res = resolve.resolve_accounts(recs)
    truth = pd.read_csv(FIX / "truth" / "account_resolution.csv", dtype=str)
    m = resolve.measure_accounts(res, recs, truth, site_ids={"ACC-00001"}, contact_ids=set())
    for sl in ("dev", "held_out"):
        assert m[sl]["name_matcher_plus_same_id"]["precision"] == 1.0
        assert m[sl]["name_matcher_plus_same_id"]["recall"] == 1.0
    assert m["duplicate_ids_cited_by_sites"] == 0


def test_metrics_detect_a_wrong_merge():
    recs = _records()
    res = resolve.resolve_accounts(recs, match=0.60, grey=0.5)   # low threshold merges Northwind Media into Northwind Capital
    truth = pd.read_csv(FIX / "truth" / "account_resolution.csv", dtype=str)
    m = resolve.measure_accounts(res, recs, truth, set(), set())
    assert any(m[sl]["name_matcher_only"]["fp"] > 0 for sl in ("dev", "held_out"))


def test_asset_resolution_inverts_s1_s2_and_is_measured():
    assets = pd.read_csv(FIX / "enterprise" / "installed_asset.csv", dtype=str).rename(columns={"serial_number": "erp_serial"})
    drives = pd.Series(["ZA1000AA", "ZA1000AB", "ZA1000AC", "ZA1000AD", "ZA1000AE", "ZA1000AF", "ZZ9999ZZ"])
    m = resolve.resolve_assets(assets, drives)
    assert len(m) == 6 and set(m["method"]) == {"exact", "normalized"}
    truth = pd.read_csv(FIX / "truth" / "asset_serial_map.csv", dtype=str)
    met = resolve.measure_assets(m, assets, truth)
    assert met["precision"] == 1.0 and met["recall"] == 1.0 and met["by_mess"]["vendor_prefix"]["correct"] == 2
