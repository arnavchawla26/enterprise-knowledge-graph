# Entry points. Everything runs through uv so the pinned environment is used.
# Order for a fresh machine: up -> load -> resolve -> lineage -> gds -> eval -> trust -> screenshot
# Inputs are read-only from the sibling checkout named by SOIS_REPO (default ../services-ops-intelligence).
.PHONY: sync test up down load resolve lineage gds eval trust screenshot api all

UV ?= uv

sync:            ## create .venv from uv.lock
	$(UV) sync

test:            ## fixture-only tests, no services needed (live tests skip without Neo4j)
	$(UV) run pytest

up:              ## Neo4j 5 (APOC+GDS), Elasticsearch 8, Kibana 8
	docker compose up -d

down:
	docker compose down

load:            ## ontology terms + enterprise + drives into Neo4j (about a minute)
	$(UV) run python -m ekg.ontology check && $(UV) run python -m ekg.ontology render
	$(UV) run python -m ekg.load --reset

resolve:         ## entity resolution, metrics and the uncertain-matches list; writes Account nodes and SAME_AS edges
	$(UV) run python -m ekg.resolve --write

lineage:         ## Table/Column/DataProduct/Job/Query layer and the two documented queries
	$(UV) run python -m ekg.lineage load
	$(UV) run python -m ekg.lineage provenance reports/fleet_risk_sites.csv expected_failures_30d
	$(UV) run python -m ekg.lineage impact installed_asset

gds:             ## communities, part centrality, FastRP embeddings, and the lift test against Project 1's model
	$(UV) run python -m ekg.gds all

eval:            ## text-to-Cypher execution accuracy per backend on both question sets (cached responses make reruns free)
	$(UV) run python -m ekg.nl2cypher eval --backend nim claude --offline
	$(UV) run python -m ekg.nl2cypher eval --backend nim claude --offline --questions questions/hard.yaml

eval-live:       ## same, allowed to call the model backends for uncached prompts
	$(UV) run python -m ekg.nl2cypher eval --backend nim claude
	$(UV) run python -m ekg.nl2cypher eval --backend nim claude --questions questions/hard.yaml

trust:           ## completeness, freshness, schema drift -> Elasticsearch, both clocks
	$(UV) run python -m ekg.trust run
	$(UV) run python -m ekg.trust run --as-of 2026-07-01T00:00:00

screenshot:      ## import the Kibana dashboard and screenshot it headlessly
	$(UV) run python scripts/screenshot_kibana.py

api:             ## context-assembly API on :8008
	$(UV) run uvicorn ekg.api:app --port 8008

all: load resolve lineage gds eval trust screenshot
