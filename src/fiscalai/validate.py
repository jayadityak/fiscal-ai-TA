from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd

from .companies import COMPANIES
from .compile import canonical_key


def _latest_value(rows: pd.DataFrame, labels: tuple[str, ...]) -> Decimal | None:
    matches = rows[rows["canonical_id"].isin(labels)].sort_values("report_year", ascending=False)
    if matches.empty or matches.iloc[0]["value"] == "":
        return None
    return Decimal(matches.iloc[0]["value"])


def validate_balance_sheet(winners: pd.DataFrame) -> dict[str, object]:
    rows = winners[winners["statement"] == "balance_sheet"]
    assets = _latest_value(rows, ("total assets",))
    liabilities = _latest_value(rows, ("total liabilities",))
    equity = _latest_value(rows, ("total equity", "total shareholders equity"))

    if assets is None or liabilities is None or equity is None:
        return {
            "check": "assets_equal_liabilities_plus_equity",
            "status": "failed",
            "actual": "",
            "expected": "",
            "difference": "",
            "message": "Required total rows were not found",
        }

    expected = liabilities + equity
    difference = assets - expected
    return {
        "check": "assets_equal_liabilities_plus_equity",
        "status": "passed" if abs(difference) <= Decimal("1") else "failed",
        "actual": str(assets),
        "expected": str(expected),
        "difference": str(difference),
        "message": "Tolerance is one reported presentation unit",
    }


def _period_value(
    rows: pd.DataFrame,
    period_end: str,
    labels: tuple[str, ...],
) -> tuple[Decimal, Decimal] | None:
    canonical_labels = [canonical_key(label) for label in labels]
    period_rows = rows[rows["period_end"] == period_end]
    for label in canonical_labels:
        matches = period_rows[
            (period_rows["canonical_id"] == label) & (period_rows["value"] != "")
        ]
        if not matches.empty:
            row = matches.sort_values("report_year", ascending=False).iloc[0]
            return Decimal(row["value"]), Decimal(
                str(row.get("effective_multiplier", row.get("unit_multiplier", 1)))
            )
    return None


def _period_sum(
    values: list[tuple[Decimal, Decimal] | None],
) -> tuple[Decimal, Decimal] | None:
    if any(value is None for value in values):
        return None
    present = [value for value in values if value is not None]
    return (
        sum((value for value, _ in present), Decimal(0)),
        max(unit for _, unit in present),
    )


def _result(
    check: str,
    period_end: str,
    actual: tuple[Decimal, Decimal] | None,
    components: list[tuple[Decimal, Decimal] | None],
    component_names: tuple[str, ...],
) -> dict[str, object]:
    if actual is None or any(component is None for component in components):
        missing = []
        if actual is None:
            missing.append("total")
        missing.extend(
            name
            for name, component in zip(component_names, components, strict=True)
            if component is None
        )
        return {
            "check": check,
            "period_end": period_end,
            "status": "skipped",
            "actual": "",
            "expected": "",
            "difference": "",
            "message": f"Required rows not found: {', '.join(missing)}",
        }

    actual_value, actual_unit = actual
    component_values = [component[0] for component in components if component is not None]
    component_units = [component[1] for component in components if component is not None]
    expected = sum(component_values, Decimal(0))
    difference = actual_value - expected
    tolerance = max([actual_unit, *component_units])
    return {
        "check": check,
        "period_end": period_end,
        "status": "passed" if abs(difference) <= tolerance else "failed",
        "actual": str(actual_value),
        "expected": str(expected),
        "difference": str(difference),
        "message": f"Tolerance is one largest source presentation unit ({tolerance})",
    }


def _period_checks(rows: pd.DataFrame, period_end: str) -> list[dict[str, object]]:
    balance = rows[rows["statement"] == "balance_sheet"]
    income = rows[rows["statement"] == "income_statement"]
    cash_flow = rows[rows["statement"] == "cash_flow"]

    balance_check = _result(
        "assets_equal_liabilities_plus_equity",
        period_end,
        _period_value(balance, period_end, ("Total assets",)),
        [
            _period_value(balance, period_end, ("Total liabilities",))
            or _period_sum(
                [
                    _period_value(
                        balance,
                        period_end,
                        ("Total current liabilities", "Current liabilities"),
                    ),
                    _period_value(
                        balance,
                        period_end,
                        ("Total non-current liabilities", "Non-current liabilities"),
                    ),
                ]
            ),
            _period_value(
                balance,
                period_end,
                ("Total equity", "Total shareholders' equity"),
            ),
        ],
        ("liabilities", "equity"),
    )
    attributable_check = _result(
        "profit_equals_parent_plus_non_controlling_interests",
        period_end,
        _period_value(
            income,
            period_end,
            (
                "Profit for the year",
                "Net profit",
                "Net income",
                "Profit",
                "Profit/(Loss)",
                "Total net profit",
            ),
        ),
        [
            _period_value(
                income,
                period_end,
                (
                    "of which attributable to shareholders of the parent (Net profit)",
                    "Attributable to shareholders of the parent",
                    "Attributable to owners of the parent",
                    "Net profit attributable to shareholders of the Company",
                    "Equity holders of the Company (net profit)",
                    "Shareholders of the Company (net profit)",
                    "Shareholders' equity",
                ),
            ),
            _period_value(
                income,
                period_end,
                (
                    "of which attributable to non-controlling interests",
                    "Attributable to non-controlling interests",
                    "Non-controlling interests",
                ),
            ),
        ],
        ("parent shareholders", "non-controlling interests"),
    )
    cash_components = [
        _period_value(
            cash_flow,
            period_end,
            (
                "Cash and cash equivalents at beginning of year",
                "Cash and cash equivalents at the beginning of the year",
                "Cash and cash equivalents at 1 January",
                "Cash and cash equivalents as at 1 January",
            ),
        ),
        _period_value(
            cash_flow,
            period_end,
            (
                "Net increase/(decrease) in cash and cash equivalents",
                "Increase/(decrease) in cash and cash equivalents",
                "Net increase in cash and cash equivalents",
                "Net decrease in cash and cash equivalents",
                "Net cash flow",
            ),
        ),
    ]
    cash_component_names = ["opening cash", "net change"]
    exchange_effect = _period_value(
        cash_flow,
        period_end,
        (
            "Effect of exchange rate changes",
            "Effect of foreign exchange rate changes",
            "Exchange differences",
            "Effect of movements in exchange rates",
        ),
    )
    if exchange_effect is not None:
        cash_components.append(exchange_effect)
        cash_component_names.append("exchange-rate effect")
    cash_check = _result(
        "ending_cash_equals_opening_cash_plus_change",
        period_end,
        _period_value(
            cash_flow,
            period_end,
            (
                "Cash and cash equivalents at end of year",
                "Cash and cash equivalents at the end of the year",
                "Cash and cash equivalents at 31 December",
                "Cash and cash equivalents as at 31 December",
            ),
        ),
        cash_components,
        tuple(cash_component_names),
    )
    return [balance_check, attributable_check, cash_check]


def write_validation(
    winners: pd.DataFrame,
    artifacts_dir: Path = Path("artifacts"),
) -> pd.DataFrame:
    results = []
    for company, rows in winners.groupby("company", sort=False):
        expected_periods = [
            f"{year}-12-31" for year in COMPANIES[company].target_years
        ]
        for statement in ("income_statement", "balance_sheet", "cash_flow"):
            actual_periods = set(
                rows.loc[rows["statement"] == statement, "period_end"].astype(str)
            )
            missing = sorted(set(expected_periods) - actual_periods)
            results.append(
                {
                    "company": company,
                    "check": f"{statement}_coverage_2016_2025",
                    "period_end": "",
                    "status": "passed" if not missing else "failed",
                    "actual": str(len(set(expected_periods) & actual_periods)),
                    "expected": "10",
                    "difference": str(-len(missing)) if missing else "0",
                    "message": (
                        "All target periods present"
                        if not missing
                        else f"Missing periods: {', '.join(missing)}"
                    ),
                }
            )
        for period_end in expected_periods:
            for result in _period_checks(rows, period_end):
                result["company"] = company
                results.append(result)
    result = pd.DataFrame(results)
    result.to_csv(artifacts_dir / "validation.csv", index=False)
    return result
