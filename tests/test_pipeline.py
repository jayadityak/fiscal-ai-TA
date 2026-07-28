from decimal import Decimal

import pandas as pd

from fiscalai.compile import (
    canonical_key,
    display_label,
    normalize_currency,
    parse_number,
    prepare_observations,
    resolve_restatements,
)
from fiscalai.extract import (
    MIN_STATEMENT_SCORE,
    TEXT_COMPANIONS,
    apply_source_row_repairs,
    _document_id,
    _page_score,
    _text_source,
)
from fiscalai.llm import (
    CanonicalGroup,
    Canonicalization,
    _cache_key,
    _parse_cached,
    canonicalize_statement_labels,
)
from fiscalai.scrape import classify_pdf, infer_report_year
from fiscalai.validate import (
    validate_balance_sheet,
    write_reconciliation,
    write_validation,
)


def test_parse_number_preserves_dash_and_parentheses() -> None:
    assert parse_number("(1,234)") == (Decimal("-1234"), "number")
    assert parse_number("2\u202f345") == (Decimal("2345"), "number")
    assert parse_number("14.8%") == (Decimal("14.8"), "number")
    assert normalize_currency("€") == "EUR"
    assert normalize_currency("€ million") == "EUR"
    assert parse_number("—") == (None, "dash")
    assert parse_number("") == (None, "missing")
    assert display_label("  Share of net profit\n  from associates ") == (
        "Share of net profit from associates"
    )


def test_classification_is_direct_and_explainable() -> None:
    assert classify_pdf("Nestlé Financial Statements 2024")[0] == "annual_report"
    assert classify_pdf("Creating Shared Value Sustainability Report")[0] == "sustainability"
    assert infer_report_year("Annual report 2025, published February 2026") == 2025
    assert infer_report_year("heineken-nv-annual-report-2021-25-02-2022.pdf") == 2021


def test_cached_semantic_result_does_not_require_api_key(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task = "test"
    instructions = "instructions"
    payload = "payload"
    model = "gpt-5-mini"
    result = Canonicalization(
        groups=[CanonicalGroup(canonical_label="Sales", members=["Sales"])]
    )
    key = _cache_key(
        task,
        model,
        instructions,
        payload,
        Canonicalization,
    )
    (tmp_path / f"{key}.json").write_text(result.model_dump_json())
    assert _parse_cached(
        task,
        instructions,
        payload,
        Canonicalization,
        tmp_path,
    ) == result


def test_all_companies_share_the_requested_window() -> None:
    from fiscalai.companies import COMPANIES

    assert set(COMPANIES) == {"nestle", "heineken", "unilever"}
    for company in COMPANIES.values():
        assert company.target_years == tuple(range(2016, 2026))
        assert company.selected_report_years == (2017, 2019, 2021, 2023, 2025)


def test_statement_title_at_top_beats_a_note_reference() -> None:
    statement = (
        "Consolidated income statement\nIn millions of CHF\n2025 2024\nSales 10 9"
        + " 123" * 100
    )
    note = ("8. Property, plant and equipment " * 20) + " consolidated income statement " + (
        "123 " * 100
    )
    assert _page_score(statement, "income_statement") > _page_score(note, "income_statement")
    assert _page_score(statement, "income_statement") >= MIN_STATEMENT_SCORE


def test_companion_pdf_becomes_the_source_document_id(
    tmp_path,
    monkeypatch,
) -> None:
    official = tmp_path / "official.pdf"
    companion = tmp_path / "companion.pdf"
    official.write_bytes(b"official")
    companion.write_bytes(b"companion")
    official_id = _document_id(official)
    companion_id = _document_id(companion)
    monkeypatch.setitem(TEXT_COMPANIONS, official_id, companion)

    source_path, method, source_id = _text_source(official)

    assert source_path == companion
    assert method == "official_pdf_with_text_companion"
    assert source_id == companion_id


def test_latest_selected_report_wins() -> None:
    rows = pd.DataFrame(
        [
            {
                "company": "nestle",
                "document_id": "old",
                "report_year": 2022,
                "statement": "income_statement",
                "reported_label": "Sales",
                "period_end": "2021-12-31",
                "raw_value": "10",
                "currency": "CHF",
            },
            {
                "company": "nestle",
                "document_id": "new",
                "report_year": 2024,
                "statement": "income_statement",
                "reported_label": "Sales",
                "period_end": "2021-12-31",
                "raw_value": "12",
                "currency": "CHF",
            },
        ]
    )
    winners, lineage = resolve_restatements(prepare_observations(rows))
    assert winners.iloc[0]["value"] == "12"
    assert lineage["restated"].sum() == 1
    assert set(lineage["winning_report_year"]) == {2024}


def test_repeated_labels_remain_distinct_rows() -> None:
    rows = pd.DataFrame(
        [
            {
                "company": "heineken",
                "document_id": "report",
                "report_year": 2025,
                "statement": "balance_sheet",
                "row_order": row_order,
                "reported_label": "Borrowings",
                "period_end": "2025-12-31",
                "raw_value": raw_value,
                "currency": "EUR",
            }
            for row_order, raw_value in ((8, "16,191"), (21, "3,088"))
        ]
    )
    prepared = prepare_observations(rows)
    winners, _ = resolve_restatements(prepared)
    assert winners["reported_occurrence"].tolist() == [1, 2]
    assert winners["value"].tolist() == ["16191", "3088"]


def test_weighted_average_share_count_is_not_scaled_as_currency() -> None:
    rows = pd.DataFrame(
        [
            {
                "company": "heineken",
                "document_id": "report",
                "report_year": 2025,
                "statement": "income_statement",
                "row_order": 1,
                "reported_label": "Weighted average number of shares – basic",
                "period_end": "2025-12-31",
                "raw_value": "556,774,934",
                "currency": "EUR",
                "unit_multiplier": 1_000_000,
            }
        ]
    )
    prepared = prepare_observations(rows)
    assert prepared.iloc[0]["value_kind"] == "shares"
    assert prepared.iloc[0]["effective_multiplier"] == 1
    assert prepared.iloc[0]["value"] == "556774934"


def test_verified_source_repairs_reread_values_from_pdf(tmp_path) -> None:
    import fitz

    source = tmp_path / "report.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((350, 100), "2019")
    page.insert_text((400, 100), "2018*")
    page.insert_text((40, 200), "Total change in working capital")
    page.insert_text((350, 200), "8")
    page.insert_text((400, 200), "713")
    document.save(source)
    document.close()

    rows = pd.DataFrame(
        [
            {
                "company": "heineken",
                "document_id": _document_id(source),
                "report_year": 2019,
                "statement": "cash_flow",
                "page": "1",
                "row_order": 28,
                "row_kind": "line_item",
                "reported_label": "Total change in working capital",
                "footnote_marker": "",
                "period_end": "2019-12-31",
                "raw_value": "713",
                "currency": "EUR",
                "unit_multiplier": 1_000_000,
                "extraction_method": "llm_structured_native_pdf_text",
            }
        ]
    )
    corrected = apply_source_row_repairs(rows, source).sort_values("period_end")
    assert corrected[["period_end", "raw_value"]].values.tolist() == [
        ["2018-12-31", "713"],
        ["2019-12-31", "8"],
    ]
    assert corrected["extraction_method"].str.endswith(
        "_verified_pdf_spatial_repair"
    ).all()


def test_reconciliation_rejects_incomplete_line_item_periods(tmp_path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"source")
    document_id = _document_id(source)
    observations = pd.DataFrame(
        [
            {
                "company": "heineken",
                "document_id": document_id,
                "report_year": 2019,
                "statement": "cash_flow",
                "page": "66",
                "row_order": 1,
                "row_kind": "line_item",
                "reported_label": "Complete row",
                "period_end": period,
                "raw_value": value,
            }
            for period, value in (
                ("2019-12-31", "10"),
                ("2018-12-31", "9"),
            )
        ]
        + [
            {
                "company": "heineken",
                "document_id": document_id,
                "report_year": 2019,
                "statement": "cash_flow",
                "page": "66",
                "row_order": 2,
                "row_kind": "line_item",
                "reported_label": "Incomplete row",
                "period_end": "2019-12-31",
                "raw_value": "7",
            }
        ]
    )
    manifest = pd.DataFrame(
        [
            {
                "company": "heineken",
                "report_year": 2019,
                "status": "downloaded",
                "document_type": "annual_report",
                "local_path": str(source),
            }
        ]
    )
    result = write_reconciliation(observations, manifest, tmp_path)
    assert result.iloc[0]["incomplete_line_item_rows"] == 1
    assert result.iloc[0]["status"] == "failed"


def test_labels_that_coexist_cannot_be_canonicalized_together(
    monkeypatch,
) -> None:
    proposed = Canonicalization(
        groups=[
            CanonicalGroup(
                canonical_label="Cash flow from operations",
                members=[
                    "Cash flow from operations",
                    "Cash flow from operating activities",
                ],
            )
        ]
    )
    monkeypatch.setattr("fiscalai.llm._parse_cached", lambda *args, **kwargs: proposed)
    records = [
        {
            "reported_label": label,
            "latest_report_year": 2025,
            "occurrences": [{"report_year": 2025, "row_order": row_order}],
        }
        for row_order, label in enumerate(proposed.groups[0].members)
    ]
    result = canonicalize_statement_labels("cash_flow", records)
    assert sorted(group.members for group in result.groups) == [
        ["Cash flow from operating activities"],
        ["Cash flow from operations"],
    ]


def test_balance_sheet_identity() -> None:
    winners = pd.DataFrame(
        [
            {
                "statement": "balance_sheet",
                "canonical_id": canonical_key("Total assets"),
                "report_year": 2024,
                "value": "100",
            },
            {
                "statement": "balance_sheet",
                "canonical_id": canonical_key("Total liabilities"),
                "report_year": 2024,
                "value": "60",
            },
            {
                "statement": "balance_sheet",
                "canonical_id": canonical_key("Total equity"),
                "report_year": 2024,
                "value": "40",
            },
        ]
    )
    assert validate_balance_sheet(winners)["status"] == "passed"


def test_validation_is_scoped_by_company(tmp_path) -> None:
    rows = []
    for company, assets in (("nestle", "100"), ("heineken", "200")):
        rows.extend(
            [
                {
                    "company": company,
                    "statement": "balance_sheet",
                    "canonical_id": "total assets",
                    "report_year": 2025,
                    "period_end": "2025-12-31",
                    "unit_multiplier": 1,
                    "value": assets,
                },
                {
                    "company": company,
                    "statement": "balance_sheet",
                    "canonical_id": "total liabilities",
                    "report_year": 2025,
                    "period_end": "2025-12-31",
                    "unit_multiplier": 1,
                    "value": str(Decimal(assets) - Decimal("40")),
                },
                {
                    "company": company,
                    "statement": "balance_sheet",
                    "canonical_id": "total equity",
                    "report_year": 2025,
                    "period_end": "2025-12-31",
                    "unit_multiplier": 1,
                    "value": "40",
                },
            ]
        )
    result = write_validation(pd.DataFrame(rows), tmp_path)
    balance_results = result[
        result["check"] == "assets_equal_liabilities_plus_equity"
    ].set_index("company")
    assert balance_results["status"].to_dict() == {
        "nestle": "passed",
        "heineken": "passed",
    }


def test_balance_check_can_sum_current_and_non_current_liabilities(
    tmp_path,
) -> None:
    rows = pd.DataFrame(
        [
            {
                "company": "heineken",
                "statement": "balance_sheet",
                "canonical_id": canonical_key(label),
                "report_year": 2025,
                "period_end": "2025-12-31",
                "effective_multiplier": 1,
                "value": value,
            }
            for label, value in (
                ("Total assets", "100"),
                ("Total current liabilities", "30"),
                ("Total non-current liabilities", "20"),
                ("Total equity", "50"),
            )
        ]
    )
    result = write_validation(rows, tmp_path)
    balance = result[
        result["check"] == "assets_equal_liabilities_plus_equity"
    ].iloc[-1]
    assert balance["status"] == "passed"
