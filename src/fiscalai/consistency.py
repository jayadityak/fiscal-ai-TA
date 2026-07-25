"""Cross-report consistency check.

Every fiscal period is extracted independently from more than one annual report
(e.g. 2021 appears in the 2021, 2023, and 2025 reports as a comparative column).
This check compares those independent extractions of the *same* cell -- keyed by
company, statement, row identity (canonical label + occurrence), and period -- and
confirms they agree. Agreement across independent source documents is strong
evidence a figure was transcribed correctly; a disagreement is either a genuine
restatement (already tracked by the pipeline's ``restated`` flag) or a transcription
error. Any disagreement that is *not* an accounted-for restatement is surfaced as a
suspect cell.

This is a deterministic audit over committed artifacts -- no LLM, no PDF re-parsing.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def compute_consistency(lineage_path: Path = Path("artifacts/lineage.csv")) -> dict:
    lineage = pd.read_csv(lineage_path)
    numeric = lineage[lineage["cell_status"].astype(str) == "number"].copy()
    numeric["restated_flag"] = numeric["restated"].astype(str).str.lower() == "true"

    key = ["company", "statement", "row_id", "period_end"]
    records = []
    for (company, statement, row_id, period), group in numeric.groupby(key):
        editions = int(group["report_year"].nunique())
        distinct_values = int(group["value"].astype(str).nunique())
        if editions < 2:
            status = "single_source"
        elif distinct_values == 1:
            status = "consistent"
        elif bool(group["restated_flag"].any()):
            status = "restated"
        else:
            status = "unexplained"  # differs across editions with no restatement flag
        records.append(
            {
                "company": company,
                "statement": statement,
                "row_id": row_id,
                "period_end": period,
                "editions": editions,
                "distinct_values": distinct_values,
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
    frame.to_csv(out_path, index=False)

    cross_checked = frame[frame["editions"] >= 2]
    summary = {
        "cellsTotal": int(len(frame)),
        "crossVerified": int(len(cross_checked)),
        "consistent": int((cross_checked["status"] == "consistent").sum()),
        "restated": int((cross_checked["status"] == "restated").sum()),
        "unexplained": int((cross_checked["status"] == "unexplained").sum()),
        "singleSource": int((frame["status"] == "single_source").sum()),
    }
    return summary


if __name__ == "__main__":
    print(write_consistency())
