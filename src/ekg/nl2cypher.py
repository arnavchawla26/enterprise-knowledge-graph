"""Text-to-Cypher with a self-correcting loop, a write guard, and a disk cache.

    uv run python -m ekg.nl2cypher ask "which 3 drive models have the most failed drives?" [--backend nim|claude]
    uv run python -m ekg.nl2cypher freeze          # run the gold Cypher in questions/eval.yaml, store result hashes
    uv run python -m ekg.nl2cypher eval [--backend nim claude]   # execution accuracy -> reports/nl2cypher_eval.csv

Loop: build a schema prompt from the live graph (labels, properties,
relationship patterns) plus the glossary (every Term with its one
definition, which is how "failure" gets the right meaning); ask the model
for Cypher; refuse anything that is not read-only; run it in a READ
transaction as the reader account with a 30 s timeout and a LIMIT; if it
errors or returns no rows, hand the error (or "no rows") back to the model
and retry, at most 3 attempts.

Read-only, twice over: (1) the guard rejects CREATE, MERGE, DELETE, SET,
REMOVE, DROP, FOREACH, LOAD CSV, CALL { } subqueries that write, apoc.* and
gds.* write/mutate procedures, and any schema command, before execution;
(2) execution uses the driver's READ access mode, in which the server
refuses writes (Neo.ClientError.Statement.AccessMode). Neo4j Community has
no roles, so the separate reader account is hygiene, not enforcement; see
README.

Backends: NVIDIA NIM (OpenAI-compatible, NVIDIA_API_KEY, NVIDIA_MODEL) and
Claude (ANTHROPIC_API_KEY, ANTHROPIC_MODEL). Every model call is cached on
disk under cache/nl2cypher/<sha256>.json keyed by backend, model and the
full message list, so `eval` reruns cost nothing and are reproducible.
Keys are read from the environment and never logged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO / "cache" / "nl2cypher"
EVAL_PATH = REPO / "questions" / "eval.yaml"
MAX_ATTEMPTS = 3
ROW_LIMIT = 200
TIMEOUT_S = 30

# ---------------------------------------------------------------- write guard
_WRITE_WORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV|ALTER|GRANT|DENY|REVOKE|START\s+DATABASE|STOP\s+DATABASE|"
    r"CREATE\s+(INDEX|CONSTRAINT|USER|ROLE|DATABASE))\b", re.IGNORECASE)
_WRITE_PROCS = re.compile(
    r"\bCALL\s+(apoc\.(?!meta\.|text\.|coll\.|map\.|date\.|number\.|convert\.|util\.|path\.)|"
    r"gds\.[\w.]*\.(write|mutate)\b|gds\.graph\.(project|drop|create)|db\.create|dbms\.|tx\.|db\.index\.fulltext\.create)", re.IGNORECASE)


def strip_strings(cypher: str) -> str:
    """Blank out string literals so a keyword inside quotes is not a false positive."""
    return re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "''", cypher)


def is_read_only(cypher: str) -> tuple[bool, str]:
    body = strip_strings(cypher)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    m = _WRITE_WORDS.search(body)
    if m:
        return False, f"write keyword {m.group(0).upper()}"
    m = _WRITE_PROCS.search(body)
    if m:
        return False, f"procedure not allowed: {m.group(0)}"
    if re.search(r"\bUSE\s+\w+", body, re.IGNORECASE):
        return False, "USE clause not allowed"
    return True, ""


def ensure_limit(cypher: str, limit: int = ROW_LIMIT) -> str:
    c = cypher.strip().rstrip(";")
    if re.search(r"\bLIMIT\s+\d+\s*$", c, re.IGNORECASE):
        return c
    return f"{c}\nLIMIT {limit}"


def extract_cypher(text: str) -> str:
    m = re.search(r"```(?:cypher)?\s*(.*?)```", text, re.S | re.IGNORECASE)
    body = m.group(1) if m else text
    return body.strip()


# ---------------------------------------------------------------- schema prompt
def schema_prompt(session) -> str:
    nodes: dict[str, set[str]] = {}
    for r in session.run("CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName RETURN nodeLabels, propertyName").data():
        for lab in r["nodeLabels"]:
            nodes.setdefault(lab, set())
            if r["propertyName"]:
                nodes[lab].add(r["propertyName"])
    rels = session.run("""
        CALL db.schema.visualization() YIELD relationships
        UNWIND relationships AS r
        RETURN DISTINCT labels(startNode(r))[0] AS a, type(r) AS t, labels(endNode(r))[0] AS b ORDER BY a, t, b""").data()
    terms = session.run("MATCH (t:Term) RETURN t.label AS label, t.neo4j_label AS lab, t.definition AS d, t.kind AS kind ORDER BY t.kind, t.label").data()
    reltypes = session.run("MATCH (r:RelationshipType) RETURN r.name AS n, r.definition AS d ORDER BY n").data()
    lines = ["Node labels and properties:"]
    for lab in sorted(nodes):
        if lab in ("RelationshipType",):
            continue
        lines.append(f"  (:{lab}) {{{', '.join(sorted(nodes[lab]))}}}")
    lines.append("Relationship patterns:")
    lines += [f"  (:{r['a']})-[:{r['t']}]->(:{r['b']})" for r in rels if r["a"] != "RelationshipType"]
    lines.append("Relationship meanings:")
    lines += [f"  {r['n']}: {r['d']}" for r in reltypes]
    lines.append("Glossary (one definition per term; use these meanings):")
    for t in terms:
        lab = f" [label {t['lab']}]" if t["lab"] else ""
        lines.append(f"  {t['label']}{lab}: {t['d']}")
    lines += ["Facts about values:",
              "  Drive.failed is 1 or 0; Drive.failure_date is an ISO date string; Case.failure_reported is 1 or 0.",
              "  Case.opened_at and closed_at are ISO strings; Case.severity is an integer 1-4; Pod.risky is boolean.",
              "  Asset.warranty_end is an ISO date string or null (null means unknown).",
              "  Table names for the Postgres copies start with 'pg.' (e.g. 'pg.site'); lake datasets use bare names ('installed_asset') or paths ('reports/fleet_risk_sites.csv').",
              "  SAME_AS edges carry score, method, band; a Drive is SAME_AS its Asset; an AccountRecord is SAME_AS its Account."]
    return "\n".join(lines)


SYSTEM = """You translate questions about an enterprise knowledge graph into a single read-only Cypher query for Neo4j 5.
Rules: return ONLY one Cypher query in a ```cypher fenced block, no prose. Never write (no CREATE, MERGE, SET, DELETE, REMOVE, CALL writes).
Use only the labels, relationship types and properties in the schema. Use the glossary definitions for business terms such as failure.
When a question asks for counts per group, return the group key and the count. When it asks for 'top N', ORDER BY the count DESC then by the key, and LIMIT N.
Return scalar answers as a single row with a single column."""


# ---------------------------------------------------------------- backends and cache
@dataclass
class Backend:
    name: str
    model: str

    def complete(self, messages: list[dict]) -> str:
        if self.name == "nim":
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=os.environ["NVIDIA_API_KEY"])
            r = client.chat.completions.create(model=self.model, messages=messages, temperature=0.0, max_tokens=800)
            return r.choices[0].message.content or ""
        if self.name == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            rest = [m for m in messages if m["role"] != "system"]
            r = client.messages.create(model=self.model, max_tokens=800, system=system, messages=rest)
            return "".join(getattr(b, "text", "") for b in r.content)
        raise ValueError(f"unknown backend {self.name}")


def backend_from_env(name: str) -> Backend:
    if name == "nim":
        return Backend("nim", os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b"))
    if name == "claude":
        return Backend("claude", os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    raise ValueError(name)


def cached_complete(backend: Backend, messages: list[dict], cache_dir: Path = CACHE_DIR, offline: bool = False) -> tuple[str, bool]:
    """Returns (text, from_cache). offline=True never calls a model."""
    key = hashlib.sha256(json.dumps({"backend": backend.name, "model": backend.model, "messages": messages}, sort_keys=True).encode()).hexdigest()
    p = cache_dir / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text())["response"], True
    if offline:
        raise RuntimeError(f"no cached response for {backend.name}/{backend.model} and offline mode is on")
    text = backend.complete(messages)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"backend": backend.name, "model": backend.model, "created": datetime.now(timezone.utc).isoformat(),
                             "messages_sha256": key, "response": text}, indent=2) + "\n")
    return text, False


# ---------------------------------------------------------------- execution
def run_read_only(driver, cypher: str) -> list[dict]:
    from neo4j import READ_ACCESS
    ok, why = is_read_only(cypher)
    if not ok:
        raise PermissionError(f"refused before execution: {why}")
    q = ensure_limit(cypher)
    with driver.session(default_access_mode=READ_ACCESS) as s:
        def work(tx):
            return tx.run(q, timeout=TIMEOUT_S).data()
        return s.execute_read(work)


@dataclass
class Trace:
    question: str
    backend: str
    model: str
    attempts: int = 0
    cypher: str = ""
    rows: list[dict] = field(default_factory=list)
    error: str | None = None
    cached_calls: int = 0
    model_calls: int = 0
    steps: list[dict] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return self.error is None


def ask(driver, backend: Backend, question: str, schema: str, offline: bool = False, max_attempts: int = MAX_ATTEMPTS) -> Trace:
    tr = Trace(question=question, backend=backend.name, model=backend.model)
    messages = [{"role": "system", "content": SYSTEM + "\n\nSchema:\n" + schema}, {"role": "user", "content": question}]
    for attempt in range(1, max_attempts + 1):
        tr.attempts = attempt
        try:
            text, hit = cached_complete(backend, messages, offline=offline)
        except RuntimeError as e:
            tr.error = str(e)
            return tr
        tr.cached_calls += int(hit)
        tr.model_calls += int(not hit)
        cypher = extract_cypher(text)
        tr.cypher = cypher
        step = {"attempt": attempt, "cypher": cypher}
        try:
            rows = run_read_only(driver, cypher)
            tr.error = None
            if rows:
                tr.rows = rows
                step["rows"] = len(rows)
                tr.steps.append(step)
                return tr
            feedback = "The query ran but returned no rows. The question expects at least one row; check the labels, property names and values against the schema and fix the query."
            step["rows"] = 0
            tr.rows = []
        except PermissionError as e:
            feedback = f"Refused: {e}. Write a read-only query."
            tr.error = str(e)
            step["error"] = str(e)
        except Exception as e:  # noqa: BLE001 - the error text is the point
            msg = str(e).split("\n")[0][:500]
            feedback = f"The query failed with this Neo4j error: {msg}\nFix the query."
            tr.error = msg
            step["error"] = msg
        tr.steps.append(step)
        messages = messages + [{"role": "assistant", "content": f"```cypher\n{cypher}\n```"}, {"role": "user", "content": feedback}]
    if tr.error is None and not tr.rows:
        tr.error = "no rows after retries"
    return tr


# ---------------------------------------------------------------- eval
def result_hash(rows: list[dict]) -> str:
    """Order-free over rows and columns: each row becomes the sorted tuple of its stringified values."""
    def norm(v):
        if isinstance(v, float):
            return f"{round(v, 4):.4f}"
        if isinstance(v, bool):
            return str(v).lower()
        if isinstance(v, (list, tuple)):
            return "[" + ",".join(norm(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ",".join(f"{k}:{norm(v[k])}" for k in sorted(v)) + "}"
        return "null" if v is None else str(v)
    canon = sorted(tuple(sorted(norm(v) for v in r.values())) for r in rows)
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()


def load_eval(path: Path = EVAL_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text())["questions"]


def freeze(driver, path: Path = EVAL_PATH) -> int:
    doc = yaml.safe_load(path.read_text())
    for q in doc["questions"]:
        rows = run_read_only(driver, q["cypher"])
        q["expected_hash"] = result_hash(rows)
        q["expected_rows"] = len(rows)
        q["expected_preview"] = json.dumps(rows[:3], default=str)[:300]
    path.write_text(yaml.safe_dump(doc, sort_keys=False, width=120))
    return len(doc["questions"])


def evaluate(driver, backends: list[Backend], schema: str, reports: Path, offline: bool = False, log=print) -> dict:
    questions = load_eval()
    reports.mkdir(parents=True, exist_ok=True)
    rows_out = []
    summary: dict = {}
    for b in backends:
        correct = executed = model_calls = cached_calls = 0
        t0 = time.time()
        for q in questions:
            tr = ask(driver, b, q["question"], schema, offline=offline)
            ok = tr.executed and result_hash(tr.rows) == q["expected_hash"]
            correct += ok
            executed += tr.executed
            model_calls += tr.model_calls
            cached_calls += tr.cached_calls
            rows_out.append({"question_id": q["id"], "backend": b.name, "model": b.model, "question": q["question"], "attempts": tr.attempts,
                             "executed": tr.executed, "correct": ok, "rows": len(tr.rows), "expected_rows": q["expected_rows"],
                             "error": tr.error or "", "generated_cypher": tr.cypher.replace("\n", " "), "gold_cypher": q["cypher"].replace("\n", " ")})
            log(f"{b.name:6} {q['id']:>4} attempts={tr.attempts} executed={tr.executed} correct={ok}")
        summary[b.name] = {"model": b.model, "questions": len(questions), "executed": executed, "correct": correct,
                           "execution_accuracy": round(correct / len(questions), 4), "model_calls": model_calls, "cached_calls": cached_calls,
                           "seconds": round(time.time() - t0, 1)}
        log(f"{b.name}: {correct}/{len(questions)} correct ({summary[b.name]['execution_accuracy']:.0%}), {executed} executed, "
            f"{model_calls} model calls, {cached_calls} from cache")
    with open(reports / "nl2cypher_eval.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    (reports / "nl2cypher_eval_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


# ---------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    from ekg.config import neo4j_driver
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["ask", "freeze", "eval", "schema"])
    ap.add_argument("question", nargs="?")
    ap.add_argument("--backend", nargs="*", default=["nim"])
    ap.add_argument("--offline", action="store_true", help="only use cached model responses")
    ap.add_argument("--reports", type=Path, default=Path("reports"))
    a = ap.parse_args(argv)
    with neo4j_driver(read_only=True) as drv:
        with drv.session() as s:
            schema = schema_prompt(s)
        if a.command == "schema":
            print(schema)
            return 0
        if a.command == "freeze":
            print(f"froze {freeze(drv)} questions")
            return 0
        if a.command == "ask":
            tr = ask(drv, backend_from_env(a.backend[0]), a.question, schema, offline=a.offline)
            print(json.dumps({"cypher": tr.cypher, "attempts": tr.attempts, "error": tr.error, "rows": tr.rows[:20],
                              "model_calls": tr.model_calls, "cached": tr.cached_calls}, indent=2, default=str))
            return 0 if tr.executed else 1
        summary = evaluate(drv, [backend_from_env(b) for b in a.backend], schema, a.reports, offline=a.offline)
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
