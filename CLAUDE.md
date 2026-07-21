# Production Shift Report

You are tasked with generating a comprehensive shift performance report. 
**You must activate and strictly follow the `shift-report-agent` agent** to analyze the data, detect anomalies, and produce the supervisor-friendly output.

## Input Data
The agent expects two CSV files (you will be provided with their paths or content):
1. **Current shift log** (e.g., `current_shift.csv`)
2. **Previous shift log** (e.g., `previous_shift.csv`)

**Required CSV Columns (case-sensitive):**
- `timestamp` – Datetime of the reading
- `line` – Production line identifier
- `machine` – Specific machine or workstation
- `units_produced` – Good units counted
- `downtime_minutes` – Unplanned idle time in minutes
- `defects` – Defective units identified

## Execution Rules
1. **Load the Agent**: Automatically invoke the `shift-report-agent` logic. Do not skip or modify its internal calculations (aggregation, comparison formulas, or anomaly thresholds).
2. **File Management**: Save the final Markdown report inside the `reports/` directory. 
3. **Naming Convention**: Use the current date and shift identifier in the filename, for example:  
   `reports/shift_report_2026-07-21_DayShift.md`
4. **Output Format**: Generate the report exclusively in **clean Markdown** (no wrapping code fences around the final output). Ensure it follows the agent's structured template (Summary, Production, Downtime, Defects, Comparison, Exceptions).

## Agent Capabilities (Quick Reference)
- **Validation**: Automatically cleans missing values and deduplicates rows.
- **Aggregation**: Rolls up metrics by line, machine, and overall.
- **Comparison**: Calculates percentage changes (▲/▼) vs. the previous shift.
- **Anomaly Detection**: Flags critical downtime (>15 mins for any machine), production drops (>10%), defect surges (>50%), zero-production events, and sustained low output. (Thresholds are defined in `.claude/agents/shift_report_agent.md` — that file is the source of truth.)
- **Actionable Output**: Includes explicit recommendations for every critical exception found.

> **Note**: If a CSV file is missing, the agent will treat the available file as the current shift and note that comparison data is unavailable. Always prioritize providing at least one actionable insight for any flagged issue.