"""Build the frontend payload from the pipeline's committed artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
OUTPUT = ROOT / "web" / "app" / "data.generated.json"

COMPANIES = {
    "nestle": {"name": "Nestlé S.A.", "ticker": "NESN", "currency": "CHF"},
    "heineken": {"name": "Heineken N.V.", "ticker": "HEIA", "currency": "EUR"},
    "unilever": {"name": "Unilever PLC", "ticker": "ULVR", "currency": "EUR"},
}

STATEMENTS = {
    "income_statement": "Income Statement",
    "balance_sheet": "Balance Sheet",
    "cash_flow": "Cash Flow Statement",
}


def read_csv(name: str) -> list[dict[str, str]]:
    with (ARTIFACTS / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_number(value: str) -> int | float | None:
    if value in {"", "—", "–", "-"}:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def build_statement(company: str, statement: str) -> dict[str, object]:
    rows = read_csv(f"{company}_{statement}.csv")
    periods = [column for column in rows[0] if column[:4].isdigit()]
    return {
        "key": statement,
        "name": STATEMENTS[statement],
        "currency": rows[0]["currency"],
        "unitMultiplier": int(rows[0]["unit_multiplier"]),
        "periods": [period[:4] for period in periods],
        "rows": [
            {
                "label": row["line_item"],
                "occurrence": int(row["occurrence"]),
                "valueKind": row["value_kind"],
                "currency": row["currency"],
                "unitMultiplier": int(row["unit_multiplier"]),
                "values": [parse_number(row[period]) for period in periods],
                "statuses": [
                    (
                        "reported_dash"
                        if row[period] in {"—", "–", "-"}
                        else ("not_reported" if row[period] == "" else "reported")
                    )
                    for period in periods
                ],
            }
            for row in rows
        ],
    }


def main() -> None:
    manifest = read_csv("filings_manifest.csv")
    validation = read_csv("validation.csv")
    reconciliation = read_csv("reconciliation.csv")

    companies = []
    for slug, metadata in COMPANIES.items():
        company_filings = [row for row in manifest if row["company"] == slug]
        company_validation = [row for row in validation if row["company"] == slug]
        company_reconciliation = [
            row for row in reconciliation if row["company"] == slug
        ]

        companies.append(
            {
                "slug": slug,
                **metadata,
                "comparabilityNote": (
                    "The 2025 annual report re-presents 2024 and 2023 for the Ice "
                    "Cream discontinued operation. Earlier years retain their "
                    "original total-group presentation and are not automatically "
                    "comparable with continuing-operation rows."
                    if slug == "unilever"
                    else ""
                ),
                "statements": [
                    build_statement(slug, statement) for statement in STATEMENTS
                ],
                "filings": [
                    {
                        "year": int(float(row["report_year"])),
                        "type": row["document_type"],
                        "filename": row["filename"],
                        "url": row["url"],
                        "sha256": row["sha256"],
                        "status": row["status"],
                    }
                    for row in company_filings
                    if row["report_year"]
                ],
                "validation": {
                    "passed": sum(
                        row["status"] == "passed" for row in company_validation
                    ),
                    "total": len(company_validation),
                },
                "reconciliation": {
                    "passed": sum(
                        row["status"] == "passed" for row in company_reconciliation
                    ),
                    "total": len(company_reconciliation),
                    "rows": sum(
                        int(row["extracted_rows"]) for row in company_reconciliation
                    ),
                    "cells": sum(
                        int(row["extracted_cells"]) for row in company_reconciliation
                    ),
                },
            }
        )

    payload = {
        "coverage": {"start": 2016, "end": 2025},
        "summary": {
            "companies": len(companies),
            "annualReports": sum(
                row["document_type"] == "annual_report" for row in manifest
            ),
            "statements": len(companies) * len(STATEMENTS),
            "years": 10,
            "validationPassed": sum(row["status"] == "passed" for row in validation),
            "validationTotal": len(validation),
            "reconciliationPassed": sum(
                row["status"] == "passed" for row in reconciliation
            ),
            "reconciliationTotal": len(reconciliation),
        },
        "companies": companies,
    }

    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
