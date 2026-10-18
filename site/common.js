/* Shared helpers for the static report sites. Plain JS, no build step.
   Charts are Chart.js (pinned in index.html). Colors come from CSS tokens so
   light and dark both work; charts are rebuilt when the scheme changes. */
(function () {
  "use strict";

  const builders = [];
  const charts = [];

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5"];
  function seriesColor(i) { return cssVar(SERIES[i % SERIES.length]); }

  function baseOptions(extra) {
    const ink2 = cssVar("--ink-2"), grid = cssVar("--grid"), axis = cssVar("--axis"), surface = cssVar("--surface"), ink = cssVar("--ink");
    const base = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, position: "top", align: "start", labels: { color: ink2, boxWidth: 10, boxHeight: 10, usePointStyle: false, padding: 14, font: { size: 12.5 } } },
        tooltip: {
          backgroundColor: surface, titleColor: ink, bodyColor: ink2, borderColor: axis, borderWidth: 1,
          padding: 10, displayColors: true, boxWidth: 8, boxHeight: 8, boxPadding: 4,
        },
      },
      scales: {
        x: { grid: { display: false }, border: { color: axis }, ticks: { color: ink2, font: { size: 12 } } },
        y: { grid: { color: grid }, border: { display: false }, ticks: { color: ink2, font: { size: 12 } }, beginAtZero: true },
      },
    };
    return deepMerge(base, extra || {});
  }

  function deepMerge(a, b) {
    const out = Array.isArray(a) ? a.slice() : Object.assign({}, a);
    for (const k of Object.keys(b)) {
      if (b[k] && typeof b[k] === "object" && !Array.isArray(b[k]) && a[k] && typeof a[k] === "object" && !Array.isArray(a[k])) {
        out[k] = deepMerge(a[k], b[k]);
      } else {
        out[k] = b[k];
      }
    }
    return out;
  }

  /* Register a chart builder: fn(canvas, Chart) must return a Chart instance.
     Builders are re-run when the color scheme changes. */
  function chart(canvasId, fn) {
    const el = document.getElementById(canvasId);
    if (!el) return;
    builders.push({ el, fn });
    charts.push(fn(el));
  }

  function rebuildAll() {
    charts.forEach((c) => { try { c.destroy(); } catch (e) { /* ignore */ } });
    charts.length = 0;
    builders.forEach(({ el, fn }) => charts.push(fn(el)));
  }

  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", rebuildAll);

  function barDataset(label, data, i, extra) {
    return Object.assign({
      label, data, backgroundColor: seriesColor(i), borderColor: seriesColor(i), borderWidth: 0,
      borderRadius: 4, borderSkipped: "start", maxBarThickness: 36, categoryPercentage: 0.72, barPercentage: 0.9,
    }, extra || {});
  }

  function lineDataset(label, data, i, extra) {
    return Object.assign({
      label, data, borderColor: seriesColor(i), backgroundColor: seriesColor(i), borderWidth: 2,
      pointRadius: 0, pointHoverRadius: 4, tension: 0, fill: false,
    }, extra || {});
  }

  /* Formatting */
  const fmt = {
    pct: (x, d) => (x == null || x === "" ? "" : (100 * x).toFixed(d == null ? 1 : d) + "%"),
    num: (x, d) => (x == null || x === "" ? "" : Number(x).toLocaleString("en-US", { maximumFractionDigits: d == null ? 2 : d, minimumFractionDigits: d == null ? 0 : d })),
    int: (x) => (x == null || x === "" ? "" : Number(x).toLocaleString("en-US")),
    usd: (x, d) => (x == null || x === "" ? "" : "$" + Number(x).toFixed(d == null ? 4 : d)),
    fixed: (x, d) => (x == null || x === "" ? "" : Number(x).toFixed(d)),
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  /* table(columns, rows): columns = [{key, label, fmt, cls}] */
  function table(cols, rows, opts) {
    opts = opts || {};
    let h = '<div class="tablewrap"><table><thead><tr>';
    for (const c of cols) h += `<th class="${c.cls || ""}">${esc(c.label)}</th>`;
    h += "</tr></thead><tbody>";
    for (const r of rows) {
      const rowCls = opts.rowClass ? opts.rowClass(r) : "";
      h += `<tr class="${rowCls}">`;
      for (const c of cols) {
        const v = typeof c.key === "function" ? c.key(r) : r[c.key];
        const txt = c.fmt ? c.fmt(v, r) : v;
        h += `<td class="${c.cls || ""}">${c.raw ? txt : esc(txt)}</td>`;
      }
      h += "</tr>";
    }
    h += "</tbody></table></div>";
    return h;
  }

  function tiles(items) {
    return '<div class="tiles">' + items.map((t) => `<div class="tile"><div class="v">${esc(t.v)}</div><div class="l">${esc(t.l)}${t.s ? " <small>" + esc(t.s) + "</small>" : ""}</div></div>`).join("") + "</div>";
  }

  function setHTML(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }
  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  async function loadJSON(path) {
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(path + " " + r.status);
    return r.json();
  }
  async function loadText(path) {
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(path + " " + r.status);
    return r.text();
  }

  window.Site = { cssVar, seriesColor, baseOptions, chart, barDataset, lineDataset, fmt, esc, table, tiles, setHTML, setText, loadJSON, loadText };
})();
