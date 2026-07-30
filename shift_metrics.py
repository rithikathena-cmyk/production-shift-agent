"""Shift CSV loading and validation.

Shared by app.py and agents/report_agent.py so both start from the same
cleaned, validated DataFrame. There is no aggregation, threshold, or
anomaly logic here — all totals, breakdowns, chart series, and anomaly
detection are computed by the analysis agent (agents/report_agent.py)
directly from this raw data. The rules it follows are defined in
agents/prompts/system_prompt.md and mirrored in
.claude/agents/shift_report_agent.md for the Claude Code CLI flow.
"""

from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = [
    "timestamp",
    "line",
    "machine",
    "units_produced",
    "downtime_minutes",
    "defects",
]


def load_shift(path: Path) -> pd.DataFrame:
    """Read a shift CSV, validate columns, and coerce numerics (missing -> 0)."""
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")
    for col in ("units_produced", "downtime_minutes", "defects"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df.sort_values("timestamp").reset_index(drop=True)
