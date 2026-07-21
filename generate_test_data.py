"""
generate_test_data.py
----------------------
Generate synthetic shift-log CSVs for testing the Production Shift Report Agent.

Schema (matches the agent + app.py exactly):
    timestamp,line,machine,units_produced,downtime_minutes,defects

It writes two files into data/:
    - a PREVIOUS shift  : healthy baseline
    - a CURRENT shift   : same baseline, but with a seeded anomaly on one machine
                          (zero-production stoppage + declining output + rising defects)

Filenames encode current/previous + the date, e.g.:
    data/shift_current_2026-07-22.csv
    data/shift_previous_2026-07-21.csv

Usage:
    python generate_test_data.py                      # today + yesterday, anomaly on
    python generate_test_data.py --seed 7             # reproducible
    python generate_test_data.py --no-anomaly         # clean current shift (no flags)
    python generate_test_data.py --current-date 2026-07-22 --previous-date 2026-07-21
"""

import argparse
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# Line -> machines, with a per-machine baseline output per 15-minute interval.
MACHINES = [
    ("A", "Cutter-1", 120),
    ("A", "Cutter-2", 110),
    ("B", "Welder-1", 105),
    ("B", "Welder-2", 112),
    ("C", "Press-1", 130),
    ("C", "Press-2", 128),
]

# The machine that gets the seeded anomaly in the current shift.
ANOMALY_MACHINE = "Welder-1"


def time_slots(start="08:00", end="16:00", interval_min=15):
    """Yield 'HH:MM' timestamps across the shift."""
    t = datetime.strptime(start, "%H:%M")
    end_t = datetime.strptime(end, "%H:%M")
    step = timedelta(minutes=interval_min)
    while t <= end_t:
        yield t.strftime("%H:%M")
        t += step


def healthy_row(rng, line, machine, baseline):
    """A normal, well-behaved production reading."""
    units = baseline + rng.randint(-8, 10)
    downtime = rng.choices([0, 1, 2, 3], weights=[88, 6, 4, 2])[0]
    defects = rng.choices([0, 1, 2], weights=[85, 12, 3])[0]
    return units, downtime, defects


def generate_shift(rng, slots, anomaly=False):
    """Build all rows for one shift. If anomaly=True, degrade ANOMALY_MACHINE."""
    rows = []
    total_slots = len(slots)
    for i, ts in enumerate(slots):
        for line, machine, baseline in MACHINES:
            if anomaly and machine == ANOMALY_MACHINE:
                # Seeded anomaly: steady decline across the shift...
                decline = int((baseline - 55) * (i / max(total_slots - 1, 1)))
                units = baseline - decline
                downtime = 0
                defects = min(6, 1 + i // 2)  # defects climb over the shift

                if i == 1:
                    # ...and a hard 20-minute stoppage with ZERO output early on.
                    units, downtime, defects = 0, 20, 0
                elif i == 0:
                    downtime = 15  # a second over-threshold event at shift start
            else:
                units, downtime, defects = healthy_row(rng, line, machine, baseline)

            rows.append([ts, line, machine, max(units, 0), downtime, defects])
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["timestamp", "line", "machine", "units_produced", "downtime_minutes", "defects"]
        )
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Generate shift-log test CSVs.")
    parser.add_argument("--current-date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--previous-date", help="YYYY-MM-DD (default: current - 1 day)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    parser.add_argument("--start", default="08:00", help="Shift start HH:MM")
    parser.add_argument("--end", default="16:00", help="Shift end HH:MM")
    parser.add_argument("--interval", type=int, default=15, help="Interval minutes")
    parser.add_argument(
        "--no-anomaly",
        action="store_true",
        help="Generate a clean current shift with no seeded anomaly",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)

    current_date = (
        datetime.strptime(args.current_date, "%Y-%m-%d").date()
        if args.current_date
        else date.today()
    )
    previous_date = (
        datetime.strptime(args.previous_date, "%Y-%m-%d").date()
        if args.previous_date
        else current_date - timedelta(days=1)
    )

    slots = list(time_slots(args.start, args.end, args.interval))

    DATA_DIR.mkdir(exist_ok=True)

    previous_rows = generate_shift(rng, slots, anomaly=False)
    current_rows = generate_shift(rng, slots, anomaly=not args.no_anomaly)

    previous_path = DATA_DIR / f"shift_previous_{previous_date.isoformat()}.csv"
    current_path = DATA_DIR / f"shift_current_{current_date.isoformat()}.csv"

    write_csv(previous_path, previous_rows)
    write_csv(current_path, current_rows)

    print(f"Wrote {len(previous_rows)} rows -> {previous_path.name}  (healthy baseline)")
    print(
        f"Wrote {len(current_rows)} rows -> {current_path.name}  "
        f"({'anomaly seeded on ' + ANOMALY_MACHINE if not args.no_anomaly else 'clean, no anomaly'})"
    )


if __name__ == "__main__":
    main()
