# FiscalAI

**Live demo:** https://fiscal-ai-ta-wn9k-teal.vercel.app/

FiscalAI turns public investor-relations PDFs into three auditable, ten-year tables
for Nestlé, Heineken, and Unilever. Every output covers FY2016–FY2025.

## What it delivers

| | |
|---|---|
| Companies | 3 (Nestlé, Heineken, Unilever) |
| Annual reports | 15 — five per company (2017, 2019, 2021, 2023, 2025) |
| Output tables | 9 — three statements × three companies, FY2016–FY2025 |
| Extracted cells | 3,438, each traced to a source document hash and page |
| Accounting checks | **104 / 104 passed** |
| Source reconciliation | **45 / 45 passed** |
| Cross-report consistency | **230 figures cross-verified, 0 unexplained differences** |
| Uncached model calls | 92 (design minimum 54, hard cap 100) |

Every figure is either extracted verbatim from a filing or left explicitly absent — no
value is estimated, interpolated, or inferred. Three independent checks back this up:
reported subtotals must satisfy accounting identities, every extracted label and value
must be present in the selected source-page evidence, and any figure appearing in more
than one report edition must agree across those editions or be an accounted-for
restatement.

## Run

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
fiscalai scrape all
fiscalai locate nestle --year 2025
export OPENAI_API_KEY=...
fiscalai run
pytest
```

`scrape` downloads and classifies filings. `locate` is a deterministic page-locator
diagnostic. `run` extracts all five selected reports per company, canonicalizes labels,
resolves restatements, writes the final tables, and validates them. Extraction and
canonicalization use bounded parallelism. The selected report years are 2017, 2019,
2021, 2023, and 2025; their comparative columns cover ten fiscal years without
downloading one report per year.

Reviewing without running anything is the fastest path: the live demo and the committed
`artifacts/` are the delivered result, and the frontend reads them directly. If you do run
the pipeline, note that a few IR CDNs now refuse scripted downloads — `unilever.com` and
Nestlé's 2017 asset return HTTP 403 to `fiscalai scrape`, which records them as explicit
failures. Use `fiscalai discover` (below) to fetch those through a real browser, or drop
browser-downloaded PDFs into `.cache/pdf/`.

## Frontend

The read-only reviewer interface in `web/` renders the committed artifacts without
runtime extraction, arithmetic, or LLM calls. It is deployed live at
**https://fiscal-ai-ta-wn9k-teal.vercel.app/**.

To run it locally:

```bash
python3 scripts/export_frontend.py
cd web
npm install
npm run dev
```

Open `http://localhost:3000` to switch between all nine statements, review validation
and source provenance, and download the complete workbooks.

## Browser discovery

Some investor-relations sites block a plain-HTTP crawler: Nestlé and Unilever sit behind
Akamai/Cloudflare bot protection (HTTP 403) and Heineken behind a JavaScript age gate.
`fiscalai discover` drives a real browser (Playwright) from each company's configured
**entry point**, passes cookie banners, age gates, and bot challenges, then discovers,
downloads, and classifies the report PDFs — the "given an entry point, navigate and come
back with the reports" flow.

```bash
pip install -e '.[browser]'
python -m playwright install chromium
fiscalai discover unilever      # or: all
```

Entry points live in `companies.py`, tried in order until every selected year is found.
Each company combines a navigable aggregator (for the historical reports) with its own IR
site (for the current-year report): Unilever's Akamai-protected archive and Heineken's
age-gated reports page both yield to a headed browser, and downloads use an in-page fetch
that passes Akamai on the asset host. Discovery returns all five selected years per company
— fifteen reports in total. The one exception is Nestlé's current-year report: its IR site
is behind Cloudflare Turnstile (which standard automation cannot reliably pass) and the
aggregator lags ~2 years, so that single report falls back to a configured direct URL,
tagged `seed_fallback` in the manifest so the fallback is explicit. Results are written to
`artifacts/discovered_manifest.csv` with a `discovery_source` column, independent of the
committed extraction inputs.

## Design

The pipeline is deliberately direct:

1. `scrape.py` crawls the configured IR pages, downloads PDFs, hashes them, and classifies
   them with explainable keyword rules; `browser_discover.py` does the same through a real
   browser for sites that refuse a plain-HTTP crawler.
2. `extract.py` reads PDF text with PyMuPDF, scores pages for the three primary
   consolidated statements, and sends only those pages for structured extraction.
3. `llm.py` uses strict Pydantic schemas for statement rows and one label-grouping call
   per company and statement. A deterministic guard rejects any proposed alias pair
   that coexists in the same report.
4. `compile.py` parses and scales numbers deterministically, selects the newest reported
   value for each period, preserves repeated labels by source-order occurrence, and
   pivots ten ordered columns.
5. `validate.py` checks ten-year coverage and, where the required rows exist, the balance
   identity, profit attribution subtotal, and cash roll-forward, plus row-completeness for
   statements whose layout is known to be hard to read.
6. `consistency.py` compares independent extractions of the same cell across report
   editions and flags any disagreement that is not an accounted-for restatement.

The minimum uncached model budget is 54 calls: 45 statement extractions plus 9 label
canonicalizations. The delivered run used 92 calls after focused retries on ambiguous
side-by-side statements. Responses are cached by model, prompt, schema, and input hash,
with a hard stop at 100 uncached calls.

## Verification

Three independent checks run over the delivered data, each catching a different class of
error:

1. **Accounting identities** (`validation.csv`) — assets equal liabilities plus equity,
   profit reconciles to its parent and non-controlling components, and closing cash equals
   opening cash plus the period's movement, for every company and year. A transcription
   error in any figure feeding a reported subtotal breaks arithmetic that must balance.
   *104 / 104 passed.*
2. **Source reconciliation** (`reconciliation.csv`) — every extracted label and raw value
   must appear in the evidence from the exact source page, and the observation's document
   hash must match the filing it claims to come from. This catches invented rows and
   values attributed to the wrong document. *45 / 45 passed.*
3. **Cross-report consistency** (`consistency.csv`) — each fiscal period is extracted
   independently from several reports, because a 2023 filing also carries 2022 and 2021 as
   comparatives. Those independent extractions of the same cell must agree; a disagreement
   is either a genuine restatement (tracked in `lineage.csv`) or an extraction error.
   *230 cells cross-verified across editions: 176 identical, 54 accounted-for restatements,
   0 unexplained.*

Nil values are preserved as reported: a dash the company printed is recorded as
`reported_dash`, while a line absent from that year's statement stays empty and is never
filled with a zero.

## Outputs

- `artifacts/filings_manifest.csv`: discovered PDFs, classifications, hashes, paths, and
  visible download failures.
- `artifacts/observations.parquet`: every extracted source cell with exact wording,
  period, page, report year, scale, and document hash.
- `artifacts/lineage.csv`: winning and superseded observations, including the winning
  report year and document hash.
- `artifacts/validation.csv`: passed, failed, and skipped checks by company and period.
- `artifacts/reconciliation.csv`: statement-level source audit covering every extracted
  row and value cell.
- `artifacts/consistency.csv`: per-cell agreement across report editions, marking each as
  consistent, restated, unexplained, or single-source.
- `artifacts/discovered_manifest.csv`: reports found by browser discovery, with the entry
  point and `discovery_source` for each.
- `artifacts/{company}_{statement}.csv`: one 2016–2025 table per company and statement.
- `outputs/**/FiscalAI_Financial_Statements.xlsx`: the nine tables as a formatted workbook;
  `FiscalAI_Supporting_Outputs.xlsx` carries the filings, validation, reconciliation, and
  lineage sheets. Both are downloadable from the live site.

Values are normalized deterministically to the unit shown in the newest report; the
currency and multiplier are explicit columns. An `occurrence` column distinguishes
identically worded rows printed in separate statement sections without rewriting their
source labels. A failed validation leaves artifacts in place for diagnosis and makes
`fiscalai run` exit non-zero.

## LLM boundary

The model handles only semantic work: mapping messy statement text into a strict
`{label, period, raw_value}` shape and grouping economically equivalent labels. Exact
source labels and raw printed cells must be present in the extracted page evidence or
the run fails.

Downloading, raw PDF text extraction, numeric parsing, unit scaling, restatement
selection, pivoting, and every validation are deterministic.

## Limitations

- A low-confidence or image-only statement is rejected rather than guessed. Unilever's
  official 2017 PDF is the source artifact; because its primary statements are
  image-only, extraction uses the matching text-layer copy solely as a deterministic
  text companion while retaining the official PDF hash and provenance.
- Some public IR CDNs block scripted downloads, so `fiscalai scrape` can record explicit
  failures for links a browser retrieves without trouble. `fiscalai discover` is the answer
  to that: it drives a real browser and succeeds where the plain-HTTP path is refused.
- Source reconciliation is presence-based: it proves a label and its printed value appear
  in the selected page evidence, not that the value sits at that label's row and period
  column. The accounting identities and cross-report agreement checks cover that gap for
  any figure feeding a subtotal or appearing in more than one edition; true positional
  verification would need full table reconstruction and is not implemented.
- Currency changes fail explicitly. No foreign-exchange conversion is attempted.
- Accounting checks use the companies' reported subtotal labels and deterministic
  component sums where a single printed aggregate is absent. The delivered run has no
  skipped or failed checks.
- The extraction inputs are the configured report set, not the output of a discovery run;
  `fiscalai discover` writes to its own manifest so the two paths stay independent.
