"""Ontology v0: parse ontology/enterprise.ttl, render ontology/glossary.md,
load the terms into Neo4j as :Term nodes.

    uv run python -m ekg.ontology render        # TTL -> glossary.md
    uv run python -m ekg.ontology load          # TTL -> (:Term) nodes + RELATES_TO edges
    uv run python -m ekg.ontology check         # every term has exactly one definition

The semantic layer rule is one definition per term. `check` fails if a concept
has zero or more than one skos:definition, and `load` refuses to run until
`check` passes, so the graph can never carry a term with two meanings.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import rdflib
from rdflib.namespace import OWL, RDF, RDFS, SKOS

EKG = rdflib.Namespace("https://github.com/arnavchawla26/enterprise-knowledge-graph/ontology#")
ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "ontology" / "enterprise.ttl"
GLOSSARY_PATH = ONTOLOGY_PATH.parent / "glossary.md"


@dataclass
class Term:
    term_id: str                 # local name, e.g. TelemetryFailure
    label: str                   # skos:prefLabel
    definition: str
    kind: str                    # entity | measure | metadata
    neo4j_label: str | None
    source: str | None
    rule: str | None
    alt_labels: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class Relationship:
    name: str
    domain: str | None
    range: str | None
    definition: str


def _local(uri) -> str:
    return str(uri).split("#")[-1]


def _one(g: rdflib.Graph, s, p) -> str | None:
    vals = list(g.objects(s, p))
    return str(vals[0]) if vals else None


def parse(path: Path = ONTOLOGY_PATH) -> tuple[list[Term], list[Relationship], list[str]]:
    """Returns (terms, relationships, problems). problems is non-empty when a
    concept has zero or several definitions."""
    g = rdflib.Graph()
    g.parse(path, format="turtle")
    terms: list[Term] = []
    problems: list[str] = []
    for s in sorted(set(g.subjects(RDF.type, SKOS.Concept)), key=str):
        defs = [str(d) for d in g.objects(s, SKOS.definition)]
        if len(defs) != 1:
            problems.append(f"{_local(s)}: {len(defs)} definitions")
        label = _one(g, s, SKOS.prefLabel)
        if not label:
            problems.append(f"{_local(s)}: no prefLabel")
        terms.append(Term(
            term_id=_local(s), label=label or _local(s), definition=defs[0] if defs else "",
            kind=_one(g, s, EKG.kind) or "entity", neo4j_label=_one(g, s, EKG.neo4jLabel),
            source=_one(g, s, EKG.source), rule=_one(g, s, EKG.rule),
            alt_labels=sorted(str(a) for a in g.objects(s, SKOS.altLabel)),
            related=sorted(_local(r) for r in g.objects(s, SKOS.related)),
        ))
    rels: list[Relationship] = []
    for s in sorted(set(g.subjects(RDF.type, OWL.ObjectProperty)), key=str):
        d, r = _one(g, s, RDFS.domain), _one(g, s, RDFS.range)
        rels.append(Relationship(name=_one(g, s, RDFS.label) or _local(s),
                                 domain=_local(d) if d else None, range=_local(r) if r else None,
                                 definition=_one(g, s, SKOS.definition) or ""))
    labels = [t.label.lower() for t in terms]
    dupes = sorted({l for l in labels if labels.count(l) > 1})
    problems += [f"label used twice: {d}" for d in dupes]
    return terms, rels, problems


def render_glossary(terms: list[Term], rels: list[Relationship]) -> str:
    kinds = [("entity", "Entities"), ("measure", "Measures (derived terms)"), ("metadata", "Metadata (the data about the data)")]
    out = ["# Glossary (rendered from ontology/enterprise.ttl; do not edit by hand)", "",
           "One definition per term. `Source` names the Project 1 table, column or file the term is read from;",
           "`Rule` says how a derived term is computed. The same text is loaded into Neo4j as `:Term` nodes,",
           "and every entity node links to its term with `INSTANCE_OF`.", ""]
    for kind, heading in kinds:
        out += [f"## {heading}", ""]
        for t in [t for t in terms if t.kind == kind]:
            out.append(f"### {t.label}")
            if t.neo4j_label:
                out.append(f"Neo4j label: `{t.neo4j_label}`  ")
            out.append(f"{t.definition}  ")
            if t.source:
                out.append(f"Source: {t.source}  ")
            if t.rule:
                out.append(f"Rule: {t.rule}  ")
            if t.alt_labels:
                out.append("Also called: " + ", ".join(t.alt_labels) + "  ")
            if t.related:
                out.append("Related: " + ", ".join(t.related) + "  ")
            out.append("")
    out += ["## Relationships", "", "| Relationship | From | To | Meaning |", "|---|---|---|---|"]
    for r in rels:
        out.append(f"| `{r.name}` | {r.domain or 'any'} | {r.range or 'any'} | {r.definition} |")
    out.append("")
    return "\n".join(out)


LOAD_TERMS = """
UNWIND $terms AS t
MERGE (x:Term {term_id: t.term_id})
SET x.label = t.label, x.definition = t.definition, x.kind = t.kind, x.neo4j_label = t.neo4j_label,
    x.source = t.source, x.rule = t.rule, x.alt_labels = t.alt_labels
"""
LOAD_RELATED = """
UNWIND $pairs AS p
MATCH (a:Term {term_id: p[0]}), (b:Term {term_id: p[1]})
MERGE (a)-[:RELATED_TO]->(b)
"""
LOAD_RELTYPES = """
UNWIND $rels AS r
MERGE (x:RelationshipType {name: r.name})
SET x.domain = r.domain, x.range = r.range, x.definition = r.definition
WITH x, r
OPTIONAL MATCH (d:Term {neo4j_label: r.domain}) FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END | MERGE (d)-[:DOMAIN_OF]->(x))
WITH x, r
OPTIONAL MATCH (g:Term {neo4j_label: r.range}) FOREACH (_ IN CASE WHEN g IS NULL THEN [] ELSE [1] END | MERGE (x)-[:RANGE_IS]->(g))
"""


def load_terms(driver, terms: list[Term], rels: list[Relationship]) -> int:
    with driver.session() as s:
        s.run("CREATE CONSTRAINT term_id IF NOT EXISTS FOR (t:Term) REQUIRE t.term_id IS UNIQUE")
        s.run("CREATE CONSTRAINT reltype_name IF NOT EXISTS FOR (r:RelationshipType) REQUIRE r.name IS UNIQUE")
        s.run(LOAD_TERMS, terms=[t.__dict__ for t in terms])
        pairs = [(t.term_id, r) for t in terms for r in t.related]
        s.run(LOAD_RELATED, pairs=pairs)
        s.run(LOAD_RELTYPES, rels=[r.__dict__ for r in rels])
        return s.run("MATCH (t:Term) RETURN count(t) AS n").single()["n"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["render", "load", "check"])
    ap.add_argument("--ttl", type=Path, default=ONTOLOGY_PATH)
    ap.add_argument("--out", type=Path, default=GLOSSARY_PATH)
    a = ap.parse_args(argv)
    terms, rels, problems = parse(a.ttl)
    if problems:
        for p in problems:
            print("ontology problem:", p, file=sys.stderr)
        return 1
    print(f"{len(terms)} terms, {len(rels)} relationships, one definition each")
    if a.command == "check":
        return 0
    if a.command == "render":
        a.out.write_text(render_glossary(terms, rels))
        print(f"wrote {a.out}")
        return 0
    from ekg.config import neo4j_driver
    with neo4j_driver() as drv:
        n = load_terms(drv, terms, rels)
    print(f"loaded {n} :Term nodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
