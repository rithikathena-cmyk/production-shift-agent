"""Claude shift analysis — single structured-output API call.

Claude receives the raw shift CSV data (no pre-aggregation) and computes
every total, breakdown, chart series, and anomaly itself, following the
rules in prompts/system_prompt.md. This module only serializes the raw
DataFrames to CSV text and parses the structured tool-call result — there
is no arithmetic, threshold, or anomaly logic in Python.
"""

import os
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

PROMPTS_DIR = Path(__file__).parent / "prompts"

# The model now performs real arithmetic (totals, aggregates, deltas), so it
# needs to be a capable model rather than the fastest one.
REPORT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = (PROMPTS_DIR / "system_prompt.md").read_text(encoding="utf-8").strip()
REPORT_PROMPT_TEMPLATE = (PROMPTS_DIR / "report_prompt.md").read_text(encoding="utf-8")

_TOTALS_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {"type": "integer"},
        "downtime": {"type": "integer"},
        "defects": {"type": "integer"},
        "defect_rate": {"type": "number", "description": "defects / units * 100"},
    },
    "required": ["units", "downtime", "defects", "defect_rate"],
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "current_totals": _TOTALS_SCHEMA,
        "previous_totals": {
            "anyOf": [_TOTALS_SCHEMA, {"type": "null"}],
            "description": "null if no previous shift was provided",
        },
        "deltas": {
            "type": "object",
            "description": "Percent change current vs previous; null fields if no previous shift.",
            "properties": {
                "units_pct": {"type": ["number", "null"]},
                "downtime_pct": {"type": ["number", "null"]},
                "defects_pct": {"type": ["number", "null"]},
                "defect_rate_pts": {"type": ["number", "null"], "description": "percentage-point change"},
            },
            "required": ["units_pct", "downtime_pct", "defects_pct", "defect_rate_pts"],
        },
        "by_line": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "string"},
                    "units": {"type": "integer"},
                    "units_prev": {"type": ["integer", "null"]},
                    "units_delta_pct": {"type": ["number", "null"]},
                    "defects": {"type": "integer"},
                    "defect_rate": {"type": "number"},
                },
                "required": ["line", "units", "units_prev", "units_delta_pct", "defects", "defect_rate"],
            },
        },
        "downtime_by_machine": {
            "type": "array",
            "description": "Only machines with total downtime > 0, sorted descending.",
            "items": {
                "type": "object",
                "properties": {
                    "machine": {"type": "string"},
                    "line": {"type": "string"},
                    "downtime_minutes": {"type": "integer"},
                },
                "required": ["machine", "line", "downtime_minutes"],
            },
        },
        "anomalies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["bad", "warn"]},
                    "message": {"type": "string"},
                },
                "required": ["severity", "message"],
            },
        },
        "charts": {
            "type": "object",
            "properties": {
                "units_by_time": {
                    "type": "array",
                    "description": "units_produced summed per (timestamp, line), one row per combination.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "timestamp": {"type": "string"},
                            "line": {"type": "string"},
                            "units_produced": {"type": "integer"},
                        },
                        "required": ["timestamp", "line", "units_produced"],
                    },
                },
                "defects_by_time": {
                    "type": "array",
                    "description": "defects summed per (timestamp, line), one row per combination.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "timestamp": {"type": "string"},
                            "line": {"type": "string"},
                            "defects": {"type": "integer"},
                        },
                        "required": ["timestamp", "line", "defects"],
                    },
                },
                "downtime_heatmap": {
                    "type": "array",
                    "description": "downtime_minutes summed per (machine, timestamp), only where > 0.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "machine": {"type": "string"},
                            "timestamp": {"type": "string"},
                            "downtime_minutes": {"type": "integer"},
                        },
                        "required": ["machine", "timestamp", "downtime_minutes"],
                    },
                },
                "machine_summary": {
                    "type": "array",
                    "description": "units, defects, downtime summed per (machine, line) across the whole shift.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "machine": {"type": "string"},
                            "line": {"type": "string"},
                            "units": {"type": "integer"},
                            "defects": {"type": "integer"},
                            "downtime": {"type": "integer"},
                        },
                        "required": ["machine", "line", "units", "defects", "downtime"],
                    },
                },
            },
            "required": ["units_by_time", "defects_by_time", "downtime_heatmap", "machine_summary"],
        },
        "report_markdown": {
            "type": "string",
            "description": "The complete Markdown shift report, no code fences.",
        },
    },
    "required": [
        "current_totals", "previous_totals", "deltas", "by_line",
        "downtime_by_machine", "anomalies", "charts", "report_markdown",
    ],
}


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


def _csv_block(df: pd.DataFrame, label: str) -> str:
    return f"--- {label} ---\n{df.to_csv(index=False)}"


def analyze_shift(cur: pd.DataFrame, prev: pd.DataFrame | None,
                  cur_name: str, prev_name: str | None) -> dict:
    """Send the raw shift CSV data to Claude and return its full structured analysis."""
    blocks = [_csv_block(cur, f"CURRENT SHIFT ({cur_name})")]
    if prev is not None:
        blocks.append(_csv_block(prev, f"PREVIOUS SHIFT ({prev_name})"))
    else:
        blocks.append("PREVIOUS SHIFT: none provided — comparison and delta-based anomalies are unavailable.")
    prompt = REPORT_PROMPT_TEMPLATE.format(data="\n\n".join(blocks))

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it as a secret on your host "
            "(e.g. Streamlit Community Cloud → App settings → Secrets) or export it "
            "locally: `ANTHROPIC_API_KEY=sk-ant-...`"
        )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        # Streamed with a generous budget: the model reasons (adaptive thinking,
        # on by default for this model) and then emits large chart-series arrays
        # before writing report_markdown, so a tight max_tokens can silently
        # truncate the report — the symptom is an empty/blank PDF downstream.
        with client.messages.stream(
            model=REPORT_MODEL,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            tools=[{
                "name": "record_shift_analysis",
                "description": "Record the complete computed shift analysis and report.",
                "input_schema": ANALYSIS_SCHEMA,
            }],
            tool_choice={"type": "tool", "name": "record_shift_analysis"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anthropic API key is invalid or revoked.") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Anthropic API rate limit hit — retry in a moment.") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error ({e.status_code}): {e.message}") from e

    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's analysis was cut off before finishing (hit the max_tokens limit). "
            "Try again, or use a smaller shift file."
        )

    for block in message.content:
        if block.type == "tool_use" and block.name == "record_shift_analysis":
            if not block.input.get("report_markdown"):
                raise RuntimeError("Claude returned an analysis with no report text — please retry.")
            return block.input

    raise RuntimeError("Claude did not return a structured shift analysis.")
