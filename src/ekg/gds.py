"""Graph data science over the loaded graph (Neo4j GDS 2.x).

    uv run python -m ekg.gds communities    # Louvain + WCC on the co-failure graph  -> reports/gds_communities.csv, .json
    uv run python -m ekg.gds centrality     # degree and betweenness for parts      -> reports/gds_part_centrality.csv
    uv run python -m ekg.gds embed          # FastRP for drives/assets               -> reports/asset_embeddings.parquet
    uv run python -m ekg.gds lift           # Project 1 failure model with vs without the embedding -> reports/gds_lift.json
    uv run python -m ekg.gds all

Communities. "Assets that fail together": two failed drives are linked when
they sit at the same Site and failed within 14 days of each other, weight
1 / (1 + days apart). Louvain (weighted, undirected) and WCC run on that graph;
Drive.community is written back and each community is described by its site,
vault, drive model and the preferred supplier of the drive sled its product
uses. Because the generator places one Backblaze vault per site, a community
is a vault-and-fortnight failure cluster in real telemetry; the site and
supplier labels on it are generated.

Centrality. Product-USES->Part (weight: dispatches that consumed it) and
Part-SUPPLIED_BY->Supplier, undirected. Degree and betweenness rank the parts
that many products depend on and that bridge products to few suppliers. It is
a 48-node graph (10 products, 30 parts, 8 suppliers); the ranking is honest
but small.

Embeddings. FastRP (32 dims) on the structural graph only: Drive-SAME_AS-Asset,
Site-HOSTS-Asset, Asset-IS_A-Product, Account-HAS-Site, Drive-IN_POD-Pod,
Pod-IN_VAULT-Vault, Drive-OF_MODEL-DriveModel. No Case or Dispatch nodes (their
timing is driven by the failure dates, which would leak the label) and no
failure property. The embedding therefore encodes where a drive sits and what
it is, nothing about when it failed.

Lift. Re-runs Project 1's failure model (its own feature builder, split,
downsampling, calibration and LightGBM parameters, imported from the sibling
checkout, never modified) three ways on the same leak-free split: baseline
features; baseline + vault_id as a categorical; baseline + the 32 embedding
dims. Three seeds each. The deltas in reports/gds_lift.json are the measured
lift, and the vault-id arm says whether a plain id would have done the same.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

COFAIL_GRAPH = "ekg_cofail"
PARTS_GRAPH = "ekg_parts"
STRUCT_GRAPH = "ekg_structure"
EMBED_DIM = 32
COFAIL_WINDOW_DAYS = 14


def _drop(session, name: str) -> None:
    session.run("CALL gds.graph.drop($name, false) YIELD graphName", name=name).consume()


# ---------------------------------------------------------------- communities
PROJECT_COFAIL = f"""
MATCH (a:Drive {{failed: 1}})-[:SAME_AS]->(:Asset)<-[:HOSTS]-(s:Site)-[:HOSTS]->(:Asset)<-[:SAME_AS]-(b:Drive {{failed: 1}})
WHERE a.serial_number < b.serial_number AND a.failure_date IS NOT NULL AND b.failure_date IS NOT NULL
WITH a, b, abs(duration.inDays(date(a.failure_date), date(b.failure_date)).days) AS gap
WHERE gap <= {COFAIL_WINDOW_DAYS}
WITH a, b, 1.0 / (1 + gap) AS w
RETURN gds.graph.project('{COFAIL_GRAPH}', a, b, {{relationshipProperties: {{w: w}}}}, {{undirectedRelationshipTypes: ['*']}}) AS g
"""
DESCRIBE_COMMUNITIES = """
MATCH (d:Drive)-[:SAME_AS]->(a:Asset)<-[:HOSTS]-(s:Site)
WHERE d.community IS NOT NULL
OPTIONAL MATCH (a)-[:IS_A]->(p:Product)
OPTIONAL MATCH (p)-[:USES]->(pt:Part {category: 'drive'})-[sb:SUPPLIED_BY {preferred: true}]->(sup:Supplier)
WITH d.community AS community, d, s, p, sup
WITH community, count(DISTINCT d) AS drives, collect(DISTINCT s.site_id) AS sites, collect(DISTINCT d.vault_id) AS vaults,
     collect(DISTINCT d.model) AS models, collect(DISTINCT p.product_id) AS products, collect(DISTINCT sup.name) AS suppliers,
     min(d.failure_date) AS first_failure, max(d.failure_date) AS last_failure
RETURN community, drives, sites, vaults, models, products, suppliers, first_failure, last_failure
ORDER BY drives DESC, community
"""


def communities(session, reports: Path, log=print) -> dict:
    _drop(session, COFAIL_GRAPH)
    g = session.run(PROJECT_COFAIL).single()["g"]
    log(f"co-failure graph: {g['nodeCount']} failed drives, {g['relationshipCount']} links (same site, <= {COFAIL_WINDOW_DAYS} days apart)")
    out = {"window_days": COFAIL_WINDOW_DAYS, "nodes": g["nodeCount"], "relationships": g["relationshipCount"]}
    if g["nodeCount"] == 0:
        return out
    session.run("MATCH (d:Drive) WHERE d.community IS NOT NULL REMOVE d.community, d.wcc").consume()
    lv = session.run(f"CALL gds.louvain.write('{COFAIL_GRAPH}', {{writeProperty: 'community', relationshipWeightProperty: 'w'}}) "
                     "YIELD communityCount, modularity, modularities").single()
    wcc = session.run(f"CALL gds.wcc.write('{COFAIL_GRAPH}', {{writeProperty: 'wcc'}}) YIELD componentCount, componentDistribution").single()
    out.update({"louvain_communities": lv["communityCount"], "modularity": round(lv["modularity"], 4),
                "wcc_components": wcc["componentCount"], "wcc_max_size": wcc["componentDistribution"]["max"]})
    rows = session.run(DESCRIBE_COMMUNITIES).data()
    df = pd.DataFrame(rows)
    for c in ("sites", "vaults", "models", "products", "suppliers"):
        df[c] = df[c].map(lambda v: ";".join(sorted(x for x in v if x)))
    df.to_csv(reports / "gds_communities.csv", index=False)
    total_failed = session.run("MATCH (d:Drive {failed: 1}) RETURN count(d) AS n").single()["n"]
    out["failed_drives_total"] = total_failed
    out["failed_drives_in_a_community_of_2_or_more"] = int(df.loc[df["drives"] >= 2, "drives"].sum())
    out["communities_of_2_or_more"] = int((df["drives"] >= 2).sum())
    out["largest"] = df.head(5).to_dict(orient="records")
    out["communities_spanning_one_vault"] = int((df["vaults"].str.count(";") == 0).sum())
    out["communities_with_one_drive_model"] = int((df["models"].str.count(";") == 0).sum())
    log(f"louvain: {out['louvain_communities']} communities, modularity {out['modularity']}; "
        f"{out['failed_drives_in_a_community_of_2_or_more']}/{total_failed} failed drives share a community with another")
    _drop(session, COFAIL_GRAPH)
    return out


# ---------------------------------------------------------------- centrality
PROJECT_PARTS = f"""
MATCH (src)-[r:USES|SUPPLIED_BY]->(dst)
WHERE (src:Product AND dst:Part) OR (src:Part AND dst:Supplier)
WITH src, dst, CASE WHEN r.dispatch_count IS NULL THEN 1.0 ELSE toFloat(r.dispatch_count) END AS w
RETURN gds.graph.project('{PARTS_GRAPH}', src, dst, {{sourceNodeLabels: labels(src), targetNodeLabels: labels(dst), relationshipProperties: {{w: w}}}},
                         {{undirectedRelationshipTypes: ['*']}}) AS g
"""


def centrality(session, reports: Path, log=print) -> dict:
    _drop(session, PARTS_GRAPH)
    g = session.run(PROJECT_PARTS).single()["g"]
    log(f"parts graph: {g['nodeCount']} nodes, {g['relationshipCount']} relationships")
    session.run(f"CALL gds.degree.write('{PARTS_GRAPH}', {{writeProperty: 'degree'}}) YIELD centralityDistribution").consume()
    session.run(f"CALL gds.degree.write('{PARTS_GRAPH}', {{writeProperty: 'weighted_degree', relationshipWeightProperty: 'w'}}) YIELD centralityDistribution").consume()
    session.run(f"CALL gds.betweenness.write('{PARTS_GRAPH}', {{writeProperty: 'betweenness'}}) YIELD centralityDistribution").consume()
    rows = session.run("""
        MATCH (p:Part)
        OPTIONAL MATCH (p)-[:SUPPLIED_BY]->(s:Supplier)
        OPTIONAL MATCH (pr:Product)-[u:USES]->(p)
        RETURN p.part_id AS part_id, p.part_number AS part_number, p.category AS category,
               p.degree AS degree, p.weighted_degree AS weighted_degree, round(p.betweenness, 2) AS betweenness,
               count(DISTINCT s) AS suppliers, count(DISTINCT pr) AS products, coalesce(sum(u.dispatch_count), 0) AS dispatches
        ORDER BY betweenness DESC, weighted_degree DESC""").data()
    df = pd.DataFrame(rows)
    df.to_csv(reports / "gds_part_centrality.csv", index=False)
    _drop(session, PARTS_GRAPH)
    top = df.head(5)[["part_number", "category", "degree", "weighted_degree", "betweenness", "suppliers", "products", "dispatches"]].to_dict(orient="records")
    log("top parts by betweenness: " + ", ".join(f"{r['part_number']} ({r['betweenness']})" for r in top))
    return {"nodes": g["nodeCount"], "relationships": g["relationshipCount"], "parts": int(len(df)),
            "single_supplier_parts": int((df["suppliers"] == 1).sum()), "top_by_betweenness": top}


# ---------------------------------------------------------------- embeddings
PROJECT_STRUCT = f"""
CALL gds.graph.project('{STRUCT_GRAPH}',
  ['Drive', 'Asset', 'Site', 'Product', 'Account', 'Pod', 'Vault', 'DriveModel'],
  {{SAME_AS: {{orientation: 'UNDIRECTED'}}, HOSTS: {{orientation: 'UNDIRECTED'}}, IS_A: {{orientation: 'UNDIRECTED'}},
   HAS: {{orientation: 'UNDIRECTED'}}, IN_POD: {{orientation: 'UNDIRECTED'}}, IN_VAULT: {{orientation: 'UNDIRECTED'}},
   OF_MODEL: {{orientation: 'UNDIRECTED'}}}})
YIELD nodeCount, relationshipCount
"""


def embed(session, reports: Path, dim: int = EMBED_DIM, seed: int = 20261017, log=print) -> dict:
    _drop(session, STRUCT_GRAPH)
    g = session.run(PROJECT_STRUCT).single()
    log(f"structure graph: {g['nodeCount']:,} nodes, {g['relationshipCount']:,} relationships (no Case/Dispatch, no failure property)")
    t0 = time.time()
    rows = session.run(f"""
        CALL gds.fastRP.stream('{STRUCT_GRAPH}', {{embeddingDimension: $dim, randomSeed: $seed, iterationWeights: [0.0, 1.0, 1.0, 1.0],
                                                   nodeLabels: ['Drive', 'Asset', 'Site', 'Product', 'Account', 'Pod', 'Vault', 'DriveModel']}})
        YIELD nodeId, embedding
        WITH gds.util.asNode(nodeId) AS n, embedding
        WHERE n:Drive
        RETURN n.serial_number AS serial_number, embedding""", dim=dim, seed=seed).data()
    emb = np.array([r["embedding"] for r in rows], dtype=np.float32)
    df = pd.DataFrame(emb, columns=[f"e{i}" for i in range(dim)])
    df.insert(0, "serial_number", [r["serial_number"] for r in rows])
    df.to_parquet(reports / "asset_embeddings.parquet", index=False)
    _drop(session, STRUCT_GRAPH)
    log(f"fastrp: {len(df):,} drive embeddings x {dim} dims in {time.time() - t0:.0f}s -> reports/asset_embeddings.parquet")
    return {"nodes": g["nodeCount"], "relationships": g["relationshipCount"], "drives_embedded": int(len(df)), "dim": dim, "seed": seed,
            "iteration_weights": [0.0, 1.0, 1.0, 1.0]}


# ---------------------------------------------------------------- lift
def _import_sois(sois_repo: Path):
    src = sois_repo / "src"
    if not (src / "sois" / "failure_model.py").exists():
        raise SystemExit(f"Project 1 source not found under {src}; set SOIS_REPO")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import sois.failure_model as fm  # noqa: PLC0415
    return fm


def _fit_predict(fm, fit_df: pd.DataFrame, cal_df: pd.DataFrame, test_df: pd.DataFrame, numeric: list[str],
                 categorical: list[str], categories: dict[str, list[str]], seed: int, params: dict) -> dict:
    import lightgbm as lgb

    def matrix(df):
        X = df[numeric + categorical].copy()
        for c in categorical:
            X[c] = pd.Categorical(X[c], categories=categories[c])
        return X

    p = {**params, "seed": seed}
    n_rounds = int(p.pop("num_boost_round"))
    booster = lgb.train(p, lgb.Dataset(matrix(fit_df), label=fit_df["y"].to_numpy(), categorical_feature=categorical), num_boost_round=n_rounds)
    cal_raw = booster.predict(matrix(cal_df))
    ab = fm.fit_platt(cal_raw, cal_df["y"].to_numpy())
    test_raw = booster.predict(matrix(test_df))
    s = fm.apply_platt(test_raw, ab)
    y = test_df["y"].to_numpy()
    return {"pr_auc": round(fm.pr_auc(y, s), 4), "recall_top_0.001": round(fm.recall_at_fraction(y, s, 0.001), 4),
            "recall_top_0.01": round(fm.recall_at_fraction(y, s, 0.01), 4), "brier_calibrated": round(fm.brier(y, s), 6)}


def lift(reports: Path, sois_repo: Path, parquet_dir: Path, seeds: tuple[int, ...] = (20261017, 1, 2), log=print) -> dict:
    from datetime import date, timedelta
    import duckdb

    fm = _import_sois(sois_repo)
    metrics_p1 = json.loads((sois_repo / "reports" / "failure_model_metrics.json").read_text())
    cfg = metrics_p1["config"]
    train_start, train_end = (date.fromisoformat(x) for x in cfg["train_prediction_dates"])
    test_start, test_end = (date.fromisoformat(x) for x in cfg["test_prediction_dates"])
    horizon, neg_rate, seed0 = cfg["horizon_days"], cfg["negative_downsample_rate_fit"], cfg["seed"]
    cal_dates = {date.fromisoformat(x) for x in cfg["calibration_prediction_dates"]}
    params = dict(cfg["lightgbm_params"])
    log(f"split from Project 1: train {train_start}..{train_end}, test {test_start}..{test_end}, horizon {horizon}, neg_rate {neg_rate}")
    if test_start <= train_end + timedelta(days=horizon):
        raise SystemExit("leaking split")

    emb = pd.read_parquet(reports / "asset_embeddings.parquet")
    emb_cols = [c for c in emb.columns if c.startswith("e")]
    con = duckdb.connect()
    con.execute("SET threads = 8")
    summary = con.execute(f"SELECT serial_number, failure_date, last_seen FROM read_parquet('{parquet_dir / 'drive_summary.parquet'}')").df()
    model_categories = sorted(con.execute(f"SELECT DISTINCT model FROM read_parquet('{parquet_dir / 'drive_model.parquet'}')").df()["model"].dropna().tolist())
    rng = np.random.default_rng(seed0)
    fit_parts, cal_parts, test_parts = [], [], []
    t0 = time.time()
    for t in fm.daterange(train_start, train_end):
        df = fm.add_labels(fm.build_features(con, parquet_dir, t), summary, horizon)
        bucket = fm.holdout_bucket(df["serial_number"])
        fit = df[bucket != 0]
        keep = (fit["y"].to_numpy() == 1) | (rng.random(len(fit)) < neg_rate)
        fit_parts.append(fit[keep])
        if t in cal_dates:
            cal_parts.append(df[bucket == 0])
    for t in fm.daterange(test_start, test_end):
        test_parts.append(fm.add_labels(fm.build_features(con, parquet_dir, t), summary, horizon))
    con.close()
    fit_df, cal_df, test_df = (pd.concat(p, ignore_index=True) for p in (fit_parts, cal_parts, test_parts))
    del fit_parts, cal_parts, test_parts
    log(f"features built in {time.time() - t0:.0f}s: fit {len(fit_df):,} (pos {int(fit_df['y'].sum()):,}), cal {len(cal_df):,}, test {len(test_df):,} (pos {int(test_df['y'].sum()):,})")

    for name in ("fit", "cal", "test"):
        df = {"fit": fit_df, "cal": cal_df, "test": test_df}[name]
        merged = df.merge(emb, on="serial_number", how="left")
        if name == "fit":
            fit_df = merged
        elif name == "cal":
            cal_df = merged
        else:
            test_df = merged
    coverage = float(test_df[emb_cols[0]].notna().mean())
    vault_categories = sorted(pd.concat([fit_df["vault_id"], cal_df["vault_id"], test_df["vault_id"]]).dropna().astype(str).unique().tolist())
    for df in (fit_df, cal_df, test_df):
        df["vault_id"] = df["vault_id"].astype(str)
    categories = {"model": model_categories, "vault_id": vault_categories}
    base = list(fm.NUMERIC_FEATURES)
    arms = {
        "baseline": (base, ["model"]),
        "baseline_plus_vault_id": (base, ["model", "vault_id"]),
        "baseline_plus_embedding": (base + emb_cols, ["model"]),
    }
    results: dict = {"config": {"train": [str(train_start), str(train_end)], "test": [str(test_start), str(test_end)], "horizon_days": horizon,
                                "seeds": list(seeds), "embedding_dims": len(emb_cols), "embedding_coverage_of_test_rows": round(coverage, 4),
                                "lightgbm_params": params, "lightgbm_version": __import__("lightgbm").__version__,
                                "project1_reported_lightgbm_test": metrics_p1["test"]["lightgbm"]},
                     "counts": {"fit_rows": int(len(fit_df)), "fit_positives": int(fit_df["y"].sum()), "cal_rows": int(len(cal_df)),
                                "test_rows": int(len(test_df)), "test_positives": int(test_df["y"].sum())},
                     "arms": {}}
    for arm, (numeric, categorical) in arms.items():
        per_seed = []
        for sd in seeds:
            t1 = time.time()
            m = _fit_predict(fm, fit_df, cal_df, test_df, numeric, categorical, categories, sd, params)
            per_seed.append({"seed": sd, **m, "seconds": round(time.time() - t1, 1)})
            log(f"{arm} seed {sd}: PR-AUC {m['pr_auc']} recall@0.1% {m['recall_top_0.001']} recall@1% {m['recall_top_0.01']} brier {m['brier_calibrated']}")
        mean = {k: round(float(np.mean([p[k] for p in per_seed])), 4) for k in ("pr_auc", "recall_top_0.001", "recall_top_0.01")}
        mean["brier_calibrated"] = round(float(np.mean([p["brier_calibrated"] for p in per_seed])), 6)
        std = {k: round(float(np.std([p[k] for p in per_seed])), 4) for k in ("pr_auc",)}
        results["arms"][arm] = {"per_seed": per_seed, "mean": mean, "std": std}
    b = results["arms"]["baseline"]["mean"]
    results["lift"] = {arm: {k: round(results["arms"][arm]["mean"][k] - b[k], 4) for k in ("pr_auc", "recall_top_0.001", "recall_top_0.01")}
                       for arm in arms if arm != "baseline"}
    pr_std = max(results["arms"][a]["std"]["pr_auc"] for a in arms)
    d_emb = results["lift"]["baseline_plus_embedding"]["pr_auc"]
    results["verdict"] = ("embedding lift in PR-AUC is within seed noise" if abs(d_emb) <= 2 * pr_std
                          else "embedding improves PR-AUC beyond seed noise" if d_emb > 0 else "embedding hurts PR-AUC beyond seed noise")
    results["verdict"] += f" (delta {d_emb:+.4f}, max seed std {pr_std:.4f})"
    (reports / "gds_lift.json").write_text(json.dumps(results, indent=2) + "\n")
    log(results["verdict"])
    return results


# ---------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    from ekg.config import neo4j_driver, settings
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["communities", "centrality", "embed", "lift", "all"])
    ap.add_argument("--reports", type=Path, default=Path("reports"))
    ap.add_argument("--seeds", type=int, nargs="*", default=[20261017, 1, 2])
    a = ap.parse_args(argv)
    s = settings()
    a.reports.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)  # noqa: E731
    summary_path = a.reports / "gds_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    if a.command in ("communities", "centrality", "embed", "all"):
        with neo4j_driver() as drv, drv.session() as session:
            if a.command in ("communities", "all"):
                summary["communities"] = communities(session, a.reports, log)
            if a.command in ("centrality", "all"):
                summary["centrality"] = centrality(session, a.reports, log)
            if a.command in ("embed", "all"):
                summary["embeddings"] = embed(session, a.reports, log=log)
        summary_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    if a.command in ("lift", "all"):
        lift(a.reports, s.sois_repo, s.parquet_dir, tuple(a.seeds), log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
