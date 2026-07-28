"""Claude report generator — single fast Anthropic API call, no tool loop.

Prompt text lives in prompts/ (system_prompt.md, report_prompt.md) so the
wording can change without touching this code. This module only turns
pre-computed pandas data into the facts block and calls the API.
"""

import os
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

from shift_metrics import pct_delta, totals

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Fast model — the numbers are pre-computed in pandas, Claude only writes prose.
REPORT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = (PROMPTS_DIR / "system_prompt.md").read_text(encoding="utf-8").strip()
REPORT_PROMPT_TEMPLATE = (PROMPTS_DIR / "report_prompt.md").read_text(encoding="utf-8")


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
    prompt = REPORT_PROMPT_TEMPLATE.format(facts=facts)

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
            system=SYSTEM_PROMPT,
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
