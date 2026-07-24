from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from .companies import COMPANIES
from .llm import canonicalize_statement_labels


def parse_number(raw: str) -> tuple[Decimal | None, str]:
    value = unicodedata.normalize("NFKC", raw).strip()
    value = re.sub(r"[\u00a0\u202f\s]", "", value)
    if value in {"", "-", "–", "—"}:
        return None, "dash" if value else "missing"

    value = value.removesuffix("%")
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
    value = value.replace("−", "-").replace("'", "").replace(",", "")
    value = re.sub(r"(?<=\d)[a-zA-Z*]+$", "", value)
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse numeric value {raw!r}") from exc
    return (-number if negative else number), "number"


def canonical_key(label: str) -> str:
    value = unicodedata.normalize("NFKC", label).casefold()
    value = re.sub(r"[\s\W_]+", " ", value).strip()
    return value


def presentation_section(company: str, statement: str, label: str) -> int:
    if company != "heineken":
        return 0
    value = canonical_key(label)
    if statement == "cash_flow":
        cash_rollforward = {
            "net cash flow",
            "cash and cash equivalents as at 1 january",
            "effect of movements in exchange rates",
            "cash and cash equivalents as at 31 december",
        }
        financing = {
            "proceeds from borrowings",
            "repayment of borrowings",
            "payment of principal portion of lease commitments",
            "payment of lease commitments",
            "dividends paid",
            "purchase own shares and shares issued",
            "acquisition of non controlling interests",
            "cash flow from used in financing activities",
        }
        investing = {
            "proceeds from sale of property plant and equipment and intangible assets",
            "purchase of property plant and equipment",
            "purchase of intangible assets",
            "loans issued to customers and other investments",
            "repayment on loans to customers and other investments",
            "cash flow used in operational investing activities",
            "free operating cash flow",
            "acquisition of subsidiaries net of cash acquired",
            "acquisition of additions to associates joint ventures and other investments",
            "disposal of subsidiaries net of cash disposed of",
            "disposal of associates joint ventures and other investments",
            "cash flow from used in acquisitions and disposals",
            "cash flow used in investing activities",
        }
        if value in cash_rollforward:
            return 3
        if value in financing:
            return 2
        if value in investing:
            return 1
        return 0
    if statement != "balance_sheet":
        return 0
    if value == "total equity and liabilities":
        return 3
    equity_labels = {
        "shareholders equity",
        "non controlling interests",
        "total equity",
        "share capital",
        "share premium",
        "reserves",
        "retained earnings",
        "equity attributable to equity holders of the company",
    }
    if value in equity_labels:
        return 1
    asset_labels = {
        "intangible assets",
        "property plant and equipment",
        "investments in associates and joint ventures",
        "loans and advances to customers",
        "deferred tax assets",
        "equity instruments",
        "other non current assets",
        "inventories",
        "trade and other receivables",
        "current tax assets",
        "derivative assets",
        "cash and cash equivalents",
        "assets classified as held for sale",
        "total non current assets",
        "total current assets",
        "total assets",
        "other investments and receivables",
        "advances to customers",
        "prepayments",
    }
    return 0 if value in asset_labels else 2


def normalize_currency(currency: str) -> str:
    value = unicodedata.normalize("NFKC", currency).strip().casefold()
    if "€" in value or "eur" in value or "euro" in value:
        return "EUR"
    if "chf" in value or "swiss franc" in value:
        return "CHF"
    return currency.strip().upper()


def prepare_observations(observations: pd.DataFrame) -> pd.DataFrame:
    frame = observations.copy()
    if "row_order" not in frame:
        frame["row_order"] = frame.groupby(
            ["company", "statement", "report_year"], sort=False
        ).cumcount()
    occurrence_group = ["company", "statement", "report_year", "reported_label"]
    if "document_id" in frame:
        occurrence_group.insert(1, "document_id")
    frame["reported_occurrence"] = (
        frame.groupby(occurrence_group, sort=False)["row_order"]
        .rank(method="dense")
        .astype(int)
    )
    frame["currency"] = frame["currency"].map(normalize_currency)
    parsed = frame["raw_value"].map(parse_number)
    source_multipliers = (
        frame["unit_multiplier"]
        if "unit_multiplier" in frame
        else pd.Series(1, index=frame.index)
    )
    frame["value_kind"] = [
        (
            "percent"
            if unicodedata.normalize("NFKC", str(raw)).strip().endswith("%")
            else (
                "currency_per_share"
                if "per share" in canonical_key(str(label))
                else "currency"
            )
        )
        for raw, label in zip(frame["raw_value"], frame["reported_label"], strict=True)
    ]
    frame["effective_multiplier"] = [
        1 if kind in {"percent", "currency_per_share"} else int(multiplier)
        for kind, multiplier in zip(
            frame["value_kind"], source_multipliers, strict=True
        )
    ]
    frame["value"] = [
        str(number * Decimal(str(multiplier))) if number is not None else ""
        for (number, _), multiplier in zip(
            parsed, frame["effective_multiplier"], strict=True
        )
    ]
    frame["cell_status"] = parsed.map(lambda item: item[1])
    frame["canonical_id"] = frame["reported_label"].map(canonical_key)
    frame["canonical_label"] = frame["reported_label"]
    frame["row_id"] = (
        frame["canonical_id"] + "::" + frame["reported_occurrence"].astype(str)
    )
    return frame


def canonicalize_observations(observations: pd.DataFrame) -> pd.DataFrame:
    frame = observations.copy()
    tasks = []
    for (_, statement), rows in frame.groupby(["company", "statement"], sort=False):
        label_records = []
        for label, occurrences in rows.groupby("reported_label", sort=False):
            ordered = occurrences.sort_values(
                ["report_year", "row_order"], ascending=[False, True]
            )
            latest = ordered.iloc[0]
            label_records.append(
                {
                    "reported_label": label,
                    "latest_report_year": int(latest["report_year"]),
                    "latest_row_order": int(latest["row_order"]),
                    "row_kinds": sorted(set(ordered["row_kind"].astype(str))),
                    "occurrences": [
                        {
                            "report_year": int(row.report_year),
                            "row_order": int(row.row_order),
                        }
                        for row in ordered.itertuples()
                    ],
                }
            )
        label_records.sort(key=lambda record: str(record["reported_label"]).casefold())
        tasks.append((rows.index, str(statement), label_records))

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(canonicalize_statement_labels, statement, records): row_index
            for row_index, statement, records in tasks
        }
        for future in as_completed(futures):
            row_index = futures[future]
            grouping = future.result()
            mapping = {
                member: group.canonical_label
                for group in grouping.groups
                for member in group.members
            }
            frame.loc[row_index, "canonical_label"] = frame.loc[
                row_index, "reported_label"
            ].map(mapping)
            frame.loc[row_index, "canonical_id"] = frame.loc[
                row_index, "canonical_label"
            ].map(canonical_key)
            frame.loc[row_index, "row_id"] = (
                frame.loc[row_index, "canonical_id"]
                + "::"
                + frame.loc[row_index, "reported_occurrence"].astype(str)
            )
    return frame


def resolve_restatements(observations: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    key = [
        "company",
        "statement",
        "row_id",
        "period_end",
        "currency",
        "value_kind",
    ]
    eligible = observations[observations["cell_status"].isin(["number", "dash"])].copy()
    eligible = eligible.sort_values(
        [*key, "report_year", "document_id"],
        ascending=[True] * len(key) + [False, True],
        kind="stable",
    )
    eligible["winner"] = ~eligible.duplicated(key, keep="first")
    winners = eligible[eligible["winner"]].copy()

    winning_values = winners[
        key + ["report_year", "document_id", "value", "cell_status"]
    ].rename(
        columns={
            "report_year": "winning_report_year",
            "document_id": "winning_document_id",
            "value": "winning_value",
            "cell_status": "winning_cell_status",
        }
    )
    lineage = eligible.merge(winning_values, on=key, how="left")
    lineage["superseded"] = ~lineage["winner"]
    lineage["restated"] = (
        lineage["superseded"]
        & (
            (lineage["value"] != lineage["winning_value"])
            | (lineage["cell_status"] != lineage["winning_cell_status"])
        )
    )
    return winners, lineage


def write_outputs(
    observations: pd.DataFrame,
    artifacts_dir: Path = Path("artifacts"),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prepared = canonicalize_observations(prepare_observations(observations))
    winners, lineage = resolve_restatements(prepared)
    lineage.to_csv(artifacts_dir / "lineage.csv", index=False)

    for (company, statement), group in winners.groupby(["company", "statement"], sort=False):
        target_periods = [f"{year}-12-31" for year in COMPANIES[company].target_years]
        group = group[group["period_end"].isin(target_periods)].copy()
        currencies = set(group["currency"])
        if len(currencies) != 1:
            raise RuntimeError(f"{company} {statement} contains mixed currencies: {currencies}")
        group["row_key"] = list(
            zip(group["row_id"], group["value_kind"], strict=True)
        )
        latest_rows = (
            group.sort_values(["report_year", "row_order"], ascending=[False, True])
            .drop_duplicates("row_key")
            .set_index("row_key")
        )
        display_multipliers = latest_rows["effective_multiplier"].astype(int)
        group["display_multiplier"] = group["row_key"].map(display_multipliers)
        group["display_value"] = group["value"].map(
            lambda value: value
        )
        valued = group["display_value"] != ""
        group.loc[valued, "display_value"] = [
            str(Decimal(value) / Decimal(str(multiplier)))
            for value, multiplier in zip(
                group.loc[valued, "value"],
                group.loc[valued, "display_multiplier"],
                strict=True,
            )
        ]
        latest_order = (
            group.sort_values(["report_year", "row_order"], ascending=[False, True])
            .drop_duplicates("row_key")
            .set_index("row_key")
        )
        table = group.pivot(
            index="row_key", columns="period_end", values="display_value"
        ).reindex(columns=target_periods)
        table["_section"] = [
            presentation_section(
                str(company),
                str(statement),
                str(latest_order.at[row_key, "canonical_label"]),
            )
            for row_key in table.index
        ]
        table["_report_order"] = table.index.map(
            -latest_order["report_year"].astype(int)
        )
        table["_row_order"] = table.index.map(latest_order["row_order"].astype(int))
        table = (
            table.sort_values(["_section", "_report_order", "_row_order"])
            .drop(columns=["_section", "_report_order", "_row_order"])
            .fillna("")
        )
        labels = (
            group.sort_values(["report_year", "row_order"], ascending=[False, True])
            .drop_duplicates("row_key")
            .set_index("row_key")["canonical_label"]
        )
        table.insert(0, "line_item", table.index.map(labels))
        occurrences = latest_rows["reported_occurrence"].astype(int)
        table.insert(1, "occurrence", table.index.map(occurrences))
        kinds = latest_rows["value_kind"]
        row_currencies = latest_rows["currency"].where(kinds != "percent", "")
        table.insert(2, "currency", table.index.map(row_currencies))
        table.insert(3, "value_kind", table.index.map(kinds))
        table.insert(4, "unit_multiplier", table.index.map(display_multipliers))
        table.to_csv(artifacts_dir / f"{company}_{statement}.csv", index=False)

    prepared.to_parquet(artifacts_dir / "observations.parquet", index=False)
    return winners, lineage
