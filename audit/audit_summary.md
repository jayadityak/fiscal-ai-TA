# FiscalAI value audit

## Scope

- Nine published financial-statement sheets.
- 362 line-item rows × 10 fiscal years = 3,620 output positions.
- 3,097 populated/dash positions and 523 legitimate blanks after correction.
- 3,461 extracted source cells across 45 statements from 15 selected annual reports.
- Exact source PDFs, output artifacts, and workbooks are recorded in `baseline_hashes.csv`.

## Final result

- 3,393 source cells matched the claimed PDF row and fiscal-year column spatially.
- 39 Unilever balance-sheet section subtotals matched the correct PDF year column and reconciled exactly to their component rows.
- 9 wrapped/split PDF labels matched the complete page label and correct fiscal-year column.
- 20 records were non-numeric source headings, not financial values.
- 0 unexplained source exceptions remain in `source_exceptions.csv`.
- 0 published-output mismatches remain in `output_mismatches.csv`.
- The financial workbook was re-imported and verified against all 3,620 output positions.
- The supporting workbook was re-imported and verified across 101,490 populated table cells.
- All 104 accounting/coverage validations pass.
- All 45 enhanced source reconciliation checks pass, including zero incomplete line-item rows and zero blank line-item cells.

## Confirmed corrections

| Company | Statement | Line item | Period | Before | Correct |
|---|---|---|---:|---:|---:|
| Heineken | Cash flow | Total change in working capital | 2018 | blank | 713 |
| Heineken | Cash flow | Total change in working capital | 2019 | 713 | 8 |
| Heineken | Cash flow | Other non-cash items | 2020 | blank | 231 |
| Heineken | Cash flow | Other non-cash items | 2021 | 231 | 30 |
| Unilever | Income statement | Other income/(loss) from non-current investments and associates | 2016 | 91 | 104 |

The source observations were also corrected outside the target window where needed: Unilever 2017 = 18 and 2015 = 91 for the same three-column row.

## Controls added

- Exact PDF row/year spatial audit for every extracted numeric or dash cell.
- Explicit reconciliation failure for incomplete line-item period coverage or blank line-item cells.
- Targeted PDF-coordinate repairs that reread exact printed tokens under their year
  headers; no financial values are hardcoded.
- Canonical alias correction for the split Unilever cash-flow row.
- Cached semantic results can now be reused without an API key.
- Reproducible scripts to audit values and synchronize/verify both workbooks.

## Remaining scope limitation

The selected report set is 2017, 2019, 2021, 2023, and 2025 for each company. Therefore, this audit certifies the nine sheets against all 15 selected source reports, but it does **not** certify the assignment bonus against annual reports that were never ingested (2018, 2020, 2022, and 2024 for each company). Those 12 intervening reports must be acquired and parsed before claiming that every period uses the newest comparative available across *all* issuer reports.
