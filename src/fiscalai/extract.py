from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

import fitz
import pandas as pd

from .companies import Company
from .llm import StatementExtraction, extract_statement

STATEMENT_TITLES = {
    "income_statement": (
        "consolidated income statement",
        "consolidated statement of income",
    ),
    "balance_sheet": (
        "consolidated balance sheet",
        "consolidated statement of financial position",
    ),
    "cash_flow": (
        "consolidated cash flow statement",
        "consolidated statement of cash flows",
    ),
}
MIN_STATEMENT_SCORE = 30
TEXT_COMPANIONS = {
    "a8ef8fdd207ac63cee6af97ab6012d39ff9a6aa449cd4948309dcc47654c57c3":
        Path(
            ".cache/pdf/"
            "798661ad641ab15daa68854b13b9ae1e264e62a878090aae79ff3d07deb235d0.pdf"
        )
}
REQUIRED_ROW_ANCHORS = {
    ("heineken", "income_statement"): (
        "Other net finance income/(expenses)",
        "Weighted average number of shares – basic",
        "Weighted average number of shares – diluted",
        "Basic earnings per share (€)",
        "Diluted earnings per share (€)",
        "Shareholders of the Company (net profit)",
        "Non-controlling interests",
    ),
}


def _document_id(pdf_path: Path) -> str:
    return (
        pdf_path.stem
        if re.fullmatch(r"[0-9a-f]{64}", pdf_path.stem)
        else hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    )


def _text_source(pdf_path: Path) -> tuple[Path, str, str]:
    document_id = _document_id(pdf_path)
    companion = TEXT_COMPANIONS.get(document_id)
    if companion and companion.exists():
        return (
            companion,
            "official_pdf_with_text_companion",
            _document_id(companion),
        )
    return pdf_path, "native_pdf_text", document_id


def _page_score(text: str, statement: str) -> int:
    value = " ".join(unicodedata.normalize("NFKC", text).lower().split())
    value = re.sub(r"\bfl\s+ow\b", "flow", value)
    opening = value[:400]
    score = 0
    for title in STATEMENT_TITLES[statement]:
        if title in opening:
            score += 20
        elif title in value:
            score += 4
    if "consolidated" in value:
        score += 4
    if re.search(r"\b20(?:1[6-9]|2[0-5])\b", value):
        score += 2
    if re.search(
        r"(?:\b(chf|eur)\b|€).*\b(million|millions|mio)\b|"
        r"\b(million|millions|mio)\b.*(?:\b(chf|eur)\b|€)",
        opening,
    ):
        score += 3
    if sum(character.isdigit() for character in value) > 80:
        score += 2
    if "contents" in value[:500] or "table of contents" in value:
        score -= 15
    if "parent company" in value or "nestlé s.a. income statement" in value:
        score -= 12
    return score


def locate_statements(pdf_path: Path) -> dict[str, dict[str, object]]:
    candidates: dict[str, dict[str, object]] = {}
    text_path, _, _ = _text_source(pdf_path)
    with fitz.open(text_path) as document:
        ranked_by_statement: dict[str, list[dict[str, object]]] = {
            statement: [] for statement in STATEMENT_TITLES
        }
        for page_index, page in enumerate(document):
            text = page.get_text("text", sort=True)
            block_text = "\n".join(
                str(block[4]) for block in page.get_text("blocks", sort=True)
            )
            for statement in STATEMENT_TITLES:
                ranked_by_statement[statement].append(
                    {
                        "page_index": page_index,
                        "page_number": page_index + 1,
                        "score": _page_score(text, statement),
                        "text": text,
                        "block_text": block_text,
                    }
                )
        for statement in STATEMENT_TITLES:
            ranked = ranked_by_statement[statement]
            ranked.sort(key=lambda item: (-int(item["score"]), int(item["page_index"])))
            candidate = ranked[0]
            if int(candidate["score"]) < MIN_STATEMENT_SCORE:
                raise RuntimeError(
                    f"Could not confidently locate {statement} in {pdf_path}; "
                    f"best page was {candidate['page_number']} with score {candidate['score']}. "
                    "The statement may be scanned or use an unsupported title."
                )
            page_indices = [int(candidate["page_index"])]
            next_index = page_indices[0] + 1
            if next_index < len(document):
                next_text = document[next_index].get_text("text", sort=True)
                next_opening = " ".join(
                    unicodedata.normalize("NFKC", next_text).lower().split()
                )[:400]
                next_opening = re.sub(r"\bfl\s+ow\b", "flow", next_opening)
                if any(title in next_opening for title in STATEMENT_TITLES[statement]):
                    page_indices.append(next_index)
                    candidate["text"] = f"{candidate['text']}\n\n{next_text}"
                    next_block_text = "\n".join(
                        str(block[4])
                        for block in document[next_index].get_text("blocks", sort=True)
                    )
                    candidate["block_text"] = (
                        f"{candidate['block_text']}\n\n{next_block_text}"
                    )
            candidate["page_numbers"] = [index + 1 for index in page_indices]
            candidate["page_number"] = "-".join(str(index + 1) for index in page_indices)
            candidates[statement] = candidate
    return candidates


def write_candidates(
    company: Company,
    report_year: int,
    pdf_path: Path,
    artifacts_dir: Path = Path("artifacts"),
) -> dict[str, dict[str, object]]:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    candidates = locate_statements(pdf_path)
    serializable = {
        statement: {
            key: value
            for key, value in candidate.items()
            if key not in {"text", "block_text"}
        }
        for statement, candidate in candidates.items()
    }
    path = artifacts_dir / f"{company.slug}_{report_year}_candidates.json"
    path.write_text(json.dumps(serializable, indent=2) + "\n")
    return candidates


def _source_contains(source: str, exact: str) -> bool:
    compact_source = re.sub(r"\s+", " ", source).casefold()
    compact_exact = re.sub(r"\s+", " ", exact).casefold()
    return not compact_exact or compact_exact in compact_source


def _validate_evidence(extraction: StatementExtraction, evidence: str) -> None:
    for row in extraction.rows:
        if not _source_contains(evidence, row.exact_label):
            raise ValueError(f"Extracted label not found in source evidence: {row.exact_label!r}")
        for cell in row.cells:
            if not _source_contains(evidence, cell.raw_value):
                raise ValueError(
                    f"Extracted value {cell.raw_value!r} for {row.exact_label!r} "
                    "not found in source evidence"
                )


def _statement_metadata(
    evidence: str,
    fallback_currency: str,
    fallback_multiplier: int,
) -> tuple[str, int]:
    opening = unicodedata.normalize("NFKC", evidence[:1200]).casefold()
    if "chf" in opening:
        currency = "CHF"
    elif "eur" in opening or "€" in opening or "euro" in opening:
        currency = "EUR"
    else:
        currency = fallback_currency

    if re.search(r"\b(million|millions|mio)\b", opening):
        multiplier = 1_000_000
    elif re.search(r"\b(thousand|thousands)\b", opening):
        multiplier = 1_000
    else:
        multiplier = fallback_multiplier
    return currency, multiplier


def _period_end(value: str) -> str:
    return f"{value}-12-31" if re.fullmatch(r"20\d{2}", value) else value


def _left_statement_column(
    pdf_path: Path,
    page_index: int,
) -> tuple[str, str]:
    text_path, _, _ = _text_source(pdf_path)
    with fitz.open(text_path) as document:
        page = document[page_index]
        clip = fitz.Rect(0, 0, page.rect.width * 0.52, page.rect.height)
        text = page.get_text("text", clip=clip, sort=True)
        block_text = "\n".join(
            str(block[4])
            for block in page.get_text("blocks", clip=clip, sort=True)
        )
    return text, block_text


def _source_visible_required_labels(
    evidence: str,
    company: str,
    statement: str,
) -> tuple[str, ...]:
    compact = " ".join(unicodedata.normalize("NFKC", evidence).casefold().split())
    return tuple(
        label
        for label in REQUIRED_ROW_ANCHORS.get((company, statement), ())
        if " ".join(unicodedata.normalize("NFKC", label).casefold().split()) in compact
    )


def extract_report(
    company: Company,
    report_year: int,
    pdf_path: Path,
    artifacts_dir: Path = Path("artifacts"),
    persist: bool = True,
) -> pd.DataFrame:
    _, source_text_method, document_id = _text_source(pdf_path)
    candidates = locate_statements(pdf_path)
    rows: list[dict[str, object]] = []

    for statement, candidate in candidates.items():
        evidence = str(candidate["text"])
        block_evidence = str(candidate["block_text"])
        focus_hint = ""
        if (
            statement == "income_statement"
            and "statement of other comprehensive income" in evidence.casefold()
        ):
            evidence, block_evidence = _left_statement_column(
                pdf_path, int(candidate["page_index"])
            )
            focus_hint = (
                "The page also contains a separate statement of other comprehensive "
                "income. Extract only the table headed Consolidated Income Statement. "
                "Do not take attributable-profit rows or values from the comprehensive-"
                "income table."
            )
        try:
            extraction = extract_statement(
                statement,
                evidence,
                focus_hint,
                _source_visible_required_labels(evidence, company.slug, statement),
            )
            _validate_evidence(
                extraction,
                f"{evidence}\n\nBLOCK-ORDER EVIDENCE:\n{block_evidence}",
            )
        except Exception as exc:
            raise RuntimeError(
                f"{company.slug} {report_year} {statement} extraction failed: {exc}"
            ) from exc
        currency, unit_multiplier = _statement_metadata(
            evidence,
            extraction.currency,
            extraction.unit_multiplier,
        )
        for row in extraction.rows:
            if row.row_kind == "heading" and not row.cells:
                continue
            for cell in row.cells:
                rows.append(
                    {
                        "company": company.slug,
                        "document_id": document_id,
                        "report_year": report_year,
                        "statement": statement,
                        "page": candidate["page_number"],
                        "row_order": row.row_order,
                        "row_kind": row.row_kind,
                        "reported_label": row.exact_label,
                        "footnote_marker": row.footnote_marker,
                        "period_end": _period_end(cell.period_end),
                        "raw_value": cell.raw_value,
                        "currency": currency,
                        "unit_multiplier": unit_multiplier,
                        "extraction_method": (
                            f"llm_structured_{source_text_method}"
                        ),
                    }
                )

    observations = pd.DataFrame(rows)
    if not persist:
        return observations

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    observations_path = artifacts_dir / "observations.parquet"
    if observations_path.exists():
        existing = pd.read_parquet(observations_path)
        keep = ~(
            (existing["company"] == company.slug)
            & (existing["report_year"] == report_year)
        )
        observations = pd.concat([existing[keep], observations], ignore_index=True)
    observations.to_parquet(observations_path, index=False)
    return observations
