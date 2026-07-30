=== SHIFT DATA (raw CSV) ===
{data}
=== END DATA ===

Analyze the shift data above and call record_shift_analysis with:

1. Totals, the per-line breakdown, downtime-by-machine, and anomalies, computed directly from the raw rows.
2. Chart series (units_by_time, defects_by_time, downtime_heatmap, machine_summary) as described in the tool schema.
3. A complete report_markdown following EXACTLY this structure:

# Production Shift Report

## Summary
2-4 sentences: total units, downtime, defects, and an overall assessment.

## Production
Markdown table: Line | Units Produced | vs Previous Shift. Then a bold Total line.

## Downtime
Markdown table: Line | Machine | Downtime (mins) | Notes. Only machines with downtime > 0. Then a bold Total Downtime line.

## Defects
Markdown table: Line | Defects | Defect Rate (%). Then a bold Total Defects line.

## Comparison with Previous Shift
Markdown table: Metric | Current Shift | Previous Shift | Change, for Units, Downtime, Defects. Use ▲ for an increase and ▼ for a decrease. If there is no previous shift, state that comparison is unavailable instead of a table.

## Exceptions
One bullet for EVERY anomaly detected — include the machine/line, the values, and a one-line actionable recommendation. If none, write "No exceptions detected."
