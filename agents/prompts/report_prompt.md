=== SHIFT DATA ===
{facts}
=== END DATA ===

Write a concise, professional Production Shift Report in clean Markdown with EXACTLY these sections:

# Production Shift Report

## Summary
2-4 sentences: total units, downtime, defects, and an overall assessment.

## Production
Markdown table: Line | Units Produced | vs Previous Shift. Then a bold Total line.

## Downtime
Markdown table: Line | Machine | Downtime (mins) | Notes. Only machines with downtime > 0. Then bold Total Downtime.

## Defects
Markdown table: Line | Defects | Defect Rate (%). Then bold Total Defects.

## Comparison with Previous Shift
Markdown table: Metric | Current Shift | Previous Shift | Change for Units, Downtime, Defects. Use the ▲ symbol for an increase and ▼ for a decrease. If there is no previous shift, state that comparison is unavailable.

## Exceptions
One bullet for EVERY detected anomaly listed above — include the machine/line, the values, and a one-line actionable recommendation. If none, write "No exceptions detected."

Return ONLY the Markdown report — no code fences, no preamble.
