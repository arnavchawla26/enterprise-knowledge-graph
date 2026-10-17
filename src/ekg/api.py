"""Context-assembly API: what an agent is allowed to see before it answers.

    uv run uvicorn ekg.api:app --port 8008
    curl -s localhost:8008/context -X POST -H 'content-type: application/json' \\
         -d '{"question": "which parts have the longest lead time for account ACC-00009?", "caller_role": "technician"}'

POST /context {question, caller_role} returns:
  terms          glossary terms the question mentions (label, definition), always allowed
  tables         tables those terms are stored in plus tables the question names, filtered by role
  columns        columns of those tables, filtered by role (deny_columns removed)
  documents      runbooks whose title or symptoms match the question (technician, ops_lead)
  neighborhood   1-hop graph neighborhood of any entity id in the question (ACC-, AST-, CS-, SITE-,
                 DSP-, PRT-, TEC-), only through labels the role may see, capped per role
  denied         what the role could not have (names only, so the agent knows what it is not seeing)
  audit_id       the id of the audit line written for this call

The allow-list in policy/roles.yaml is applied server-side to every section;
the question is matched against the glossary and the schema by keyword, with
no model call. Every call appends one JSON line to logs/context_audit.jsonl
(timestamp, role, question hash, what was returned and what was denied).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO / "policy" / "roles.yaml"
AUDIT_PATH = REPO / "logs" / "context_audit.jsonl"

ID_PATTERNS = {
    "Account": r"\bACC-\d{5}\b", "Asset": r"\bAST-\d{7}\b", "Case": r"\bCS-\d{7}\b", "Site": r"\bSITE-\d{5}\b",
    "Dispatch": r"\bDSP-\d{7}\b", "Part": r"\bPRT-\d{4}\b", "Technician": r"\bTEC-\d{4}\b", "Product": r"\bPRD-[A-Z]{2}\d{3}\b",
}
ID_PROPERTY = {"Account": "account_id", "Asset": "asset_id", "Case": "case_id", "Site": "site_id", "Dispatch": "dispatch_id",
               "Part": "part_id", "Technician": "technician_id", "Product": "product_id"}


# ---------------------------------------------------------------- policy
def load_policy(path: Path = POLICY_PATH) -> dict:
    return yaml.safe_load(path.read_text())["roles"]


def allowed(name: str, allow: list[str], deny: list[str]) -> bool:
    if name in deny:
        return False
    return "*" in allow or name in allow


def filter_tables(tables: list[dict], role: dict) -> tuple[list[dict], list[str]]:
    keep, denied = [], []
    for t in tables:
        (keep if allowed(t["name"], role["tables"], role.get("deny_tables", [])) else denied).append(t if allowed(t["name"], role["tables"], role.get("deny_tables", [])) else t["name"])
    return keep, denied


def filter_columns(columns: list[dict], role: dict, allowed_tables: set[str]) -> tuple[list[dict], list[str]]:
    keep, denied = [], []
    for c in columns:
        if c["table"] not in allowed_tables:
            continue
        (denied if c["column_key"] in role.get("deny_columns", []) else keep).append(c["column_key"] if c["column_key"] in role.get("deny_columns", []) else c)
    return keep, denied


def filter_neighborhood(nodes: list[dict], role: dict) -> tuple[list[dict], list[str]]:
    keep, denied = [], []
    cap = int(role.get("max_neighborhood", 50))
    for n in nodes:
        if allowed(n["label"], role["labels"], role.get("deny_labels", [])):
            keep.append(n)
        else:
            denied.append(n["label"])
    return keep[:cap], sorted(set(denied))


# ---------------------------------------------------------------- graph access (swappable for tests)
class Repository(Protocol):
    def terms(self) -> list[dict]: ...
    def tables_for_terms(self, term_ids: list[str]) -> list[dict]: ...
    def tables_named(self, names: list[str]) -> list[dict]: ...
    def columns_of(self, table_names: list[str]) -> list[dict]: ...
    def runbooks_matching(self, words: list[str]) -> list[dict]: ...
    def neighborhood(self, label: str, prop: str, value: str) -> list[dict]: ...


class Neo4jRepository:
    def __init__(self, driver):
        self.driver = driver

    def _read(self, cypher: str, **params) -> list[dict]:
        from neo4j import READ_ACCESS
        with self.driver.session(default_access_mode=READ_ACCESS) as s:
            return s.execute_read(lambda tx: tx.run(cypher, **params).data())

    def terms(self):
        return self._read("MATCH (t:Term) RETURN t.term_id AS term_id, t.label AS label, t.definition AS definition, "
                          "t.alt_labels AS alt_labels, t.neo4j_label AS neo4j_label, t.kind AS kind")

    def tables_for_terms(self, term_ids):
        return self._read("MATCH (t:Term)-[:STORED_IN]->(x:Table) WHERE t.term_id IN $ids "
                          "RETURN DISTINCT x.name AS name, x.kind AS kind, x.store AS store, x.path AS path, x.pii AS pii", ids=term_ids)

    def tables_named(self, names):
        return self._read("MATCH (x:Table) WHERE x.name IN $names RETURN x.name AS name, x.kind AS kind, x.store AS store, x.path AS path, x.pii AS pii", names=names)

    def columns_of(self, table_names):
        return self._read("MATCH (c:Column)-[:BELONGS_TO]->(t:Table) WHERE t.name IN $names "
                          "OPTIONAL MATCH (c)-[:MEANS]->(m:Term) "
                          "RETURN c.column_key AS column_key, t.name AS table, c.name AS name, c.data_type AS data_type, m.label AS means "
                          "ORDER BY t.name, c.ordinal", names=table_names)

    def runbooks_matching(self, words):
        return self._read("MATCH (r:Runbook) WITH r, [w IN $words WHERE toLower(r.title) CONTAINS w OR toLower(r.symptoms) CONTAINS w] AS hits "
                          "WHERE size(hits) > 0 RETURN r.runbook_id AS runbook_id, r.title AS title, r.product_family AS product_family, "
                          "r.category AS category, size(hits) AS hits ORDER BY hits DESC, runbook_id LIMIT 5", words=words)

    def neighborhood(self, label, prop, value):
        return self._read(f"MATCH (n:{label} {{{prop}: $v}})-[r]-(m) WHERE NOT m:Term "
                          "RETURN labels(m)[0] AS label, type(r) AS rel, "
                          "coalesce(m.name, m.account_id, m.asset_id, m.case_id, m.site_id, m.dispatch_id, m.part_id, m.technician_id, m.product_id, m.serial_number, m.pod_key, m.vault_key, m.model, m.runbook_id, m.supplier_id, toString(m.row_id)) AS id "
                          "LIMIT 200", v=value)


# ---------------------------------------------------------------- assembly
STOP = {"the", "a", "an", "of", "for", "in", "on", "to", "and", "or", "is", "are", "what", "which", "how", "many", "with", "by", "at", "from", "that", "this", "do", "does", "me", "show", "list", "give"}


def words_of(question: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9_]+", question.lower()) if w not in STOP and len(w) > 2]


def match_terms(question: str, terms: list[dict]) -> list[dict]:
    q = " " + re.sub(r"[^a-z0-9 ]+", " ", question.lower()) + " "
    out = []
    for t in terms:
        names = [t["label"].lower()] + [a.lower() for a in (t.get("alt_labels") or [])]
        names = [re.sub(r"\s*\(.*?\)", "", n).strip() for n in names]
        if any(f" {n} " in q or f" {n}s " in q for n in names if n):
            out.append(t)
    return out


def assemble(repo: Repository, policy: dict, question: str, caller_role: str, audit_path: Path = AUDIT_PATH) -> dict[str, Any]:
    if caller_role not in policy:
        raise HTTPException(status_code=403, detail=f"unknown role {caller_role!r}")
    role = policy[caller_role]
    terms = match_terms(question, repo.terms())
    term_ids = [t["term_id"] for t in terms]
    tables = repo.tables_for_terms(term_ids) if term_ids else []
    named = [w for w in re.findall(r"\b(?:pg\.)?[a-z_]+(?:\.csv|\.parquet|\.json)?\b", question.lower())]
    if named:
        seen = {t["name"] for t in tables}
        tables += [t for t in repo.tables_named(named) if t["name"] not in seen]
    tables_ok, tables_denied = filter_tables(tables, role)
    columns_ok, columns_denied = filter_columns(repo.columns_of([t["name"] for t in tables_ok]), role, {t["name"] for t in tables_ok})
    documents = repo.runbooks_matching(words_of(question)) if "runbook" in role.get("documents", []) else []
    neighborhood: list[dict] = []
    nb_denied: list[str] = []
    for label, pat in ID_PATTERNS.items():
        for value in re.findall(pat, question):
            if not allowed(label, role["labels"], role.get("deny_labels", [])):
                nb_denied.append(label)
                continue
            keep, denied = filter_neighborhood(repo.neighborhood(label, ID_PROPERTY[label], value), role)
            neighborhood += [{"anchor": value, **n} for n in keep]
            nb_denied += denied
    audit_id = uuid.uuid4().hex[:12]
    out = {
        "audit_id": audit_id, "caller_role": caller_role,
        "terms": [{"term_id": t["term_id"], "label": t["label"], "definition": t["definition"]} for t in terms],
        "tables": tables_ok, "columns": columns_ok, "documents": documents, "neighborhood": neighborhood,
        "denied": {"tables": tables_denied, "columns": columns_denied, "labels": sorted(set(nb_denied))},
    }
    line = {"ts": datetime.now(timezone.utc).isoformat(), "audit_id": audit_id, "role": caller_role,
            "question_sha256": hashlib.sha256(question.encode()).hexdigest()[:16],
            "returned": {k: len(out[k]) for k in ("terms", "tables", "columns", "documents", "neighborhood")},
            "denied": {k: len(v) for k, v in out["denied"].items()}}
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a") as f:
        f.write(json.dumps(line) + "\n")
    return out


# ---------------------------------------------------------------- app
class ContextRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    caller_role: str = Field(min_length=1, max_length=40)


def create_app(repo: Repository | None = None, policy: dict | None = None, audit_path: Path = AUDIT_PATH) -> FastAPI:
    app = FastAPI(title="ekg context assembly", version="0.1.0")
    state: dict[str, Any] = {"repo": repo, "policy": policy or load_policy()}

    def get_repo() -> Repository:
        if state["repo"] is None:
            from ekg.config import neo4j_driver
            state["repo"] = Neo4jRepository(neo4j_driver(read_only=True))
        return state["repo"]

    @app.post("/context")
    def context(req: ContextRequest):
        return assemble(get_repo(), state["policy"], req.question, req.caller_role, audit_path)

    @app.get("/roles")
    def roles():
        return {k: {"description": v.get("description"), "documents": v.get("documents", [])} for k, v in state["policy"].items()}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = create_app()
