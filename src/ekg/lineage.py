"""Lineage and metadata graph: Table, Column, DataProduct, Job, Query.

    uv run python -m ekg.lineage load                      # build the metadata layer in Neo4j
    uv run python -m ekg.lineage provenance reports/fleet_risk_sites.csv expected_failures_30d
    uv run python -m ekg.lineage impact installed_asset

Sources: lineage/pipeline.yaml (the real Project 1 and Project 2 pipeline,
declared by hand and checked against the source by tests), the generated
metadata tables (data_product, job, lineage, query_log), Postgres
information_schema for the warehouse columns, DuckDB DESCRIBE for the lake
files (CSV, Parquet, report CSVs), and ontology/enterprise.ttl for the terms
a column means.

Two questions the graph answers, with Cypher in docs/LINEAGE.md:
  provenance  "where did this number come from": from a Column, walk
              Table-PRODUCED_BY->Job-READS->Table upstream to the raw files.
  impact      "what breaks if this table changes": the downstream closure of
              jobs, tables, data products and logged queries.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

from ekg import graph

PIPELINE_PATH = Path(__file__).resolve().parents[2] / "lineage" / "pipeline.yaml"


def read_pipeline(path: Path = PIPELINE_PATH) -> dict:
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------- column discovery
def columns_from_postgres(con, tables: list[str]) -> dict[str, list[dict]]:
    sql = """SELECT table_name, column_name, data_type, ordinal_position FROM information_schema.columns
             WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"""
    out: dict[str, list[dict]] = {}
    for t, c, dt, pos in con.execute(sql).fetchall():
        if t in tables:
            out.setdefault(t, []).append({"name": c, "data_type": dt, "ordinal": int(pos)})
    return out


def columns_from_file(path: Path) -> list[dict]:
    """DuckDB DESCRIBE over a CSV, a Parquet file or a hive-partitioned Parquet directory."""
    import duckdb
    if not path.exists():
        return []
    if path.is_dir():
        src = f"read_parquet('{path}/*/*.parquet', hive_partitioning=true)"
    elif path.suffix == ".parquet":
        src = f"read_parquet('{path}')"
    elif path.suffix == ".csv":
        src = f"read_csv('{path}', header=true, sample_size=2000)"
    else:
        return []
    con = duckdb.connect()
    try:
        rows = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
    finally:
        con.close()
    return [{"name": r[0], "data_type": str(r[1]).lower(), "ordinal": i + 1} for i, r in enumerate(rows)]


# ---------------------------------------------------------------- build rows (pure)
def build(pipeline: dict, generated: dict[str, pd.DataFrame], lake_columns: dict[str, list[dict]],
          warehouse_columns: dict[str, list[dict]], sois_repo: str) -> dict[str, list[dict]]:
    """Turns the declarations and the discovered columns into node and edge rows."""
    tables: dict[str, dict] = {}
    for name, d in pipeline["datasets"].items():
        tables[name] = {"name": name, "kind": d["kind"], "path": d.get("path"), "source": d.get("source", "derived"),
                        "repo": d.get("repo", sois_repo), "pii": bool(d.get("pii", False)), "store": "lake",
                        "gitignored": bool(d.get("gitignored", False)), "note": d.get("note")}
    for t in pipeline["warehouse_tables"]:
        tables[f"pg.{t}"] = {"name": f"pg.{t}", "kind": "postgres_table", "path": f"postgres://{sois_repo}/public.{t}", "source": "warehouse_copy",
                             "repo": sois_repo, "pii": t == "contact", "store": "warehouse", "gitignored": False, "note": None}
    columns: list[dict] = []
    for name, cols in lake_columns.items():
        for c in cols:
            columns.append({"column_key": f"{name}.{c['name']}", "table": name, **c})
    for t, cols in warehouse_columns.items():
        for c in cols:
            columns.append({"column_key": f"pg.{t}.{c['name']}", "table": f"pg.{t}", **c})
    for t in tables.values():
        t["columns"] = sum(1 for c in columns if c["table"] == t["name"])

    jobs: dict[str, dict] = {}
    reads: list[dict] = []
    produced: list[dict] = []
    derived: list[dict] = []     # Table-DERIVED_FROM->Table: precise where the job is a copy, coarse (every write from every read) otherwise
    for j in pipeline["jobs"]:
        jobs[j["job_id"]] = {"job_id": j["job_id"], "name": j["name"], "entrypoint": j["entrypoint"], "repo": j["repo"],
                             "module": j.get("module"), "schedule": j.get("schedule"), "status": "real", "source": "declared"}
        reads += [{"src": j["job_id"], "dst": t} for t in j.get("reads", [])]
        writes = list(j.get("writes", []))
        if j.get("writes_warehouse"):
            writes += [f"pg.{t}" for t in pipeline["warehouse_tables"]]
            derived += [{"src": f"pg.{t}", "dst": t, "via": j["job_id"], "precision": "copy"} for t in pipeline["warehouse_tables"] if t in j.get("reads", [])]
            derived += [{"src": "pg.telemetry_drive_summary", "dst": "drive_summary", "via": j["job_id"], "precision": "copy"},
                        {"src": "pg.telemetry_daily_recent", "dst": "telemetry_daily", "via": j["job_id"], "precision": "copy"},
                        {"src": "pg.drive_model", "dst": "drive_model", "via": j["job_id"], "precision": "copy"}]
        else:
            derived += [{"src": w, "dst": r, "via": j["job_id"], "precision": "job"} for w in writes for r in j.get("reads", [])]
        produced += [{"src": t, "dst": j["job_id"]} for t in writes]
    # generated job.csv / lineage.csv: keep the rows the declared pipeline does not already cover, marked generated
    gj = generated.get("job")
    if gj is not None:
        for r in gj.itertuples():
            if r.job_id not in jobs:
                jobs[r.job_id] = {"job_id": r.job_id, "name": r.name, "entrypoint": r.entrypoint, "repo": sois_repo, "module": None,
                                  "schedule": r.schedule, "status": "planned" if r.entrypoint == "planned" else "real", "source": "generated"}
    gl = generated.get("lineage")
    aliases = pipeline.get("aliases", {})
    if gl is not None:
        gl = gl.replace({"input_table": aliases, "output_table": aliases})
        for r in gl.itertuples():
            for t in (r.input_table, r.output_table):
                if t not in tables and f"pg.{t}" not in tables:
                    tables[t] = {"name": t, "kind": "declared_only", "path": None, "source": "generated", "repo": sois_repo, "pii": False,
                                 "store": "planned", "gitignored": False, "note": "named in lineage.csv but not materialized", "columns": 0}
            inp = r.input_table if r.input_table in tables else f"pg.{r.input_table}"
            outp = r.output_table if r.output_table in tables else f"pg.{r.output_table}"
            produces = {p["src"] for p in produced if p["dst"] == r.job_id}
            # a row "A -> B" inside a job is derivation, not a read of its own output
            if inp not in produces and {"src": r.job_id, "dst": inp} not in reads:
                reads.append({"src": r.job_id, "dst": inp})
            if {"src": outp, "dst": r.job_id} not in produced:
                produced.append({"src": outp, "dst": r.job_id})
            e = {"src": outp, "dst": inp, "via": r.job_id, "precision": "declared_row"}
            if e not in derived:
                derived.append(e)

    dps: list[dict] = []
    composed: list[dict] = []
    gd = generated.get("data_product")
    if gd is not None:
        for r in gd.itertuples():
            dps.append({"data_product_id": r.data_product_id, "name": r.name, "owner": r.owner, "description": r.description,
                        "freshness_sla_hours": int(r.freshness_sla_hours)})
            for t in str(r.tables).split(";"):
                composed.append({"src": r.data_product_id, "dst": f"pg.{t}" if f"pg.{t}" in tables else t})

    queries: list[dict] = []
    qreads: list[dict] = []
    gq = generated.get("query_log")
    if gq is not None:
        for r in gq.itertuples():
            queries.append({"query_id": r.query_id, "ran_at": str(r.ran_at), "user_name": r.user_name, "sql_text": r.sql_text,
                            "duration_ms": int(r.duration_ms), "rows_returned": int(r.rows_returned)})
            for t in str(r.tables).split(";"):
                qreads.append({"src": r.query_id, "dst": f"pg.{t}" if f"pg.{t}" in tables else t})

    means: list[dict] = []
    for key, term in pipeline["column_meanings"].items():
        table, col = key.rsplit(".", 1)
        for tname in (table, f"pg.{table}"):
            if any(c["column_key"] == f"{tname}.{col}" for c in columns):
                means.append({"src": f"{tname}.{col}", "dst": term})
    stored: list[dict] = [{"src": term, "dst": t} for term, ts in pipeline["term_tables"].items() for t in ts if t in tables]
    stored += [{"src": term, "dst": f"pg.{t}"} for term, ts in pipeline["term_tables"].items() for t in ts if f"pg.{t}" in tables]
    derived = [d for d in derived if d["src"] in tables and d["dst"] in tables and d["src"] != d["dst"]]
    return {"tables": list(tables.values()), "columns": columns, "jobs": list(jobs.values()), "reads": reads, "produced": produced,
            "derived": derived, "data_products": dps, "composed": composed, "queries": queries, "query_reads": qreads, "means": means, "stored": stored}


METADATA_EDGES = ("BELONGS_TO", "READS", "PRODUCED_BY", "DERIVED_FROM", "COMPOSED_OF", "MEANS", "STORED_IN")


def write(session, rows: dict[str, list[dict]], log=print) -> dict[str, int]:
    """Rebuilds the metadata layer: its edge types are deleted first so a changed declaration
    cannot leave a stale edge behind (MERGE alone never removes anything)."""
    graph.ensure_schema(session)
    for rel in METADATA_EDGES:
        session.run(f"MATCH ()-[r:{rel}]->() DELETE r").consume()
    c: dict[str, int] = {}
    c["Table"] = graph.write_nodes(session, "Table", "name", rows["tables"])
    c["Column"] = graph.write_nodes(session, "Column", "column_key", rows["columns"])
    c["Job"] = graph.write_nodes(session, "Job", "job_id", rows["jobs"])
    c["DataProduct"] = graph.write_nodes(session, "DataProduct", "data_product_id", rows["data_products"])
    c["Query"] = graph.write_nodes(session, "Query", "query_id", rows["queries"])
    c["BELONGS_TO"] = graph.write_edges(session, "Column", "column_key", "BELONGS_TO", "Table", "name",
                                        [{"src": x["column_key"], "dst": x["table"]} for x in rows["columns"]])
    c["READS(job)"] = graph.write_edges(session, "Job", "job_id", "READS", "Table", "name", rows["reads"])
    c["PRODUCED_BY"] = graph.write_edges(session, "Table", "name", "PRODUCED_BY", "Job", "job_id", rows["produced"])
    c["DERIVED_FROM"] = graph.write_edges(session, "Table", "name", "DERIVED_FROM", "Table", "name", rows["derived"], props=("via", "precision"))
    c["COMPOSED_OF"] = graph.write_edges(session, "DataProduct", "data_product_id", "COMPOSED_OF", "Table", "name", rows["composed"])
    c["READS(query)"] = graph.write_edges(session, "Query", "query_id", "READS", "Table", "name", rows["query_reads"])
    c["MEANS"] = graph.write_edges(session, "Column", "column_key", "MEANS", "Term", "term_id", rows["means"])
    c["STORED_IN"] = graph.write_edges(session, "Term", "term_id", "STORED_IN", "Table", "name", rows["stored"])
    from ekg.load import LABEL_TERMS
    for label in ("Table", "Column", "DataProduct", "Job", "Query"):
        c[f"INSTANCE_OF:{label}"] = graph.link_instances(session, label, LABEL_TERMS[label])
    for k, v in c.items():
        log(f"  {k}: {v:,}")
    return c


# ---------------------------------------------------------------- the two questions
PROVENANCE = """
// Where did this number come from: the column, what it means, the job that wrote its table,
// and every dataset upstream through Table-DERIVED_FROM->Table, with the job that produced each.
MATCH (c:Column {column_key: $column_key})-[:BELONGS_TO]->(t:Table)
OPTIONAL MATCH (c)-[:MEANS]->(term:Term)
OPTIONAL MATCH (t)-[:PRODUCED_BY]->(j:Job)
WITH c, t, term, collect(DISTINCT j.name) AS producers
OPTIONAL MATCH p = (t)-[:DERIVED_FROM*1..12]->(up:Table)
WITH c, t, term, producers, up, min(length(p)) AS depth
OPTIONAL MATCH (up)-[:PRODUCED_BY]->(uj:Job)
WITH c, t, term, producers, up, depth, collect(DISTINCT uj.name) AS via
RETURN c.column_key AS column, t.name AS table, producers, term.label AS means, term.definition AS definition,
       [x IN collect({depth: depth, node: up.name, kind: up.kind, source: up.source, produced_by: via}) WHERE x.node IS NOT NULL] AS upstream
"""
IMPACT = """
// What breaks if this table changes: downstream tables through DERIVED_FROM, the jobs that read any of
// them (they must re-run), the data products built on them, and the logged queries that touch them.
MATCH (t:Table {name: $table})
OPTIONAL MATCH p = (t)<-[:DERIVED_FROM*1..12]-(d:Table)
WITH t, d, min(length(p)) AS depth
WITH t, [x IN collect({depth: depth, node: d.name, kind: d.kind}) WHERE x.node IS NOT NULL] AS downstream
WITH t, downstream, [t.name] + [x IN downstream | x.node] AS affected
OPTIONAL MATCH (j:Job)-[:READS]->(a:Table) WHERE a.name IN affected
WITH t, downstream, affected, collect(DISTINCT j.name) AS jobs
OPTIONAL MATCH (dp:DataProduct)-[:COMPOSED_OF]->(b:Table) WHERE b.name IN affected
WITH t, downstream, affected, jobs, collect(DISTINCT dp.name) AS data_products
OPTIONAL MATCH (q:Query)-[:READS]->(qt:Table) WHERE qt.name IN affected
RETURN t.name AS table, downstream, jobs, data_products, count(DISTINCT q) AS logged_queries,
       collect(DISTINCT q.user_name) AS query_users
"""


def provenance(session, column_key: str) -> dict | None:
    rec = session.run(PROVENANCE, column_key=column_key).single()
    if rec is None:
        return None
    d = dict(rec)
    d["upstream"] = sorted(d["upstream"], key=lambda u: (u["depth"], u["node"]))
    return d


def impact(session, table: str) -> dict | None:
    rec = session.run(IMPACT, table=table).single()
    if rec is None:
        return None
    d = dict(rec)
    d["downstream"] = sorted(d["downstream"], key=lambda u: (u["depth"], u["node"]))
    d["jobs"] = sorted(j for j in d["jobs"] if j)
    d["query_users"] = sorted(u for u in d["query_users"] if u)
    return d


def format_provenance(d: dict) -> str:
    lines = [f"{d['column']}  (table {d['table']}, written by {', '.join(d['producers']) or 'unknown'})"]
    if d.get("means"):
        lines.append(f"  means: {d['means']}: {d['definition']}")
    for u in d["upstream"]:
        via = f", by {', '.join(u['produced_by'])}" if u.get("produced_by") else ""
        lines.append(f"  {'  ' * (u['depth'] - 1)}<- {u['node']} ({u['kind']}{', ' + u['source'] if u.get('source') else ''}{via})")
    return "\n".join(lines)


def format_impact(d: dict) -> str:
    lines = [f"{d['table']}"]
    for u in d["downstream"]:
        lines.append(f"  {'  ' * (u['depth'] - 1)}-> {u['node']} ({u['kind']})")
    lines.append(f"  jobs that must re-run: {', '.join(d['jobs']) or 'none'}")
    lines.append(f"  data products affected: {', '.join(d['data_products']) or 'none'}")
    lines.append(f"  logged queries affected: {d['logged_queries']} (users: {', '.join(d['query_users']) or 'none'})")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    from ekg.config import neo4j_driver, pg_connect, settings
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["load", "provenance", "impact"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--no-postgres", action="store_true")
    ap.add_argument("--report", type=Path, default=Path("reports/lineage_counts.json"))
    a = ap.parse_args(argv)
    s = settings()
    if a.command == "load":
        pipe = read_pipeline()
        generated = {n: pd.read_csv(s.enterprise_dir / f"{n}.csv", dtype=str) for n in ("data_product", "job", "lineage", "query_log")}
        lake_cols: dict[str, list[dict]] = {}
        for name, d in pipe["datasets"].items():
            if d["kind"] in ("csv", "parquet", "report", "model_output") and d.get("path") and not d.get("repo"):
                lake_cols[name] = columns_from_file(s.sois_repo / d["path"])
            elif d.get("repo") == "enterprise-knowledge-graph" and d["kind"] in ("report", "model_output"):
                lake_cols[name] = columns_from_file(Path(d["path"]))
        wh_cols: dict[str, list[dict]] = {}
        if not a.no_postgres:
            try:
                with pg_connect() as con:
                    wh_cols = columns_from_postgres(con, pipe["warehouse_tables"])
            except Exception as e:  # noqa: BLE001
                print(f"postgres not available ({type(e).__name__}); warehouse columns skipped", file=sys.stderr)
        rows = build(pipe, generated, lake_cols, wh_cols, "services-ops-intelligence")
        with neo4j_driver() as drv, drv.session() as session:
            counts = write(session, rows)
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(counts, indent=2) + "\n")
        return 0
    with neo4j_driver() as drv, drv.session() as session:
        if a.command == "provenance":
            key = ".".join(a.args) if len(a.args) == 2 else a.args[0]
            d = provenance(session, key)
            print(format_provenance(d) if d else f"no column {key}")
        else:
            d = impact(session, a.args[0])
            print(format_impact(d) if d else f"no table {a.args[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
