import json
from pathlib import Path

import pytest

from ekg import nl2cypher as n


@pytest.mark.parametrize("cypher", [
    "MATCH (a:Asset) RETURN count(a)",
    "MATCH (c:Case) WHERE c.description CONTAINS 'SET the drive' RETURN c.case_id LIMIT 5",   # keyword inside a string literal
    "MATCH (p:Part)-[s:SUPPLIED_BY]->(:Supplier) RETURN p.part_number, avg(s.lead_time_days) ORDER BY 2 DESC LIMIT 3",
    "CALL db.labels() YIELD label RETURN label",
    "MATCH (n) RETURN apoc.meta.type(n) LIMIT 1",
])
def test_read_only_queries_pass(cypher):
    ok, why = n.is_read_only(cypher)
    assert ok, why


@pytest.mark.parametrize("cypher", [
    "MATCH (a:Asset) SET a.status = 'gone' RETURN a",
    "CREATE (x:Hack) RETURN x",
    "MATCH (a:Asset) DETACH DELETE a",
    "MERGE (a:Account {account_id: 'X'}) RETURN a",
    "MATCH (a) REMOVE a.name RETURN a",
    "DROP CONSTRAINT asset_id",
    "LOAD CSV FROM 'file:///x.csv' AS row RETURN row",
    "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})",
    "CALL gds.louvain.write('g', {writeProperty: 'c'})",
    "CALL gds.graph.project('g', '*', '*')",
    "MATCH (a:Asset) FOREACH (x IN [1] | SET a.k = 1) RETURN a",
    "USE system SHOW USERS",
    "CALL dbms.security.createUser('x', 'y')",
])
def test_write_queries_are_refused(cypher):
    ok, why = n.is_read_only(cypher)
    assert not ok and why


def test_limit_is_appended_only_when_missing():
    assert n.ensure_limit("MATCH (a) RETURN a").endswith("LIMIT 200")
    assert n.ensure_limit("MATCH (a) RETURN a LIMIT 5;") == "MATCH (a) RETURN a LIMIT 5"


def test_cypher_is_extracted_from_fences_or_taken_raw():
    assert n.extract_cypher("Sure:\n```cypher\nMATCH (a) RETURN a\n```\nDone") == "MATCH (a) RETURN a"
    assert n.extract_cypher("MATCH (a) RETURN a") == "MATCH (a) RETURN a"


def test_result_hash_ignores_row_and_column_order_and_names():
    a = [{"model": "X", "n": 3}, {"model": "Y", "n": 1.23456}]
    b = [{"count": 1.2346, "m": "Y"}, {"count": 3, "m": "X"}]
    assert n.result_hash(a) == n.result_hash(b)
    assert n.result_hash(a) != n.result_hash([{"model": "X", "n": 4}])


class FakeBackend(n.Backend):
    def __init__(self, replies):
        super().__init__("fake", "fake-1")
        self.replies = list(replies)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return self.replies.pop(0)


class FakeDriver:
    """A driver whose session rejects a bad property and returns rows otherwise."""
    def __init__(self):
        self.ran = []

    def session(self, **kw):
        outer = self

        class S:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def execute_read(self_, fn):
                class Tx:
                    def run(self__, q, timeout=None):
                        outer.ran.append(q)
                        if "failedd" in q:
                            raise RuntimeError("Neo.ClientError.Statement.SyntaxError: Unknown property failedd")
                        if "nothing" in q:
                            class R:
                                def data(self___):
                                    return []
                            return R()
                        class R:
                            def data(self___):
                                return [{"n": 1}]
                        return R()
                return fn(Tx())
        return S()


def test_loop_retries_on_error_and_empty_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(n, "cached_complete", lambda backend, messages, cache_dir=tmp_path, offline=False: (backend.complete(messages), False))
    b = FakeBackend(["```cypher\nMATCH (d:Drive {failedd: 1}) RETURN count(d)\n```",
                     "```cypher\nMATCH (d:Drive {nothing: 1}) RETURN count(d)\n```",
                     "```cypher\nMATCH (d:Drive {failed: 1}) RETURN count(d)\n```"])
    drv = FakeDriver()
    tr = n.ask(drv, b, "how many failed drives?", "schema", max_attempts=3)
    assert tr.attempts == 3 and tr.executed and tr.rows == [{"n": 1}]
    assert [s.get("error", s.get("rows")) for s in tr.steps][0].startswith("Neo.ClientError")
    assert tr.steps[1]["rows"] == 0


def test_loop_refuses_writes_without_running_them(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "cached_complete", lambda backend, messages, cache_dir=tmp_path, offline=False: (backend.complete(messages), False))
    b = FakeBackend(["```cypher\nMATCH (a:Asset) SET a.x = 1 RETURN a\n```"] * 3)
    drv = FakeDriver()
    tr = n.ask(drv, b, "set things", "schema", max_attempts=3)
    assert not tr.executed and "refused" in tr.error and drv.ran == []


def test_cache_round_trip_and_offline(tmp_path):
    b = FakeBackend(["one"])
    msgs = [{"role": "user", "content": "q"}]
    t1, hit1 = n.cached_complete(b, msgs, cache_dir=tmp_path)
    t2, hit2 = n.cached_complete(b, msgs, cache_dir=tmp_path, offline=True)
    assert (t1, hit1, t2, hit2) == ("one", False, "one", True) and b.calls == 1
    with pytest.raises(RuntimeError):
        n.cached_complete(b, [{"role": "user", "content": "other"}], cache_dir=tmp_path, offline=True)


def test_eval_set_has_20_questions_with_gold_and_hashes():
    qs = n.load_eval()
    assert len(qs) == 20 and all(q.get("expected_hash") and q["cypher"] for q in qs)
    assert all(n.is_read_only(q["cypher"])[0] for q in qs)
