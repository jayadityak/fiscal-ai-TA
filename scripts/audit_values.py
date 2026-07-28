"""Independent, deterministic audit of the nine published statement CSVs.

This script deliberately does not call the extraction LLM.  It checks:

1. every published output position against the winning lineage record;
2. every extracted source cell against the cited PDF page, row, and year column;
3. source/document hashes so the result can be reproduced against the same inputs.

Run from the repository root:

    python scripts/audit_values.py
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fitz
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
PDF_DIR = ROOT / ".cache" / "pdf"
AUDIT_DIR = ROOT / "audit"
PERIODS = [f"{year}-12-31" for year in range(2016, 2026)]
STATEMENTS = ("income_statement", "balance_sheet", "cash_flow")
COMPANIES = ("nestle", "heineken", "unilever")


def canonical_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\s\W_]+", " ", text).strip()


def compact(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    return re.sub(r"[^a-z0-9]+", "", text)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decimal_text(value: object) -> str:
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    normalized = format(number, "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def pages(value: object) -> list[int]:
    numbers = [int(number) for number in re.findall(r"\d+", str(value))]
    if len(numbers) == 2 and "-" in str(value):
        return list(range(numbers[0], numbers[1] + 1))
    return numbers


@dataclass(frozen=True)
class Word:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int
    line: int
    order: int

    @property
    def x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y(self) -> float:
        return (self.y0 + self.y1) / 2


class PdfEvidence:
    def __init__(self, path: Path):
        self.path = path
        self.document = fitz.open(path)
        self._words: dict[int, list[Word]] = {}
        self._blocks: dict[int, list[tuple[int, float, float, float, float, str]]] = {}
        self._text: dict[int, str] = {}

    def close(self) -> None:
        self.document.close()

    def words(self, page_number: int) -> list[Word]:
        if page_number not in self._words:
            page = self.document[page_number - 1]
            self._words[page_number] = [
                Word(
                    page_number,
                    float(item[0]),
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    str(item[4]),
                    int(item[5]),
                    int(item[6]),
                    int(item[7]),
                )
                for item in page.get_text("words", sort=True)
            ]
        return self._words[page_number]

    def blocks(
        self, page_number: int
    ) -> list[tuple[int, float, float, float, float, str]]:
        if page_number not in self._blocks:
            page = self.document[page_number - 1]
            self._blocks[page_number] = [
                (
                    index,
                    float(item[0]),
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    str(item[4]),
                )
                for index, item in enumerate(page.get_text("blocks", sort=True))
            ]
        return self._blocks[page_number]

    def text(self, page_number: int) -> str:
        if page_number not in self._text:
            self._text[page_number] = self.document[page_number - 1].get_text(
                "text", sort=True
            )
        return self._text[page_number]


def grouped_lines(words: list[Word]) -> list[list[Word]]:
    groups: dict[tuple[int, int], list[Word]] = {}
    for word in words:
        groups.setdefault((word.block, word.line), []).append(word)
    return [
        sorted(group, key=lambda word: (word.x0, word.order))
        for _, group in sorted(
            groups.items(),
            key=lambda item: (
                min(word.y0 for word in item[1]),
                min(word.x0 for word in item[1]),
            ),
        )
    ]


def value_spans(words: list[Word], raw_value: object) -> list[tuple[float, float, float, float]]:
    raw = str(raw_value).strip()
    target = compact(raw)
    dash = raw in {"-", "–", "—"}
    spans: list[tuple[float, float, float, float]] = []
    for line in grouped_lines(words):
        for start in range(len(line)):
            combined = ""
            for end in range(start, min(len(line), start + 5)):
                combined += compact(line[end].text)
                exact_dash = dash and any(
                    character in line[end].text for character in ("-", "–", "—")
                )
                if (target and combined == target) or exact_dash:
                    selected = line[start : end + 1]
                    spans.append(
                        (
                            min(word.x0 for word in selected),
                            min(word.y0 for word in selected),
                            max(word.x1 for word in selected),
                            max(word.y1 for word in selected),
                        )
                    )
                    break
                if target and len(combined) > len(target):
                    break
    return spans


def label_blocks(
    evidence: PdfEvidence, page_numbers: list[int], label: object
) -> list[tuple[int, float, float, float, float, str]]:
    target = compact(label)
    matches = []
    for page_number in page_numbers:
        for _, x0, y0, x1, y1, text in evidence.blocks(page_number):
            if target and target in compact(text):
                matches.append((page_number, x0, y0, x1, y1, text))
    return matches


def year_headers(words: list[Word], years: set[int]) -> dict[int, list[float]]:
    # Include every fiscal year printed in the table, including a third comparative
    # year that the observation did not claim.  Otherwise a value shifted into the
    # omitted third column can look closest to the second column and falsely pass.
    result: dict[int, list[float]] = {year: [] for year in range(2015, 2026)}
    for word in words:
        match = re.match(r"^(20\d{2})", word.text)
        # Exclude running headers/page numbers at the very top of the page.
        if match and 50 < word.y0 < 250:
            result[int(match.group(1))].append(word.x)
    return result


def best_year_x(
    headers: dict[int, list[float]], year: int, value_x: float
) -> tuple[int | None, float | None]:
    candidates = [
        (candidate_year, x)
        for candidate_year, positions in headers.items()
        for x in positions
        if x > 200
    ]
    if not candidates:
        return None, None
    nearest_year, nearest_x = min(candidates, key=lambda item: abs(item[1] - value_x))
    return nearest_year, nearest_x


def audit_source_cells(observations: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    documents: dict[str, PdfEvidence] = {}
    subtotal_components = {
        (2019, 10): range(3, 10),
        (2019, 18): range(12, 18),
        (2019, 27): range(22, 27),
        (2019, 37): range(29, 37),
        (2019, 45): range(41, 45),
        (2021, 10): range(3, 10),
        (2021, 18): range(12, 18),
        (2021, 27): range(22, 27),
        (2021, 37): range(29, 37),
        (2023, 10): range(3, 10),
        (2023, 18): range(12, 18),
        (2023, 27): range(22, 27),
        (2023, 37): range(29, 37),
        (2025, 10): range(3, 10),
        (2025, 18): range(12, 18),
        (2025, 27): range(22, 27),
        (2025, 37): range(29, 37),
    }
    row_group = [
        "company",
        "document_id",
        "report_year",
        "statement",
        "page",
        "row_order",
        "reported_label",
    ]
    try:
        for key, group in observations.groupby(row_group, sort=False, dropna=False):
            (
                company,
                document_id,
                report_year,
                statement,
                page_value,
                row_order,
                label,
            ) = key
            document_id = str(document_id)
            if document_id not in documents:
                documents[document_id] = PdfEvidence(PDF_DIR / f"{document_id}.pdf")
            evidence = documents[document_id]
            page_numbers = pages(page_value)
            matches = label_blocks(evidence, page_numbers, label)
            same_block_text = [match[5] for match in matches]
            page_label_found = compact(label) in compact(
                "\n".join(evidence.text(page_number) for page_number in page_numbers)
            )

            for row in group.itertuples(index=False):
                raw = str(row.raw_value)
                target_year = int(str(row.period_end)[:4])
                same_block = any(compact(raw) in compact(text) for text in same_block_text)
                span_candidates: list[
                    tuple[int, tuple[float, float, float, float], int | None]
                ] = []
                for page_number in page_numbers:
                    words = evidence.words(page_number)
                    headers = year_headers(words, {target_year, int(report_year) - 1, int(report_year)})
                    for span in value_spans(words, raw):
                        value_x = (span[0] + span[2]) / 2
                        nearest_year, _ = best_year_x(headers, target_year, value_x)
                        span_candidates.append((page_number, span, nearest_year))

                spatial_candidates = []
                for page_number, span, nearest_year in span_candidates:
                    value_y = (span[1] + span[3]) / 2
                    for label_page, _, y0, _, y1, _ in matches:
                        if label_page == page_number and y0 - 3 <= value_y <= y1 + 3:
                            spatial_candidates.append((page_number, span, nearest_year))

                correct_column = [
                    candidate
                    for candidate in spatial_candidates
                    if candidate[2] in {None, target_year}
                ]
                wrong_column = [
                    candidate
                    for candidate in spatial_candidates
                    if candidate[2] not in {None, target_year}
                ]
                column_matches = [
                    candidate
                    for candidate in span_candidates
                    if candidate[2] in {None, target_year}
                ]
                subtotal_reconciled = False
                component_rows = subtotal_components.get(
                    (int(report_year), int(row_order))
                )
                if (
                    company == "unilever"
                    and statement == "balance_sheet"
                    and str(row.row_kind) == "subtotal"
                    and component_rows is not None
                ):
                    components = observations[
                        (observations["document_id"].astype(str) == document_id)
                        & (observations["statement"] == statement)
                        & (observations["period_end"].astype(str) == str(row.period_end))
                        & (observations["row_order"].isin(component_rows))
                    ]
                    component_values = pd.to_numeric(
                        components["value"], errors="coerce"
                    )
                    subtotal_reconciled = (
                        pd.notna(row.value)
                        and not component_values.isna().any()
                        and Decimal(str(row.value))
                        == sum(
                            (Decimal(str(value)) for value in component_values),
                            Decimal(0),
                        )
                    )

                if not raw and str(row.row_kind) == "heading":
                    status = "non_numeric_heading"
                elif correct_column:
                    status = "spatial_row_and_column_match"
                elif wrong_column:
                    status = "possible_column_mismatch"
                elif (
                    subtotal_reconciled
                    and column_matches
                    and page_label_found
                ):
                    status = "section_subtotal_reconciled"
                elif column_matches and page_label_found:
                    status = "page_label_and_column_match"
                elif same_block:
                    status = "same_pdf_block_unresolved_column"
                elif span_candidates and matches:
                    status = "page_presence_only"
                elif span_candidates:
                    status = "value_found_label_not_exact"
                else:
                    status = "value_not_found"

                records.append(
                    {
                        "company": company,
                        "report_year": int(report_year),
                        "statement": statement,
                        "document_id": document_id,
                        "source_page": page_value,
                        "row_order": int(row_order),
                        "row_kind": row.row_kind,
                        "reported_label": label,
                        "period_end": row.period_end,
                        "raw_value": raw,
                        "label_block_matches": len(matches),
                        "value_span_matches": len(span_candidates),
                        "spatial_row_matches": len(spatial_candidates),
                        "correct_column_matches": len(correct_column),
                        "wrong_column_matches": len(wrong_column),
                        "same_block": same_block,
                        "page_label_found": page_label_found,
                        "subtotal_reconciled": subtotal_reconciled,
                        "source_status": status,
                    }
                )
    finally:
        for document in documents.values():
            document.close()
    return pd.DataFrame(records)


def audit_output_cells(lineage: pd.DataFrame) -> pd.DataFrame:
    winners = lineage[lineage["winner"].astype(str).str.casefold() == "true"].copy()
    lookup_keys = [
        "company",
        "statement",
        "row_id",
        "period_end",
        "currency",
        "value_kind",
    ]
    winner_groups = {
        key: group
        for key, group in winners.groupby(lookup_keys, dropna=False, sort=False)
    }
    records: list[dict[str, object]] = []

    for company in COMPANIES:
        for statement in STATEMENTS:
            path = ARTIFACTS / f"{company}_{statement}.csv"
            table = pd.read_csv(path, dtype=str, keep_default_na=False)
            for csv_row, row in table.iterrows():
                row_id = f"{canonical_key(row['line_item'])}::{int(row['occurrence'])}"
                currency = row["currency"] if row["value_kind"] != "percent" else ""
                source_currency = row["currency"] or (
                    "CHF" if company == "nestle" else "EUR"
                )
                for period_end in PERIODS:
                    output_value = row[period_end]
                    key = (
                        company,
                        statement,
                        row_id,
                        period_end,
                        source_currency,
                        row["value_kind"],
                    )
                    matches = winner_groups.get(key, pd.DataFrame())
                    if len(matches) == 1:
                        source = matches.iloc[0]
                        if str(source["cell_status"]) == "dash":
                            expected = "—"
                        else:
                            expected = decimal_text(
                                Decimal(str(source["winning_value"]))
                                / Decimal(str(row["unit_multiplier"]))
                            )
                    else:
                        source = None
                        expected = ""

                    normalized_output = (
                        "—"
                        if output_value in {"-", "–", "—"}
                        else decimal_text(output_value)
                    )
                    if len(matches) > 1:
                        status = "ambiguous_winning_lineage"
                    elif output_value and source is None:
                        status = "published_value_missing_lineage"
                    elif not output_value and source is not None:
                        status = "published_blank_has_winner"
                    elif normalized_output != expected:
                        status = "published_value_mismatch"
                    elif output_value:
                        status = "matched"
                    else:
                        status = "blank_no_source"

                    records.append(
                        {
                            "company": company,
                            "statement": statement,
                            "csv_row": csv_row + 2,
                            "line_item": row["line_item"],
                            "occurrence": row["occurrence"],
                            "currency": currency,
                            "value_kind": row["value_kind"],
                            "unit_multiplier": row["unit_multiplier"],
                            "period_end": period_end,
                            "published_value": output_value,
                            "expected_from_lineage": expected,
                            "lineage_matches": len(matches),
                            "output_status": status,
                            "winning_report_year": (
                                source["winning_report_year"] if source is not None else ""
                            ),
                            "winning_document_id": (
                                source["winning_document_id"] if source is not None else ""
                            ),
                            "source_page": source["page"] if source is not None else "",
                            "reported_label": (
                                source["reported_label"] if source is not None else ""
                            ),
                            "raw_value": source["raw_value"] if source is not None else "",
                        }
                    )
    return pd.DataFrame(records)


def write_hashes() -> pd.DataFrame:
    paths = [
        *sorted(ARTIFACTS.glob("*.csv")),
        ARTIFACTS / "observations.parquet",
        *sorted((ROOT / "outputs").glob("*/*.xlsx")),
        *sorted(PDF_DIR.glob("*.pdf")),
    ]
    records = [
        {
            "path": str(path.relative_to(ROOT)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in paths
        if path.exists()
    ]
    return pd.DataFrame(records)


def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    observations = pd.read_parquet(ARTIFACTS / "observations.parquet")
    lineage = pd.read_csv(ARTIFACTS / "lineage.csv", dtype=str, keep_default_na=False)

    hashes = write_hashes()
    output_cells = audit_output_cells(lineage)
    source_cells = audit_source_cells(observations)

    hashes.to_csv(AUDIT_DIR / "baseline_hashes.csv", index=False)
    output_cells.to_csv(AUDIT_DIR / "cell_audit.csv", index=False)
    source_cells.to_csv(AUDIT_DIR / "source_cell_audit.csv", index=False)

    output_issues = output_cells[
        ~output_cells["output_status"].isin({"matched", "blank_no_source"})
    ].copy()
    source_issues = source_cells[
        ~source_cells["source_status"].isin(
            {
                "spatial_row_and_column_match",
                "section_subtotal_reconciled",
                "page_label_and_column_match",
                "non_numeric_heading",
            }
        )
    ].copy()
    output_issues.to_csv(AUDIT_DIR / "output_mismatches.csv", index=False)
    source_issues.to_csv(AUDIT_DIR / "source_exceptions.csv", index=False)

    print("Baseline files:", len(hashes))
    print("Published positions:", len(output_cells))
    print(output_cells["output_status"].value_counts().to_string())
    print("Source cells:", len(source_cells))
    print(source_cells["source_status"].value_counts().to_string())
    print("Audit output:", AUDIT_DIR)


if __name__ == "__main__":
    main()
