import base64
import io
import os
from pathlib import Path

import altair as alt
import anthropic
import markdown as md
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from xhtml2pdf import pisa

# -----------------------------
# Configuration
# -----------------------------
DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = Path(__file__).parent / "reports"
DATA_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

STORED_PREVIOUS = DATA_DIR / "shift_previous.csv"

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

st.set_page_config(
    page_title="Production Shift Report Agent",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------
# Styling
# -----------------------------
st.markdown(
    """
    <style>
      .hero {
        background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 55%, #0ea5e9 100%);
        padding: 1.5rem 2rem;
        border-radius: 16px;
        color: #ffffff;
        margin-bottom: 1.2rem;
        box-shadow: 0 8px 24px rgba(37, 99, 235, 0.25);
      }
      .hero h1 { margin: 0; font-size: 1.9rem; font-weight: 700; color:#fff; }
      .hero p  { margin: .35rem 0 0; opacity: .92; font-size: 1rem; }
      div[data-testid="stMetric"] {
        background: rgba(127,127,127,0.06);
        border: 1px solid rgba(127,127,127,0.18);
        border-radius: 12px;
        padding: 14px 16px 10px;
      }
      div[data-testid="stMetricValue"] { font-size: 1.7rem; }
      .stButton>button { border-radius: 10px; font-weight: 600; }
      .pill {
        display:inline-block; padding:3px 12px; border-radius:999px;
        font-size:.8rem; font-weight:600; margin:2px 6px 2px 0;
      }
      .pill-ok   { background:rgba(22,163,74,.15);  color:#16a34a; }
      .pill-warn { background:rgba(234,179,8,.18);   color:#b45309; }
      .pill-bad  { background:rgba(220,38,38,.15);   color:#dc2626; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
      <h1>🏭 Production Shift Report Agent</h1>
      <p>Drop a shift log, review the live metrics, and generate a supervisor-ready report — anomalies flagged automatically.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# -----------------------------
# Data helpers
# -----------------------------
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


# -----------------------------
# Claude report generator (single fast Anthropic API call, no tool loop)
# -----------------------------
# Fast model — the numbers are pre-computed in pandas, Claude only writes prose.
REPORT_MODEL = "claude-haiku-4-5-20251001"


def _get_api_key() -> str | None:
    """Resolve the Anthropic API key from the environment or Streamlit secrets.

    Local dev: export ANTHROPIC_API_KEY.
    Hosted (Streamlit Community Cloud / any host): set it as a secret, which the
    platform exposes via st.secrets or the environment. Never commit the key.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        return st.secrets["ANTHROPIC_API_KEY"]  # raises if no secrets.toml at all
    except Exception:
        return None


def _fmt_delta(cur_val, prev_val) -> str:
    d = pct_delta(cur_val, prev_val)
    return f"{d:+.1f}%" if d is not None else "n/a"


def build_facts(cur: pd.DataFrame, prev: pd.DataFrame | None,
                cur_name: str, prev_name: str | None,
                flags: list[tuple[str, str]]) -> str:
    """Turn the already-computed pandas data into a compact facts block."""
    ct = totals(cur)
    pt = totals(prev) if prev is not None else None
    L: list[str] = []

    L.append(f"Current shift file: {cur_name} "
             f"({cur['timestamp'].nunique()} intervals, "
             f"{cur['timestamp'].min()}-{cur['timestamp'].max()})")
    if prev is not None:
        L.append(f"Previous shift file: {prev_name} ({prev['timestamp'].nunique()} intervals)")
    else:
        L.append("Previous shift: NONE — no comparison available.")

    L.append("\nOVERALL TOTALS:")
    if pt:
        L.append(f"- Units produced: {ct['units']} (previous {pt['units']}, {_fmt_delta(ct['units'], pt['units'])})")
        L.append(f"- Downtime minutes: {ct['downtime']} (previous {pt['downtime']}, {_fmt_delta(ct['downtime'], pt['downtime'])})")
        L.append(f"- Defects: {ct['defects']} (previous {pt['defects']}, {_fmt_delta(ct['defects'], pt['defects'])})")
        L.append(f"- Defect rate: {ct['defect_rate']:.2f}% (previous {pt['defect_rate']:.2f}%)")
    else:
        L.append(f"- Units produced: {ct['units']}")
        L.append(f"- Downtime minutes: {ct['downtime']}")
        L.append(f"- Defects: {ct['defects']}")
        L.append(f"- Defect rate: {ct['defect_rate']:.2f}%")

    cu = cur.groupby("line")["units_produced"].sum()
    pu = prev.groupby("line")["units_produced"].sum() if prev is not None else None
    L.append("\nUNITS BY LINE:")
    for line in cu.index:
        if pu is not None:
            L.append(f"- Line {line}: {int(cu[line])} "
                     f"(previous {int(pu.get(line, 0))}, {_fmt_delta(cu[line], pu.get(line, 0))})")
        else:
            L.append(f"- Line {line}: {int(cu[line])}")

    cf = cur.groupby("line")["defects"].sum()
    cfu = cur.groupby("line")["units_produced"].sum()
    L.append("\nDEFECTS BY LINE (defects, rate%):")
    for line in cf.index:
        rate = (cf[line] / cfu[line] * 100) if cfu[line] else 0
        L.append(f"- Line {line}: {int(cf[line])} ({rate:.2f}%)")

    dm = cur.groupby(["line", "machine"])["downtime_minutes"].sum()
    dm = dm[dm > 0].sort_values(ascending=False)
    L.append("\nDOWNTIME BY MACHINE (>0 min):")
    L.extend([f"- {machine} (Line {line}): {int(mins)} min" for (line, machine), mins in dm.items()]
             or ["- none"])

    L.append("\nDETECTED ANOMALIES (include EVERY one of these in the Exceptions section):")
    L.extend([f"- [{sev.upper()}] {msg}" for sev, msg in flags] or ["- none detected"])

    return "\n".join(L)


def generate_report(cur: pd.DataFrame, prev: pd.DataFrame | None,
                    cur_name: str, prev_name: str | None,
                    flags: list[tuple[str, str]]) -> str:
    facts = build_facts(cur, prev, cur_name, prev_name, flags)

    system = (
        "You are an experienced manufacturing operations analyst writing a shift "
        "report for a plant supervisor. Use ONLY the pre-computed data provided. "
        "Do NOT invent or recompute numbers — every figure must come from that data. "
        "Return ONLY the Markdown report — no code fences, no preamble."
    )

    prompt = f"""=== SHIFT DATA ===
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

Return ONLY the Markdown report — no code fences, no preamble."""

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it as a secret on your host "
            "(e.g. Streamlit Community Cloud → App settings → Secrets) or export it "
            "locally: `ANTHROPIC_API_KEY=sk-ant-...`"
        )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=REPORT_MODEL,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anthropic API key is invalid or revoked.") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Anthropic API rate limit hit — retry in a moment.") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error ({e.status_code}): {e.message}") from e

    output = "".join(b.text for b in message.content if b.type == "text").strip()
    if output.startswith("```"):
        nl = output.find("\n")
        if nl != -1:
            output = output[nl + 1:]
        if output.rstrip().endswith("```"):
            output = output.rstrip()[:-3]
    return output.strip()


# -----------------------------
# Markdown -> PDF (pure Python: markdown -> HTML -> reportlab, no system binaries)
# -----------------------------
# base-14 Helvetica uses WinAnsi encoding; map glyphs outside it to ASCII so
# they don't drop out of the PDF (font embedding is blocked by App Control here).
_PDF_GLYPHS = {"▲": "+", "▼": "-", "×": "x", "≥": ">=", "≤": "<=", "→": "->", "•": "-"}


def md_to_pdf(md_text: str) -> bytes:
    for k, v in _PDF_GLYPHS.items():
        md_text = md_text.replace(k + " ", v).replace(k, v)

    body = md.markdown(md_text, extensions=["tables", "sane_lists"])
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
      @page {{ size: A4; margin: 1.6cm; }}
      body {{ font-family: Helvetica, sans-serif; font-size: 10pt; color:#1f2937; line-height:1.45; }}
      h1 {{ font-size:20pt; color:#1e3a8a; border-bottom:2px solid #2563eb; padding-bottom:4px; }}
      h2 {{ font-size:13pt; color:#2563eb; margin-top:14px; }}
      table {{ width:100%; border-collapse:collapse; margin:8px 0; }}
      th {{ background-color:#2563eb; color:#ffffff; padding:5px 8px; text-align:left; font-size:9pt; }}
      td {{ border:1px solid #d1d5db; padding:5px 8px; font-size:9pt; }}
      li {{ margin-bottom:3px; }}
    </style></head><body>{body}</body></html>"""

    buf = io.BytesIO()
    res = pisa.CreatePDF(src=html, dest=buf, encoding="utf-8")
    if res.err:
        raise RuntimeError("PDF generation failed")
    return buf.getvalue()


# -----------------------------
# Sidebar
# -----------------------------
with st.sidebar:
    st.subheader("📥 Shift logs")
    current_file = st.file_uploader("Current shift CSV", type=["csv"], key="cur",
                                    help="Required — the shift you want the report for.")
    previous_file = st.file_uploader("Previous shift CSV (optional)", type=["csv"], key="prev",
                                     help="Optional — falls back to the stored data/shift_previous.csv.")

    st.divider()
    st.caption("**Anomaly thresholds**")
    st.markdown(
        f"- Downtime **> {DOWNTIME_LIMIT} min** / reading\n"
        f"- Production drop **> {PROD_DROP_LIMIT}%** / line\n"
        f"- Defect surge **> {DEFECT_SURGE_LIMIT}%** total\n"
        f"- Any **zero-production** reading"
    )
    st.caption("Defined in `.claude/agents/shift_report_agent.md`")


# -----------------------------
# Resolve current + previous data
# -----------------------------
current_path = None
previous_path = None
current_df = None
previous_df = None
load_error = None

if current_file:
    current_path = DATA_DIR / current_file.name
    current_path.write_bytes(current_file.getbuffer())

if previous_file:
    previous_path = DATA_DIR / previous_file.name
    previous_path.write_bytes(previous_file.getbuffer())
elif STORED_PREVIOUS.exists():
    previous_path = STORED_PREVIOUS

try:
    if current_path:
        current_df = load_shift(current_path)
    if previous_path:
        previous_df = load_shift(previous_path)
except ValueError as e:
    load_error = str(e)


# -----------------------------
# Empty state
# -----------------------------
if current_df is None:
    if load_error:
        st.error(f"⚠️ {load_error}")
    st.info("👈 Upload a **current shift CSV** in the sidebar to begin. "
            "The previous shift is optional — the stored one is used automatically.")
    with st.expander("Expected CSV format"):
        st.code("timestamp,line,machine,units_produced,downtime_minutes,defects\n"
                "08:00,A,Cutter-1,120,0,2\n08:00,B,Welder-1,90,15,3", language="text")
    st.stop()

if load_error:
    st.error(f"⚠️ {load_error}")
    st.stop()


# -----------------------------
# Analyze gate — require an explicit click after every (new) upload
# -----------------------------
# Use the uploader's file_id (unique per upload) so re-uploading resets the gate.
cur_id = getattr(current_file, "file_id", current_path.name)
prev_id = getattr(previous_file, "file_id", None) if previous_file else "stored"
upload_key = f"{cur_id}|{prev_id}"

if st.session_state.get("upload_key") != upload_key:
    st.session_state["upload_key"] = upload_key
    st.session_state["analyzed"] = False  # new upload -> must press the button again

if not st.session_state.get("analyzed"):
    st.success(
        f"📄 Ready: **{current_path.name}**"
        + (f"  +  **{previous_path.name}**" if previous_path else "  (no previous shift)")
    )
    st.info("Files uploaded. Press **Analyze shift** to compute metrics and enable the report.")
    if st.button("▶️ Analyze shift", type="primary", use_container_width=True):
        st.session_state["analyzed"] = True
        st.rerun()
    st.stop()


# -----------------------------
# Live KPI row
# -----------------------------
cur_t = totals(current_df)
prev_t = totals(previous_df) if previous_df is not None else None

src_note = "vs uploaded previous shift" if previous_file else (
    "vs stored previous shift" if previous_path else "no baseline")
st.caption(f"📊 Live metrics from **{current_path.name}** · {src_note}")

k1, k2, k3, k4 = st.columns(4)


def _delta(cur_val, prev_val):
    if prev_t is None:
        return None
    d = pct_delta(cur_val, prev_val)
    return f"{d:+.1f}% vs prev" if d is not None else None


k1.metric("Units produced", f"{cur_t['units']:,}",
          _delta(cur_t["units"], prev_t["units"]) if prev_t else None)
k2.metric("Downtime (min)", f"{cur_t['downtime']:,}",
          _delta(cur_t["downtime"], prev_t["downtime"]) if prev_t else None,
          delta_color="inverse")
k3.metric("Defects", f"{cur_t['defects']:,}",
          _delta(cur_t["defects"], prev_t["defects"]) if prev_t else None,
          delta_color="inverse")
k4.metric("Defect rate", f"{cur_t['defect_rate']:.2f}%",
          (f"{cur_t['defect_rate'] - prev_t['defect_rate']:+.2f} pts" if prev_t else None),
          delta_color="inverse")


# -----------------------------
# Instant anomaly flags
# -----------------------------
flags = detect_flags(current_df, previous_df)
st.subheader("🚨 Anomaly check")
if not flags:
    st.markdown('<span class="pill pill-ok">✔ No anomalies detected</span>', unsafe_allow_html=True)
else:
    bad = sum(1 for s, _ in flags if s == "bad")
    warn = len(flags) - bad
    st.markdown(
        f'<span class="pill pill-bad">{bad} critical</span>'
        f'<span class="pill pill-warn">{warn} warning</span>',
        unsafe_allow_html=True,
    )
    for sev, msg in flags:
        (st.error if sev == "bad" else st.warning)(msg)


# -----------------------------
# Charts + preview + report tabs
# -----------------------------
st.divider()
tab_charts, tab_data, tab_report = st.tabs(["📈 Charts", "🗂️ Data", "📋 Shift Report"])

with tab_charts:
    # Fixed line colors (validated CVD-safe): color follows the ENTITY, not rank.
    LINE_SCALE = alt.Scale(domain=["A", "B", "C"], range=["#2a78d6", "#eb6834", "#1baf7a"])

    def line_color(legend=True):
        return alt.Color(
            "line:N", title="Line", scale=LINE_SCALE,
            legend=alt.Legend(orient="top") if legend else None,
        )

    def with_ts(df):
        d = df.copy()
        d["ts"] = pd.to_datetime("2000-01-01 " + d["timestamp"].astype(str))
        return d

    # 1) Hero time-series — units produced across the shift, one line per line.
    ut = with_ts(current_df.groupby(["timestamp", "line"], as_index=False)["units_produced"].sum())
    units_ts = (
        alt.Chart(ut)
        .mark_line(point=alt.OverlayMarkDef(size=40, filled=True), strokeWidth=2.5)
        .encode(
            x=alt.X("ts:T", title="Time", axis=alt.Axis(format="%H:%M")),
            y=alt.Y("units_produced:Q", title="Units / interval"),
            color=line_color(),
            tooltip=[alt.Tooltip("timestamp:N", title="Time"), alt.Tooltip("line:N", title="Line"),
                     alt.Tooltip("units_produced:Q", title="Units")],
        )
        .properties(height=300, title="Units produced across the shift (by line)")
    )
    st.altair_chart(units_ts, use_container_width=True)

    c1, c2 = st.columns(2)

    # 2a) Total units by machine — horizontal ranking, colored by its line.
    mu = current_df.groupby(["machine", "line"], as_index=False)["units_produced"].sum()
    bar_m = (
        alt.Chart(mu)
        .mark_bar(cornerRadiusEnd=4)
        .encode(
            x=alt.X("units_produced:Q", title="Total units"),
            y=alt.Y("machine:N", sort="-x", title=None),
            color=line_color(legend=False),
            tooltip=["machine:N", "line:N", alt.Tooltip("units_produced:Q", title="Units")],
        )
        .properties(height=300, title="Total units by machine")
    )
    c1.altair_chart(bar_m, use_container_width=True)

    # 2b) Defect rate by line.
    dl = current_df.groupby("line", as_index=False).agg(
        defects=("defects", "sum"), units=("units_produced", "sum"))
    dl["rate"] = (dl["defects"] / dl["units"].where(dl["units"] != 0, 1) * 100).round(2)
    bar_d = (
        alt.Chart(dl)
        .mark_bar(cornerRadiusEnd=4)
        .encode(
            x=alt.X("line:N", title="Line"),
            y=alt.Y("rate:Q", title="Defect rate (%)"),
            color=line_color(legend=False),
            tooltip=["line:N", alt.Tooltip("defects:Q", title="Defects"),
                     alt.Tooltip("rate:Q", title="Rate %")],
        )
        .properties(height=300, title="Defect rate by line")
    )
    c2.altair_chart(bar_d, use_container_width=True)

    # 3) Downtime heatmap — machine × time (sequential single-hue blue).
    hm = current_df.groupby(["machine", "timestamp"], as_index=False)["downtime_minutes"].sum()
    heat = (
        alt.Chart(hm)
        .mark_rect(stroke="#ffffff", strokeWidth=0.5)
        .encode(
            x=alt.X("timestamp:O", title="Time", axis=alt.Axis(labelAngle=-90, labelFontSize=8)),
            y=alt.Y("machine:N", title=None),
            color=alt.Color("downtime_minutes:Q", title="Downtime (min)",
                            scale=alt.Scale(range=["#eef3fb", "#0d366b"]),
                            legend=alt.Legend(orient="top")),
            tooltip=["machine:N", alt.Tooltip("timestamp:N", title="Time"),
                     alt.Tooltip("downtime_minutes:Q", title="Downtime")],
        )
        .properties(height=230, title="Downtime heatmap — machine × time")
    )
    st.altair_chart(heat, use_container_width=True)

    c3, c4 = st.columns(2)

    # 4a) Defects across the shift, one line per line.
    dts = with_ts(current_df.groupby(["timestamp", "line"], as_index=False)["defects"].sum())
    def_ts = (
        alt.Chart(dts)
        .mark_line(point=alt.OverlayMarkDef(size=35, filled=True), strokeWidth=2.5)
        .encode(
            x=alt.X("ts:T", title="Time", axis=alt.Axis(format="%H:%M")),
            y=alt.Y("defects:Q", title="Defects / interval"),
            color=line_color(),
            tooltip=[alt.Tooltip("timestamp:N", title="Time"), "line:N",
                     alt.Tooltip("defects:Q", title="Defects")],
        )
        .properties(height=300, title="Defects across the shift (by line)")
    )
    c3.altair_chart(def_ts, use_container_width=True)

    # 4b) Units vs defects per machine, bubble sized by downtime.
    ms = current_df.groupby(["machine", "line"], as_index=False).agg(
        units=("units_produced", "sum"), defects=("defects", "sum"),
        downtime=("downtime_minutes", "sum"))
    bubble = (
        alt.Chart(ms)
        .mark_circle(opacity=0.85, stroke="#ffffff", strokeWidth=1)
        .encode(
            x=alt.X("units:Q", title="Total units"),
            y=alt.Y("defects:Q", title="Total defects"),
            size=alt.Size("downtime:Q", title="Downtime (min)", scale=alt.Scale(range=[60, 900])),
            color=line_color(),
            tooltip=["machine:N", "line:N", alt.Tooltip("units:Q", title="Units"),
                     alt.Tooltip("defects:Q", title="Defects"),
                     alt.Tooltip("downtime:Q", title="Downtime")],
        )
        .properties(height=300, title="Units vs defects by machine (size = downtime)")
    )
    c4.altair_chart(bubble, use_container_width=True)

with tab_data:
    st.dataframe(
        current_df,
        use_container_width=True,
        hide_index=True,
        height=360,
        column_config={
            "units_produced": st.column_config.NumberColumn("Units", format="%d"),
            "downtime_minutes": st.column_config.NumberColumn("Downtime (min)", format="%d"),
            "defects": st.column_config.NumberColumn("Defects", format="%d"),
        },
    )

# -----------------------------
# Generate report
# -----------------------------
st.divider()
if st.button("🚀 Generate Shift Report", type="primary", use_container_width=True):
    import time
    try:
        with st.status("Generating report…", expanded=True) as status:
            st.write("🧮 Metrics already computed in-app — sending them to Claude…")
            st.write(f"🤖 One fast call ({REPORT_MODEL.split('-')[1].title()}), no file-reading loop…")
            t0 = time.perf_counter()
            report = generate_report(
                current_df, previous_df,
                current_path.name, previous_path.name if previous_path else None,
                flags,
            )
            elapsed = time.perf_counter() - t0
            (REPORTS_DIR / "shift_report.md").write_text(report, encoding="utf-8")
            st.write(f"✅ Done in {elapsed:.1f}s · saved to reports/shift_report.md")
            # Build the PDF now so it can auto-download in the browser.
            pdf_bytes = md_to_pdf(report)
            (REPORTS_DIR / "shift_report.pdf").write_bytes(pdf_bytes)
            st.write("📄 PDF built — downloading automatically…")
            status.update(label=f"Report generated in {elapsed:.1f}s", state="complete", expanded=False)

        st.session_state["report"] = report
        st.session_state["report_pdf"] = pdf_bytes
        st.session_state["report_pdf_src"] = report

        # Auto-trigger a browser download of the PDF (no click needed).
        # The anchor is created in the PARENT document to escape the component
        # iframe's sandbox, which otherwise blocks downloads.
        b64 = base64.b64encode(pdf_bytes).decode()
        components.html(
            f"""
            <script>
              (function() {{
                const doc = window.parent.document;
                const a = doc.createElement('a');
                a.href = "data:application/pdf;base64,{b64}";
                a.download = "shift_report.pdf";
                doc.body.appendChild(a);
                a.click();
                a.remove();
              }})();
            </script>
            """,
            height=0,
        )
        st.toast("PDF downloaded automatically", icon="📄")
    except Exception as e:  # noqa: BLE001
        st.error(f"Error generating report:\n\n{e}")

with tab_report:
    if "report" in st.session_state:
        report_md = st.session_state["report"]
        dl_pdf, dl_md = st.columns(2)

        # PDF — built on demand, cached in session so re-renders don't rebuild it.
        try:
            if st.session_state.get("report_pdf_src") != report_md:
                pdf_bytes = md_to_pdf(report_md)
                (REPORTS_DIR / "shift_report.pdf").write_bytes(pdf_bytes)
                st.session_state["report_pdf"] = pdf_bytes
                st.session_state["report_pdf_src"] = report_md
            dl_pdf.download_button(
                "⬇️ Download PDF",
                data=st.session_state["report_pdf"],
                file_name="shift_report.pdf",
                mime="application/pdf",
                type="primary",
                use_container_width=True,
            )
        except Exception as e:  # noqa: BLE001
            dl_pdf.error(f"PDF failed: {e}")

        dl_md.download_button(
            "⬇️ Download Markdown",
            data=report_md,
            file_name="shift_report.md",
            mime="text/markdown",
            use_container_width=True,
        )
        st.markdown(report_md)
    else:
        st.info("Press **🚀 Generate Shift Report** to produce the full supervisor report here.")
