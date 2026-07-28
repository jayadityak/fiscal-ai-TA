from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from .companies import COMPANIES
from .llm import canonicalize_statement_labels


CURATED_ALIASES: dict[tuple[str, str], dict[str, str]] = {
    ("heineken", "income_statement"): {
        "profit loss": "Profit",
        "total expenses": "Total other expenses",
    },
    ("heineken", "balance_sheet"): {
        "equity attributable to equity holders of the company": "Shareholders' equity",
    },
    ("heineken", "cash_flow"): {
        "profit loss": "Profit",
        "cash flow from operations before changes in working capital":
            "Cash flow from operations before changes in working capital and provisions",
        "change in provisions and employee benefits":
            "Change in provisions and post-retirement obligations",
        "payment of lease commitments":
            "Payment of principal portion of lease commitments",
        "proceeds from sale of property plant and equipment and":
            "Proceeds from sale of property, plant and equipment and intangible assets",
        "repayment on loans to customers":
            "Repayment on loans to customers and other investments",
        "share of profit of associates and joint ventures and dividend income":
            "Share of profit/(loss) of associates and joint ventures and dividend "
            "income on fair value through OCI investments",
        "share of profit of associates and joint ventures and dividend income on fair "
        "value through oci investments":
            "Share of profit/(loss) of associates and joint ventures and dividend "
            "income on fair value through OCI investments",
        "share of profit loss of associates and joint ventures and dividend income on "
        "fair value through oci investments":
            "Share of profit/(loss) of associates and joint ventures and dividend "
            "income on fair value through OCI investments",
    },
    ("nestle", "cash_flow"): {
        "dividends and interest from associates and joint ventures":
            "Dividends, other distributions and interest from associates and joint ventures",
        "purchase net of sale of treasury shares a": "Purchase of treasury shares (a)",
        "purchase of treasury shares b": "Purchase of treasury shares (a)",
    },
    ("unilever", "income_statement"): {
        "net profit": "Total net profit",
    },
    ("unilever", "cash_flow"): {
        "net cash flow from operating activities":
            "Total cash flows from operating activities",
        "net cash flow used in from investing activities":
            "Total cash outflow used in investing activities",
        "net cash flow used in from financing activities":
            "Total cash flow used in financing activities",
        "other financing activities": "Other financing activities(c)",
        "other financing activities a": "Other financing activities(c)",
        "share of net profit of joint ventures associates and other income loss "
        "from non current investments and associates":
            "Share of net profit of joint ventures/associates and other "
            "(income)/loss from non-current investments",
    },
}


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


def display_label(label: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", label).split())


def presentation_section(
    company: str,
    statement: str,
    label: str,
    occurrence: int = 1,
) -> int:
    value = canonical_key(label)
    if statement == "income_statement":
        if value == "excise tax expense":
            return 0
        if "earnings per share" in value or "number of shares" in value:
            return 16
        if (
            value.startswith("attributable")
            or "attributable to" in value
            or value in {"continuing operations", "discontinued operations"}
            or value in {
                "shareholders of the company net profit",
                "shareholders of the company net profit loss",
                "equity holders of the company net profit",
                "shareholders equity",
                "non controlling interests",
            }
        ):
            return 15
        if (
            "profit before" in value
            or "before taxes" in value
        ):
            return 12
        if value == "taxation" or "tax expense" in value:
            return 13
        if (
            value in {"profit", "profit loss", "net profit", "total net profit"}
            or "profit for the year" in value
            or "profit from continuing" in value
            or "profit from discontinued" in value
            or "profit after taxation" in value
        ):
            return 14
        if "share of profit" in value or "income from associates" in value:
            return 11
        if any(
            token in value
            for token in ("finance", "interest income", "interest expense")
        ):
            return 10
        return 0
    if statement == "cash_flow":
        if value in {
            "cash flow from operating activities",
            "net cash flow from operating activities",
            "operating cash flow",
            "total cash flows from operating activities",
        }:
            return 9
        if (
            value == "investing cash flow"
            or value == "cash flow used in investing activities"
            or value == "cash flow from used in investing activities"
            or value == "total cash outflow used in investing activities"
        ):
            return 19
        if (
            value == "financing cash flow"
            or value == "cash flow used in financing activities"
            or value == "cash flow from used in financing activities"
            or value == "total cash flow used in financing activities"
        ):
            return 29
        if (
            "cash and cash equivalents at the beginning" in value
            or "cash and cash equivalents at beginning" in value
            or "cash and cash equivalents as at 1 january" in value
            or "cash and cash equivalents at the end" in value
            or "cash and cash equivalents at end" in value
            or "cash and cash equivalents as at 31 december" in value
            or "cash and cash equivalents classified as held for sale" in value
            or "cash and cash equivalents as per balance sheet" in value
            or "increase decrease in cash and cash equivalents" in value
            or value == "net cash flow"
            or "exchange rate" in value
            or "exchange rates" in value
            or "currency retranslation" in value
        ):
            if "beginning" in value or "1 january" in value:
                return 30
            if "increase decrease" in value or value == "net cash flow":
                return 31
            if "exchange" in value or "currency retranslation" in value:
                return 32
            return 33
        if any(
            token in value
            for token in (
                "financing activit",
                "financing cash",
                "borrowings",
                "financial debt",
                "financial liabilities",
                "treasury shares",
                "treasury activities",
                "repurchase of shares",
                "purchase own shares",
                "preference shares",
                "dividends paid",
                "dividend paid",
                "lease rental payments",
                "lease commitments",
            )
        ) or (company == "unilever" and value == "interest paid"):
            return 20
        if any(
            token in value
            for token in (
                "investing activit",
                "investing cash",
                "acquisition",
                "disposal",
                "divestment",
                "capital expenditure",
                "proceeds from sale",
                "purchase of property",
                "purchase of intangible",
                "loans issued",
                "repayment on loans",
                "investments in associates",
                "long term investments",
                "long term financial",
                "other non current investments",
                "financial assets",
                "operational investing",
                "free operating cash flow",
            )
        ):
            return 10
        if company == "heineken" and value == "other":
            return 32
        return 0
    if statement != "balance_sheet":
        return 0
    if value in {"total equity and liabilities", "total liabilities and equity"}:
        return 70
    if value == "total assets":
        return 20
    if value == "total liabilities":
        return 60 if company == "heineken" else 50
    if value == "equity instruments":
        return 0
    asset_label = "asset" in value and "liabilit" not in value
    is_equity = any(
        token in value
        for token in (
            "equity",
            "share capital",
            "share premium",
            "treasury shares",
            "retained earnings",
            "retained profit",
            "translation reserve",
            "reserves",
            "other reserves",
            "non controlling interests",
        )
    )
    is_liability = any(
        token in value
        for token in (
            "liabilit",
            "borrowings",
            "overdraft",
            "financial debt",
            "trade and other payables",
            "trade payables",
            "other payables",
            "accruals",
            "provisions",
            "employee benefits",
            "post retirement obligations",
            "funded schemes in deficit",
            "unfunded schemes",
            "returnable packaging deposits",
        )
    )
    current_asset = any(
        token in value
        for token in (
            "inventories",
            "trade and other receivables",
            "trade and other current receivables",
            "prepayments",
            "cash and cash equivalents",
            "short term investments",
            "derivative assets",
            "other financial assets",
            "current income tax assets",
            "current tax assets",
            "assets held for sale",
            "assets classified as held for sale",
        )
    ) or value in {"total current assets", "current assets"}
    if asset_label:
        is_liability = False
        is_equity = False
    if not is_equity and not is_liability:
        if company == "nestle":
            nestle_noncurrent_occurrence = (
                value in {"derivative assets", "current income tax assets"}
                and occurrence > 1
            )
            asset_base = 10 if nestle_noncurrent_occurrence or not current_asset else 0
        else:
            asset_base = 10 if current_asset else 0
        if value in {
            "total current assets",
            "current assets",
            "total non current assets",
            "non current assets",
        }:
            asset_base += 9
        return asset_base
    if is_equity:
        equity_order = 0
        if "shareholders equity" in value or "attributable to shareholders" in value:
            equity_order = 1
        elif value == "non controlling interests":
            equity_order = 2
        elif value == "total equity":
            equity_order = 3
        return (30 if company == "heineken" else 60) + equity_order
    current_liability = (
        value in {"current liabilities", "total current liabilities"}
        or value in {"current tax liabilities", "current income tax liabilities"}
        or "trade and other payables" in value
        or "trade payables" in value
        or "accruals" in value
        or "returnable packaging deposits" in value
        or "associated with assets classified as held for sale" in value
        or (value.startswith("liabilities") and "held for sale" in value)
        or (
            value == "borrowings"
            and occurrence > 1
        )
        or (
            value == "provisions"
            and (
                (company == "heineken" and occurrence > 1)
                or (company != "heineken" and occurrence == 1)
            )
        )
        or (
            value in {"financial debt", "financial liabilities"}
            and occurrence == 1
        )
        or (
            value == "derivative liabilities"
            and (company == "heineken" or occurrence == 1)
        )
    )
    if company == "heineken":
        liability_section = 50 if current_liability else 40
    else:
        liability_section = 30 if current_liability else 40
    if value in {
        "current liabilities",
        "total current liabilities",
        "non current liabilities",
        "total non current liabilities",
    }:
        liability_section += 9
    return liability_section


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
                "shares"
                if "number of shares" in canonical_key(str(label))
                else (
                    "currency_per_share"
                    if "per share" in canonical_key(str(label))
                    else "currency"
                )
            )
        )
        for raw, label in zip(frame["raw_value"], frame["reported_label"], strict=True)
    ]
    frame["effective_multiplier"] = [
        1 if kind in {"percent", "currency_per_share", "shares"} else int(multiplier)
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
    return apply_curated_aliases(frame)


def apply_curated_aliases(observations: pd.DataFrame) -> pd.DataFrame:
    frame = observations.copy()
    for (company, statement), aliases in CURATED_ALIASES.items():
        mask = (frame["company"] == company) & (frame["statement"] == statement)
        mapped = frame.loc[mask, "reported_label"].map(
            lambda label: aliases.get(canonical_key(str(label)))
        )
        replace = mapped.notna()
        target_index = mapped.index[replace]
        frame.loc[target_index, "canonical_label"] = mapped.loc[target_index]
        frame.loc[target_index, "canonical_id"] = frame.loc[
            target_index, "canonical_label"
        ].map(canonical_key)
        frame.loc[target_index, "row_id"] = (
            frame.loc[target_index, "canonical_id"]
            + "::"
            + frame.loc[target_index, "reported_occurrence"].astype(str)
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
    use_existing_canonicalization: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if use_existing_canonicalization:
        canonical_columns = ["canonical_id", "canonical_label", "row_id"]
        missing = [
            column for column in canonical_columns if column not in observations
        ]
        if missing:
            raise ValueError(
                "Existing canonicalization requested but columns are missing: "
                f"{missing}"
            )
        lookup_key = [
            "company",
            "document_id",
            "report_year",
            "statement",
            "row_order",
            "reported_label",
        ]
        lookup = observations[lookup_key + canonical_columns].drop_duplicates(
            lookup_key
        )
        prepared = prepare_observations(observations).drop(
            columns=canonical_columns
        )
        prepared = prepared.merge(
            lookup,
            on=lookup_key,
            how="left",
            validate="many_to_one",
        )
        if prepared[canonical_columns].isna().any().any():
            raise RuntimeError("Existing canonicalization could not be restored")
        prepared = apply_curated_aliases(prepared)
    else:
        prepared = canonicalize_observations(prepare_observations(observations))
    winners, lineage = resolve_restatements(prepared)
    lineage.to_csv(artifacts_dir / "lineage.csv", index=False)

    for (company, statement), group in winners.groupby(["company", "statement"], sort=False):
        target_periods = [f"{year}-12-31" for year in COMPANIES[company].target_years]
        group = group[group["period_end"].isin(target_periods)].copy()
        if company == "heineken" and statement == "income_statement":
            group = group[
                ~(
                    (group["canonical_id"] == canonical_key("Profit"))
                    & (group["reported_occurrence"] > 1)
                )
            ].copy()
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
        group.loc[group["cell_status"] == "dash", "display_value"] = "—"
        valued = group["display_value"] != ""
        valued &= group["cell_status"] == "number"
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
                int(latest_order.at[row_key, "reported_occurrence"]),
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
        table.insert(0, "line_item", table.index.map(labels).map(display_label))
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
