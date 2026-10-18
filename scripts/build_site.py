"""Regenerate site/data/*.json and site/img/* from reports/ and docs/.

The static site under site/ is published on Vercel. Every number it shows comes
from the files this script reads; nothing is typed into the page by hand.
Run from the repository root:  python3 scripts/build_site.py
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
DOCS = ROOT / "docs"
SITE = ROOT / "site"
DATA = SITE / "data"
IMG = SITE / "img"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def num(s: str) -> float | int | str:
    if s is None or s == "":
        return ""
    try:
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        return s


def typed(rows: list[dict[str, str]]) -> list[dict]:
    return [{k: num(v) for k, v in r.items()} for r in rows]


# ----------------------------------------------------------------------------
# A small Markdown-to-HTML converter: headings, paragraphs, lists, tables,
# fenced code, inline code, bold, italics, links. Enough for the docs.
# ----------------------------------------------------------------------------

def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            if len(para) > 1 and all(re.match(r"^[A-Z][a-z]+: ", s) for s in para):
                out.append("<p>" + "<br>".join(inline(s.strip()) for s in para) + "</p>")
            else:
                out.append("<p>" + inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush_para()
            j = i + 1
            code: list[str] = []
            while j < len(lines) and not lines[j].startswith("```"):
                code.append(lines[j])
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1]):
            flush_para()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            j = i + 2
            body: list[list[str]] = []
            while j < len(lines) and lines[j].startswith("|"):
                body.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            out.append('<div class="tablewrap"><table><thead><tr>' + "".join(f"<th>{inline(h)}</th>" for h in header) + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table></div>")
            i = j
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            flush_para()
            ordered = m.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            j = i
            while j < len(lines):
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[j])
                if mm:
                    items.append(mm.group(3))
                    j += 1
                elif lines[j].startswith("  ") and items:
                    items[-1] += " " + lines[j].strip()
                    j += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{inline(it)}</li>" for it in items) + f"</{tag}>")
            i = j
            continue
        if line.startswith("!["):
            i += 1
            continue
        if not line.strip():
            flush_para()
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para()
    return "\n".join(out)


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def readme_intro() -> str:
    text = (ROOT / "README.md").read_text()
    body = text.split("\n", 1)[1].strip()
    first = body.split("\n\n", 1)[0]
    return " ".join(s.strip() for s in first.splitlines())


def lineage_example() -> dict:
    """The provenance example from docs/LINEAGE.md: the command line, its printed
    output block and the paragraph that reads it."""
    text = (DOCS / "LINEAGE.md").read_text()
    lines = text.splitlines()
    cmd_idx = next(i for i, l in enumerate(lines) if "ekg.lineage provenance" in l)
    cmd = re.search(r"`([^`]+)`", " ".join(lines[cmd_idx:cmd_idx + 2])).group(1)
    start = next(i for i in range(cmd_idx, len(lines)) if lines[i].startswith("```"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    block = "\n".join(lines[start + 1:end])
    after: list[str] = []
    i = end + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and lines[i].strip():
        after.append(lines[i].strip())
        i += 1
    query_start = next(i for i, l in enumerate(lines) if l.startswith("## Where did this number come from"))
    q1 = next(i for i in range(query_start, len(lines)) if lines[i].startswith("```"))
    q1e = next(i for i in range(q1 + 1, len(lines)) if lines[i].startswith("```"))
    return {
        "command": cmd,
        "output": block,
        "reading": " ".join(after),
        "column_query": "\n".join(lines[q1 + 1:q1e]),
    }


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)

    resolution = json.loads((REPORTS / "resolution_metrics.json").read_text())
    gds_lift = json.loads((REPORTS / "gds_lift.json").read_text())
    gds_summary = json.loads((REPORTS / "gds_summary.json").read_text())
    nl_easy = json.loads((REPORTS / "nl2cypher_eval_summary.json").read_text())
    nl_hard = json.loads((REPORTS / "nl2cypher_hard_eval_summary.json").read_text())
    lineage_counts = json.loads((REPORTS / "lineage_counts.json").read_text())
    load_counts = json.loads((REPORTS / "load_counts.json").read_text())
    trust_now = json.loads((REPORTS / "trust_results_now.json").read_text())
    trust_asof = json.loads((REPORTS / "trust_results_asof.json").read_text())

    uncertain = typed(read_csv(REPORTS / "uncertain_matches.csv"))[:20]
    easy_rows = read_csv(REPORTS / "nl2cypher_eval.csv")
    hard_rows = read_csv(REPORTS / "nl2cypher_hard_eval.csv")

    def eval_rows(rows: list[dict[str, str]]) -> list[dict]:
        out = []
        for r in rows:
            out.append({
                "question_id": r["question_id"],
                "backend": r["backend"],
                "model": r["model"],
                "question": r["question"],
                "attempts": num(r["attempts"]),
                "executed": r["executed"] == "True",
                "correct": r["correct"] == "True",
                "rows": num(r["rows"]),
                "expected_rows": num(r["expected_rows"]),
                "error": r.get("error", ""),
            })
        return out

    def trust_checks(d: dict) -> list[dict]:
        keep = ("data_product", "check", "table", "status", "threshold", "value")
        return [{k: doc.get(k) for k in keep if k in doc} for doc in d.get("docs", [])]

    def trust_by_check(d: dict) -> list[dict]:
        agg: dict[str, dict] = {}
        for doc in d.get("docs", []):
            kind = str(doc.get("check", "")).split(".")[0].split(":")[0]
            a = agg.setdefault(kind, {"kind": kind, "pass": 0, "fail": 0, "skip": 0})
            a[doc.get("status", "skip")] = a.get(doc.get("status", "skip"), 0) + 1
        return sorted(agg.values(), key=lambda x: x["kind"])

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "intro": readme_intro(),
        "load": {k: v for k, v in load_counts.items() if k != "labels" and not k.startswith("INSTANCE_OF")},
        "lineage_counts": lineage_counts,
        "resolution": resolution,
        "uncertain": uncertain,
        "gds": {
            "lift": gds_lift,
            "communities": {k: v for k, v in gds_summary["communities"].items() if k != "largest"},
            "largest": gds_summary["communities"]["largest"],
            "centrality": gds_summary["centrality"],
            "embeddings": gds_summary["embeddings"],
        },
        "nl2cypher": {
            "easy_summary": nl_easy,
            "hard_summary": nl_hard,
            "easy": eval_rows(easy_rows),
            "hard": eval_rows(hard_rows),
        },
        "trust": {
            "now": {"run_id": trust_now["run_id"], "as_of": trust_now["as_of"], "checks": trust_now["checks"], "by_status": trust_now["by_status"], "summary": trust_now["summary"], "by_kind": trust_by_check(trust_now), "failed": [c for c in trust_checks(trust_now) if c.get("status") == "fail"]},
            "asof": {"run_id": trust_asof["run_id"], "as_of": trust_asof["as_of"], "checks": trust_asof["checks"], "by_status": trust_asof["by_status"], "summary": trust_asof["summary"], "by_kind": trust_by_check(trust_asof), "failed": [c for c in trust_checks(trust_asof) if c.get("status") == "fail"]},
        },
        "lineage_example": lineage_example(),
    }
    (DATA / "site.json").write_text(json.dumps(out, separators=(",", ":")))

    for stale in IMG.glob("*.png"):
        stale.unlink()
    shutil.copyfile(REPORTS / "trust_dashboard.png", IMG / "trust_dashboard.png")
    print(f"wrote {DATA / 'site.json'} ({(DATA / 'site.json').stat().st_size} bytes), {len(list(IMG.iterdir()))} images")


if __name__ == "__main__":
    main()
