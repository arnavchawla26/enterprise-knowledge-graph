import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ekg import api


class FakeRepo:
    """A tiny graph in memory, shaped like the real one."""
    TERMS = [
        {"term_id": "PartsLeadTime", "label": "Parts Lead Time", "definition": "lead time def", "alt_labels": ["lead time"], "neo4j_label": None, "kind": "measure"},
        {"term_id": "Account", "label": "Account", "definition": "account def", "alt_labels": ["customer"], "neo4j_label": "Account", "kind": "entity"},
        {"term_id": "TelemetryFailure", "label": "Failure (telemetry)", "definition": "failure def", "alt_labels": ["failed drive"], "neo4j_label": None, "kind": "measure"},
    ]
    TABLES = {"part_supplier": {"name": "part_supplier", "kind": "csv", "store": "lake", "path": "x", "pii": False},
              "account": {"name": "account", "kind": "csv", "store": "lake", "path": "x", "pii": False},
              "contact": {"name": "contact", "kind": "csv", "store": "lake", "path": "x", "pii": True}}
    STORED = {"PartsLeadTime": ["part_supplier"], "Account": ["account", "contact"], "TelemetryFailure": []}
    COLUMNS = {"part_supplier": [{"column_key": "part_supplier.lead_time_days", "table": "part_supplier", "name": "lead_time_days", "data_type": "int", "means": "Parts Lead Time"},
                                 {"column_key": "part_supplier.unit_cost_usd", "table": "part_supplier", "name": "unit_cost_usd", "data_type": "float", "means": None}],
               "account": [{"column_key": "account.account_name", "table": "account", "name": "account_name", "data_type": "str", "means": None}],
               "contact": [{"column_key": "contact.email", "table": "contact", "name": "email", "data_type": "str", "means": None}]}

    def terms(self):
        return self.TERMS

    def tables_for_terms(self, ids):
        return [self.TABLES[n] for i in ids for n in self.STORED.get(i, [])]

    def tables_named(self, names):
        return [self.TABLES[n] for n in names if n in self.TABLES]

    def columns_of(self, names):
        return [c for n in names for c in self.COLUMNS.get(n, [])]

    def runbooks_matching(self, words):
        return [{"runbook_id": "RB-001", "title": "Expand a volume", "hits": 1}] if "volume" in words else []

    def neighborhood(self, label, prop, value):
        return [{"label": "Site", "rel": "HAS", "id": "SITE-00001"}, {"label": "AccountRecord", "rel": "SAME_AS", "id": "7"},
                {"label": "Case", "rel": "ABOUT", "id": "CS-0000001"}]


@pytest.fixture
def client(tmp_path):
    return TestClient(api.create_app(repo=FakeRepo(), policy=api.load_policy(), audit_path=tmp_path / "audit.jsonl")), tmp_path / "audit.jsonl"


def test_terms_are_matched_by_label_and_alt_label():
    t = api.match_terms("what is the lead time for the customer's failed drives?", FakeRepo.TERMS)
    assert {x["term_id"] for x in t} == {"PartsLeadTime", "Account", "TelemetryFailure"}


def test_technician_cannot_see_costs_contacts_or_accounts(client):
    c, audit = client
    r = c.post("/context", json={"question": "lead time for the customer ACC-00009 and asset AST-0000001", "caller_role": "technician"})
    assert r.status_code == 200
    body = r.json()
    tables = {t["name"] for t in body["tables"]}
    assert "part_supplier" in tables and "account" not in tables and "contact" not in tables
    assert set(body["denied"]["tables"]) == {"account", "contact"}
    cols = {c["column_key"] for c in body["columns"]}
    assert "part_supplier.lead_time_days" in cols and "part_supplier.unit_cost_usd" not in cols
    assert "part_supplier.unit_cost_usd" in body["denied"]["columns"]
    assert "Account" in body["denied"]["labels"], "the ACC- anchor itself is off-limits to a technician"
    labels = {n["label"] for n in body["neighborhood"]}
    assert "AccountRecord" not in labels and "Site" in labels and all(n["anchor"] == "AST-0000001" for n in body["neighborhood"])


def test_ops_lead_sees_accounts_but_never_contacts(client):
    c, _ = client
    body = c.post("/context", json={"question": "customer ACC-00009 lead time", "caller_role": "ops_lead"}).json()
    tables = {t["name"] for t in body["tables"]}
    assert "account" in tables and "contact" not in tables and body["denied"]["tables"] == ["contact"]
    assert {n["label"] for n in body["neighborhood"]} == {"Site", "AccountRecord", "Case"}
    assert body["denied"]["labels"] == []


def test_analyst_gets_no_runbooks_but_the_glossary(client):
    c, _ = client
    body = c.post("/context", json={"question": "how do I expand a volume", "caller_role": "analyst"}).json()
    assert body["documents"] == []
    body = c.post("/context", json={"question": "how do I expand a volume", "caller_role": "technician"}).json()
    assert body["documents"][0]["runbook_id"] == "RB-001"


def test_unknown_role_is_refused_and_audit_line_written(client):
    c, audit = client
    assert c.post("/context", json={"question": "anything at all", "caller_role": "ceo"}).status_code == 403
    c.post("/context", json={"question": "lead time please", "caller_role": "analyst"})
    lines = [json.loads(l) for l in audit.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["role"] == "analyst" and lines[0]["returned"]["terms"] == 1
    assert "lead time" not in audit.read_text(), "the question text itself is not logged, only its hash"


def test_policy_file_has_the_three_roles_and_denies_contact_pii():
    pol = api.load_policy()
    assert set(pol) == {"ops_lead", "technician", "analyst"}
    for role in pol.values():
        assert not api.allowed("contact", role["tables"], role.get("deny_tables", []))
