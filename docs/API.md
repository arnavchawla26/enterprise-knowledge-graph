# Context-assembly API (ekg.api)

`POST /context {question, caller_role}` returns what the caller may use to answer: glossary
terms the question mentions, the tables those terms are stored in (plus tables the question
names), their columns with the term each column means, matching runbooks, and the 1-hop
graph neighbourhood of any entity id in the question, each section filtered server-side by
`policy/roles.yaml`. The response also lists what was denied (names only), so an agent
knows what it is not seeing. One JSON line per call goes to `logs/context_audit.jsonl` with
the role, a hash of the question (never the text), and counts returned and denied.

Roles: `ops_lead` (everything except contact PII), `technician` (support, product, parts
and lead times; no accounts, no prices, no hire dates), `analyst` (every table and the
metadata layer; no contacts, no site addresses, no runbooks).

Example, live graph, 2026-10-17: "Which parts have the longest lead time for the failed
drives at account ACC-00009?"

| Role | Terms | Tables returned | Denied |
|---|---|---|---|
| technician | Account, Drive, Installed Asset, Part, Parts Lead Time, Failure (telemetry) | installed_asset, pg.installed_asset, dispatch_part, part, pg.part, pg.dispatch_part | tables drive_summary, telemetry_daily, account, truth/account_resolution, pg.account; columns part.unit_cost_usd, pg.part.unit_cost_usd; label Account (so no neighbourhood for the ACC- anchor) |
| ops_lead | same | same plus the account and telemetry tables | nothing; neighbourhood of ACC-00009: its Sites and AccountRecords |

No model is called: terms are matched by label and alternative label, tables by the
`STORED_IN` edges from the ontology, runbooks by keyword. That is deliberate (the API must
be cheap and deterministic enough to call before every answer) and limited (a question that
uses none of the glossary's words gets only what it names). `tests/test_api.py` covers the
permission filter with an in-memory graph.
