# equity-lab — design

Date: 2026-10-17. Status: draft for approval.

## Intent and the why

A public website where a recruiter (trader / PM internships) types the name of **any listed
company**, US or European, and gets a full, honest equity analysis on one page.

It is a portfolio piece next to the five public labs (`options-lab`, `credit-lab`,
`rates-lab`, `valuation-lab`, `portfolio-lab`). It must show reasoning, not just pretty
numbers: what the share price already implies, how that compares with the company's own
history and its peers, and where the model is weak. Same tone as the labs' READMEs:
a result, its limits, no overclaiming.

The recruiter has about two minutes. The first thing on screen must be a one-sentence
verdict, e.g. *"At 481 EUR, the market prices 9% average revenue growth over five years;
the company's normalized history is -2%."* That reverse-DCF framing is what separates this
from a Yahoo Finance page.

## Goals

1. Free-text search on any listed company name (US + Europe), resolved to a ticker with
   autocompletion so "Total" does not silently land on the wrong company.
2. Numbers in seconds: reverse DCF headline, DCF value, WACC bridge, sensitivity grids,
   multiples against peers, five-year financial health (margins, FCF, leverage).
3. A written analysis by Claude that comments on the computed numbers, arriving after the
   numbers (30 s – 2 min on first request), cached afterwards.
4. Every missing input is stated on the page ("no lease line reported; leases treated as
   zero") — partial analysis over silent invention.
5. Zero running cost beyond what the user already pays (VPS, Claude subscription).

## Non-goals (v1)

- Banks, insurers, REITs: FCFF DCF does not apply. Detected from sector, shown as
  "not covered by this model", with the reason.
- Companies outside US/Europe, unlisted companies, ETFs/funds.
- Intraday or real-time prices. Last close is enough.
- User accounts, watchlists, portfolio tracking, exports.
- French UI. English, like the labs.

## Anti-goals (failure even if it works)

- Any "buy / sell / target price / undervalued" language. The page measures what the price
  implies against *this model*; it never says a share is mispriced.
- Price prediction of any kind (ML, technicals).
- Claude inventing or altering figures. The prompt receives only the computed JSON and must
  cite it; numbers on the page always come from the engine, never from the text.
- A demo that hangs or 500s in front of a recruiter. Failures must degrade to a clear
  message and whatever partial result exists.

## Constraints

- **No paid API, no Anthropic API key.** Claude is called only through the user's existing
  subscription tunnel: `claude -p` authenticated with `CLAUDE_CODE_OAUTH_TOKEN`, the same
  pattern already used on the VPS for MissiForge (`missiforge-catalog`) and in
  `emilien-assistant` (`claudeCliOnce`, `ANTHROPIC_API_KEY` stripped from the child env).
  Accepted risk, stated to the user: serving a public site through a subscription token is
  a grey zone of Anthropic's terms; a revoked token also breaks MissiForge. Caching and
  pre-generation keep call volume low.
- **Runs on the VPS** (`ssh missiforge-vps`), not Vercel: the CLI is a subprocess and the
  tunnel is slow. Low memory: one heavy job at a time, export
  `OPENBLAS_NUM_THREADS=OMP_NUM_THREADS=MKL_NUM_THREADS=1`.
- **Reuse `valuation-lab`'s engine (`vlab`), do not rewrite it.** Drivers, DCF, reverse DCF,
  WACC, sensitivity already exist and are tested. Generalise where it is hard-wired.
- Public repo, same standards as the labs: CI, tests (80% on the new code), ruff,
  conventional commits, no raw third-party data committed.

## Decided

| Topic | Decision | By |
|---|---|---|
| Audience | Recruiters for trader/PM internships | user |
| Coverage | Any company name, US **and** Europe | user |
| Cost | Free; Claude only through the subscription tunnel | user |
| Latency | Numbers immediately, Claude text later on first visit | user |
| Layout, stack, data source, hosting | Delegated ("take the best") | user → Claude's calls below |

## Claude's calls (user may override)

**Data source: `yfinance`, already used by `vlab`.** It covers US and European listings,
gives annual statements (≈4 years), prices, FX, sector/industry and a name search
(`yf.Search`). It is unofficial and can rate-limit a VPS IP, hence the cache. SEC EDGAR
and ESEF (filings.xbrl.org) were considered as primary sources and rejected for v1: two
parsers, patchy ESEF coverage, no prices. They stay a possible fallback later.

**Hosting.** FastAPI app on the VPS behind the existing reverse proxy, on a subdomain such
as `analyse.emiliensabathier.fr` (domain already owned). systemd service like the other
VPS services.

**Request flow.**
1. `GET /search?q=` → `yf.Search`, filtered to equities on US/European exchanges, top 8.
2. `GET /analysis/{ticker}` → engine run (target < 10 s cold, instant when cached) → JSON.
3. `GET /analysis/{ticker}/narrative` → returns cached text, or enqueues a Claude job and
   returns `pending`; the page polls every few seconds and fills the section when ready.
4. Cache on disk (SQLite or JSON files): engine result 24 h, narrative keyed on
   (ticker, engine-result hash), so it is regenerated only when the numbers change.
5. Pre-generation job (systemd timer, weekly): ~50 large caps (CAC 40 heavyweights,
   Euro Stoxx 50, US mega caps) so obvious names are instant.

**Guards.** One Claude job at a time (queue), per-IP rate limit on cold analyses and on
narrative jobs, CLI timeout, input validated as a ticker returned by search.

**Generalising `vlab` (the main engineering work).**
- `universe.py`: replace the fixed luxury `PEERS` with a `Company` built from yfinance info
  (reporting vs trading currency kept distinct — the Richemont lesson).
- `wacc.py`: risk-free per currency (USD, EUR, GBP, CHF, SEK…; a small dated table in
  config, documented), market index per exchange (S&P 500 for US, STOXX Europe 600 for
  Europe, or local index), ERP stays 5% and is shown.
- Lease handling: US GAAP filers often lack `Capital Lease Obligations` → treat as zero
  and say so, instead of `require()` raising.
- Any `DataError` / `ValuationError` becomes a section-level "could not compute: reason"
  on the page, not a failed request.

**Peers / multiples.** yfinance gives no peer list. A weekly job builds a peer table from
index constituents (S&P 500 + STOXX Europe 600) with industry, EV/EBIT, EV/Sales, P/E.
Peers = same industry, nearest by market cap, up to 8; fall back to sector when the
industry has fewer than 4. Shown as a table with the median and the company's position.
Approximate by construction, and the page says so.

**Page (single page, English).**
1. Search bar, autocompletion.
2. Verdict card: price, implied 5-y average growth, normalized history, the one sentence.
3. Reverse DCF + DCF: value per share, WACC bridge, implied growth at WACC ±2 pts,
   WACC × terminal-growth and margin × growth grids.
4. Multiples vs peers.
5. Financial health: revenue, EBIT margin, FCF, net debt / EBITDA over the reported years.
6. Claude's analysis (pending state while generating).
7. Limits of the model and missing data, explicit.
8. Footer: data source and date, link to the GitHub repo and to `valuation-lab`.
Static HTML + vanilla JS + one light chart library from a CDN; no frontend build step.

**Claude prompt.** Input: the engine JSON only. Output: ~250 words, four short paragraphs
(what the price implies, how plausible vs history and peers, balance-sheet points, what
would change the reading). Must quote figures from the input; forbidden words: buy, sell,
undervalued, overvalued, target. A post-check rejects text containing a number absent from
the input (with tolerance for rounding) and retries once, else shows "analysis unavailable".

**Repo.** New public repo `equity-lab`, depending on `vlab` (git dependency or vendored
package — builder's choice), README in the labs' style with a screenshot and the live link.

## Claude's calls after the builder check

- **Missing statement lines.** `drivers_from` requires 17 lines and raises on any absence.
  Split them in two: *core* lines (Total Revenue, EBIT, Diluted shares / shares outstanding,
  Total Debt, Cash) — missing one means the DCF section reports "could not compute: <line>
  not reported"; *defaultable* lines (Inventory, Receivables, Payables, Sale Of PPE,
  Minority Interest, Capital Lease Obligations, Current Capital Lease Obligation, lease
  payments, Interest Expense) — default to zero via an explicit whitelist, each default
  listed on the page in the Limits section ("Inventory not reported: treated as zero").
  Coverage target: the DCF computes for ≥ 80% of the pre-generation list.
- **Peer multiples.** No per-company statement pulls for peers. Use the light `info` fields
  (`enterpriseToEbitda`, `enterpriseToRevenue`, `trailingPE`, `marketCap`, `industry`) —
  EV/EBITDA replaces EV/EBIT, stated on the page. Constituent lists (S&P 500, STOXX Europe
  600 or, if no free list, the major national indices) are fetched by the weekly job from a
  public source and kept only in the VPS cache, never committed. The weekly job is slow and
  resumable (throttled, retries, partial table usable).
- **Beta index.** Local index per exchange (e.g. `.PA` → CAC 40, `.DE` → DAX, `.L` → FTSE
  100, `.SW` → SMI, `.AS` → AEX, `.MC` → IBEX, `.MI` → FTSE MIB, `.ST` → OMXS30, US → S&P
  500), small table in config; stock and index in the same trading currency, so no FX in the
  regression. Unknown exchange → STOXX Europe 600 with FX conversion, stated.
- **VPS sharing.** equity-lab has its own Claude queue (one job at a time) and its own
  heavy-job lock; MissiForge is not modified. Before deploying, the builder checks on the
  VPS that `claude -p` works with the `.env` token for the new service user, which reverse
  proxy is in place, and free memory; deployment adapts to what is there.

## Left to the builder

- Cache storage format, queue implementation, exact rate-limit numbers.
- Chart library, visual styling (sober, readable, works on mobile).
- How `vlab` is consumed (git dependency vs moving engine into a shared package), as long
  as `valuation-lab`'s own tests and report stay green.
- Exact pre-generation list and risk-free table values (dated and sourced).
- Test strategy details beyond: engine generalisation unit-tested with frozen fixtures,
  API tested with injected fetchers (no network in CI), narrative post-check tested.
