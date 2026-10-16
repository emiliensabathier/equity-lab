# equity-lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public site where a recruiter types any US or European listed company and gets a reverse-DCF-led equity analysis, with a Claude-written commentary produced through the subscription tunnel.

**Architecture:** FastAPI app on the VPS. `elab` wraps `valuation-lab`'s engine (`vlab`) for arbitrary tickers fetched from yfinance; each page section (DCF, multiples, health) computes or fails independently. Results are cached in SQLite; the narrative is produced by a single background worker calling `claude -p`, and the static page polls for it.

**Tech Stack:** Python ≥3.11, FastAPI + uvicorn, yfinance, pandas, `vlab` (git dependency), SQLite (stdlib), vanilla HTML/JS + Chart.js 4.4.1 from cdnjs, pytest + pytest-cov, ruff (line length 100, rules E F I UP B, same as `vlab`).

**Spec:** `docs/superpowers/specs/2026-10-17-equity-lab-design.md`

## Global Constraints

- No paid API, no `ANTHROPIC_API_KEY`. Claude only via `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN`; strip `ANTHROPIC_API_KEY` from the child environment.
- English UI and copy. No "buy", "sell", "undervalued", "overvalued", "target" anywhere on the page or in the narrative.
- Numbers on the page come only from the engine JSON, never parsed from Claude's text.
- Every defaulted or missing input appears in `limits` and on the page.
- Banks, insurers, REITs → `coverage: "not_covered"` with reason (yfinance `sector` in `{"Financial Services", "Real Estate"}`).
- Reporting currency and trading currency are always distinct fields; never compare across them without conversion.
- No network in tests or CI: every fetcher and the Claude runner are injected.
- No raw third-party data committed (statements, prices, constituent lists). Hand-built fixtures only.
- 80% coverage on `src/elab`. Conventional commits, ending with the `Co-Authored-By` trailer from the session.
- Heavy work on the VPS: one engine run at a time, one Claude job at a time; `OPENBLAS_NUM_THREADS=OMP_NUM_THREADS=MKL_NUM_THREADS=1` in every unit file.
- `valuation-lab`'s own tests and published numbers must stay unchanged.

## Review Focus

1. **LSE prices in pence** (`currency == "GBp"`) against statements in GBP → a 100× value error. Expect price divided by 100 and the company treated as GBP-traded. Test in Task 3.
2. **Debt-free, lease-free company** (common in US tech) → `_lease_payment_ratio` divides by zero debt, returns no ratios and raises. Expect lease ratio 0 and a valuation. Test in Task 1.
3. **yfinance throttled or down mid-request** → expect the affected section `unavailable` with the reason, the last cached result served if one exists (marked stale), never a 500. Test in Task 7.
4. **Claude CLI missing, timing out or token revoked** → expect numbers shown, narrative `unavailable`, worker keeps serving the next job. Test in Task 6.
5. **Two recruiters hit the same cold ticker at once** → expect one engine run and one queued narrative job, both requests served the same result. Test in Task 7.

---

## File Structure

```
equity-lab/
  pyproject.toml            package "elab", deps incl. vlab @ git+...@<commit>
  src/elab/
    config.py               constants: risk-free table, index map, exchanges, TTLs, limits
    resolve.py              name search, Company + metadata from yfinance info
    statements.py           core/defaultable line handling
    engine.py               analyze(): Analysis JSON (dcf, health, limits, verdict)
    peers.py                constituent lists, weekly peer table, peers_for()
    narrative.py            prompt, number post-check, Claude runner, single-worker queue
    cache.py                SQLite key/value with TTL
    api.py                  FastAPI factory, rate limit, per-key locks, stale fallback
    __main__.py             CLI: serve | build-peers | pregenerate
  static/index.html, app.js, style.css
  deploy/                   systemd units + nginx site
  tests/
  .github/workflows/ci.yml
  README.md
```

---

### Task 1: Upstream — expose `value_company` and accept lease-free companies in `vlab`

Repo: `valuation-lab`.

**Files:**
- Modify: `src/vlab/fundamentals.py` (`_lease_payment_ratio`)
- Modify: `src/vlab/pipeline.py` (rename `_value_company` → `value_company`, update caller in `run`)
- Test: `tests/test_fundamentals.py`, `tests/test_pipeline.py`

**Interfaces:**
- Produces:
  - `vlab.pipeline.value_company(company, prices, market_prices, *, cache_dir, refresh, statement_fetcher, price_fetcher, risk_free: float = RISK_FREE) -> CompanyResult` (today's `_value_company` plus `risk_free`).
  - `vlab.wacc.compute_wacc(..., risk_free: float = RISK_FREE)` — used instead of the module constant.

- [ ] **Step 1: Write the failing tests**
  - `test_lease_ratio_is_zero_when_no_leases_and_no_debt` in `tests/test_fundamentals.py`: statements built with the existing fixture helpers where `Capital Lease Obligations`, `Current Capital Lease Obligation` and `Total Debt` are 0 in every year; assert `drivers_from(...).lease_ratio == 0.0` and no exception.
  - `test_compute_wacc_uses_supplied_risk_free` in `tests/test_wacc.py`: same inputs, `risk_free=0.04` vs default → `cost_of_equity` differs by exactly 0.01.
- [ ] **Step 2: Run** `pytest tests/test_fundamentals.py tests/test_wacc.py -v` → FAIL (`ValuationError: ... cannot infer lease payments`; unexpected keyword `risk_free`).
- [ ] **Step 3: Implement** in `_lease_payment_ratio`: if every value of `leases` and `current_leases` is 0, return `0.0` before the division. Add `risk_free` to `compute_wacc` and thread it through `value_company`. Rename `_value_company` to `value_company`; keep the docstring.
- [ ] **Step 4: Run** full suite `pytest` → all pass, including `test_regression.py` and `test_report_matches_fixture.py` (published numbers unchanged).
- [ ] **Step 5: Commit** `fix: value lease-free, debt-free companies; expose value_company`. Push only after the user confirms (public repo). Record the commit hash for Task 2.

---

### Task 2: Scaffold `equity-lab`, config, and statement line handling

**Files:**
- Create: `pyproject.toml`, `src/elab/__init__.py`, `src/elab/config.py`, `src/elab/statements.py`, `.gitignore` (cache/, *.sqlite, .env), `LICENSE` (same as valuation-lab)
- Test: `tests/test_statements.py`, `tests/conftest.py` (fixture builder `make_statements(**overrides) -> vlab.data.loader.Statements` with 4 annual columns, all 17 lines vlab reads, plausible values)

**Interfaces:**
- Consumes: `vlab.data.loader.Statements`.
- Produces:
  - `config.CORE_LINES: dict[str, tuple[str, ...]]` — keyed by frame (`"income"`, `"cashflow"`, `"balance"`): income `Total Revenue, EBIT, Pretax Income, Tax Provision`; cashflow `Capital Expenditure, Depreciation And Amortization`; balance `Total Debt, Cash Cash Equivalents And Short Term Investments, Ordinary Shares Number`. (Spec lists revenue, EBIT, shares, debt, cash; pretax, tax, capex, D&A are added as core because zero would inflate the value — Claude's call.)
  - `config.DEFAULTABLE_LINES: dict[str, tuple[str, ...]]` — income `Interest Expense`; cashflow `Sale Of PPE`; balance `Minority Interest, Capital Lease Obligations, Current Capital Lease Obligation, Inventory, Accounts Receivable, Accounts Payable`.
  - `statements.MissingCoreLine(DataError)` with attribute `line: str`.
  - `statements.fill_defaults(s: Statements) -> tuple[Statements, tuple[str, ...]]` — returns new `Statements` (inputs untouched) with absent or all-NaN defaultable lines added as 0.0 rows, and the tuple of filled line names in config order; raises `MissingCoreLine` on the first absent core line.
  - `statements.limit_sentence(line: str) -> str` → exactly `f"{line} not reported: treated as zero."`

- [ ] **Step 1: Write failing tests**
  - `test_fill_defaults_adds_zero_rows_for_missing_defaultable_lines`: drop `Inventory` and `Sale Of PPE`; assert both rows exist and equal 0.0 in every column, returned names `("Sale Of PPE", "Inventory")`.
  - `test_fill_defaults_leaves_input_unmodified`: the original frame still lacks `Inventory`.
  - `test_fill_defaults_raises_on_missing_core_line`: drop `EBIT` → `MissingCoreLine` with `.line == "EBIT"`.
  - `test_fill_defaults_treats_all_nan_row_as_missing`.
  - `test_filled_statements_pass_vlab_drivers_from`: drop all defaultable lines, `drivers_from(filled, "X")` returns `Drivers` with `lease_ratio == 0.0` (needs Task 1's vlab).
- [ ] **Step 2: Run** `pytest tests/test_statements.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `fill_defaults` with `pd.concat` on copies; `pyproject.toml` deps: `vlab @ git+https://github.com/emiliensabathier/valuation-lab@<Task 1 hash>`, `fastapi>=0.115`, `uvicorn>=0.30`, `yfinance>=0.2.40`, `pandas>=2.2`, `lxml>=5.0`; dev: `pytest>=8.0`, `pytest-cov>=5.0`, `ruff>=0.5`, `httpx>=0.27`.
- [ ] **Step 4: Run** `pytest tests/test_statements.py -v` → PASS; `ruff check .` clean.
- [ ] **Step 5: Commit** `git init`, `feat: scaffold equity-lab with statement line defaults`.

---

### Task 3: Resolve names and build a `Company`

**Files:**
- Create: `src/elab/resolve.py`; extend `src/elab/config.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: `vlab.universe.Company(ticker, name, reporting_currency, trading_currency)`.
- Produces:
  - `config.US_EXCHANGES = frozenset({"NMS", "NYQ", "NGM", "NCM", "ASE", "BTS", "PCX"})`
  - `config.INDEX_BY_SUFFIX: dict[str, str]` — `"": "^GSPC"`, `".PA": "^FCHI"`, `".DE": "^GDAXI"`, `".L": "^FTSE"`, `".SW": "^SSMI"`, `".AS": "^AEX"`, `".MC": "^IBEX"`, `".MI": "FTSEMIB.MI"`, `".ST": "^OMX"`, `".BR": "^BFX"`, `".CO": "^OMXC25"`, `".HE": "^OMXH25"`, `".LS": "PSI20.LS"`, `".VI": "^ATX"`, `".OL": "OBX.OL"`, `".IR": "^ISEQ"`. `config.FALLBACK_INDEX = "^STOXX"`. Before committing, check each symbol returns weekly closes from yfinance (one-off script in the scratchpad, not committed); drop any that do not and let them fall back.
  - `config.EXCLUDED_SECTORS = frozenset({"Financial Services", "Real Estate"})`
  - `resolve.Match(ticker: str, name: str, exchange: str)` frozen dataclass.
  - `resolve.search(query: str, searcher: Callable[[str], list[dict]]) -> list[Match]` — searcher defaults to a wrapper over `yf.Search(query, max_results=20).quotes`; keeps `quoteType == "EQUITY"` with exchange in `US_EXCHANGES` or symbol suffix in `INDEX_BY_SUFFIX`; max 8; query stripped, 2–60 chars else `[]`.
  - `resolve.CompanyMeta(company: Company, exchange: str, sector: str, industry: str, market_index: str, price_scale: float, excluded_reason: str | None)` frozen dataclass.
  - `resolve.company_from_info(ticker: str, info: dict) -> CompanyMeta` — `name` = `longName` or `shortName`; `trading_currency` = `info["currency"]`, except `"GBp"` → `"GBP"` with `price_scale = 0.01` (else 1.0); `reporting_currency` = `info["financialCurrency"]` or trading currency; `market_index` from suffix (US when no `.`), else fallback; `excluded_reason` = `f"{sector} companies are not covered: a free-cash-flow DCF does not apply to balance-sheet businesses."` when sector excluded. Raises `DataError` when `quoteType != "EQUITY"`.
  - `config.TICKER_PATTERN = r"^[A-Z0-9][A-Z0-9.\-]{0,14}$"`

- [ ] **Step 1: Write failing tests**
  - `test_search_keeps_us_and_european_equities_only` (fake searcher returning an ETF, a `.T` Tokyo equity, `TTE` NYQ, `TTE.PA` → `[TTE, TTE.PA]` with exchanges).
  - `test_search_rejects_too_short_query`.
  - `test_company_from_info_converts_pence_listing` (`currency "GBp"`, `financialCurrency "GBP"` → trading `GBP`, `price_scale 0.01`, index `^FTSE`).
  - `test_company_from_info_keeps_adr_currencies_distinct` (`SAP` US listing: currency USD, financialCurrency EUR → reporting EUR, trading USD, index `^GSPC`).
  - `test_company_from_info_flags_bank_as_not_covered`.
  - `test_unknown_suffix_falls_back_to_stoxx`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: resolve company names to tickers and currencies`.

---

### Task 4: Engine — DCF and health sections, verdict, limits

**Files:**
- Create: `src/elab/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `fill_defaults`, `limit_sentence`, `MissingCoreLine` (Task 2); `CompanyMeta` (Task 3); `vlab.pipeline.value_company`, `CompanyResult` (Task 1); `vlab.data.loader.load_prices`, `StatementFetcher`, `PriceFetcher`.
- Produces:
  - `engine.Fetchers(statements: StatementFetcher, prices: PriceFetcher, info: Callable[[str], dict])` frozen dataclass; `engine.default_fetchers() -> Fetchers` (yfinance).
  - `engine.analyze(ticker: str, *, fetchers: Fetchers, cache_dir: Path) -> dict` — the **Analysis JSON**, the contract every later task reads:

```
{ "ticker", "name", "exchange", "sector", "industry",
  "reporting_currency", "trading_currency", "price", "price_date", "as_of",
  "coverage": "full" | "partial" | "not_covered",
  "verdict": str | null,
  "dcf":     {"status": "ok"|"unavailable", "reason"?: str,
              "value_per_share", "implied_growth", "implied_average_growth",
              "implied_average_growth_at_lower_wacc": {"0.01": float, "0.02": float},
              "normalized_growth", "wacc", "beta", "beta_index", "cost_of_equity",
              "cost_of_debt", "equity_weight", "debt_weight", "terminal_share",
              "exit_multiple", "fiscal_years": [str],
              "sensitivity": {"rows": [...], "cols": [...], "values": [[float|null]]},
              "margin_sensitivity": {same shape}},
  "health":  {"status", "reason"?, "years": [{"fiscal_year", "revenue", "ebit_margin",
              "fcf", "net_debt_to_ebitda"}]},
  "multiples": {"status": "unavailable", "reason": "peer table not built yet"},
  "limits":  [str] }
```

  Rules: prices loaded separately for the stock and for `meta.market_index` (`load_prices([ticker])`, `load_prices([index])`) then passed to `value_company`; price frame multiplied by `meta.price_scale` before use. `excluded_reason` → `coverage "not_covered"`, sections absent, `limits = [excluded_reason]`, no fetch of statements. Any `DataError`/`ValuationError` inside the DCF → `dcf.status "unavailable"`, `reason = str(exc)`; same isolation for health. `coverage` = `"full"` if every section ok, else `"partial"`. `fcf` = operating cash flow + capital expenditure (`Operating Cash Flow` line; health unavailable if absent). `net_debt_to_ebitda` = (Total Debt − Capital Lease Obligations − Cash) / (EBIT + D&A), null when EBITDA ≤ 0. `limits` always includes, in this order: each `limit_sentence` for filled lines; `"Equity risk premium fixed at 5%; risk-free rate {rf:.2%} for {ccy} (as of {RISK_FREE_DATE})."`; `"Beta regressed on weekly returns against {index} over five years."`; `"Normalized drivers are medians over the reported years ({first}–{last})."`. Risk-free: `config.RISK_FREE: dict[str, float]` + `config.RISK_FREE_DATE: str`, 10-year government yields per currency (USD, EUR, GBP, CHF, SEK, DKK, NOK), filled at implementation from a public source cited in a comment, looked up by **reporting** currency and passed as `value_company(..., risk_free=rf)` (Task 1). Currency missing from the table → `dcf unavailable`, reason `f"no risk-free rate configured for {ccy}"`. Engine runs are serialized by one module-level `threading.Lock`.
  - Verdict (exact copy), only when dcf ok: `f"At {price:,.2f} {ccy}, the market prices {avg:.1%} average revenue growth over five years; the company's normalized history is {hist:.1%}."`

- [ ] **Step 1: Write failing tests** (all with fake fetchers built from `make_statements` and synthetic weekly price series):
  - `test_analyze_full_coverage_returns_verdict_and_sections`
  - `test_analyze_bank_is_not_covered_without_fetching_statements` (statement fetcher raises if called)
  - `test_analyze_missing_core_line_marks_dcf_unavailable_keeps_health`
  - `test_analyze_lists_defaulted_lines_in_limits`
  - `test_analyze_pence_listing_scales_price` (price 2500 GBp → `price == 25.0`)
  - `test_analyze_implied_growth_outside_bracket_is_unavailable_not_raised`
  - `test_analyze_uses_risk_free_of_reporting_currency` (EUR company → `cost_of_equity == RISK_FREE["EUR"] + beta * 0.05`)
  - `test_analyze_unknown_currency_marks_dcf_unavailable`
  - `test_analysis_json_is_serializable` (`json.dumps` with NaN → null)
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: analyze any ticker into dcf and health sections`.

---

### Task 5: Peer table and multiples section

**Files:**
- Create: `src/elab/peers.py`
- Modify: `src/elab/engine.py` (fill `multiples` when a peer table exists)
- Test: `tests/test_peers.py` (hand-written mini HTML tables as fixtures)

**Interfaces:**
- Produces:
  - `config.CONSTITUENT_SOURCES: tuple[ConstituentSource, ...]` — `ConstituentSource(index: str, url: str, ticker_column: str, suffix: str)` for S&P 500, CAC 40, DAX, FTSE 100, SMI, AEX, IBEX 35, FTSE MIB, OMX Stockholm 30 (Wikipedia pages); builder sets columns from the live pages.
  - `peers.constituents(source, reader: Callable[[str], list[pd.DataFrame]]) -> list[str]` — reader defaults to `pd.read_html`; ticker normalized (`BRK.B` → `BRK-B`, suffix appended).
  - `peers.build_peer_table(tickers: list[str], info: Callable[[str], dict], *, sleep_s: float = 1.0, existing: pd.DataFrame | None = None) -> pd.DataFrame` — columns `ticker, name, sector, industry, market_cap, ev_ebitda, ev_sales, pe`; skips tickers already in `existing` and fetched within 7 days (resumable); a failing ticker is skipped and logged, never aborts. Stored as parquet in the cache dir (`peers.parquet`), never committed.
  - `peers.peers_for(ticker: str, industry: str, sector: str, market_cap: float, table: pd.DataFrame) -> dict` — the `multiples` section: same industry, excluding itself, nearest 8 by |log market cap|; if fewer than 4, same sector and `"basis": "sector"`; returns `{"status": "ok", "basis", "company": {...}, "peers": [...], "median": {...}, "note": "Multiples use EV/EBITDA, not EV/EBIT: peer figures come from summary data."}`; fewer than 4 even by sector → `unavailable`, reason `"fewer than four comparable companies in the peer table"`. Medians ignore nulls and non-positive multiples.

- [ ] **Step 1: Write failing tests:** `test_constituents_normalizes_tickers`, `test_build_peer_table_skips_failures_and_resumes`, `test_peers_for_prefers_industry_then_sector`, `test_peers_for_excludes_company_itself`, `test_peers_for_median_ignores_negative_pe`, `test_engine_multiples_unavailable_without_table`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: weekly peer table and multiples section`.

---

### Task 6: Cache and narrative

**Files:**
- Create: `src/elab/cache.py`, `src/elab/narrative.py`
- Test: `tests/test_cache.py`, `tests/test_narrative.py`

**Interfaces:**
- Produces:
  - `cache.Cache(path: Path)` with `get(key: str, max_age: timedelta | None) -> tuple[dict, datetime] | None` (returns value and stored time; `max_age=None` returns any age) and `set(key: str, value: dict) -> None`; one SQLite table `entries(key TEXT PRIMARY KEY, value TEXT, stored_at TEXT)`; connection per call, thread-safe.
  - `config.ANALYSIS_TTL = timedelta(hours=24)`; narrative never expires (keyed by content).
  - `narrative.payload_hash(analysis: dict) -> str` — sha256 of `json.dumps(analysis, sort_keys=True)` minus `as_of`.
  - `narrative.build_prompt(analysis: dict) -> str` — system rules + the JSON. Rules (exact): write ~250 words in four short paragraphs: what the price implies; plausibility against history and peers; balance-sheet points; what would change the reading. Quote only figures present in the JSON. Never use the words buy, sell, undervalued, overvalued, target. Do not give advice.
  - `narrative.check_text(text: str, analysis: dict) -> list[str]` — returns violations: forbidden words (case-insensitive, word boundary), and numbers in the text that match no number in the payload. A text number `x` with `d` decimals matches a payload value `v` if `|x − v| ≤ 0.5·10^−d` or `|x − 100·v| ≤ 0.5·10^−d`; always allowed: integers 1–10 and years 1990–2100.
  - `narrative.ClaudeRunner(timeout_s: int = 240)` callable `(prompt: str) -> str`: runs `["claude", "-p", "--model", "sonnet"]`, prompt on stdin, env copy without `ANTHROPIC_API_KEY`; raises `NarrativeError` on non-zero exit, timeout, or empty output.
  - `narrative.NarrativeWorker(cache: Cache, runner: Callable[[str], str])` — one daemon thread, `queue.Queue(maxsize=20)`; `request(analysis) -> str` returns `"ready"`, `"pending"` or `"unavailable"`; dedups by hash (already queued → `"pending"`); a job: run → check → on violations retry once with the violations appended to the prompt → still failing or `NarrativeError` → store `{"status": "unavailable"}` for that hash for 1 hour, then allow retry. `get(analysis) -> dict | None` → `{"status": "ready", "text": ...}` or `{"status": "unavailable"}`.

- [ ] **Step 1: Write failing tests:** `test_cache_respects_max_age`, `test_check_text_accepts_rounded_percentages` (payload 0.0915 ↔ "9.2%" and "9.15%"), `test_check_text_rejects_invented_number`, `test_check_text_rejects_forbidden_word`, `test_worker_dedups_same_payload`, `test_worker_retries_once_then_marks_unavailable`, `test_worker_survives_runner_crash_and_serves_next_job`, `test_runner_strips_api_key_from_env` (inject a fake `subprocess.run`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: sqlite cache and checked claude narrative worker`.

---

### Task 7: API

**Files:**
- Create: `src/elab/api.py`
- Test: `tests/test_api.py` (FastAPI `TestClient`)

**Interfaces:**
- Consumes: `search`, `analyze`, `Cache`, `NarrativeWorker`, `peers_for`.
- Produces: `api.create_app(*, fetchers: Fetchers, cache: Cache, worker: NarrativeWorker, searcher, cache_dir: Path, static_dir: Path) -> FastAPI` with:
  - `GET /api/search?q=` → `[{"ticker","name","exchange"}]`.
  - `GET /api/analysis/{ticker}` → Analysis JSON plus `"stale": bool`. Ticker must match `TICKER_PATTERN` (else 422). Fresh cache → serve. Else, under a per-ticker lock (so concurrent requests run one analysis), run `analyze` in a thread; if it raises unexpectedly and any cached copy exists → serve it with `stale: true`; if none → 503 `{"detail": "Data source unavailable, try again in a minute."}`. Unknown ticker (`DataError` from info) → 404.
  - `GET /api/analysis/{ticker}/narrative` → `{"status": "ready", "text"}` | `{"status": "pending"}` | `{"status": "unavailable"}`; reads the cached analysis (404 if none), calls `worker.request`.
  - Rate limits per client IP (`X-Forwarded-For` first hop when the request comes from 127.0.0.1, else peer address), in-memory sliding window: `config.COLD_ANALYSES_PER_HOUR = 10` (only cache misses count), `config.NARRATIVE_JOBS_PER_HOUR = 5` (only new jobs count), 429 with `"Too many new analyses from this address; cached companies stay available."`.
  - `/` serves `static/index.html`; `/static` mounted.

- [ ] **Step 1: Write failing tests:** `test_analysis_served_from_cache_without_fetch`, `test_concurrent_cold_requests_run_one_analysis` (two threads, analyze counter == 1), `test_upstream_failure_serves_stale_copy`, `test_upstream_failure_without_cache_is_503`, `test_invalid_ticker_is_422`, `test_cold_rate_limit_returns_429_but_cached_still_served`, `test_narrative_pending_then_ready`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; `pytest --cov=elab --cov-fail-under=80`.
- [ ] **Step 5: Commit** `feat: http api with cache, locks, rate limits`.

---

### Task 8: Page

**Files:**
- Create: `static/index.html`, `static/app.js`, `static/style.css`
- Test: `tests/test_static.py` (served at `/`; contains the search input; no forbidden word in any static file)

**Interfaces:**
- Consumes: Task 7 endpoints and the Analysis JSON.

Sections in order (spec): search with autocompletion (debounce 250 ms, shows `name · ticker · exchange`); verdict card; reverse DCF + DCF (value per share vs price, WACC bridge table, implied growth at WACC −1/−2 pts, both sensitivity grids as tables with the base cell highlighted); multiples vs peers table with median row and basis/note; financial health (Chart.js line/bar: revenue, EBIT margin, FCF; net debt/EBITDA in a row); Claude analysis (polls narrative every 5 s up to 3 min, shows "Analysis being written…", then text or "Written analysis unavailable right now; the figures above are complete."); limits list; footer with "Data: Yahoo Finance via yfinance, as of {as_of}", repo link, `valuation-lab` link. `not_covered` → only the reason card. `stale` → banner "Showing the last saved analysis from {as_of}; live data is unavailable." Unavailable section → its reason in place of the content. All text inserted with `textContent` (no `innerHTML` from data). Chart.js from `https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js`. Readable on a 375 px screen, light and dark via `prefers-color-scheme`.

- [ ] **Step 1: Write failing test** `test_index_served_and_free_of_forbidden_words`. **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4:** run tests, then `python -m elab serve` locally with real yfinance and check by eye: `MC.PA`, `AAPL`, `SAP`, `ULVR.L`, `JPM` (not covered), a nonsense query. **Step 5: Commit** `feat: single-page analysis view`.

---

### Task 9: CLI jobs, deploy files, README, CI

**Files:**
- Create: `src/elab/__main__.py`, `deploy/equity-lab.service`, `deploy/equity-lab-jobs.service`, `deploy/equity-lab-jobs.timer`, `deploy/nginx-equity-lab.conf`, `.github/workflows/ci.yml`, `README.md`
- Test: `tests/test_main.py`

**Interfaces:**
- `python -m elab serve [--host 127.0.0.1] [--port 8040] [--data-dir PATH]` — builds real fetchers, `Cache(data_dir/"cache.sqlite")`, `NarrativeWorker(cache, ClaudeRunner())`, uvicorn single worker.
- `python -m elab build-peers [--data-dir PATH]` — all `CONSTITUENT_SOURCES` → `build_peer_table` resumable → `peers.parquet`.
- `python -m elab pregenerate [--data-dir PATH]` — `config.PREGENERATE: tuple[str, ...]` (~50 tickers: Euro Stoxx 50 heavyweights, CAC 40 leaders, US mega caps; builder picks), analyze + enqueue narrative, waits for the queue to drain, one at a time.
- Timer: weekly, Sunday 03:00, runs `build-peers` then `pregenerate`. All units set the three BLAS thread variables to 1 and `EnvironmentFile=` pointing at the VPS `.env` that holds `CLAUDE_CODE_OAUTH_TOKEN`.
- CI: Python 3.11 and 3.12, `ruff check .`, `pytest --cov=elab --cov-fail-under=80`.
- README in the labs' style: one-line purpose, live link, screenshot, "In short" three bullets, how it works, limits (yfinance unofficial, ESEF/EDGAR not used, EV/EBITDA for peers, financials not covered, subscription-tunnel narrative checked against the numbers), how to run locally.

- [ ] **Step 1: Write failing tests:** `test_cli_parses_subcommands`, `test_pregenerate_processes_list_one_at_a_time` (fakes). **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** full suite + ruff → PASS. **Step 5: Commit** `chore: cli jobs, deploy units, ci and readme`. Create the public GitHub repo and push only after the user confirms.

---

### Task 10: Deploy on the VPS (each outward step confirmed with the user)

- [ ] **Step 1: Inspect** `ssh missiforge-vps`: free memory (`free -m`), reverse proxy in place (`systemctl status nginx caddy`), `claude -p "OK"` with `export $(grep ^CLAUDE_CODE_OAUTH_TOKEN= /home/missiforge/app/.env)`, Python version. Report findings; adapt the deploy files if the proxy is not nginx.
- [ ] **Step 2: Install** in `/opt/equity-lab` (venv, `pip install .`), data dir `/var/lib/equity-lab`, units copied and enabled.
- [ ] **Step 3: DNS + TLS** for `analyse.emiliensabathier.fr` (user confirms the subdomain and adds the DNS record if not scriptable), certbot.
- [ ] **Step 4: First run** `build-peers` then `pregenerate` in the foreground (session reaper kills background jobs); watch memory.
- [ ] **Step 5: Verify live**: `curl https://analyse.emiliensabathier.fr/api/search?q=lvmh`, open the page on desktop and phone, the five names of Task 8, the narrative arrives. Record outcome; update the labs' profile README link only on request.
