"use strict";

// Every number shown comes from the analysis JSON; every string from data goes in via textContent.
const SEARCH_DEBOUNCE_MS = 250;
const NARRATIVE_POLL_MS = 5000;
const NARRATIVE_MAX_MS = 180000;
const NETWORK_MESSAGE = "Data source unavailable, try again in a minute.";
const NARRATIVE_FAILED = "Written analysis unavailable right now; the figures above are complete.";
const REPO_URL = "https://github.com/emiliensabathier/equity-lab";
const VLAB_URL = "https://github.com/emiliensabathier/valuation-lab";
const NA = "n/a";
const MIN_QUERY = 2;
// Same shape as config.TICKER_PATTERN on the server.
const TICKER_RE = /^[A-Z0-9][A-Z0-9.-]{0,14}$/;
const SECTION_ERROR = "This section could not be displayed.";

const $ = (id) => document.getElementById(id);
const input = $("search");
const list = $("matches");
const statusEl = $("status");
const report = $("report");

let searchTimer = null;
let searchAbort = null;
let loadAbort = null;
let matches = [];
let active = -1;
let listFor = "";
let narrativeTimer = null;
let narrativeRun = 0;
let charts = [];

// ---------- formatting ----------

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

function pct(v) {
  return isNum(v) ? (v * 100).toFixed(1) + "%" : NA;
}

function num(v, digits = 2) {
  return isNum(v)
    ? v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : NA;
}

function mult(v) {
  return isNum(v) ? v.toFixed(1) + "x" : NA;
}

function compact(v, ccy) {
  if (!isNum(v)) return NA;
  const abs = Math.abs(v);
  const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  const hit = units.find(([size]) => abs >= size);
  const body = hit ? (v / hit[0]).toFixed(1) + hit[1] : v.toFixed(0);
  return ccy ? body + " " + ccy : body;
}

function money(v, ccy) {
  return isNum(v) ? num(v) + (ccy ? " " + ccy : "") : NA;
}

function day(iso) {
  return typeof iso === "string" && iso ? iso.slice(0, 10) : NA;
}

// ---------- DOM helpers ----------

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function card(title, ...body) {
  return el("section", {}, el("h2", {}, title), ...body);
}

function unavailable(section) {
  const reason = section && section.reason ? section.reason : "No data for this section.";
  return el("p", { class: "unavailable" }, "Not available: " + reason);
}

function table(caption, head, rows) {
  const thead = el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { scope: "col" }, h))));
  const tbody = el("tbody", {}, ...rows.map((r) => {
    const cells = r.cells.map((c, i) => (i === 0
      ? el("th", { scope: "row" }, c)
      : el("td", { class: r.hl === i ? "hl" : "" }, c)));
    return el("tr", { class: r.cls || "" }, ...cells);
  }));
  return el("div", { class: "scroll" },
    el("table", {}, caption ? el("caption", {}, caption) : null, thead, tbody));
}

function setStatus(text, isError) {
  statusEl.textContent = text;
  statusEl.className = isError ? "status error" : "status";
}

// ---------- search ----------

function setSearchStatus(text) {
  $("search-status").textContent = text;
}

function closeList() {
  list.hidden = true;
  list.replaceChildren();
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  matches = [];
  listFor = "";
  active = -1;
}

// Closing for any reason also cancels a pending or in-flight search so it cannot reopen the list.
function dismiss() {
  clearTimeout(searchTimer);
  if (searchAbort) searchAbort.abort();
  closeList();
}

function highlight(i) {
  active = i;
  [...list.children].forEach((li, idx) => li.setAttribute("aria-selected", String(idx === i)));
  if (i >= 0) input.setAttribute("aria-activedescendant", "m" + i);
  else input.removeAttribute("aria-activedescendant");
}

function showMatches(found, q) {
  closeList();
  if (!found.length) return;
  matches = found;
  listFor = q;
  found.forEach((m, i) => {
    const li = el("li", { role: "option", id: "m" + i, "aria-selected": "false" },
      `${m.name} · ${m.ticker} · ${m.exchange}`);
    li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(i); });
    list.append(li);
  });
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function choose(i) {
  const m = matches[i];
  if (!m) return;
  input.value = m.name;
  dismiss();
  setSearchStatus("");
  loadAnalysis(m.ticker);
}

async function runSearch(q, pickFirst = false) {
  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();
  const mine = searchAbort;
  // A response only counts while it is the latest request and still matches what is typed.
  const current = () => mine === searchAbort && !mine.signal.aborted && input.value.trim() === q;
  try {
    const res = await fetch("/api/search?q=" + encodeURIComponent(q), { signal: mine.signal });
    const body = await res.json();
    if (!current()) return;
    if (!res.ok) {
      setSearchStatus(typeof body.detail === "string" ? body.detail : NETWORK_MESSAGE);
      if (pickFirst) tryAsTicker(q);
      return;
    }
    const found = Array.isArray(body) ? body : [];
    showMatches(found, q);
    if (pickFirst && found.length) {
      choose(0);
      return;
    }
    setSearchStatus(found.length ? "" : "No match for this search.");
    if (pickFirst) tryAsTicker(q);
  } catch (err) {
    if (err.name === "AbortError" || !current()) return;
    setSearchStatus(NETWORK_MESSAGE);
    if (pickFirst) tryAsTicker(q);
  }
}

// Last resort for Enter: the search found nothing, so the text may be a ticker typed as is.
function tryAsTicker(q) {
  const ticker = q.toUpperCase();
  if (!TICKER_RE.test(ticker)) return;
  setSearchStatus("");
  loadAnalysis(ticker, q);
}

input.addEventListener("input", () => {
  clearTimeout(searchTimer);
  closeList(); // the old list belongs to an older query
  const q = input.value.trim();
  if (q.length < MIN_QUERY) {
    if (searchAbort) searchAbort.abort();
    setSearchStatus("");
    return;
  }
  searchTimer = setTimeout(() => runSearch(q), SEARCH_DEBOUNCE_MS);
});

function submit() {
  const q = input.value.trim();
  if (matches.length && listFor === q) {
    choose(active >= 0 ? active : 0);
  } else if (q.length >= MIN_QUERY) {
    // Search first: "apple" is a company name before it is a ticker.
    clearTimeout(searchTimer);
    runSearch(q, true);
  } else if (TICKER_RE.test(q.toUpperCase())) {
    tryAsTicker(q);
  }
}

input.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { dismiss(); return; }
  if (e.key === "Enter") { e.preventDefault(); submit(); return; }
  if (!matches.length) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    highlight((active + 1) % matches.length);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    highlight((active - 1 + matches.length) % matches.length);
  }
});

input.addEventListener("blur", () => setTimeout(dismiss, 100));

// ---------- loading ----------

function stopNarrative() {
  narrativeRun += 1;
  clearTimeout(narrativeTimer);
}

function destroyCharts() {
  charts.forEach((c) => c.destroy());
  charts = [];
}

async function loadAnalysis(ticker, typed) {
  stopNarrative();
  if (loadAbort) loadAbort.abort();
  loadAbort = new AbortController();
  const mine = loadAbort;
  destroyCharts();
  report.hidden = true;
  report.replaceChildren();
  setStatus("Analysing " + ticker + "… a first run can take up to a minute.", false);
  try {
    const res = await fetch("/api/analysis/" + encodeURIComponent(ticker), { signal: mine.signal });
    const body = await res.json();
    if (mine.signal.aborted) return;
    if (!res.ok) {
      const notFound = res.status === 404 && typed;
      setStatus(notFound ? `No company found for \u201c${typed}\u201d.`
        : typeof body.detail === "string" ? body.detail : NETWORK_MESSAGE, true);
      return;
    }
    input.value = body.name || String(body.ticker || ticker).toUpperCase();
    history.replaceState(null, "", "?t=" + encodeURIComponent(body.ticker || ticker));
    setStatus("", false);
    render(body);
  } catch (err) {
    if (err.name === "AbortError") return;
    setStatus(NETWORK_MESSAGE, true);
  }
}

// ---------- rendering ----------

function render(a) {
  report.replaceChildren();
  report.hidden = false;
  if (a.stale) {
    report.append(el("div", { class: "banner", role: "note" },
      `Showing the last saved analysis from ${day(a.as_of)}; live data is unavailable.`));
  }
  if (a.coverage === "not_covered") {
    report.append(el("section", { class: "verdict" },
      el("h2", {}, `${a.name} (${a.ticker})`),
      el("p", { class: "sentence" }, "Not covered by this tool."),
      el("p", {}, (a.limits && a.limits[0]) || "This company is outside the supported scope.")));
    guardedFooter(a);
    return;
  }
  report.append(guarded("Verdict", () => verdictCard(a)));
  report.append(guarded("Reverse DCF and DCF", () => dcfCard(a)));
  report.append(guarded("Multiples vs peers", () => multiplesCard(a.multiples, a)));
  report.append(guarded("Financial health", () => healthCard(a.health, a)));
  report.append(el("section", { class: "narrative" }, el("h2", {}, "Claude analysis"),
    el("p", { id: "narrative-body", class: "unavailable" }, "Analysis being written…")));
  report.append(guarded("Limits of this analysis", () => limitsCard(a.limits)));
  guardedFooter(a);
  pollNarrative(a.ticker);
}

// One malformed field must cost one card, never the narrative, limits or footer.
function guarded(title, build) {
  try {
    return build();
  } catch (err) {
    console.error("render failed:", title, err);
    return card(title, el("p", { class: "unavailable" }, SECTION_ERROR));
  }
}

function guardedFooter(a) {
  try {
    renderFooter(a);
  } catch (err) {
    console.error("footer failed:", err);
  }
}

function kpi(label, value) {
  return el("div", { class: "kpi" }, el("dt", {}, label), el("dd", {}, value));
}

function verdictCard(a) {
  const sentence = a.verdict
    || "The reverse DCF could not be computed for this company; see the sections below.";
  return el("section", { class: "verdict" },
    el("p", { class: "sentence" }, sentence),
    el("p", { class: "name" }, `${a.name} · ${a.ticker} · ${a.exchange}`),
    el("dl", { class: "kpis" },
      kpi("Price", money(a.price, a.trading_currency)),
      kpi("Price date", day(a.price_date)),
      kpi("Sector", a.sector || NA),
      kpi("Reporting currency", a.reporting_currency || NA)));
}

function dcfCard(a) {
  const d = a.dcf;
  if (!d || d.status !== "ok") return card("Reverse DCF and DCF", unavailable(d));
  const ccy = a.trading_currency;
  const parts = [
    el("dl", { class: "kpis" },
      kpi("Price", money(a.price, ccy)),
      kpi("DCF value per share", money(d.value_per_share, ccy)),
      kpi("Implied growth, 5-yr average", pct(d.implied_average_growth)),
      kpi("Normalized history", pct(d.normalized_growth))),
    el("p", { class: "note" },
      "The reverse DCF solves for the revenue growth that makes the DCF equal the price. "
      + "The DCF value uses the company's own normalized history."),
    el("h3", {}, "WACC bridge"),
    table(null, ["Component", "Value"], [
      { cells: ["Beta vs " + (d.beta_index || "index"), num(d.beta)] },
      { cells: ["Cost of equity", pct(d.cost_of_equity)] },
      { cells: ["Cost of debt (pre-tax)", pct(d.cost_of_debt)] },
      { cells: ["Equity weight", pct(d.equity_weight)] },
      { cells: ["Debt weight", pct(d.debt_weight)] },
      { cells: ["WACC", pct(d.wacc)], cls: "median" },
    ]),
    el("h3", {}, "Implied growth at a lower discount rate"),
  ];
  const lower = d.implied_average_growth_at_lower_wacc || {};
  const steps = Object.keys(lower).sort((x, y) => Number(x) - Number(y));
  parts.push(table(null, ["WACC", "Implied 5-yr average growth"], [
    { cells: [`${pct(d.wacc)} (base)`, pct(d.implied_average_growth)] },
    ...steps.map((s) => ({
      cells: [`${pct(d.wacc - Number(s))} (WACC - ${(Number(s) * 100).toFixed(0)} pt)`,
        pct(lower[s])],
    })),
  ]));
  parts.push(el("p", { class: "note" },
    `Terminal value is ${pct(d.terminal_share)} of the DCF; implied exit multiple `
    + `${mult(d.exit_multiple)}.`));
  parts.push(el("h3", {}, "Sensitivity"));
  parts.push(gridTable(d.sensitivity,
    `Value per share (${ccy}): WACC (rows) vs terminal growth (columns)`, "WACC"));
  parts.push(gridTable(d.margin_sensitivity,
    `Value per share (${ccy}): EBIT margin (rows) vs revenue growth (columns)`, "EBIT margin"));
  return card("Reverse DCF and DCF", ...parts);
}

function gridTable(g, caption, corner) {
  if (!g || !Array.isArray(g.rows) || !Array.isArray(g.cols) || !Array.isArray(g.values)) {
    return el("p", { class: "unavailable" }, "Not available: grid missing.");
  }
  const midRow = Math.floor(g.rows.length / 2);
  const midCol = Math.floor(g.cols.length / 2);
  const rows = g.rows.map((r, i) => ({
    cells: [pct(r), ...g.values[i].map((v) => num(v))],
    hl: i === midRow ? midCol + 1 : undefined,
  }));
  return table(caption, [corner, ...g.cols.map((c) => pct(c))], rows);
}

function multiplesCard(m, a) {
  if (!m || m.status !== "ok") return card("Multiples vs peers", unavailable(m));
  const row = (label, o, cls, ccy) => ({
    cls,
    cells: [label, compact(o.market_cap, ccy), mult(o.ev_ebitda), mult(o.ev_sales), mult(o.pe)],
  });
  const rows = [
    row(a.ticker + " (this company)", m.company || {}, "company", a.trading_currency),
    ...(m.peers || []).map((p) => row(`${p.name || p.ticker} · ${p.ticker}`, p)),
    row("Peer median", m.median || {}, "median"),
  ];
  return card("Multiples vs peers",
    el("p", { class: "note" }, `Peers chosen by ${m.basis || "industry"}, nearest by market cap.`),
    table(null, ["Company", "Mkt cap", "EV/EBITDA", "EV/Sales", "P/E"], rows),
    m.note ? el("p", { class: "note" }, m.note) : null);
}

function chartColors() {
  const css = getComputedStyle(document.documentElement);
  const get = (n) => css.getPropertyValue(n).trim();
  return { bar: get("--bar"), line: get("--accent"), grid: get("--grid"), text: get("--muted") };
}

function chartBox(title, type, years, key, fmt) {
  const summary = years.map((y) => `${y.fiscal_year.slice(0, 4)}: ${fmt(y[key])}`).join(", ");
  const canvas = el("canvas", { role: "img", "aria-label": `${title}. ${summary}` });
  const node = el("div", {}, el("p", { class: "chart-title" }, title),
    el("div", { class: "chart-box" }, canvas));
  const draw = () => {
    if (typeof Chart === "undefined" || !canvas.isConnected) return;
    const col = chartColors();
    charts.push(new Chart(canvas, {
      type,
      data: {
        labels: years.map((y) => y.fiscal_year.slice(0, 4)),
        datasets: [{
          data: years.map((y) => (isNum(y[key]) ? y[key] : null)),
          backgroundColor: col.bar,
          borderColor: col.line,
          borderWidth: type === "line" ? 2 : 0,
          pointRadius: 3,
        }],
      },
      options: {
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (c) => fmt(c.parsed.y) } },
        },
        scales: {
          x: { ticks: { color: col.text }, grid: { display: false } },
          y: { ticks: { color: col.text, callback: (v) => fmt(v) }, grid: { color: col.grid } },
        },
      },
    }));
  };
  return { node, draw };
}

function healthTable(years, ccy) {
  return table(`Reporting currency: ${ccy}`,
    ["Fiscal year", ...years.map((y) => y.fiscal_year.slice(0, 4))], [
      { cells: ["Revenue", ...years.map((y) => compact(y.revenue))] },
      { cells: ["EBIT margin", ...years.map((y) => pct(y.ebit_margin))] },
      { cells: ["Free cash flow", ...years.map((y) => compact(y.fcf))] },
      { cells: ["Net debt / EBITDA", ...years.map((y) => mult(y.net_debt_to_ebitda))] },
    ]);
}

function healthCard(h, a) {
  if (!h || h.status !== "ok") return card("Financial health", unavailable(h));
  const years = h.years || [];
  const ccy = a.reporting_currency;
  // Without Chart.js (CDN blocked) the table carries the same figures.
  if (typeof Chart === "undefined") return card("Financial health", healthTable(years, ccy));
  const boxes = [
    chartBox(`Revenue (${ccy})`, "bar", years, "revenue", (v) => compact(v)),
    chartBox("EBIT margin", "line", years, "ebit_margin", (v) => pct(v)),
    chartBox(`Free cash flow (${ccy})`, "bar", years, "fcf", (v) => compact(v)),
  ];
  const leverage = table(null, ["Fiscal year", ...years.map((y) => y.fiscal_year.slice(0, 4))],
    [{ cells: ["Net debt / EBITDA", ...years.map((y) => mult(y.net_debt_to_ebitda))] }]);
  const node = card("Financial health",
    el("div", { class: "charts" }, ...boxes.map((b) => b.node)),
    el("h3", {}, "Leverage"), leverage);
  // Chart.js needs the canvases attached to the document before it can size them.
  requestAnimationFrame(() => {
    try {
      boxes.forEach((b) => b.draw());
    } catch (err) {
      console.error("charts failed:", err);
      destroyCharts();
      node.replaceChildren(el("h2", {}, "Financial health"), healthTable(years, ccy));
    }
  });
  return node;
}

function limitsCard(limits) {
  const items = Array.isArray(limits) ? limits : [];
  return card("Limits of this analysis",
    el("ul", { class: "limits" }, ...items.map((t) => el("li", {}, t))));
}

function renderFooter(a) {
  $("footer").replaceChildren(
    `Data: Yahoo Finance via yfinance, as of ${day(a.as_of)}. `,
    el("a", { href: REPO_URL, rel: "noopener" }, "equity-lab on GitHub"), " · ",
    el("a", { href: VLAB_URL, rel: "noopener" }, "valuation-lab"),
    " · Not investment advice.");
}

// ---------- narrative ----------

function narrativeText(text) {
  const body = $("narrative-body");
  if (!body) return;
  const box = body.parentNode;
  body.remove();
  String(text).split(/\n{2,}/).filter((p) => p.trim())
    .forEach((p) => box.append(el("p", {}, p.trim())));
}

function narrativeMessage(text) {
  const body = $("narrative-body");
  if (body) body.textContent = text;
}

function pollNarrative(ticker) {
  stopNarrative();
  const run = narrativeRun;
  const started = Date.now();
  let blips = 0;
  const later = () => { narrativeTimer = setTimeout(tick, NARRATIVE_POLL_MS); };
  const tick = async () => {
    if (run !== narrativeRun) return;
    let res;
    let body;
    try {
      res = await fetch(`/api/analysis/${encodeURIComponent(ticker)}/narrative`);
      body = await res.json();
    } catch (err) {
      // One transient network error is retried on the next tick; a second in a row gives up.
      blips += 1;
      if (run !== narrativeRun) return;
      if (blips > 1 || Date.now() - started >= NARRATIVE_MAX_MS) narrativeMessage(NARRATIVE_FAILED);
      else later();
      return;
    }
    blips = 0;
    if (run !== narrativeRun) return;
    if (!res.ok) {
      narrativeMessage(res.status === 429 && typeof body.detail === "string"
        ? body.detail : NARRATIVE_FAILED);
      return;
    }
    if (body.status === "ready") { narrativeText(body.text || ""); return; }
    if (body.status === "unavailable" || Date.now() - started >= NARRATIVE_MAX_MS) {
      narrativeMessage(NARRATIVE_FAILED);
      return;
    }
    later();
  };
  tick();
}

// ---------- boot ----------

$("footer").replaceChildren("Data: Yahoo Finance via yfinance. ",
  el("a", { href: REPO_URL, rel: "noopener" }, "equity-lab on GitHub"), " · ",
  el("a", { href: VLAB_URL, rel: "noopener" }, "valuation-lab"));

const initial = new URLSearchParams(location.search).get("t");
if (initial !== null) {
  const ticker = initial.trim().toUpperCase();
  if (TICKER_RE.test(ticker)) {
    input.value = ticker;
    loadAnalysis(ticker);
  } else {
    setStatus("The ticker in this link is not valid.", true);
  }
}
