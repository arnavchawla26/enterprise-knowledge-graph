"""Entity resolution: CRM account records -> one Account; telemetry Drive -> its ERP Asset.

    uv run python -m ekg.resolve [--write] [--reports reports]

Accounts. Every CRM row is an AccountRecord. Candidate pairs come from a
block (same region, same first three letters of the normalized name); each
pair is scored:

    score = 0.45 * jaro_winkler(name_norm_a, name_norm_b)
          + 0.30 * token_overlap(name_norm_a, name_norm_b)   # equal, prefix, or JW >= 0.9 per token
          + 0.15 * same_industry + 0.10 * same_region

Pairs with score >= MATCH are merged (union-find); pairs in [GREY, MATCH) go to
reports/uncertain_matches.csv for a human; below GREY is a non-match. A second,
deterministic rule links rows that share an account_id (rule A3: same id, same
company) and is reported separately, because it is an id join, not matching.
Site and contact overlap are computed but carry no weight here: in this data no
site or contact ever cites a duplicate id (checked by the metrics), so the
feature is always zero across spellings; it is kept so the code is honest about
what a real CRM would offer.

Canonical record of a cluster: the crm_cloud row with the fewest mess markers
(all caps, doubled space, leading 'The', comma, abbreviation dot), lowest row id
on ties; its account_id becomes Account.account_id.

Assets. The ERP serial is the telemetry serial with, sometimes, a vendor prefix
(rule S1) or lowercase (rule S2). A blind "strip and upper-case" rule is wrong
on this data: 430 real telemetry serials are natively lowercase and 86 start
with a real "WD-". So the matcher tries candidates in order (raw, prefix
stripped, upper-cased, both) against the set of known telemetry serials and
takes the first hit, scored 1.0 / 0.95 / 0.95 / 0.9 by how much it had to
change; a candidate already claimed by another asset is a collision and goes
to the report instead of the graph. This is a rule matcher checked against
the data, not a learned one, and the metrics say so.

Measurement. Companies are split 50/50 by a hash of their true canonical id;
thresholds were chosen on the dev half and every number in
reports/resolution_metrics.json is reported for both halves, with the held-out
half the one to quote. Metrics are pairwise: a pair of records is a true pair
when truth gives them one canonical id; precision = TP / predicted pairs,
recall = TP / true pairs, both restricted to pairs inside the slice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import pandas as pd
from rapidfuzz.distance import JaroWinkler

from ekg.load import _PREFIX_RE, normalize_name, normalize_serial  # noqa: PLC2701

MATCH = 0.85
GREY = 0.70
WEIGHTS = {"jw": 0.45, "tokens": 0.30, "industry": 0.15, "region": 0.10}
NAME_ONLY_WEIGHTS = {"jw": 0.6, "tokens": 0.4, "industry": 0.0, "region": 0.0}


# ---------------------------------------------------------------- scoring
def token_overlap(a: str, b: str) -> float:
    """Share of tokens in the shorter name that match a token in the other name:
    equal, one a prefix of the other (abbreviations like 'aero'/'aerospace'),
    or Jaro-Winkler >= 0.9 (typos like 'anlytcs'/'analytics')."""
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return 0.0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    hit = 0
    used: set[int] = set()
    for t in short:
        for i, u in enumerate(long_):
            if i in used:
                continue
            if t == u or t.startswith(u) or u.startswith(t) or JaroWinkler.similarity(t, u) >= 0.9:
                hit += 1
                used.add(i)
                break
    return hit / len(short)


def score_pair(a: dict, b: dict, weights: dict | None = None) -> dict:
    w = weights or WEIGHTS
    jw = JaroWinkler.similarity(a["name_norm"], b["name_norm"])
    tok = token_overlap(a["name_norm"], b["name_norm"])
    ind = 1.0 if a.get("industry") and a.get("industry") == b.get("industry") else 0.0
    reg = 1.0 if a.get("region") and a.get("region") == b.get("region") else 0.0
    s = w["jw"] * jw + w["tokens"] * tok + w["industry"] * ind + w["region"] * reg
    return {"score": round(s, 4), "jw": round(jw, 4), "tokens": round(tok, 4), "same_industry": ind, "same_region": reg,
            "same_id": 1.0 if a["account_id"] == b["account_id"] else 0.0}


def block_key(rec: dict) -> str:
    first = rec["name_norm"].split(" ")[0] if rec["name_norm"] else ""
    return f"{rec.get('region', '')}|{first[:3]}"


MESS_MARKERS = (re.compile(r"[A-Z]{4,}"), re.compile(r"  "), re.compile(r"^The "), re.compile(r","), re.compile(r"\.\s|\.$"))


def mess_score(name: str, source_system: str) -> tuple[int, int]:
    return (0 if source_system == "crm_cloud" else 1, sum(1 for m in MESS_MARKERS if m.search(name)))


class UnionFind:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


@dataclass
class AccountResolution:
    pairs: pd.DataFrame          # every scored candidate pair with its band
    clusters: dict[int, int]     # row_id -> cluster root row_id (name matcher only)
    clusters_with_id: dict[int, int]  # row_id -> root, name matcher + same-id rule
    canonical: dict[int, dict]   # root -> canonical record (for clusters_with_id)


def resolve_accounts(records: list[dict], match: float = MATCH, grey: float = GREY, weights: dict | None = None) -> AccountResolution:
    """records: dicts with row_id, account_id, name, name_norm, industry, region, source_system."""
    weights = weights or WEIGHTS
    by_block: dict[str, list[dict]] = {}
    for r in records:
        by_block.setdefault(block_key(r), []).append(r)
    rows = []
    for key, members in by_block.items():
        for a, b in combinations(sorted(members, key=lambda r: r["row_id"]), 2):
            f = score_pair(a, b, weights)
            band = "match" if f["score"] >= match else "uncertain" if f["score"] >= grey else "no_match"
            rows.append({"row_id_a": a["row_id"], "row_id_b": b["row_id"], "account_id_a": a["account_id"], "account_id_b": b["account_id"],
                         "name_a": a["name"], "name_b": b["name"], "block": key, "band": band, **f})
    pairs = pd.DataFrame(rows, columns=["row_id_a", "row_id_b", "account_id_a", "account_id_b", "name_a", "name_b", "block", "band",
                                        "score", "jw", "tokens", "same_industry", "same_region", "same_id"])
    uf = UnionFind([r["row_id"] for r in records])
    for p in pairs[pairs["band"] == "match"].itertuples():
        uf.union(p.row_id_a, p.row_id_b)
    clusters = {r["row_id"]: uf.find(r["row_id"]) for r in records}
    uf2 = UnionFind([r["row_id"] for r in records])
    for p in pairs[pairs["band"] == "match"].itertuples():
        uf2.union(p.row_id_a, p.row_id_b)
    by_id: dict[str, list[int]] = {}
    for r in records:
        by_id.setdefault(r["account_id"], []).append(r["row_id"])
    for ids in by_id.values():
        for x in ids[1:]:
            uf2.union(ids[0], x)
    clusters_with_id = {r["row_id"]: uf2.find(r["row_id"]) for r in records}
    rec_by_id = {r["row_id"]: r for r in records}
    canonical: dict[int, dict] = {}
    for row_id, root in clusters_with_id.items():
        r = rec_by_id[row_id]
        cur = canonical.get(root)
        key = (*mess_score(r["name"], r["source_system"]), r["row_id"])
        if cur is None or key < cur["_key"]:
            canonical[root] = {**r, "_key": key}
    for c in canonical.values():
        c.pop("_key")
    return AccountResolution(pairs=pairs, clusters=clusters, clusters_with_id=clusters_with_id, canonical=canonical)


# ---------------------------------------------------------------- assets
def serial_candidates(erp_serial: str) -> list[tuple[str, str, float]]:
    """(candidate, method, score) in the order tried. Real telemetry serials can be lowercase
    (430 of them in 2026 Q2) or start with 'WD-' (86), so the raw serial is tried first and each
    transformation is a separate, lower-scored step rather than a blind normalization."""
    raw = str(erp_serial).strip()
    stripped = _PREFIX_RE.sub("", raw)
    out = [(raw, "exact", 1.0)]
    if stripped != raw:
        out.append((stripped, "prefix_stripped", 0.95))
    if raw.upper() != raw:
        out.append((raw.upper(), "uppercased", 0.95))
    if stripped != raw and stripped.upper() != stripped:
        out.append((stripped.upper(), "prefix_stripped_uppercased", 0.9))
    return out


def resolve_assets(assets: pd.DataFrame, drive_serials: pd.Series) -> pd.DataFrame:
    """One row per matched (asset_id, serial_number) with score and method; the first candidate
    that is a known telemetry serial wins. An asset whose winning serial is already claimed by an
    earlier asset is reported with band 'collision' and is not linked. assets has asset_id, erp_serial."""
    known = set(drive_serials.astype(str))
    claimed: dict[str, str] = {}
    rows = []
    for a in assets.itertuples():
        hit = next(((c, m, sc) for c, m, sc in serial_candidates(a.erp_serial) if c in known), None)
        if hit is None:
            continue
        serial, method, score = hit
        if serial in claimed:
            rows.append({"asset_id": a.asset_id, "erp_serial": a.erp_serial, "serial_number": serial, "method": method, "score": score, "band": "collision"})
            continue
        claimed[serial] = a.asset_id
        rows.append({"asset_id": a.asset_id, "erp_serial": a.erp_serial, "serial_number": serial, "method": method, "score": score, "band": "match"})
    return pd.DataFrame(rows, columns=["asset_id", "erp_serial", "serial_number", "method", "score", "band"])


# ---------------------------------------------------------------- measurement
def slice_of(canonical_id: str) -> str:
    return "dev" if int(hashlib.blake2b(canonical_id.encode(), digest_size=4).hexdigest(), 16) % 2 == 0 else "held_out"


def pairwise_metrics(pred: dict[int, int], truth: dict[int, str], members: set[int]) -> dict:
    """pred/truth: row_id -> cluster key. Pairs restricted to both rows in `members`."""
    ids = sorted(members)
    tp = fp = fn = 0
    for a, b in combinations(ids, 2):
        p = pred[a] == pred[b]
        t = truth[a] == truth[b]
        tp += p and t
        fp += p and not t
        fn += t and not p
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"records": len(ids), "true_pairs": tp + fn, "predicted_pairs": tp + fp, "tp": tp, "fp": fp, "fn": fn,
            "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def measure_accounts(res: AccountResolution, records: list[dict], truth: pd.DataFrame, site_ids: set[str], contact_ids: set[str]) -> dict:
    """truth rows are in account.csv order (checked on account_id and account_name)."""
    if len(truth) != len(records):
        raise ValueError("truth/account_resolution.csv must have one row per account row")
    truth_by_row: dict[int, str] = {}
    for r, t in zip(records, truth.itertuples()):
        if r["account_id"] != t.account_id or r["name"] != t.account_name:
            raise ValueError(f"truth row order does not match account.csv at row {r['row_id']}")
        truth_by_row[r["row_id"]] = t.canonical_account_id
    out: dict = {"thresholds": {"match": MATCH, "grey": GREY}, "weights": WEIGHTS,
                 "candidate_pairs": int(len(res.pairs)), "uncertain_pairs": int((res.pairs["band"] == "uncertain").sum())}
    dup_ids = set(truth.loc[truth["variant_type"] == "duplicate_id", "account_id"])
    out["duplicate_ids_cited_by_sites"] = len(dup_ids & site_ids)
    out["duplicate_ids_cited_by_contacts"] = len(dup_ids & contact_ids)
    for sl in ("dev", "held_out"):
        members = {rid for rid, cid in truth_by_row.items() if slice_of(cid) == sl}
        out[sl] = {"name_matcher_only": pairwise_metrics(res.clusters, truth_by_row, members),
                   "name_matcher_plus_same_id": pairwise_metrics(res.clusters_with_id, truth_by_row, members)}
        roots = {res.clusters_with_id[rid] for rid in members}
        correct = sum(1 for root in roots if res.canonical[root]["account_id"] == truth_by_row[root])
        out[sl]["clusters"] = len(roots)
        out[sl]["true_companies"] = len({truth_by_row[rid] for rid in members})
        out[sl]["canonical_id_accuracy"] = round(correct / len(roots), 4) if roots else None
    # how the uncertain band splits against truth (the reviewer's workload and what it buys)
    unc = res.pairs[res.pairs["band"] == "uncertain"]
    if len(unc):
        same = [truth_by_row[a] == truth_by_row[b] for a, b in zip(unc["row_id_a"], unc["row_id_b"])]
        out["uncertain_band_true_matches"] = int(sum(same))
    return out


def measure_assets(matches: pd.DataFrame, assets: pd.DataFrame, truth_map: pd.DataFrame) -> dict:
    t = truth_map[["asset_id", "telemetry_serial", "serial_mess"]]
    collisions = int((matches["band"] == "collision").sum()) if "band" in matches else 0
    matches = matches[matches["band"] == "match"] if "band" in matches else matches
    m = matches.merge(t, on="asset_id", how="outer", indicator=True)
    correct = (m["serial_number"] == m["telemetry_serial"]).sum()
    predicted = m["serial_number"].notna().sum()
    out = {"assets": int(len(assets)), "matched": int(predicted), "correct": int(correct),
           "precision": round(float(correct / predicted), 6) if predicted else None,
           "recall": round(float(correct / len(t)), 6), "unmatched_assets": int(len(t) - predicted), "collisions": collisions,
           "by_mess": {}}
    for mess, g in m.groupby("serial_mess"):
        out["by_mess"][str(mess)] = {"n": int(len(g)), "correct": int((g["serial_number"] == g["telemetry_serial"]).sum())}
    out["by_method"] = matches["method"].value_counts().to_dict()
    # held-out: assets are split by a hash of the asset id; nothing was tuned, so the halves should agree
    sl = m["asset_id"].map(lambda x: slice_of(str(x)))
    for name in ("dev", "held_out"):
        g = m[sl == name]
        out[name] = {"assets": int(len(g)), "correct": int((g["serial_number"] == g["telemetry_serial"]).sum()),
                     "recall": round(float((g["serial_number"] == g["telemetry_serial"]).sum() / len(g)), 6)}
    return out


# ---------------------------------------------------------------- write
WRITE_ACCOUNTS = """
UNWIND $rows AS r
MERGE (a:Account {account_id: r.account_id})
SET a.name = r.name, a.industry = r.industry, a.region = r.region, a.records = r.records, a.resolved_by = 'ekg.resolve'
WITH a, r
UNWIND r.members AS m
MATCH (rec:AccountRecord {row_id: m.row_id})
MERGE (rec)-[s:SAME_AS]->(a)
SET s.score = m.score, s.method = m.method, s.band = 'match'
"""
WRITE_HAS = """
MATCH (rec:AccountRecord)-[:SAME_AS]->(a:Account)
MATCH (s:Site {account_id: rec.account_id})
MERGE (a)-[:HAS]->(s)
"""
WRITE_ASSET_LINKS = """
UNWIND $rows AS r
MATCH (d:Drive {serial_number: r.serial_number}) MATCH (a:Asset {asset_id: r.asset_id})
MERGE (d)-[s:SAME_AS]->(a) SET s.score = r.score, s.method = r.method, s.band = 'match'
SET a.telemetry_serial = r.serial_number
"""


def write_accounts(session, res: AccountResolution, records: list[dict]) -> int:
    rec_by_id = {r["row_id"]: r for r in records}
    pair_score: dict[tuple[int, int], float] = {}
    for p in res.pairs.itertuples():
        pair_score[(p.row_id_a, p.row_id_b)] = p.score
    rows = []
    for root, canon in res.canonical.items():
        members = []
        for rid, r in res.clusters_with_id.items():
            if r != root:
                continue
            rec = rec_by_id[rid]
            if rid == canon["row_id"]:
                members.append({"row_id": rid, "score": 1.0, "method": "canonical"})
            elif rec["account_id"] == canon["account_id"]:
                members.append({"row_id": rid, "score": 1.0, "method": "same_id"})
            else:
                a, b = sorted((rid, canon["row_id"]))
                members.append({"row_id": rid, "score": pair_score.get((a, b), MATCH), "method": "name_match"})
        rows.append({"account_id": canon["account_id"], "name": canon["name"], "industry": canon.get("industry"),
                     "region": canon.get("region"), "records": len(members), "members": members})
    session.run("CREATE CONSTRAINT account_id IF NOT EXISTS FOR (n:Account) REQUIRE n.account_id IS UNIQUE").consume()
    for i in range(0, len(rows), 500):
        session.run(WRITE_ACCOUNTS, rows=rows[i:i + 500]).consume()
    session.run(WRITE_HAS).consume()
    return len(rows)


def write_assets(session, matches: pd.DataFrame) -> int:
    rows = matches[matches["band"] == "match"][["asset_id", "serial_number", "method", "score"]].to_dict(orient="records")
    for i in range(0, len(rows), 5000):
        session.run(WRITE_ASSET_LINKS, rows=rows[i:i + 5000]).consume()
    return len(rows)


# ---------------------------------------------------------------- cli
def records_from_csv(enterprise: Path) -> list[dict]:
    from ekg.load import account_record_rows
    acc = pd.read_csv(enterprise / "account.csv", dtype=str, keep_default_na=False, na_values=[""])
    acc["account_row_id"] = acc["account_row_id"].astype(int)
    return account_record_rows(acc)


def main(argv: list[str] | None = None) -> int:
    from ekg.config import neo4j_driver, settings
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write Account nodes and SAME_AS edges to Neo4j")
    ap.add_argument("--reports", type=Path, default=Path("reports"))
    a = ap.parse_args(argv)
    s = settings()
    a.reports.mkdir(parents=True, exist_ok=True)

    records = records_from_csv(s.enterprise_dir)
    res = resolve_accounts(records)
    truth = pd.read_csv(s.truth_dir / "account_resolution.csv", dtype=str)
    site_ids = set(pd.read_csv(s.enterprise_dir / "site.csv", dtype=str)["account_id"])
    contact_ids = set(pd.read_csv(s.enterprise_dir / "contact.csv", dtype=str)["account_id"])
    acc_metrics = measure_accounts(res, records, truth, site_ids, contact_ids)
    unc = res.pairs[res.pairs["band"] == "uncertain"].sort_values("score", ascending=False)
    unc.to_csv(a.reports / "uncertain_matches.csv", index=False)

    assets = pd.read_csv(s.enterprise_dir / "installed_asset.csv", dtype=str, usecols=["asset_id", "serial_number"]).rename(columns={"serial_number": "erp_serial"})
    drives = pd.read_parquet(s.parquet_dir / "drive_summary.parquet", columns=["serial_number"])["serial_number"]
    matches = resolve_assets(assets, drives)
    truth_map = pd.read_csv(s.truth_dir / "asset_serial_map.csv", dtype=str)
    asset_metrics = measure_assets(matches, assets, truth_map)

    # ablation: the string matcher alone, no credit for shared industry or region
    name_only = resolve_accounts(records, weights=NAME_ONLY_WEIGHTS)
    acc_metrics["ablation_name_only"] = {"weights": NAME_ONLY_WEIGHTS, **{
        sl: measure_accounts(name_only, records, truth, site_ids, contact_ids)[sl]["name_matcher_only"] for sl in ("dev", "held_out")},
        "uncertain_pairs": int((name_only.pairs["band"] == "uncertain").sum())}

    metrics = {"accounts": acc_metrics, "assets": asset_metrics,
               "note": ("Pairwise precision/recall against truth/account_resolution.csv and truth/asset_serial_map.csv. "
                        "The mess was generated by the rules in Project 1 docs/MESS_RULES.md (A1-A3, S1-S2); these numbers "
                        "prove the matcher inverts those rules on the held-out half, not that it would score the same on a real CRM.")}
    (a.reports / "resolution_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    ho = acc_metrics["held_out"]
    print(f"accounts held-out: name matcher P={ho['name_matcher_only']['precision']} R={ho['name_matcher_only']['recall']}; "
          f"+same-id P={ho['name_matcher_plus_same_id']['precision']} R={ho['name_matcher_plus_same_id']['recall']}; "
          f"uncertain pairs={acc_metrics['uncertain_pairs']}")
    print(f"assets: matched {asset_metrics['matched']:,}/{asset_metrics['assets']:,} precision={asset_metrics['precision']} recall={asset_metrics['recall']}")
    if a.write:
        with neo4j_driver() as drv, drv.session() as session:
            n_acc = write_accounts(session, res, records)
            n_ast = write_assets(session, matches)
        print(f"wrote {n_acc} Account nodes and {n_ast:,} Drive-SAME_AS->Asset edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
