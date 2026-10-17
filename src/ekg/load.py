"""Load the enterprise into Neo4j.

    uv run python -m ekg.load [--reset] [--limit-assets N] [--no-postgres]

Sources (all read-only, from the services-ops-intelligence checkout named by
SOIS_REPO): the enterprise CSVs under data/enterprise/, the per-drive summary
from Postgres (telemetry_drive_summary; falls back to data/parquet/
drive_summary.parquet with --no-postgres), the last SMART 5/187/197 raw values
per drive from the daily Parquet (DuckDB, one pass), and Project 1's calibrated
30-day failure probability per drive from models/test_scores.parquet (last
scored date). Daily telemetry rows stay out of the graph.

Nodes: AccountRecord, Site, Asset, Drive, DriveModel, Pod, Vault, Product, Part,
Supplier, Case, Dispatch, Technician, Runbook (+ Term from ekg.ontology).
Account nodes are created by ekg.resolve, not here: an Account is the output
of entity resolution, and the loader must not guess it.

Edges: Site-HOSTS->Asset, Asset-IS_A->Product, Case-ABOUT->Asset,
Case-OPENED_BY->AccountRecord, Dispatch-RESOLVES->Case,
Technician-PERFORMS->Dispatch, Dispatch-USED->Part, Part-SUPPLIED_BY->Supplier,
Product-USES->Part (derived from dispatches), Runbook-COVERS->Product,
Drive-OF_MODEL->DriveModel, Drive-IN_POD->Pod, Pod-IN_VAULT->Vault, and
every node -INSTANCE_OF-> its Term.

Derived here: Case.opened_at/closed_at parsed from both source formats (rule
D1; the raw strings are kept), Asset.serial_norm (vendor prefix stripped,
lowercased; rules S1/S2), Pod.expected_failures_30d and Pod.risky (the Risky
Pod term: top 5% of pods with >= 10 scored drives).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ekg import graph
from ekg.ontology import load_terms, parse as parse_ontology

VENDOR_PREFIXES: tuple[str, ...] = ("SG-", "HGST_", "TOS-", "WD-", "HIT-", "OEM-")
_PREFIX_RE = re.compile(r"^(SG-|HGST_|TOS-|WD-|HIT-|OEM-)", re.IGNORECASE)
RISKY_POD_QUANTILE = 0.95
RISKY_POD_MIN_DRIVES = 10

# label -> term_id (ontology/enterprise.ttl)
LABEL_TERMS: dict[str, str] = {
    "AccountRecord": "AccountRecord", "Account": "Account", "Site": "Site", "Asset": "InstalledAsset",
    "Drive": "Drive", "Product": "Product", "Part": "Part", "Supplier": "Supplier", "Case": "Case",
    "Dispatch": "Dispatch", "Technician": "Technician", "Runbook": "Runbook",
    "Table": "Table", "Column": "Column", "DataProduct": "DataProduct", "Job": "Job", "Query": "Query",
}


# ---------------------------------------------------------------- transforms (pure, tested)
def parse_case_ts(value) -> str | None:
    """ISO 8601 ('2026-04-02T12:11:06') or US 'MM/DD/YYYY HH:MM' (rule D1) -> ISO string, else None."""
    if value is None or (isinstance(value, float) and np.isnan(value)) or value == "":
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


def normalize_serial(serial: str) -> str:
    """Strip a vendor prefix (rule S1) and upper-case (rule S2). Telemetry serials are upper-case."""
    return _PREFIX_RE.sub("", str(serial).strip()).upper()


def normalize_name(name: str) -> str:
    """Account-name normalization shared with ekg.resolve: lowercase, collapse whitespace,
    drop punctuation, drop a leading 'the' and trailing legal forms."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(name).lower())
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    tokens = [t for t in s.split(" ") if t not in LEGAL_FORMS]
    return " ".join(tokens)


LEGAL_FORMS = {"inc", "llc", "ltd", "plc", "gmbh", "corp", "co", "sa", "ag", "holdings", "group", "limited", "corporation"}


def _nan_to_none(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and np.isnan(v):
            out[k] = None
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        elif isinstance(v, (np.floating,)):
            out[k] = float(v)
        elif isinstance(v, (np.bool_,)):
            out[k] = bool(v)
        elif isinstance(v, pd.Timestamp):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def records(df: pd.DataFrame) -> list[dict]:
    return [_nan_to_none(r) for r in df.to_dict(orient="records")]


def account_record_rows(acc: pd.DataFrame) -> list[dict]:
    df = acc.rename(columns={"account_row_id": "row_id", "account_name": "name"})
    df["name_norm"] = df["name"].map(normalize_name)
    return records(df[["row_id", "account_id", "name", "name_norm", "industry", "region", "source_system"]])


def asset_rows(ia: pd.DataFrame) -> list[dict]:
    df = ia.rename(columns={"serial_number": "erp_serial"}).copy()
    df["serial_norm"] = df["erp_serial"].map(normalize_serial)
    df["install_date"] = df["install_date"].astype(str)
    df["warranty_end"] = df["warranty_end"].where(df["warranty_end"].notna(), None)
    return records(df[["asset_id", "erp_serial", "serial_norm", "site_id", "product_id", "install_date", "warranty_end", "status", "order_id"]])


def case_rows(case: pd.DataFrame, truth: pd.DataFrame | None = None) -> list[dict]:
    df = case.copy()
    df["opened_at_raw"] = df["opened_at"]
    df["closed_at_raw"] = df["closed_at"]
    df["opened_at"] = df["opened_at_raw"].map(parse_case_ts)
    df["closed_at"] = df["closed_at_raw"].map(parse_case_ts)
    df["failure_reported"] = df["failure_reported"].astype(int)
    df["severity"] = df["severity"].astype(int)
    if truth is not None:
        t = truth[["case_id", "deflectable", "true_hardware_fault", "linked_to_telemetry_failure"]].rename(columns={
            "deflectable": "truth_deflectable", "true_hardware_fault": "truth_hardware_fault",
            "linked_to_telemetry_failure": "truth_linked_to_telemetry_failure"})
        df = df.merge(t, on="case_id", how="left")
        for c in ("truth_deflectable", "truth_hardware_fault", "truth_linked_to_telemetry_failure"):
            df[c] = df[c].map(lambda v: None if pd.isna(v) else bool(v))
    return records(df)


def pod_rows(drives: pd.DataFrame) -> list[dict]:
    """One row per (datacenter, vault_id, pod_id) with expected 30-day failures and the Risky Pod flag."""
    d = drives.copy()
    d["pod_key"] = d["datacenter"] + "/" + d["vault_id"].astype(str) + "/" + d["pod_id"].astype(str)
    g = d.groupby(["pod_key", "datacenter", "vault_id", "pod_id"], as_index=False).agg(
        drives=("serial_number", "size"), scored_drives=("p_fail_30d", "count"),
        expected_failures_30d=("p_fail_30d", "sum"), failed_drives=("failed", "sum"))
    g["vault_key"] = g["datacenter"] + "/" + g["vault_id"].astype(str)
    eligible = g["scored_drives"] >= RISKY_POD_MIN_DRIVES
    threshold = float(g.loc[eligible, "expected_failures_30d"].quantile(RISKY_POD_QUANTILE)) if eligible.any() else float("inf")
    g["risky"] = eligible & (g["expected_failures_30d"] >= threshold)
    g["risky_threshold"] = threshold
    g["expected_failures_30d"] = g["expected_failures_30d"].round(4)
    return records(g)


def product_uses_part(case: pd.DataFrame, dispatch: pd.DataFrame, dispatch_part: pd.DataFrame, assets: pd.DataFrame) -> list[dict]:
    """Product-USES->Part derived from dispatches: parts consumed on cases about assets of the product."""
    j = (dispatch_part.merge(dispatch[["dispatch_id", "case_id"]], on="dispatch_id")
         .merge(case[["case_id", "asset_id"]], on="case_id")
         .merge(assets[["asset_id", "product_id"]], on="asset_id"))
    g = j.groupby(["product_id", "part_id"], as_index=False).agg(dispatch_count=("dispatch_id", "nunique"), quantity=("quantity", "sum"))
    return [{"src": r.product_id, "dst": r.part_id, "dispatch_count": int(r.dispatch_count), "quantity": int(r.quantity)} for r in g.itertuples()]


# ---------------------------------------------------------------- sources
def read_enterprise(enterprise: Path, limit_assets: int | None = None) -> dict[str, pd.DataFrame]:
    names = ["account", "site", "installed_asset", "product", "part", "supplier", "part_supplier", "case",
             "dispatch", "dispatch_part", "technician", "runbook"]
    d = {n: pd.read_csv(enterprise / f"{n}.csv", dtype=str, keep_default_na=False, na_values=[""]) for n in names}
    for col in ("severity", "failure_reported"):
        d["case"][col] = d["case"][col].astype(int)
    d["dispatch"]["visit_number"] = d["dispatch"]["visit_number"].astype(int)
    d["dispatch_part"]["quantity"] = d["dispatch_part"]["quantity"].astype(int)
    d["part_supplier"]["lead_time_days"] = d["part_supplier"]["lead_time_days"].astype(int)
    d["part_supplier"]["unit_cost_usd"] = d["part_supplier"]["unit_cost_usd"].astype(float)
    d["part_supplier"]["preferred"] = d["part_supplier"]["preferred"].str.lower().eq("true")
    for col in ("drive_bays", "list_price_usd"):
        d["product"][col] = pd.to_numeric(d["product"][col])
    d["part"]["unit_cost_usd"] = pd.to_numeric(d["part"]["unit_cost_usd"])
    if limit_assets:
        d["installed_asset"] = d["installed_asset"].head(limit_assets)
        keep = set(d["installed_asset"]["asset_id"])
        d["case"] = d["case"][d["case"]["asset_id"].isin(keep)]
        d["dispatch"] = d["dispatch"][d["dispatch"]["case_id"].isin(set(d["case"]["case_id"]))]
        d["dispatch_part"] = d["dispatch_part"][d["dispatch_part"]["dispatch_id"].isin(set(d["dispatch"]["dispatch_id"]))]
    return d


def read_drive_summary(settings, use_postgres: bool, log=print) -> pd.DataFrame:
    cols = ["serial_number", "model", "capacity_bytes", "datacenter", "cluster_id", "vault_id", "pod_id",
            "first_seen", "last_seen", "days_observed", "failed", "failure_date", "max_power_on_hours"]
    if use_postgres:
        from ekg.config import pg_connect
        with pg_connect() as con:
            df = pd.read_sql(f"SELECT {', '.join(cols)} FROM telemetry_drive_summary", con)
        log(f"drive summary from Postgres telemetry_drive_summary: {len(df):,} rows")
    else:
        df = pd.read_parquet(settings.parquet_dir / "drive_summary.parquet", columns=cols)
        log(f"drive summary from Parquet: {len(df):,} rows")
    for c in ("first_seen", "last_seen", "failure_date"):
        df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d").where(df[c].notna(), None)
    df["failed"] = df["failed"].astype(int)
    return df


def read_last_smart(parquet_dir: Path, summary: pd.DataFrame, log=print) -> pd.DataFrame:
    """Last SMART 5/187/197 raw per drive, read from the daily Parquet at each drive's last_seen day."""
    import duckdb
    con = duckdb.connect()
    con.register("s", summary[["serial_number", "last_seen"]])
    g = str(parquet_dir / "telemetry_daily" / "*" / "*.parquet").replace("'", "''")
    t0 = time.time()
    df = con.execute(f"""
        SELECT t.serial_number, t.smart_5_raw AS smart_5_raw_last, t.smart_187_raw AS smart_187_raw_last,
               t.smart_197_raw AS smart_197_raw_last
        FROM read_parquet('{g}', hive_partitioning=true) t
        JOIN s ON t.serial_number = s.serial_number AND CAST(t.date AS DATE) = CAST(s.last_seen AS DATE)
    """).df()
    con.close()
    log(f"last SMART values from Parquet: {len(df):,} drives in {time.time() - t0:.0f}s")
    return df


def read_risk_scores(models_dir: Path, reports_dir: Path, log=print) -> tuple[pd.DataFrame, str]:
    """Project 1 p_fail_30d per drive at the last scored date (models/test_scores.parquet), or the
    top-100 CSV when the full scores are absent (they are gitignored in Project 1)."""
    full = models_dir / "test_scores.parquet"
    if full.exists():
        ts = pd.read_parquet(full, columns=["t", "serial_number", "lightgbm"])
        last = ts["t"].max()
        df = ts[ts["t"] == last][["serial_number", "lightgbm"]].rename(columns={"lightgbm": "p_fail_30d"})
        log(f"risk scores from models/test_scores.parquet at {last.date()}: {len(df):,} drives")
        return df, str(last.date())
    top = reports_dir / "fleet_risk_top100.csv"
    df = pd.read_csv(top)[["serial_number", "p_fail_30d"]]
    log(f"risk scores from reports/fleet_risk_top100.csv: {len(df)} drives (full scores not present)")
    return df, "fleet_risk_top100"


# ---------------------------------------------------------------- load
def load(session, d: dict[str, pd.DataFrame], drives: pd.DataFrame, score_date: str, log=print) -> dict[str, int]:
    graph.ensure_schema(session)
    terms, rels, problems = parse_ontology()
    if problems:
        raise SystemExit("ontology problems: " + "; ".join(problems))
    counts: dict[str, int] = {}

    log("nodes")
    counts["AccountRecord"] = graph.write_nodes(session, "AccountRecord", "row_id", account_record_rows(d["account"]), log)
    site = d["site"].rename(columns={"site_name": "name"})
    counts["Site"] = graph.write_nodes(session, "Site", "site_id", records(site), log)
    counts["Product"] = graph.write_nodes(session, "Product", "product_id", records(d["product"]), log)
    counts["Part"] = graph.write_nodes(session, "Part", "part_id", records(d["part"]), log)
    counts["Supplier"] = graph.write_nodes(session, "Supplier", "supplier_id", records(d["supplier"]), log)
    tech = d["technician"].copy()
    tech["skills"] = tech["skills"].fillna("").map(lambda s: [x for x in s.split(";") if x])
    counts["Technician"] = graph.write_nodes(session, "Technician", "technician_id", records(tech), log)
    counts["Runbook"] = graph.write_nodes(session, "Runbook", "runbook_id", records(d["runbook"]), log)
    counts["Asset"] = graph.write_nodes(session, "Asset", "asset_id", asset_rows(d["installed_asset"]), log)
    counts["Case"] = graph.write_nodes(session, "Case", "case_id", case_rows(d["case"], d.get("case_truth")), log)
    disp = d["dispatch"].drop(columns=["parts_used"])
    counts["Dispatch"] = graph.write_nodes(session, "Dispatch", "dispatch_id", records(disp), log)

    dm = drives.groupby("model", as_index=False).agg(capacity_bytes=("capacity_bytes", "max"), drive_count=("serial_number", "size"), failed_count=("failed", "sum"))
    if "vendor" in d.get("drive_model", pd.DataFrame()).columns:
        dm = dm.merge(d["drive_model"][["model", "vendor"]], on="model", how="left")
    counts["DriveModel"] = graph.write_nodes(session, "DriveModel", "model", records(dm), log)
    pods = pod_rows(drives)
    vaults = pd.DataFrame(pods).groupby(["vault_key", "datacenter", "vault_id"], as_index=False).agg(
        pods=("pod_key", "size"), drives=("drives", "sum"), expected_failures_30d=("expected_failures_30d", "sum"), failed_drives=("failed_drives", "sum"))
    vaults["expected_failures_30d"] = vaults["expected_failures_30d"].round(4)
    counts["Vault"] = graph.write_nodes(session, "Vault", "vault_key", records(vaults), log)
    counts["Pod"] = graph.write_nodes(session, "Pod", "pod_key", pods, log)
    dr = drives.copy()
    dr["pod_key"] = dr["datacenter"] + "/" + dr["vault_id"].astype(str) + "/" + dr["pod_id"].astype(str)
    dr["score_date"] = score_date
    counts["Drive"] = graph.write_nodes(session, "Drive", "serial_number", records(dr), log)

    log("edges")
    ia = d["installed_asset"]
    counts["HOSTS"] = graph.write_edges(session, "Site", "site_id", "HOSTS", "Asset", "asset_id",
                                        [{"src": s, "dst": a} for s, a in zip(ia["site_id"], ia["asset_id"])], log=log)
    counts["IS_A"] = graph.write_edges(session, "Asset", "asset_id", "IS_A", "Product", "product_id",
                                       [{"src": a, "dst": p} for a, p in zip(ia["asset_id"], ia["product_id"])], log=log)
    cs = d["case"]
    counts["ABOUT"] = graph.write_edges(session, "Case", "case_id", "ABOUT", "Asset", "asset_id",
                                        [{"src": c, "dst": a} for c, a in zip(cs["case_id"], cs["asset_id"])], log=log)
    counts["OPENED_BY"] = graph.write_edges(session, "Case", "case_id", "OPENED_BY", "AccountRecord", "account_id",
                                            [{"src": c, "dst": a} for c, a in zip(cs["case_id"], cs["account_id"])], log=log)
    dp = d["dispatch"]
    counts["RESOLVES"] = graph.write_edges(session, "Dispatch", "dispatch_id", "RESOLVES", "Case", "case_id",
                                           [{"src": x, "dst": c} for x, c in zip(dp["dispatch_id"], dp["case_id"])], log=log)
    counts["PERFORMS"] = graph.write_edges(session, "Technician", "technician_id", "PERFORMS", "Dispatch", "dispatch_id",
                                           [{"src": t, "dst": x} for t, x in zip(dp["technician_id"], dp["dispatch_id"])], log=log)
    dpp = d["dispatch_part"]
    counts["USED"] = graph.write_edges(session, "Dispatch", "dispatch_id", "USED", "Part", "part_id",
                                       [{"src": x, "dst": p, "quantity": int(q)} for x, p, q in zip(dpp["dispatch_id"], dpp["part_id"], dpp["quantity"])],
                                       props=("quantity",), log=log)
    ps = d["part_supplier"]
    counts["SUPPLIED_BY"] = graph.write_edges(session, "Part", "part_id", "SUPPLIED_BY", "Supplier", "supplier_id",
                                              [{"src": r.part_id, "dst": r.supplier_id, "lead_time_days": int(r.lead_time_days),
                                                "unit_cost_usd": float(r.unit_cost_usd), "preferred": bool(r.preferred)} for r in ps.itertuples()],
                                              props=("lead_time_days", "unit_cost_usd", "preferred"), log=log)
    counts["USES"] = graph.write_edges(session, "Product", "product_id", "USES", "Part", "part_id",
                                       product_uses_part(cs, dp, dpp, ia), props=("dispatch_count", "quantity"), log=log)
    rb, pr = d["runbook"], d["product"]
    covers = [{"src": r.runbook_id, "dst": p.product_id} for r in rb.itertuples() for p in pr.itertuples() if r.product_family == p.family]
    counts["COVERS"] = graph.write_edges(session, "Runbook", "runbook_id", "COVERS", "Product", "product_id", covers, log=log)
    counts["OF_MODEL"] = graph.write_edges(session, "Drive", "serial_number", "OF_MODEL", "DriveModel", "model",
                                           [{"src": s, "dst": m} for s, m in zip(dr["serial_number"], dr["model"])], log=log)
    counts["IN_POD"] = graph.write_edges(session, "Drive", "serial_number", "IN_POD", "Pod", "pod_key",
                                         [{"src": s, "dst": p} for s, p in zip(dr["serial_number"], dr["pod_key"])], log=log)
    counts["IN_VAULT"] = graph.write_edges(session, "Pod", "pod_key", "IN_VAULT", "Vault", "vault_key",
                                           [{"src": p["pod_key"], "dst": p["vault_key"]} for p in pods], log=log)
    log("INSTANCE_OF")
    for label, term_id in LABEL_TERMS.items():
        if label in counts:
            counts[f"INSTANCE_OF:{label}"] = graph.link_instances(session, label, term_id)
    return counts


def create_reader_user(driver, user: str, password: str, log=print) -> str:
    """Neo4j Community has users but no roles (GRANT is Enterprise-only). The reader
    account exists so the tools never hold admin credentials; read-only enforcement
    is the driver's read transaction mode (the server rejects writes in it) plus
    ekg.nl2cypher's write guard. Returns a one-line status for the README."""
    with driver.session(database="system") as s:
        try:
            s.run(f"CREATE USER {user} IF NOT EXISTS SET PASSWORD $pw SET PASSWORD CHANGE NOT REQUIRED", pw=password).consume()
        except Exception as e:  # noqa: BLE001
            msg = f"reader user not created ({type(e).__name__}); tools fall back to the admin account in read mode"
            log(msg)
            return msg
        try:
            s.run(f"GRANT ROLE reader TO {user}").consume()
            msg = "reader user created with the built-in reader role (Enterprise RBAC)"
        except Exception as e:  # noqa: BLE001
            msg = f"reader user created; role grant unsupported on this edition ({type(e).__name__}): read-only is enforced by read transactions and the write guard"
        log(msg)
        return msg


def main(argv: list[str] | None = None) -> int:
    from ekg.config import neo4j_driver, settings
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="delete every node first")
    ap.add_argument("--limit-assets", type=int, default=None, help="load only the first N assets (smoke runs)")
    ap.add_argument("--no-postgres", action="store_true", help="read the drive summary from Parquet instead of Postgres")
    ap.add_argument("--no-smart", action="store_true", help="skip the Parquet pass for last SMART values")
    ap.add_argument("--report", type=Path, default=Path("reports/load_counts.json"))
    a = ap.parse_args(argv)
    s = settings()
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)  # noqa: E731

    d = read_enterprise(s.enterprise_dir, a.limit_assets)
    d["case_truth"] = pd.read_csv(s.truth_dir / "case_truth.csv", usecols=["case_id", "deflectable", "true_hardware_fault", "linked_to_telemetry_failure"])
    d["drive_model"] = pd.read_parquet(s.parquet_dir / "drive_model.parquet")
    drives = read_drive_summary(s, use_postgres=not a.no_postgres, log=log)
    if a.limit_assets:
        keep = set(d["installed_asset"]["serial_number"].map(normalize_serial))
        drives = drives[drives["serial_number"].isin(keep)]
    scores, score_date = read_risk_scores(s.sois_models_dir, s.sois_reports_dir, log)
    drives = drives.merge(scores, on="serial_number", how="left")
    if not a.no_smart:
        drives = drives.merge(read_last_smart(s.parquet_dir, drives, log), on="serial_number", how="left")
    log(f"drives to load: {len(drives):,} ({drives['p_fail_30d'].notna().sum():,} scored, {int(drives['failed'].sum()):,} failed)")

    with neo4j_driver() as drv:
        with drv.session() as session:
            if a.reset:
                log("wiping the graph")
                graph.wipe(session)
            terms, rels, _ = parse_ontology()
            load_terms(drv, terms, rels)
            counts = load(session, d, drives, score_date, log)
            counts["labels"] = graph.count_by_label(session)
        counts["reader_user"] = create_reader_user(drv, s.neo4j_reader_user, s.neo4j_reader_password, log)
    counts["score_date"] = score_date
    counts["seconds"] = round(time.time() - t0, 1)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(counts, indent=2) + "\n")
    log(f"done; counts in {a.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
