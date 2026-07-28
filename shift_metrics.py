"""Shift CSV loading, aggregation, and anomaly-detection rules.

Shared by app.py (live UI metrics) and report_agent.py (facts sent to Claude),
so the numbers behind the report and the numbers on screen never drift apart.
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

# Anomaly thresholds — mirror .claude/agents/shift_report_agent.md (source of truth)
DOWNTIME_LIMIT = 15   # minutes, per machine reading
PROD_DROP_LIMIT = 10  # percent, per line vs previous
DEFECT_SURGE_LIMIT = 50  # percent, total vs previous


def load_shift(path: Path) -> pd.DataFrame:
    """Read a shift CSV, validate columns, and coerce numerics (missing -> 0)."""
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")
    for col in ("units_produced", "downtime_minutes", "defects"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df.sort_values("timestamp").reset_index(drop=True)


def totals(df: pd.DataFrame) -> dict:
    units = int(df["units_produced"].sum())
    defects = int(df["defects"].sum())
    return {
        "units": units,
        "downtime": int(df["downtime_minutes"].sum()),
        "defects": defects,
        "defect_rate": (defects / units * 100) if units else 0.0,
    }


def pct_delta(current: float, previous: float):
    """Return a signed percentage change, or None if no baseline."""
    if not previous:
        return None
    return (current - previous) / previous * 100


def detect_flags(cur: pd.DataFrame, prev: pd.DataFrame | None) -> list[tuple[str, str]]:
    """Client-side anomaly detection mirroring the agent's rules.

    Returns a list of (severity, message) where severity is 'bad' or 'warn'.
    """
    flags: list[tuple[str, str]] = []

    # Excessive downtime on any single reading
    hot = cur[cur["downtime_minutes"] > DOWNTIME_LIMIT]
    for _, r in hot.iterrows():
        flags.append(
            ("bad", f"{r['machine']} (Line {r['line']}): "
                    f"{int(r['downtime_minutes'])} min downtime at {r['timestamp']} "
                    f"(> {DOWNTIME_LIMIT} min)")
        )

    # Zero-production readings
    zero = cur[cur["units_produced"] == 0]
    for _, r in zero.iterrows():
        flags.append(
            ("bad", f"{r['machine']} (Line {r['line']}): zero production at {r['timestamp']}")
        )

    if prev is not None:
        # Per-line production drop
        cu = cur.groupby("line")["units_produced"].sum()
        pu = prev.groupby("line")["units_produced"].sum()
        for line in cu.index:
            d = pct_delta(cu[line], pu.get(line, 0))
            if d is not None and d < -PROD_DROP_LIMIT:
                flags.append(
                    ("warn", f"Line {line}: production down {abs(d):.1f}% vs previous "
                             f"(> {PROD_DROP_LIMIT}% drop)")
                )
        # Total defect surge
        d = pct_delta(int(cur["defects"].sum()), int(prev["defects"].sum()))
        if d is not None and d > DEFECT_SURGE_LIMIT:
            flags.append(("warn", f"Total defects up {d:.0f}% vs previous (> {DEFECT_SURGE_LIMIT}% surge)"))

    return flags
