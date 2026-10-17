from pathlib import Path

import pytest

from ekg import ontology

REQUIRED_TERMS = {
    "Account", "Site", "Installed Asset", "Product", "Part", "Supplier", "Case", "Dispatch", "Technician",
    "Runbook", "Failure (telemetry)", "Service Failure (support)", "Repeat Dispatch", "Deflectable Case",
    "Fleet Health", "Risky Pod", "Warranty Status", "Parts Lead Time",
}
REQUIRED_RELS = {
    ("HAS", "Account", "Site"), ("HOSTS", "Site", "InstalledAsset"), ("IS_A", "InstalledAsset", "Product"),
    ("USES", "Product", "Part"), ("SUPPLIED_BY", "Part", "Supplier"), ("ABOUT", "Case", "InstalledAsset"),
    ("RESOLVES", "Dispatch", "Case"), ("PERFORMS", "Technician", "Dispatch"), ("COVERS", "Runbook", "Product"),
    ("PRODUCED_BY", "Table", "Job"), ("BELONGS_TO", "Column", "Table"), ("COMPOSED_OF", "DataProduct", "Table"),
    ("READS", None, "Table"),
}


def test_every_term_has_exactly_one_definition():
    terms, rels, problems = ontology.parse()
    assert problems == []
    assert all(t.definition for t in terms)


def test_notes_13c_terms_and_relationships_present():
    terms, rels, _ = ontology.parse()
    labels = {t.label for t in terms}
    assert REQUIRED_TERMS <= labels
    have = {(r.name, r.domain, r.range) for r in rels}
    assert REQUIRED_RELS <= have


def test_failure_definitions_match_project_1_rule_f1():
    terms, _, _ = ontology.parse()
    by = {t.term_id: t for t in terms}
    assert "failure = 1" in by["TelemetryFailure"].definition
    assert "failure_reported = 1" in by["ServiceFailure"].definition
    assert "hardware_replaced" in by["ServiceFailure"].definition
    assert "ServiceFailure" in by["TelemetryFailure"].related


def test_duplicate_definition_is_rejected(tmp_path: Path):
    ttl = ontology.ONTOLOGY_PATH.read_text()
    ttl = ttl.replace('skos:prefLabel "Risky Pod" ;', 'skos:prefLabel "Risky Pod" ; skos:definition "a second meaning" ;')
    p = tmp_path / "bad.ttl"
    p.write_text(ttl)
    _, _, problems = ontology.parse(p)
    assert any("RiskyPod" in x for x in problems)
    assert ontology.main(["check", "--ttl", str(p)]) == 1


def test_glossary_is_rendered_from_the_ttl_and_in_sync():
    terms, rels, _ = ontology.parse()
    rendered = ontology.render_glossary(terms, rels)
    assert rendered == ontology.GLOSSARY_PATH.read_text(), "run: uv run python -m ekg.ontology render"
    assert all(ord(c) < 128 for c in rendered)
