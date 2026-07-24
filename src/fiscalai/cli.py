from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from .companies import COMPANIES, get_company
from .compile import write_outputs
from .extract import extract_report, write_candidates
from .scrape import scrape_company
from .validate import write_reconciliation, write_validation


def _select_report(manifest: pd.DataFrame, company: str, report_year: int) -> Path:
    matches = manifest[
        (manifest["company"] == company)
        & (manifest["status"] == "downloaded")
        & (manifest["document_type"] == "annual_report")
        & (manifest["report_year"] == report_year)
    ].copy()
    if matches.empty:
        raise RuntimeError(
            f"No downloaded annual-report PDF found for {company} {report_year}"
        )
    preferred = matches["filename"].str.contains("financial", case=False, na=False)
    row = matches[preferred].iloc[0] if preferred.any() else matches.iloc[0]
    return Path(row["local_path"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract consolidated financial statements")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scrape = subparsers.add_parser("scrape")
    scrape.add_argument("company", choices=[*COMPANIES, "all"])
    locate = subparsers.add_parser("locate")
    locate.add_argument("company", choices=COMPANIES)
    locate.add_argument("--year", type=int, default=2025)
    subparsers.add_parser("run")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    artifacts = Path("artifacts")

    if args.command == "scrape":
        slugs = COMPANIES if args.company == "all" else (args.company,)
        for slug in slugs:
            manifest = scrape_company(get_company(slug))
        print(f"Wrote {len(manifest)} filings to {artifacts / 'filings_manifest.csv'}")
        return

    manifest_path = artifacts / "filings_manifest.csv"
    if not manifest_path.exists():
        raise RuntimeError("Run `fiscalai scrape all` before locating or extracting reports")
    manifest = pd.read_csv(manifest_path)

    if args.command == "locate":
        company = get_company(args.company)
        pdf_path = _select_report(manifest, company.slug, args.year)
        candidates = write_candidates(company, args.year, pdf_path)
        for statement, candidate in candidates.items():
            print(f"{statement}: page {candidate['page_number']} (score {candidate['score']})")
        return

    reports = []
    for company in COMPANIES.values():
        for report_year in company.selected_report_years:
            reports.append(
                (
                    company,
                    report_year,
                    _select_report(manifest, company.slug, report_year),
                )
            )

    extracted: dict[tuple[str, int], pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(
                extract_report,
                company,
                report_year,
                pdf_path,
                artifacts,
                False,
            ): (company.slug, report_year)
            for company, report_year, pdf_path in reports
        }
        for future in as_completed(futures):
            slug, report_year = futures[future]
            extracted[(slug, report_year)] = future.result()
            print(f"Extracted {slug} {report_year}", flush=True)

    new_observations = pd.concat(
        [
            extracted[(company.slug, report_year)]
            for company, report_year, _ in reports
        ],
        ignore_index=True,
    )
    observations_path = artifacts / "observations.parquet"
    if observations_path.exists():
        existing = pd.read_parquet(observations_path)
        replaced = set(
            zip(
                new_observations["company"],
                new_observations["report_year"],
                strict=True,
            )
        )
        keep = [
            (company, report_year) not in replaced
            for company, report_year in zip(
                existing["company"], existing["report_year"], strict=True
            )
        ]
        new_observations = pd.concat(
            [existing.loc[keep], new_observations], ignore_index=True
        )
    new_observations.to_parquet(observations_path, index=False)

    print("Canonicalizing labels and compiling tables...", flush=True)
    winners, _ = write_outputs(new_observations)
    reconciliation = write_reconciliation(new_observations, manifest)
    validation = write_validation(winners)
    print(reconciliation["status"].value_counts().to_string())
    print(validation.to_string(index=False))
    if (
        (validation["status"] == "failed").any()
        or (reconciliation["status"] == "failed").any()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
