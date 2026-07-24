from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Literal, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cell(StrictModel):
    period_end: str = Field(description="ISO date represented by this column")
    raw_value: str = Field(description="Exact printed cell text, without rewriting")


class StatementRow(StrictModel):
    row_order: int
    row_kind: Literal["line_item", "subtotal", "total", "heading"]
    exact_label: str = Field(description="Exact line-item wording from the source")
    footnote_marker: str
    cells: list[Cell]


class StatementExtraction(StrictModel):
    statement: Literal["income_statement", "balance_sheet", "cash_flow"]
    scope: Literal["consolidated"]
    currency: str
    unit_multiplier: int
    rows: list[StatementRow]


class CanonicalGroup(StrictModel):
    canonical_label: str = Field(description="One exact member label, preferring latest wording")
    members: list[str] = Field(description="Exact source labels for the same economic item")


class Canonicalization(StrictModel):
    groups: list[CanonicalGroup]


EXTRACT_INSTRUCTIONS = """\
Extract one primary consolidated financial statement from the supplied PDF page text.
Return every displayed row in source order. Preserve line-item wording and raw value cells exactly.
Associate each raw cell with the correct period-end date. Do not calculate, scale, round, translate,
canonicalize, or correct values. A dash must remain a dash. Exclude page headers, footers, and note
tables. If the source is not the requested consolidated statement, return an empty rows list.
"""

CANONICALIZE_INSTRUCTIONS = """\
Group line-item labels only when they represent the same economic item within the same statement.
Preserve every input label exactly once. Be conservative: similarly worded labels may be different
items, while differently worded labels may be equivalent. Never merge a subtotal with a component,
or an attributable amount with a total. For each group, choose canonical_label from its members,
preferring the wording used in the most recent report. Do not invent labels.
"""

ParsedModel = TypeVar("ParsedModel", bound=BaseModel)
_BUDGET_LOCK = threading.Lock()


def _cache_key(
    task: str,
    model: str,
    instructions: str,
    payload: str,
    schema: type[BaseModel],
) -> str:
    raw = json.dumps(
        {
            "task": task,
            "model": model,
            "instructions": instructions,
            "payload": payload,
            "schema": schema.model_json_schema(),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _consume_budget(cache_dir: Path, limit: int = 100) -> None:
    with _BUDGET_LOCK:
        counter_path = cache_dir / "call_count.json"
        count = 0
        if counter_path.exists():
            count = json.loads(counter_path.read_text())["uncached_calls"]
        if count >= limit:
            raise RuntimeError(f"LLM call limit of {limit} reached")
        counter_path.write_text(json.dumps({"uncached_calls": count + 1}, indent=2) + "\n")


def _parse_cached(
    task: str,
    instructions: str,
    payload: str,
    schema: type[ParsedModel],
    cache_root: Path = Path(".cache/llm"),
) -> ParsedModel:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for semantic pipeline stages")

    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    cache_root.mkdir(parents=True, exist_ok=True)
    key = _cache_key(task, model, instructions, payload, schema)
    cache_path = cache_root / f"{key}.json"
    if cache_path.exists():
        return schema.model_validate_json(cache_path.read_text())

    _consume_budget(cache_root)
    response = OpenAI(api_key=api_key).responses.parse(
        model=model,
        input=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": payload},
        ],
        text_format=schema,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError(f"The model returned no parsed output for {task}")

    cache_path.write_text(parsed.model_dump_json(indent=2) + "\n")
    return parsed


def extract_statement(
    statement: str,
    evidence: str,
    focus_hint: str = "",
    required_labels: tuple[str, ...] = (),
    cache_root: Path = Path(".cache/llm"),
) -> StatementExtraction:
    payload = f"Requested statement: {statement}\n\nSOURCE PAGE TEXT:\n{evidence}"
    instructions = EXTRACT_INSTRUCTIONS
    task = "extract_statement_v1"
    if focus_hint:
        instructions = f"{EXTRACT_INSTRUCTIONS}\n{focus_hint}"
        task = "extract_statement_focused_v1"
    parsed = _parse_cached(
        task,
        instructions,
        payload,
        StatementExtraction,
        cache_root,
    )
    if any(not row.exact_label.strip() for row in parsed.rows):
        retry_instructions = (
            instructions
            + "\nEvery returned row, including totals and subtotals, must have its printed "
            "non-empty label. Re-read the row when its numeric cells were found but its "
            "label was missed."
        )
        parsed = _parse_cached(
            "extract_statement_blank_label_retry_v1",
            retry_instructions,
            payload,
            StatementExtraction,
            cache_root,
        )
    extracted_labels = {
        " ".join(row.exact_label.casefold().split()) for row in parsed.rows
    }
    missing_required = [
        label
        for label in required_labels
        if " ".join(label.casefold().split()) not in extracted_labels
    ]
    if missing_required:
        retry_instructions = (
            instructions
            + "\nThe following printed rows are visibly present in the requested statement "
            "and were missed in the first pass. Re-read the complete statement and include "
            "every one of them with both period cells: "
            + "; ".join(missing_required)
            + "."
        )
        parsed = _parse_cached(
            "extract_statement_required_rows_retry_v1",
            retry_instructions,
            payload,
            StatementExtraction,
            cache_root,
        )
    if parsed.statement != statement:
        raise ValueError(f"Expected {statement}, model returned {parsed.statement}")
    if any(not row.exact_label.strip() for row in parsed.rows):
        raise ValueError(f"{statement} contains a row with an empty label")
    extracted_labels = {
        " ".join(row.exact_label.casefold().split()) for row in parsed.rows
    }
    missing_required = [
        label
        for label in required_labels
        if " ".join(label.casefold().split()) not in extracted_labels
    ]
    if missing_required:
        raise ValueError(
            f"{statement} is missing source-visible rows after retry: {missing_required}"
        )
    return parsed


def canonicalize_statement_labels(
    statement: str,
    label_records: list[dict[str, object]],
    cache_root: Path = Path(".cache/llm"),
) -> Canonicalization:
    payload = json.dumps(
        {"statement": statement, "labels": label_records},
        ensure_ascii=False,
        sort_keys=True,
    )
    parsed = _parse_cached(
        "canonicalize_labels_v1",
        CANONICALIZE_INSTRUCTIONS,
        payload,
        Canonicalization,
        cache_root,
    )
    inputs = {str(record["reported_label"]) for record in label_records}
    member_counts = Counter(
        member for group in parsed.groups for member in group.members if member in inputs
    )
    latest_year = {
        str(record["reported_label"]): int(record["latest_report_year"])
        for record in label_records
    }
    report_years = {
        str(record["reported_label"]): {
            int(occurrence["report_year"])
            for occurrence in record["occurrences"]  # type: ignore[union-attr]
        }
        for record in label_records
    }
    cleaned_groups = []
    safely_grouped = set()
    for group in parsed.groups:
        members = [
            member
            for member in group.members
            if member in inputs and member_counts[member] == 1
        ]
        if not members:
            continue
        coexisting_members = any(
            report_years[left] & report_years[right]
            for index, left in enumerate(members)
            for right in members[index + 1 :]
        )
        if coexisting_members:
            continue
        canonical_label = (
            group.canonical_label
            if group.canonical_label in members
            else max(members, key=lambda member: latest_year[member])
        )
        cleaned_groups.append(
            CanonicalGroup(canonical_label=canonical_label, members=members)
        )
        safely_grouped.update(members)
    cleaned_groups.extend(
        CanonicalGroup(canonical_label=label, members=[label])
        for label in sorted(inputs - safely_grouped)
    )
    return Canonicalization(groups=cleaned_groups)
