"""Small helpers shared by every module that writes to Neo4j: batched UNWIND
writes, constraints, and a session wrapper that tests can replace with a fake."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

BATCH = 5000

CONSTRAINTS: list[tuple[str, str, str]] = [
    # (name, label, property)
    ("account_record_row", "AccountRecord", "row_id"),
    ("account_id", "Account", "account_id"),
    ("site_id", "Site", "site_id"),
    ("asset_id", "Asset", "asset_id"),
    ("drive_serial", "Drive", "serial_number"),
    ("drive_model_name", "DriveModel", "model"),
    ("pod_key", "Pod", "pod_key"),
    ("vault_key", "Vault", "vault_key"),
    ("product_id", "Product", "product_id"),
    ("part_id", "Part", "part_id"),
    ("supplier_id", "Supplier", "supplier_id"),
    ("case_id", "Case", "case_id"),
    ("dispatch_id", "Dispatch", "dispatch_id"),
    ("technician_id", "Technician", "technician_id"),
    ("runbook_id", "Runbook", "runbook_id"),
    ("table_name", "Table", "name"),
    ("column_key", "Column", "column_key"),
    ("data_product_id", "DataProduct", "data_product_id"),
    ("job_id", "Job", "job_id"),
    ("query_id", "Query", "query_id"),
    ("term_id", "Term", "term_id"),
]
INDEXES: list[tuple[str, str, str]] = [
    ("asset_serial_norm", "Asset", "serial_norm"),
    ("asset_erp_serial", "Asset", "erp_serial"),
    ("account_record_account_id", "AccountRecord", "account_id"),
    ("account_record_name_norm", "AccountRecord", "name_norm"),
    ("site_region", "Site", "region"),
    ("case_category", "Case", "category"),
    ("case_opened", "Case", "opened_at"),
    ("drive_failed", "Drive", "failed"),
    ("drive_model", "Drive", "model"),
    ("column_name", "Column", "name"),
    ("term_label", "Term", "neo4j_label"),
]


def chunks(rows: list[dict[str, Any]], size: int = BATCH) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def ensure_schema(session) -> None:
    for name, label, prop in CONSTRAINTS:
        session.run(f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
    for name, label, prop in INDEXES:
        session.run(f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})")


def write_nodes(session, label: str, key: str, rows: list[dict[str, Any]], log=None) -> int:
    """MERGE one node per row on `key`, SET every other property. Idempotent."""
    cypher = f"UNWIND $rows AS r MERGE (n:{label} {{{key}: r.{key}}}) SET n += r"
    n = 0
    for batch in chunks(rows):
        session.run(cypher, rows=batch).consume()
        n += len(batch)
        if log and n % (BATCH * 10) == 0:
            log(f"  {label}: {n:,}/{len(rows):,}")
    return n


def write_edges(session, src_label: str, src_key: str, rel: str, dst_label: str, dst_key: str,
                rows: list[dict[str, Any]], props: tuple[str, ...] = (), log=None) -> int:
    """rows carry `src` and `dst` key values plus optional edge properties."""
    set_clause = (" SET " + ", ".join(f"e.{p} = r.{p}" for p in props)) if props else ""
    cypher = (f"UNWIND $rows AS r MATCH (a:{src_label} {{{src_key}: r.src}}) MATCH (b:{dst_label} {{{dst_key}: r.dst}}) "
              f"MERGE (a)-[e:{rel}]->(b){set_clause}")
    n = 0
    for batch in chunks(rows):
        session.run(cypher, rows=batch).consume()
        n += len(batch)
        if log and n % (BATCH * 10) == 0:
            log(f"  {src_label}-{rel}->{dst_label}: {n:,}/{len(rows):,}")
    return n


def link_instances(session, label: str, term_id: str) -> int:
    """(:label)-[:INSTANCE_OF]->(:Term) for every node of the label, in transactions of 10k."""
    r = session.run(
        f"MATCH (t:Term {{term_id: $term_id}}) "
        f"CALL (t) {{ MATCH (n:{label}) WHERE NOT (n)-[:INSTANCE_OF]->(t) MERGE (n)-[:INSTANCE_OF]->(t) }} IN TRANSACTIONS OF 10000 ROWS "
        f"RETURN 1 AS ok", term_id=term_id)
    r.consume()
    return session.run(f"MATCH (:{label})-[r:INSTANCE_OF]->(:Term {{term_id: $term_id}}) RETURN count(r) AS n",
                       term_id=term_id).single()["n"]


def wipe(session) -> None:
    session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 20000 ROWS").consume()
    for row in session.run("SHOW CONSTRAINTS YIELD name").data():
        session.run(f"DROP CONSTRAINT {row['name']} IF EXISTS")
    for row in session.run("SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP'").data():
        session.run(f"DROP INDEX {row['name']} IF EXISTS")


def count_by_label(session) -> dict[str, int]:
    out = {}
    for row in session.run("CALL db.labels() YIELD label RETURN label ORDER BY label").data():
        out[row["label"]] = session.run(f"MATCH (n:{row['label']}) RETURN count(n) AS n").single()["n"]
    return out
