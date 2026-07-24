# FiscalAI

**Live demo:** https://fiscal-ai-ta-wn9k-teal.vercel.app/

FiscalAI turns public investor-relations PDFs into three auditable, ten-year tables
for Nestlé, Heineken, and Unilever. Every output covers FY2016–FY2025.

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

## Design

The pipeline is deliberately direct:

1. `scrape.py` discovers configured IR links, downloads PDFs, hashes them, and classifies
   them with explainable keyword rules.
2. `extract.py` reads PDF text with PyMuPDF, scores pages for the three primary
   consolidated statements, and sends only those pages for structured extraction.
3. `llm.py` uses strict Pydantic schemas for statement rows and one label-grouping call
   per company and statement. A deterministic guard rejects any proposed alias pair
   that coexists in the same report.
4. `compile.py` parses and scales numbers deterministically, selects the newest reported
   value for each period, preserves repeated labels by source-order occurrence, and
   pivots ten ordered columns.
5. `validate.py` checks ten-year coverage and, where the required rows exist, the balance
   identity, profit attribution subtotal, and cash roll-forward.

The minimum uncached model budget is 54 calls: 45 statement extractions plus 9 label
canonicalizations. The delivered run used 92 calls after focused retries on ambiguous
side-by-side statements. Responses are cached by model, prompt, schema, and input hash,
with a hard stop at 100 uncached calls.

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
- `artifacts/{company}_{statement}.csv`: one 2016–2025 table per company and statement.

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
- Some public IR CDNs block scripted downloads. Such links remain explicit failures in
  the manifest and can be pre-seeded in `.cache/pdf/` through a normal browser download.
- Currency changes fail explicitly. No foreign-exchange conversion is attempted.
- Accounting checks use the companies' reported subtotal labels and deterministic
  component sums where a single printed aggregate is absent. The delivered run has no
  skipped or failed checks.
