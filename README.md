# 🏭 Production Shift Report Agent

A Streamlit app that turns end-of-shift machine logs (CSV) into a one-page,
supervisor-ready **Production Shift Report** — with live metrics, charts,
automatic anomaly flagging, and a downloadable PDF.

Built on a Claude Code agent (`shift-report-agent`) that reads the shift logs,
computes totals, compares them to the prior shift, and writes a plain-language
report with an **Exceptions** section.

---

## Features

- **Upload → Analyze → Generate** flow: drop the current shift CSV (previous is
  optional and falls back to a stored file).
- **Live metrics**, computed by Claude from the raw CSV on Analyze: units,
  downtime, defects, defect rate — each with a % delta vs the previous shift.
- **AI anomaly detection**: excessive downtime, zero-production events,
  per-line production drops, and defect surges — flagged by Claude, not by
  hardcoded Python thresholds.
- **Charts dashboard** (Altair): units & defects over the shift, downtime
  heatmap (machine × time), units-by-machine ranking, and a units-vs-defects
  bubble chart — with consistent, colorblind-safe line colors. Every series is
  computed by Claude from the raw data.
- **AI report** — the same structured Claude call that powers the dashboard
  also writes the narrative report, which is then **auto-downloaded as a PDF**
  (Markdown download also available) with no second API call.

## Data format

Each CSV is a shift log with these columns:

```
timestamp,line,machine,units_produced,downtime_minutes,defects
08:00,A,Cutter-1,120,0,2
08:00,B,Welder-1,90,15,3
```

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate    |    macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

The report generator calls the [Anthropic API](https://docs.claude.com) directly,
so it needs an API key. Set it as `ANTHROPIC_API_KEY` — either exported in your
shell or in `.streamlit/secrets.toml` (copy `.streamlit/secrets.toml.example`).
The key is **never** committed (`secrets.toml` is gitignored).

## Run

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or put it in .streamlit/secrets.toml
python -m streamlit run app.py
```

Then open http://localhost:8501.

## Deploy

The app is host-agnostic — it only needs Python, the `requirements.txt` deps, and
the `ANTHROPIC_API_KEY` secret (no Claude CLI, no local auth).

**Streamlit Community Cloud:**
1. Push this repo to GitHub (the key stays out of git).
2. On [share.streamlit.io](https://share.streamlit.io) → **New app**, point it at
   this repo and `app.py`.
3. **App settings → Secrets**, paste:
   `ANTHROPIC_API_KEY = "sk-ant-..."`
4. Deploy. Reports generate via the API using that secret.

Any other host (Render, Railway, Fly.io, a container) works the same way — set
`ANTHROPIC_API_KEY` as an environment variable / secret and run
`streamlit run app.py`.

> On Windows, if `streamlit.exe` is blocked by an Application Control policy, use
> the `python -m streamlit ...` form above — it runs through the trusted
> interpreter.

## Test data

Ready-made shift-log CSVs live under [`data/`](data/) so you can exercise the app
and every anomaly path without generating anything. Each **current** file has a
matching clean-baseline **previous** file for comparison.

`data/scenarios/` — one pair per detection path (`shift_current_<scenario>_2026-07-22.csv`
+ `shift_previous_<scenario>_2026-07-21.csv`):

| Scenario            | What it exercises                                                        |
|---------------------|--------------------------------------------------------------------------|
| `clean`             | Healthy shift — no exceptions (control case)                             |
| `welder-decline`    | Sustained low output + a zero-production stoppage + a >15-min downtime   |
| `critical-downtime` | Multiple machines with >15-min downtime events                          |
| `production-drop`   | Plant-wide output down ~30% (>10% drop)                                  |
| `defect-surge`      | Line C defects spike well over +50%                                     |
| `zero-production`   | Cutter-2 dead the entire shift                                          |
| `mixed`             | Several anomalies across different lines (stress test)                  |

`data/history/` — plain dated current/previous pairs (`shift_current_2026-07-14.csv`,
`shift_previous_2026-07-13.csv`, …) for ad-hoc runs.

`data/shift_previous.csv` and `data/shift_today.csv` sit at the top level — the
app falls back to `shift_previous.csv` automatically when no previous-shift file
is uploaded (see `STORED_PREVIOUS` in `app.py`).

To use one in the app, upload the `current` CSV (and optionally its matching
`previous` CSV) on the upload screen.

## Project layout

```
production-shift-agent/
├── app.py                         # Streamlit UI: uploads, KPI cards, charts, PDF export
├── shift_metrics.py               # CSV loading + column/type validation only
├── agents/
│   ├── report_agent.py             # Claude API call: raw CSV in, structured analysis + report out
│   └── prompts/
│       ├── system_prompt.md         # Analysis rules and thresholds (source of truth for the app)
│       └── report_prompt.md         # User prompt template (schema + report structure)
├── requirements.txt
├── .claude/agents/
│   └── shift_report_agent.md       # The Claude Code agent: same rules/template, CLI workflow
├── .streamlit/config.toml          # Theme
├── data/
│   ├── shift_previous.csv           # Stored fallback previous-shift file
│   ├── shift_today.csv              # Ready "current" file for a quick demo
│   ├── history/                     # Plain dated current/previous pairs
│   └── scenarios/                   # One current/previous pair per anomaly path
└── reports/                        # Generated reports (gitignored)
```

## How it works

1. The app parses the uploaded CSV(s) with pandas — validation only (required
   columns, numeric coercion), no aggregation or business logic.
2. On **Analyze**, the raw CSV data is sent to Claude (`claude-sonnet-5`) in a
   single API call, forced to call a `record_shift_analysis` tool. Claude
   computes every total, per-line/machine breakdown, chart series, and
   anomaly directly from the raw rows, and writes the narrative report — all
   in that one structured response.
3. The dashboard (KPI cards, anomaly panel, charts) renders straight from that
   structured result. Pressing **Generate Shift Report** reuses the
   already-written `report_markdown` — no second API call — and renders it to
   PDF (`markdown` → HTML → `xhtml2pdf`/`reportlab`) for auto-download.

There are no hardcoded thresholds or aggregation logic in Python. The rules
Claude follows live in `agents/prompts/system_prompt.md` for the app, and are
mirrored in `.claude/agents/shift_report_agent.md` for the Claude Code CLI
version of this workflow — update both if you change a threshold.
