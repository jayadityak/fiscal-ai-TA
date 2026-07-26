"""Cross-report consistency check.

Every fiscal period is extracted independently from more than one annual report
(e.g. 2021 appears in the 2021, 2023, and 2025 reports as a comparative column).
This check compares those independent extractions of the *same* cell -- keyed the
same way ``compile.resolve_restatements`` keys a winner -- and reports whether
they agree.

Cells that agree are the meaningful signal: the same figure was read out of two
or more separate source documents and came back identical, which is evidence the
transcription is faithful. Cells that differ are the issuer re-presenting a
comparative in a later report; the pipeline keeps the newest value and records
both in ``lineage.csv``.

Note on scope: this check reports agreement, it does not adjudicate it. A
difference across editions is expected accounting behaviour (re-presentation),
and nothing here distinguishes a re-presentation from a transcription error --
the pipeline's ``restated`` flag is derived from the same value difference, so it
cannot serve as independent corroboration. Use the agreement count as the
positive signal and ``lineage.csv`` to inspect the differences.

This is a deterministic audit over committed artifacts -- no LLM, no PDF re-parsing.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def compute_consistency(lineage_path: Path = Path("artifacts/lineage.csv")) -> dict:
    lineage = pd.read_csv(lineage_path)
    # Numbers and printed dashes both carry meaning; a cell that is a number in
    # one edition and a dash in another is a real difference worth surfacing.
    comparable = lineage[lineage["cell_status"].astype(str).isin(["number", "dash"])].copy()
    comparable["cell"] = (
        comparable["value"].astype(str) + "|" + comparable["cell_status"].astype(str)
    )

    # Key matches compile.resolve_restatements so one group has exactly one winner.
    key = ["company", "statement", "row_id", "period_end", "currency", "value_kind"]
    records = []
    for group_key, group in comparable.groupby(key, dropna=False):
        editions = int(group["report_year"].nunique())
        distinct = int(group["cell"].nunique())
        if editions < 2:
            status = "single_source"
        elif distinct == 1:
            status = "identical"
        else:
            status = "differs_across_editions"
        records.append(
            {
                "company": group_key[0],
                "statement": group_key[1],
                "row_id": group_key[2],
                "period_end": group_key[3],
                "editions": editions,
                "distinct_values": distinct,
                "status": status,
            }
        )
    return {"cells": records}


def write_consistency(
    lineage_path: Path = Path("artifacts/lineage.csv"),
    out_path: Path = Path("artifacts/consistency.csv"),
) -> dict:
    cells = compute_consistency(lineage_path)["cells"]
    frame = pd.DataFrame(cells)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_path, index=False)

    cross_checked = frame[frame["editions"] >= 2]
    return {
        "cellsTotal": int(len(frame)),
        "crossVerified": int(len(cross_checked)),
        "identical": int((cross_checked["status"] == "identical").sum()),
        "differing": int((cross_checked["status"] == "differs_across_editions").sum()),
        "singleSource": int((frame["status"] == "single_source").sum()),
    }


if __name__ == "__main__":
    print(write_consistency())
