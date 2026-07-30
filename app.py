import base64
import io
import re
from pathlib import Path

import altair as alt
import markdown as md
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from xhtml2pdf import pisa

from agents.report_agent import REPORT_MODEL, analyze_shift
from shift_metrics import load_shift

# -----------------------------
# Configuration
# -----------------------------
DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = Path(__file__).parent / "reports"
DATA_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

STORED_PREVIOUS = DATA_DIR / "shift_previous.csv"

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
# Markdown -> PDF (pure Python: markdown -> HTML -> reportlab, no system binaries)
# -----------------------------
# base-14 Helvetica uses WinAnsi encoding; map glyphs outside it to ASCII so
# they don't drop out of the PDF (font embedding is blocked by App Control here).
_PDF_GLYPHS = {"▲": "+", "▼": "-", "×": "x", "≥": ">=", "≤": "<=", "→": "->", "•": "-"}


# Explicit per-column widths (percent) per report table, so xhtml2pdf's fixed
# layout renders consistent, aligned columns. Matched by the table's headers;
# unknown tables fall back to equal widths.
def _table_col_widths(headers: list[str]) -> list[str]:
    h = [x.strip().lower() for x in headers]
    joined = " | ".join(h)
    n = len(h) or 1
    if "machine" in h and "notes" in joined:        # Downtime
        return ["10%", "24%", "20%", "46%"]
    if "vs previous shift" in joined:               # Production
        return ["18%", "38%", "44%"]
    if "defect rate" in joined:                     # Defects
        return ["28%", "30%", "42%"]
    if "metric" in h:                               # Comparison
        return ["28%", "24%", "24%", "24%"]
    return [f"{100 // n}%"] * n                      # equal fallback


def _apply_col_widths(html_body: str) -> str:
    """Set explicit widths on each table's header cells (fixed layout uses these)."""
    def per_table(m: "re.Match[str]") -> str:
        table = m.group(0)
        thead = re.search(r"<thead>.*?</thead>", table, re.S)
        if not thead:
            return table
        headers = [re.sub(r"<[^>]+>", "", c).strip()
                   for c in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(0), re.S)]
        if not headers:
            return table
        widths = _table_col_widths(headers)
        counter = {"i": 0}

        def add_width(mth: "re.Match[str]") -> str:
            i = counter["i"]
            counter["i"] += 1
            w = widths[i] if i < len(widths) else ""
            return f'<th style="width:{w}">{mth.group(1)}</th>' if w else mth.group(0)

        new_thead = re.sub(r"<th[^>]*>(.*?)</th>", add_width, thead.group(0), flags=re.S)
        return table.replace(thead.group(0), new_thead)

    return re.sub(r"<table.*?</table>", per_table, html_body, flags=re.S)


def md_to_pdf(md_text: str) -> bytes:
    for k, v in _PDF_GLYPHS.items():
        md_text = md_text.replace(k + " ", v).replace(k, v)

    body = _apply_col_widths(md.markdown(md_text, extensions=["tables", "sane_lists"]))
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
      @page {{ size: A4; margin: 1.6cm; }}
      body {{ font-family: Helvetica, sans-serif; font-size: 10pt; color:#1f2937; line-height:1.45; }}
      h1 {{ font-size:20pt; color:#1e3a8a; border-bottom:2px solid #2563eb; padding-bottom:4px; }}
      h2 {{ font-size:13pt; color:#2563eb; margin-top:14px;
           -pdf-keep-with-next: true; }}
      table {{ width:100%; border-collapse:collapse; margin:8px 0; table-layout:fixed; }}
      th {{ background-color:#2563eb; color:#ffffff; border:1px solid #2563eb;
           padding:5px 8px; text-align:left; font-size:9pt;
           word-wrap:break-word; vertical-align:top; }}
      td {{ border:1px solid #d1d5db; padding:5px 8px; font-size:9pt;
           word-wrap:break-word; vertical-align:top; }}
      tr {{ page-break-inside: avoid; }}
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
    st.caption(
        "🤖 All totals, breakdowns, charts, and anomaly detection are computed "
        "by Claude directly from the raw CSV — nothing is pre-aggregated in Python."
    )
    st.caption("Rules defined in `.claude/agents/shift_report_agent.md`")


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
    st.info("Files uploaded. Press **Analyze shift** to have Claude compute metrics and enable the report.")
    if st.button("▶️ Analyze shift", type="primary", use_container_width=True):
        try:
            with st.spinner(f"🤖 Analyzing with Claude ({REPORT_MODEL})…"):
                st.session_state["analysis"] = analyze_shift(
                    current_df, previous_df,
                    current_path.name, previous_path.name if previous_path else None,
                )
            st.session_state["analyzed"] = True
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Error analyzing shift:\n\n{e}")
    st.stop()

analysis = st.session_state["analysis"]


# -----------------------------
# Live KPI row
# -----------------------------
cur_t = analysis["current_totals"]
prev_t = analysis["previous_totals"]
deltas = analysis["deltas"]

src_note = "vs uploaded previous shift" if previous_file else (
    "vs stored previous shift" if previous_path else "no baseline")
st.caption(f"📊 Metrics from **{current_path.name}** · {src_note}")

k1, k2, k3, k4 = st.columns(4)


def _fmt_pct(v):
    return f"{v:+.1f}% vs prev" if v is not None else None


k1.metric("Units produced", f"{cur_t['units']:,}", _fmt_pct(deltas["units_pct"]) if prev_t else None)
k2.metric("Downtime (min)", f"{cur_t['downtime']:,}", _fmt_pct(deltas["downtime_pct"]) if prev_t else None,
          delta_color="inverse")
k3.metric("Defects", f"{cur_t['defects']:,}", _fmt_pct(deltas["defects_pct"]) if prev_t else None,
          delta_color="inverse")
k4.metric("Defect rate", f"{cur_t['defect_rate']:.2f}%",
          (f"{deltas['defect_rate_pts']:+.2f} pts" if prev_t and deltas["defect_rate_pts"] is not None else None),
          delta_color="inverse")


# -----------------------------
# Anomaly flags
# -----------------------------
flags = [(a["severity"], a["message"]) for a in analysis["anomalies"]]
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

    charts = analysis["charts"]
    by_line = pd.DataFrame(analysis["by_line"])

    # 1) Hero time-series — units produced across the shift, one line per line.
    ut = with_ts(pd.DataFrame(charts["units_by_time"]))
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
    mu = pd.DataFrame(charts["machine_summary"]).rename(columns={"units": "units_produced"})
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
    dl = by_line.rename(columns={"defect_rate": "rate"})
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
    hm = pd.DataFrame(charts["downtime_heatmap"])
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
    dts = with_ts(pd.DataFrame(charts["defects_by_time"]))
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
    ms = pd.DataFrame(charts["machine_summary"])
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
# The Markdown report was already written by Claude during analysis
# (analysis["report_markdown"]) — this just packages it as MD/PDF and
# triggers the download, with no second API call.
st.divider()
if st.button("🚀 Generate Shift Report", type="primary", use_container_width=True):
    try:
        with st.status("Preparing report…", expanded=True) as status:
            report = analysis["report_markdown"]
            (REPORTS_DIR / "shift_report.md").write_text(report, encoding="utf-8")
            st.write("✅ Using the report Claude wrote during analysis · saved to reports/shift_report.md")
            # Build the PDF now so it can auto-download in the browser.
            pdf_bytes = md_to_pdf(report)
            (REPORTS_DIR / "shift_report.pdf").write_bytes(pdf_bytes)
            st.write("📄 PDF built — downloading automatically…")
            status.update(label="Report ready", state="complete", expanded=False)

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
        st.info("Press ** Generate Shift Report** to produce the full supervisor report here.")
