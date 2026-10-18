/* enterprise-knowledge-graph results page. Reads data/site.json written by
   scripts/build_site.py from reports/ and docs/LINEAGE.md. */
(async function () {
  "use strict";
  const S = window.Site;
  const { fmt, esc, table, tiles, setHTML, setText } = S;

  let D;
  try {
    D = await S.loadJSON("data/site.json");
  } catch (e) {
    setHTML("graph-tiles", '<p class="note">Could not load data/site.json: ' + esc(e.message) + "</p>");
    return;
  }
  setText("commit", D.commit || "unknown");
  setText("generated", D.generated_at || "");
  if (D.intro) setText("intro", D.intro.replace(/^Project \d of \d:\s*/, "").replace(/^\w/, (c) => c.toUpperCase()));

  const BACKEND_LABEL = (b, model) => (b === "nim" ? "NIM" : b === "claude" ? "Claude" : b) + (model ? ` (${model})` : "");

  /* ---------------- Graph ---------------- */
  const L = D.load, LC = D.lineage_counts;
  const entityLabels = ["Asset", "Drive", "Case", "Pod", "Dispatch", "Site", "AccountRecord", "Vault", "DriveModel", "Technician", "Part", "Runbook", "Product", "Supplier"].filter((k) => L[k] != null);
  setText("graph-sub", `Loaded by make load in ${fmt.num(L.seconds, 0)} s, scored as of ${L.score_date}. Daily telemetry stays out of the graph; the per-drive summary and the 30-day probability go in.`);
  setHTML("graph-tiles", tiles([
    { v: fmt.int(L.Asset), l: "Asset nodes", s: "each a real drive" },
    { v: fmt.int(L.Case), l: "Case nodes" },
    { v: fmt.int(L.Dispatch), l: "Dispatch nodes" },
    { v: fmt.int(LC.Table), l: "Table nodes", s: "metadata layer" },
    { v: fmt.int(LC.Column), l: "Column nodes" },
    { v: fmt.int(LC["DERIVED_FROM(column)"]), l: "column-level lineage edges", s: `${fmt.int(LC["DERIVED_FROM(table)"])} table-level` },
  ]));
  S.chart("c-nodes", (el) => new Chart(el, {
    type: "bar",
    data: { labels: entityLabels, datasets: [S.barDataset("nodes", entityLabels.map((k) => L[k]), 0)] },
    options: S.baseOptions({
      indexAxis: "y",
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmt.int(c.parsed.x) + " nodes" } } },
      scales: {
        x: { type: "logarithmic", grid: { color: S.cssVar("--grid") }, border: { display: false }, ticks: { callback: (v) => ([1, 10, 100, 1000, 10000, 100000, 1000000].includes(v) ? fmt.int(v) : "") } },
        y: { grid: { display: false }, border: { color: S.cssVar("--axis") } },
      },
    }),
  }));
  const lineageRows = ["Table", "Column", "Job", "DataProduct", "Query", "BELONGS_TO", "READS(job)", "PRODUCED_BY", "DERIVED_FROM(table)", "DERIVED_FROM(column)", "COMPOSED_OF", "READS(query)", "MEANS", "STORED_IN", "column_unresolved"].filter((k) => LC[k] != null).map((k) => ({ k, v: LC[k] }));
  setHTML("lineage-table", table([
    { key: "k", label: "Node or edge" },
    { key: "v", label: "Count", fmt: fmt.int, cls: "n" },
  ], lineageRows));

  /* ---------------- Resolution ---------------- */
  const RA = D.resolution.accounts, RS = D.resolution.assets;
  setText("resolution-sub", `Accounts are scored pairwise (Jaro-Winkler ${RA.weights.jw}, token overlap ${RA.weights.tokens}, same industry ${RA.weights.industry}, same region ${RA.weights.region}); match at ${RA.thresholds.match}, grey band from ${RA.thresholds.grey}. ${fmt.int(RA.candidate_pairs)} candidate pairs after blocking. Assets link a drive serial to its ERP asset by exact match, vendor-prefix stripping or uppercasing.`);
  setHTML("resolution-tiles", tiles([
    { v: fmt.fixed(RA.held_out.name_matcher_only.precision, 3), l: "account precision, held out" },
    { v: fmt.fixed(RA.held_out.name_matcher_only.recall, 3), l: "account recall, held out" },
    { v: fmt.fixed(RA.held_out.canonical_id_accuracy, 3), l: "canonical-id accuracy", s: `${RA.held_out.clusters} clusters, ${RA.held_out.true_companies} true companies` },
    { v: fmt.int(RA.uncertain_pairs), l: "uncertain pairs for review", s: `${fmt.int(RA.ablation_name_only.uncertain_pairs)} without industry and region` },
    { v: fmt.int(RS.matched) + " / " + fmt.int(RS.assets), l: "assets linked to their drive" },
    { v: fmt.int(RS.collisions), l: "collisions", s: `precision ${fmt.fixed(RS.precision, 3)}, recall ${fmt.fixed(RS.recall, 3)}` },
  ]));
  const accRows = [];
  for (const split of ["dev", "held_out"]) {
    for (const variant of ["name_matcher_only", "name_matcher_plus_same_id"]) {
      const m = RA[split][variant];
      accRows.push(Object.assign({ split: split === "dev" ? "dev (thresholds set here)" : "held out", variant: variant === "name_matcher_only" ? "name matcher" : "name matcher + same id" }, m));
    }
  }
  setHTML("resolution-accounts-table", table([
    { key: "split", label: "Split" },
    { key: "variant", label: "Variant" },
    { key: "records", label: "Records", fmt: fmt.int, cls: "n" },
    { key: "true_pairs", label: "True pairs", fmt: fmt.int, cls: "n" },
    { key: "tp", label: "TP", fmt: fmt.int, cls: "n" },
    { key: "fp", label: "FP", fmt: fmt.int, cls: "n" },
    { key: "fn", label: "FN", fmt: fmt.int, cls: "n" },
    { key: "precision", label: "Precision", fmt: (v) => fmt.fixed(v, 3), cls: "n" },
    { key: "recall", label: "Recall", fmt: (v) => fmt.fixed(v, 3), cls: "n" },
  ], accRows));
  const assetRows = Object.keys(RS.by_mess).map((k) => ({ mess: k, n: RS.by_mess[k].n, correct: RS.by_mess[k].correct }));
  setHTML("resolution-assets-table", table([
    { key: "mess", label: "Mess rule on the serial", fmt: (v) => ({ none: "none", lowercase: "lowercased", vendor_prefix: "vendor prefix" }[v] || v) },
    { key: "n", label: "Assets", fmt: fmt.int, cls: "n" },
    { key: "correct", label: "Correct", fmt: fmt.int, cls: "n" },
    { key: (r) => r.correct / r.n, label: "Share", fmt: (v) => fmt.pct(v, 2), cls: "n" },
  ], assetRows) + table([
    { key: "method", label: "Method" },
    { key: "n", label: "Matches", fmt: fmt.int, cls: "n" },
  ], Object.keys(RS.by_method).map((k) => ({ method: k.replace(/_/g, " "), n: RS.by_method[k] }))));
  setHTML("uncertain-table", table([
    { key: "name_a", label: "Name A" },
    { key: "name_b", label: "Name B" },
    { key: "account_id_a", label: "Id A" },
    { key: "account_id_b", label: "Id B" },
    { key: "score", label: "Score", fmt: (v) => fmt.fixed(v, 3), cls: "n" },
    { key: "jw", label: "Jaro-Winkler", fmt: (v) => fmt.fixed(v, 3), cls: "n" },
    { key: "tokens", label: "Tokens", fmt: (v) => fmt.fixed(v, 2), cls: "n" },
    { key: "same_industry", label: "Same industry", fmt: (v) => (Number(v) ? "yes" : "no") },
    { key: "same_region", label: "Same region", fmt: (v) => (Number(v) ? "yes" : "no") },
  ], D.uncertain));
  setText("resolution-note", D.resolution.note + ` ${RA.uncertain_band_true_matches} of the ${RA.uncertain_pairs} uncertain pairs is a true match.`);

  /* ---------------- GDS ---------------- */
  const G = D.gds, lift = G.lift, arms = lift.arms;
  const ARM = ["baseline", "baseline_plus_vault_id", "baseline_plus_embedding"];
  const ARM_LABEL = { baseline: "baseline (SMART features)", baseline_plus_vault_id: "+ vault id", baseline_plus_embedding: `+ ${lift.config.embedding_dims}-dim FastRP embedding` };
  setText("gds-sub", `Louvain over co-failure edges, betweenness on a part graph, FastRP embeddings over ${fmt.int(G.embeddings.drives_embedded)} drives, and a lift test that re-runs the sibling project's failure model on its own leak-free split (train ${lift.config.train[0]} to ${lift.config.train[1]}, test ${lift.config.test[0]} to ${lift.config.test[1]}) with the same parameters and ${lift.config.seeds.length} seeds per arm.`);
  setText("lift-sub", `Mean over seeds ${lift.config.seeds.join(", ")}. The sibling project reported ${fmt.fixed(lift.config.project1_reported_lightgbm_test.pr_auc, 4)} at its seed; the baseline arm reproduces it.`);
  const ARM_SHORT = { baseline: "baseline", baseline_plus_vault_id: "+ vault id", baseline_plus_embedding: "+ embedding" };
  function liftChart(el, metric, label, fmtv) {
    const points = [];
    ARM.forEach((a) => arms[a].per_seed.forEach((p) => points.push({ x: ARM_SHORT[a], y: p[metric], seed: p.seed, arm: ARM_LABEL[a] })));
    return new Chart(el, {
      type: "bar",
      data: {
        labels: ARM.map((a) => ARM_SHORT[a]),
        datasets: [
          S.barDataset(label, ARM.map((a) => arms[a].mean[metric]), 0, { maxBarThickness: 64 }),
          { type: "scatter", label: "each seed", data: points, backgroundColor: S.cssVar("--ink"), borderColor: S.cssVar("--surface"), borderWidth: 1.5, pointRadius: 4.5, pointHoverRadius: 6, showLine: false },
        ],
      },
      options: S.baseOptions({
        interaction: { mode: "nearest", intersect: true },
        plugins: { tooltip: { callbacks: {
          title: (items) => items.length ? (items[0].raw && items[0].raw.arm ? items[0].raw.arm : ARM_LABEL[ARM[items[0].dataIndex]]) : "",
          label: (c) => (c.raw && c.raw.seed != null ? `seed ${c.raw.seed}: ${fmtv(c.parsed.y)}` : `${c.dataset.label}: ${fmtv(c.parsed.y)}`),
        } } },
        scales: { x: { offset: true, ticks: { maxRotation: 0, autoSkip: false } }, y: { ticks: { callback: (v) => fmtv(v) } } },
      }),
    });
  }
  S.chart("c-lift", (el) => liftChart(el, "pr_auc", "mean PR-AUC", (v) => fmt.fixed(v, 3)));
  S.chart("c-lift-recall", (el) => liftChart(el, "recall_top_0.01", "mean recall in top 1%", (v) => fmt.pct(v, 1)));
  const liftRows = [];
  for (const a of ARM) for (const p of arms[a].per_seed) liftRows.push(Object.assign({ arm: ARM_LABEL[a] }, p));
  setHTML("lift-table", table([
    { key: "arm", label: "Arm" },
    { key: "seed", label: "Seed", cls: "n" },
    { key: "pr_auc", label: "PR-AUC", fmt: (v) => fmt.fixed(v, 4), cls: "n" },
    { key: "recall_top_0.001", label: "Recall top 0.1%", fmt: (v) => fmt.pct(v, 1), cls: "n" },
    { key: "recall_top_0.01", label: "Recall top 1%", fmt: (v) => fmt.pct(v, 1), cls: "n" },
    { key: "brier_calibrated", label: "Brier", fmt: (v) => fmt.fixed(v, 6), cls: "n" },
    { key: "seconds", label: "Seconds", fmt: (v) => fmt.num(v, 1), cls: "n" },
  ], liftRows));
  const lv = lift.lift.baseline_plus_vault_id, le = lift.lift.baseline_plus_embedding;
  setText("lift-verdict", `No lift. PR-AUC change against the baseline: vault id ${fmt.num(lv.pr_auc, 4)}, embedding ${fmt.num(le.pr_auc, 4)} (seed std up to ${fmt.num(Math.max(arms.baseline.std.pr_auc, arms.baseline_plus_vault_id.std.pr_auc, arms.baseline_plus_embedding.std.pr_auc), 4)}); recall in the top 1% changes by ${fmt.num(100 * lv["recall_top_0.01"], 1)} and ${fmt.num(100 * le["recall_top_0.01"], 1)} points. Verdict recorded in the report: "${lift.verdict}". Location identity overfits the training dates; the graph adds no signal the SMART attributes lack at this horizon.`);

  const C = G.communities;
  setHTML("community-tiles", tiles([
    { v: fmt.int(C.louvain_communities), l: "Louvain communities", s: `modularity ${fmt.fixed(C.modularity, 3)}` },
    { v: fmt.int(C.failed_drives_in_a_community_of_2_or_more) + " / " + fmt.int(C.failed_drives_total), l: "failed drives in a community" },
    { v: fmt.int(C.communities_spanning_one_vault), l: "communities inside one vault" },
    { v: fmt.int(C.communities_with_one_drive_model), l: "communities of one drive model" },
  ]));
  setHTML("community-table", table([
    { key: "community", label: "Community", cls: "n" },
    { key: "drives", label: "Drives", fmt: fmt.int, cls: "n" },
    { key: "sites", label: "Site" },
    { key: "vaults", label: "Vaults" },
    { key: "models", label: "Drive models", fmt: (v) => String(v).split(";").length + " model" + (String(v).split(";").length === 1 ? "" : "s") },
    { key: "first_failure", label: "First failure" },
    { key: "last_failure", label: "Last failure" },
  ], G.largest));
  const cen = G.centrality;
  setText("centrality-src", `reports/gds_summary.json, centrality: ${cen.nodes} nodes, ${cen.relationships} relationships, ${cen.parts} parts, ${cen.single_supplier_parts} single-sourced.`);
  S.chart("c-centrality", (el) => new Chart(el, {
    type: "bar",
    data: { labels: cen.top_by_betweenness.map((p) => p.part_number), datasets: [S.barDataset("betweenness", cen.top_by_betweenness.map((p) => p.betweenness), 0)] },
    options: S.baseOptions({
      indexAxis: "y",
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => { const p = cen.top_by_betweenness[c.dataIndex]; return `betweenness ${fmt.num(p.betweenness, 2)}, ${p.suppliers} suppliers, ${p.products} products, ${p.dispatches} dispatches`; } } } },
      scales: { x: { grid: { color: S.cssVar("--grid") }, border: { display: false } }, y: { grid: { display: false }, border: { color: S.cssVar("--axis") } } },
    }),
  }));

  /* ---------------- NL2Cypher ---------------- */
  const N = D.nl2cypher;
  const backends = Object.keys(N.easy_summary);
  S.chart("c-nl", (el) => new Chart(el, {
    type: "bar",
    data: {
      labels: [`base set (${N.easy_summary[backends[0]].questions} questions)`, `hard set (${N.hard_summary[backends[0]].questions} questions)`],
      datasets: backends.map((b, i) => S.barDataset(BACKEND_LABEL(b), [N.easy_summary[b].execution_accuracy, N.hard_summary[b].execution_accuracy], i, { maxBarThickness: 64 })),
    },
    options: S.baseOptions({
      plugins: { tooltip: { callbacks: { label: (c) => { const set = c.dataIndex === 0 ? N.easy_summary : N.hard_summary; const b = backends[c.datasetIndex]; return `${c.dataset.label}: ${set[b].correct}/${set[b].questions} (${fmt.pct(c.parsed.y, 0)})`; } } } },
      scales: { y: { max: 1, ticks: { callback: (v) => fmt.pct(v, 0) } } },
    }),
  }));
  const sumRows = [];
  for (const [name, set] of [["base", N.easy_summary], ["hard", N.hard_summary]]) for (const b of backends) sumRows.push(Object.assign({ set: name, backend: BACKEND_LABEL(b) }, set[b]));
  setHTML("nl-summary-table", table([
    { key: "set", label: "Set" },
    { key: "backend", label: "Backend" },
    { key: "model", label: "Model" },
    { key: (r) => `${r.correct}/${r.questions}`, label: "Correct", cls: "n" },
    { key: "executed", label: "Ran", cls: "n" },
    { key: "cached_calls", label: "Cached calls", cls: "n" },
    { key: "model_calls", label: "Model calls", cls: "n" },
  ], sumRows));
  const hardWrong = N.hard.filter((r) => !r.correct);
  const ranWrong = hardWrong.filter((r) => r.executed).length;
  setText("nl-note", `${hardWrong.length} misses on the hard set, ${ranWrong} of which ran and returned a plausible wrong row. The loop retries only on a database error, so it cannot see those. A guard refuses writes before execution and the session runs in READ access mode.`);
  function perQuestion(rows) {
    const byQ = {};
    for (const r of rows) { byQ[r.question_id] = byQ[r.question_id] || { question_id: r.question_id, question: r.question }; byQ[r.question_id][r.backend] = r; }
    const status = (r) => !r ? "" : r.correct ? (r.attempts > 1 ? `<span class="pass">right</span> after ${r.attempts} attempts` : '<span class="pass">right</span>') : r.executed ? '<span class="fail">ran, wrong</span>' : '<span class="fail">error</span>';
    return table([
      { key: "question_id", label: "Id" },
      { key: "question", label: "Question", cls: "text" },
    ].concat(backends.map((b) => ({ key: (r) => status(r[b]), label: BACKEND_LABEL(b), raw: true }))), Object.values(byQ));
  }
  setHTML("nl-hard-table", perQuestion(N.hard));
  setHTML("nl-easy-table", perQuestion(N.easy));

  /* ---------------- Trust ---------------- */
  const T = D.trust;
  setText("trust-sub", `${T.now.checks} checks per run against trust/slas.yaml, on the lake (the sibling project's files) and the warehouse (its Postgres): completeness, freshness and schema drift per data product. Two clocks: the real one, and ${T.asof.as_of.slice(0, 10)}, the day after the quarter of telemetry ended.`);
  setText("trust-now-title", `Against the real clock (${T.now.as_of.slice(0, 16).replace("T", " ")} UTC)`);
  setText("trust-asof-title", `As of ${T.asof.as_of.slice(0, 10)}`);
  function trustBlock(prefix, run) {
    setHTML(prefix + "-tiles", tiles([
      { v: fmt.int(run.by_status.pass), l: "pass" },
      { v: fmt.int(run.by_status.fail), l: "fail" },
      { v: fmt.int(run.checks), l: "checks" },
    ]));
    const rows = Object.keys(run.summary).map((dp) => Object.assign({ data_product: dp }, run.summary[dp]));
    setHTML(prefix + "-table", table([
      { key: "data_product", label: "Data product" },
      { key: "status", label: "Status", raw: true, fmt: (v) => `<span class="${v === "pass" ? "pass" : "fail"}">${esc(v)}</span>` },
      { key: "checks", label: "Checks", cls: "n" },
      { key: "failed", label: "Failed", cls: "n" },
      { key: "failed_checks", label: "Failed checks", fmt: (v) => (v || []).join(", ") },
      { key: "newest_event", label: "Newest event", fmt: (v) => String(v || "").slice(0, 10) },
    ], rows) + table([
      { key: "kind", label: "Check kind", fmt: (v) => (v === "summary" ? "data-product summary" : v.replace(/_/g, " ")) },
      { key: "pass", label: "Pass", cls: "n" },
      { key: "fail", label: "Fail", cls: "n" },
    ], run.by_kind));
  }
  trustBlock("trust-now", T.now);
  trustBlock("trust-asof", T.asof);
  setHTML("trust-failed-table", T.now.failed.length ? table([
    { key: "data_product", label: "Data product" },
    { key: "check", label: "Check" },
    { key: "table", label: "Table or column", fmt: (v) => v || "" },
    { key: "threshold", label: "SLA", fmt: (v, r) => (r.check === "freshness" ? `${fmt.int(v)} h` : String(v)), cls: "n" },
    { key: "value", label: "Observed", fmt: (v, r) => (v == null ? "" : r.check === "freshness" ? `${fmt.num(v, 1)} h old` : String(v)), cls: "n" },
  ], T.now.failed) : "<p class='note'>none</p>");

  /* ---------------- Lineage example ---------------- */
  const X = D.lineage_example;
  setText("lineage-cmd", X.command);
  setText("lineage-output", X.output);
  setText("lineage-reading", X.reading.replace(/`/g, ""));
  setText("lineage-query", X.column_query);
})();
