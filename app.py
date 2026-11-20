"""Streamlit dashboard for the Yahoo Finance polling screener."""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from indicators import (
    AWAITING_ZONE,
    BEARISH_BREAKDOWN,
    BULLISH_BREAKOUT,
    CHOP,
    NEAR_BEARISH,
    NEAR_BULLISH,
    calculate_setup_score,
    build_weekly_table,
    completed_weekly_rows,
    compute_atr,
    consolidation_week_dates,
    distance_to_break_percent,
    find_consolidation_zones,
)
from dashboard_theme import (
    COLOR_LABELS,
    PRESETS as THEME_PRESETS,
    css as theme_css,
    default_preferences,
    load_mode,
    load_preferences,
    mode_theme,
    save_mode,
    save_preferences,
    write_streamlit_config,
)
from screener_engine import DEFAULT_WATCHLIST, YahooFinanceProvider, local_now
from storage import SNAPSHOT_COLUMNS, CsvJournal, DashboardStore


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.getenv("SCREENER_DATA_DIR", str(ROOT / "data")))
STORE = DashboardStore(DATA_DIR / "live_screener.sqlite3")
JOURNAL = CsvJournal(DATA_DIR / "live_weekly.csv")

PRESETS = {
    "Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "AMD", "GOOGL"],
    "Financials": ["JPM", "BAC", "GS", "MS", "V", "MA"],
    "Healthcare": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE"],
    "Consumer": ["AMZN", "WMT", "COST", "HD", "MCD", "NKE"],
    "ETFs": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF"],
    "AI infrastructure & neo-cloud": [
        "NBIS", "CRWV", "IREN", "ORCL", "DLR", "APLD", "CORZ"
    ],
    "Memory & storage": ["SNDK", "MU", "SKHY", "WDC", "STX"],
    "Semiconductors": [
        "TSM", "AVGO", "SMH", "INTC", "AMD", "SOXL", "QCOM", "CDNS", "LRCX", "AEHR"
    ],
    "Fintech": ["AFRM", "TOST", "TEM", "XYZ", "PYPL", "NU"],
    "Banks & financials": [
        "GS", "SCHW", "KRE", "USB", "NC", "C", "WFC", "JPM", "BAC"
    ],
    "Cybersecurity": [
        "CRWD", "OKTA", "FTNT", "PANW", "S", "ZS", "NET", "TENB", "RBRK"
    ],
    "Cloud & software": [
        "GTLB", "WDAY", "NOW", "TEAM", "DT", "MDB", "SNOW", "CRM", "HUBS", "PLTR", "DDOG"
    ],
    "Crypto & Bitcoin-related": ["COIN", "MSTR", "CRCL", "HOOD", "HUT", "ISIT"],
    "Space": ["BKSY", "ASTS", "RDW", "LUNR", "PL", "DXYZ", "RKLB", "SPCX"],
    "Optical & photonics": [
        "AXTI", "CRDO", "LITE", "POET", "AAOI", "GCW", "FN", "COHR"
    ],
}
STATE_DESCRIPTIONS = {
    "No 2-week zone yet": (
        "No completed run of 2 or more consolidating weeks. "
        "A one-week run appears as Developing in the report."
    ),
    "Price away from confirmed zone": (
        "A confirmed zone exists, but price is outside it and not close to its edges."
    ),
    "Inside zone, not near an edge": (
        "Price is between the zone high and low, away from both edges."
    ),
    "Near upper edge": "Price is close to the zone high; an upside breakout is not confirmed.",
    "Near lower edge": "Price is close to the zone low; a downside break is not confirmed.",
    "Bullish breakout": "The screener confirmed an upside move through the zone high.",
    "Bearish breakdown": "The screener confirmed a downside move through the zone low.",
}
ALERT_STATES = {BULLISH_BREAKOUT, BEARISH_BREAKDOWN, NEAR_BULLISH, NEAR_BEARISH}
FILTERS = ["Show All", "Breakouts Only", "Near Break Only", "Hide Chop"]
CHART_OVERLAYS = [
    "Price with consolidation-zone band",
    "ATR and ATR average",
    "Weekly high/low range",
    "ATR-based high/low envelope",
]
CHART_LOOKBACKS = {
    "1 month": 1,
    "3 months": 3,
    "6 months": 6,
    "1 year": 12,
}
st.set_page_config(
    page_title="Live consolidation screener",
    page_icon=":material/monitoring:",
    layout="wide",
)


@st.cache_data(ttl="5m")
def _load_ticker_hourly(ticker: str) -> pd.DataFrame:
    """Fetch one year for indicator warm-up; the chart itself shows a shorter view."""
    frames = YahooFinanceProvider(batch_size=1).fetch(
        [ticker], period="1y", interval="1h"
    )
    return frames.get(ticker, pd.DataFrame())


def _render_ticker_atr_chart(watchlist: list[str]) -> None:
    if not watchlist:
        st.info("Add tickers to the watchlist to view an ATR chart.")
        return

    ticker_column, lookback_column, overlay_column = st.columns([1, 1, 2])
    with ticker_column:
        ticker = st.selectbox("Ticker", watchlist, key="atr_chart_ticker")
    with lookback_column:
        lookback = st.selectbox(
            "Chart period",
            list(CHART_LOOKBACKS),
            index=1,
            key="atr_chart_lookback",
        )
    with overlay_column:
        overlays = st.multiselect(
            "Chart layers",
            CHART_OVERLAYS,
            default=CHART_OVERLAYS,
            key="atr_chart_overlays",
        )

    hourly = _load_ticker_hourly(ticker)
    if hourly.empty:
        st.warning(
            f"Yahoo Finance returned no hourly history for {ticker}. "
            "Check the symbol or try again later."
        )
        return

    chart_data = hourly.copy()
    chart_data["atr"] = compute_atr(chart_data)
    chart_data["atr_avg"] = chart_data["atr"].rolling(20).mean()
    chart_data["atr_high"] = chart_data["Close"] + chart_data["atr"]
    chart_data["atr_low"] = chart_data["Close"] - chart_data["atr"]

    weekly_table = build_weekly_table(hourly, ticker)
    closed_weeks = completed_weekly_rows(weekly_table, local_now())
    zones = find_consolidation_zones(closed_weeks, min_weeks=1)

    period_months = CHART_LOOKBACKS[lookback]
    range_start = chart_data.index.max() - pd.DateOffset(months=period_months)
    visible = chart_data.loc[chart_data.index >= range_start].copy()
    weekly_ranges = (
        hourly.resample("W-FRI")
        .agg({"High": "max", "Low": "min"})
        .dropna()
        .rename_axis("datetime")
        .reset_index()
        .rename(columns={"High": "week_high", "Low": "week_low"})
    )
    weekly_ranges = weekly_ranges.loc[
        weekly_ranges["datetime"] >= range_start - pd.Timedelta(days=7)
    ]

    price_chart = alt.Chart(visible).mark_line(strokeWidth=2).encode(
        x=alt.X("datetime:T", title="Date"),
        y=alt.Y("Close:Q", title="Price", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("datetime:T", title="Time"),
            alt.Tooltip("Close:Q", title="Close", format="$.2f"),
        ],
    )
    layers: list[alt.Chart] = []
    if "Weekly high/low range" in overlays and not weekly_ranges.empty:
        layers.append(
            alt.Chart(weekly_ranges)
            .mark_area(opacity=0.1, interpolate="step-after")
            .encode(
                x="datetime:T",
                y=alt.Y("week_high:Q", scale=alt.Scale(zero=False)),
                y2="week_low:Q",
                tooltip=[
                    alt.Tooltip("datetime:T", title="Week"),
                    alt.Tooltip("week_high:Q", title="Weekly high", format="$.2f"),
                    alt.Tooltip("week_low:Q", title="Weekly low", format="$.2f"),
                ],
            )
        )
    if (
        "Price with consolidation-zone band" in overlays
        and not zones.empty
    ):
        zone_frame = zones.copy()
        zone_frame["start"] = (
            pd.to_datetime(zone_frame["zone_start_week"]) - pd.Timedelta(days=5)
        )
        zone_frame["end"] = (
            pd.to_datetime(zone_frame["zone_end_week"]) + pd.Timedelta(days=1)
        )
        zone_frame = zone_frame.loc[zone_frame["end"] >= range_start]
        if not zone_frame.empty:
            layers.append(
                alt.Chart(zone_frame)
                .mark_rect(opacity=0.16)
                .encode(
                    x="start:T",
                    x2="end:T",
                    y=alt.Y("zone_low:Q", scale=alt.Scale(zero=False)),
                    y2="zone_high:Q",
                    tooltip=[
                        alt.Tooltip("ticker:N", title="Ticker"),
                        alt.Tooltip(
                            "consecutive_weeks:Q", title="Consolidating weeks"
                        ),
                        alt.Tooltip("zone_high:Q", title="Zone high", format="$.2f"),
                        alt.Tooltip("zone_low:Q", title="Zone low", format="$.2f"),
                    ],
                )
            )
    if "ATR-based high/low envelope" in overlays:
        layers.append(
            alt.Chart(visible)
            .mark_area(opacity=0.1)
            .encode(
                x="datetime:T",
                y=alt.Y("atr_high:Q", scale=alt.Scale(zero=False)),
                y2="atr_low:Q",
                tooltip=[
                    alt.Tooltip("atr_high:Q", title="Close + ATR", format="$.2f"),
                    alt.Tooltip("atr_low:Q", title="Close - ATR", format="$.2f"),
                ],
            )
        )
    layers.append(price_chart)
    st.altair_chart(
        alt.layer(*layers).properties(height=340).interactive(),
        width="stretch",
        alt=f"{ticker} hourly price with selected consolidation and range overlays",
    )

    if "ATR and ATR average" in overlays:
        atr_view = visible[["atr", "atr_avg"]].rename(
            columns={"atr": "ATR (14)", "atr_avg": "ATR average (20)"}
        )
        atr_view.index.name = "Date"
        st.line_chart(
            atr_view,
            y=["ATR (14)", "ATR average (20)"],
            x_label="Date",
            y_label="ATR",
            alt=f"{ticker} 14-hour ATR and its 20-hour average",
        )
    st.caption(
        "ATR envelope is hourly close ± 14-hour ATR. Weekly highs/lows and "
        "consolidation zones are calculated from Yahoo Finance hourly bars; "
        "zone detection uses completed weeks only."
    )


_TTS_COMPONENT = st.components.v2.component(
    "consolidation_signal_speech",
    html="<span aria-hidden='true'></span>",
    js="""
export default function(component) {
  const { data, parentElement } = component;
  if (!data || !parentElement) return;

  const stateNode = parentElement.host || parentElement;
  const baseline = Number(data.baselineId || 0);
  let lastId = Number(stateNode.dataset.lastSignalId || baseline);
  if (!data.enabled) {
    stateNode.dataset.lastSignalId = String(data.currentId || lastId);
    return;
  }

  for (const event of (data.events || [])) {
    const id = Number(event.id || 0);
    if (id <= lastId) continue;
    if (window.speechSynthesis && typeof SpeechSynthesisUtterance !== "undefined") {
      const utterance = new SpeechSynthesisUtterance(event.text);
      utterance.rate = 1;
      window.speechSynthesis.speak(utterance);
    }
    lastId = id;
  }
  stateNode.dataset.lastSignalId = String(lastId);
}
""",
)


def parse_tickers(text: str) -> list[str]:
    """Parse comma, whitespace, or newline separated Yahoo Finance symbols."""
    tickers: set[str] = set()
    for raw in re.split(r"[\s,;]+", text):
        symbol = raw.strip().strip("\"'").upper()
        if not symbol:
            continue
        if ":" in symbol:
            symbol = symbol.rsplit(":", 1)[-1]
        if re.fullmatch(r"\^?[A-Z0-9][A-Z0-9.-]{0,14}", symbol):
            tickers.add(symbol)
    return sorted(tickers)


def parse_tradingview_csv(contents: bytes) -> list[str]:
    """Read ticker-like columns from a TradingView CSV watchlist."""
    frame = pd.read_csv(io.BytesIO(contents))
    if frame.empty and len(frame.columns) == 0:
        raise ValueError("The uploaded CSV is empty.")
    column_lookup = {str(column).strip().casefold(): column for column in frame.columns}
    symbol_column = next(
        (
            column_lookup[name]
            for name in ("symbol", "ticker", "stock", "name")
            if name in column_lookup
        ),
        frame.columns[0],
    )
    tickers = parse_tickers(" ".join(frame[symbol_column].dropna().astype(str)))
    if not tickers:
        raise ValueError("No valid ticker symbols were found in the uploaded CSV.")
    return tickers


def _selected_sector_tickers() -> list[str]:
    selected = st.session_state.get("sector_presets", [])
    return list(
        dict.fromkeys(
            ticker
            for sector in selected
            for ticker in PRESETS.get(sector, [])
        )
    )


def _display_signal_state(
    state: str | None, zone_high: Any, zone_low: Any
) -> str:
    if state == AWAITING_ZONE:
        if pd.isna(zone_high) or pd.isna(zone_low):
            return "No 2-week zone yet"
        return "Price away from confirmed zone"
    return {
        CHOP: "Inside zone, not near an edge",
        NEAR_BULLISH: "Near upper edge",
        NEAR_BEARISH: "Near lower edge",
        BULLISH_BREAKOUT: "Bullish breakout",
        BEARISH_BREAKDOWN: "Bearish breakdown",
    }.get(state, state or "Status unavailable")


def _load_uploaded_watchlist() -> None:
    uploaded = st.session_state.get("watchlist_upload")
    if uploaded is None:
        return
    try:
        tickers = parse_tradingview_csv(uploaded.getvalue())
    except (ValueError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        st.session_state["watchlist_upload_error"] = str(exc)
        return
    st.session_state["watchlist_text"] = ", ".join(tickers)
    st.session_state["watchlist_upload_error"] = ""


def _render_theme_controls() -> None:
    try:
        preferences = load_preferences()
    except (OSError, ValueError) as exc:
        st.error(f"Could not load saved theme preferences: {exc}")
        preferences = default_preferences()
    try:
        saved_mode = load_mode()
    except (OSError, ValueError) as exc:
        st.error(f"Could not load saved theme mode: {exc}")
        saved_mode = "dark"

    st.session_state.setdefault("theme_preset", preferences["preset"])
    st.session_state.setdefault("theme_mode", saved_mode.capitalize())
    for key, color in preferences["custom"].items():
        st.session_state.setdefault(f"theme_color_{key}", color)
    custom_colors = {
        key: st.session_state[f"theme_color_{key}"] for key in COLOR_LABELS
    }

    with st.sidebar:
        with st.expander("Appearance", icon=":material/palette:"):
            preset = st.selectbox(
                "Color theme",
                [*THEME_PRESETS, "Custom"],
                key="theme_preset",
            )
            mode = st.segmented_control(
                "Appearance",
                ["Dark", "Light"],
                key="theme_mode",
                selection_mode="single",
            )
            st.caption(
                "This setting updates the page, charts, and tables."
            )
            if preset == "Custom":
                st.caption("Customize the dashboard palette.")
                custom_colors = {
                    key: st.color_picker(
                        label,
                        key=f"theme_color_{key}",
                    ).upper()
                    for key, label in COLOR_LABELS.items()
                }

    selected_mode = (mode or saved_mode.capitalize()).lower()
    colors = (
        custom_colors if preset == "Custom" else dict(THEME_PRESETS[preset])
    )
    selected_preferences = {"preset": preset, "custom": custom_colors}
    preferences_changed = selected_preferences != preferences
    mode_changed = selected_mode != saved_mode
    if preferences_changed or mode_changed:
        effective_theme = mode_theme(colors, light=selected_mode == "light")
        try:
            if preferences_changed:
                save_preferences(preset, custom_colors)
            if mode_changed:
                save_mode(selected_mode)
            write_streamlit_config(effective_theme, selected_mode)
        except (OSError, ValueError) as exc:
            st.error(f"Could not apply the selected theme: {exc}")

    effective_theme = mode_theme(colors, light=selected_mode == "light")
    st.html(theme_css(effective_theme))


def _render_speech_alerts(
    enabled: bool, baseline_id: int, current_id: int, events: list[dict[str, Any]]
) -> None:
    messages = [
        {
            "id": event["id"],
            "text": f"{event['ticker']} {event['state']}",
        }
        for event in events
        if event.get("state") in ALERT_STATES
    ]
    _TTS_COMPONENT(
        key="signal-speech",
        data={
            "enabled": enabled,
            "baselineId": baseline_id,
            "currentId": current_id,
            "events": messages,
        },
    )


@st.fragment
def render_live_panel(
    filter_mode: str, audio_enabled: bool, max_zone_width_pct: float
) -> None:
    st.button(
        "Refresh live data",
        key="refresh_live_data",
        icon=":material/refresh:",
    )
    snapshots = STORE.get_snapshots()
    zones = STORE.get_zones()
    signals = STORE.get_all_signals()
    status = STORE.get_status()
    engine_state = status.get("state", {}).get("value", "not started")
    last_poll = status.get("last_poll", {}).get("value", "No successful poll yet")
    message = status.get("message", {}).get("value", "Start the engine in a terminal.")

    st.caption(
        f"Engine: {engine_state} | Last poll: {last_poll} | {message}"
    )
    if engine_state in {"not started", "starting", "error", "degraded"}:
        st.warning(
            "The screener engine is not reporting normal polling. Start or check "
            "`python screener_engine.py` in a separate PowerShell window."
        )

    all_frame = pd.DataFrame(snapshots, columns=SNAPSHOT_COLUMNS)
    if not all_frame.empty:
        all_frame["state"] = all_frame["state"].fillna(AWAITING_ZONE)
        all_frame["display_state"] = [
            _display_signal_state(state, high, low)
            for state, high, low in zip(
                all_frame["state"],
                all_frame["zone_high"],
                all_frame["zone_low"],
            )
        ]
    frame = all_frame.copy()
    if not frame.empty:
        if filter_mode == "Breakouts Only":
            frame = frame[frame["state"].isin({BULLISH_BREAKOUT, BEARISH_BREAKDOWN})]
        elif filter_mode == "Near Break Only":
            frame = frame[frame["state"].isin({NEAR_BULLISH, NEAR_BEARISH})]
        elif filter_mode == "Hide Chop":
            frame = frame[frame["state"] != CHOP]
        frame = frame[
            frame["zone_width_pct"].isna()
            | (frame["zone_width_pct"] <= max_zone_width_pct)
        ]

    snapshots_by_ticker = {row["ticker"]: row for row in snapshots}
    zones_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for zone_row in zones:
        zones_by_ticker.setdefault(zone_row["ticker"], []).append(zone_row)

    report_rows: list[dict[str, Any]] = []
    for ticker in STORE.get_watchlist():
        ticker_zones = zones_by_ticker.get(ticker, [])
        current_zone = next(
            (zone_row for zone_row in ticker_zones if zone_row["is_current"]),
            None,
        )
        latest_zone = ticker_zones[0] if ticker_zones else None
        display_zone = current_zone or latest_zone
        snapshot = snapshots_by_ticker.get(ticker, {})
        current_price = snapshot.get("price")
        distance = (
            distance_to_break_percent(
                current_price,
                float(current_zone["zone_high"]),
                float(current_zone["zone_low"]),
            )
            if current_zone and current_price is not None
            else None
        )
        weeks = (
            int(display_zone["consecutive_weeks"]) if display_zone else 0
        )
        if display_zone:
            period_start, period_end = consolidation_week_dates(
                display_zone["zone_start_week"],
                display_zone["zone_end_week"],
            )
            period_text = f"{period_start.isoformat()} to {period_end.isoformat()}"
        else:
            period_start = period_end = None
            period_text = None
        if current_zone:
            zone_status = (
                "Active - developing"
                if weeks == 1
                else "Active - confirmed"
            )
            setup_score = calculate_setup_score(
                weeks,
                distance,
                snapshot.get("atr"),
                snapshot.get("atr_avg"),
                snapshot.get("weekly_rv"),
                snapshot.get("rv_avg"),
            )
        elif latest_zone:
            zone_status = "Historical only"
            setup_score = 0
        else:
            zone_status = "No zone found"
            setup_score = 0
        report_rows.append(
            {
                "Ticker": ticker,
                "Zone status": zone_status,
                "Zone weeks": weeks,
                "Consolidation start": period_start,
                "Consolidation end": period_end,
                "Consolidation period": period_text,
                "Current price": current_price,
                "Live signal": _display_signal_state(
                    snapshot.get("state", AWAITING_ZONE),
                    snapshot.get("zone_high"),
                    snapshot.get("zone_low"),
                ),
                "Zone high": display_zone.get("zone_high") if display_zone else None,
                "Zone low": display_zone.get("zone_low") if display_zone else None,
                "Distance to break (%)": distance,
                "Setup score": setup_score,
            }
        )
    report_frame = pd.DataFrame(report_rows)
    zones_frame = pd.DataFrame(zones)
    if not zones_frame.empty:
        zone_periods = [
            consolidation_week_dates(start, end)
            for start, end in zip(
                zones_frame["zone_start_week"],
                zones_frame["zone_end_week"],
            )
        ]
        zones_frame["period_start"] = [
            start.isoformat() for start, _ in zone_periods
        ]
        zones_frame["period_end"] = [end.isoformat() for _, end in zone_periods]
        zones_frame["consolidation_period"] = [
            f"{start.isoformat()} to {end.isoformat()}"
            for start, end in zone_periods
        ]
    zones_frame = zones_frame.rename(
        columns={
            "ticker": "Ticker",
            "zone_start_week": "First qualifying week ending",
            "zone_end_week": "Last qualifying week ending",
            "consecutive_weeks": "Consecutive weeks",
            "zone_high": "Zone high",
            "zone_low": "Zone low",
            "last_close": "Last weekly close",
            "is_current": "Currently consolidating",
            "period_start": "Period start (Monday)",
            "period_end": "Period end (Friday)",
            "consolidation_period": "Consolidation period",
        }
    )
    signals_frame = pd.DataFrame(signals)
    if not signals_frame.empty:
        for column in ("zone_start_week", "zone_end_week"):
            if column not in signals_frame:
                signals_frame[column] = None
        signal_periods = [
            (
                consolidation_week_dates(start, end)
                if pd.notna(start) and pd.notna(end)
                else (None, None)
            )
            for start, end in zip(
                signals_frame["zone_start_week"],
                signals_frame["zone_end_week"],
            )
        ]
        signals_frame["zone_period_start"] = [
            start.isoformat() if start else None for start, _ in signal_periods
        ]
        signals_frame["zone_period_end"] = [
            end.isoformat() if end else None for _, end in signal_periods
        ]
        signals_frame["zone_period"] = [
            f"{start.isoformat()} to {end.isoformat()}" if start and end else None
            for start, end in signal_periods
        ]
    signals_frame = signals_frame.rename(
        columns={
            "ticker": "Ticker",
            "state": "Signal",
            "timestamp": "Time",
            "price": "Signal price",
            "zone_high": "Zone high",
            "zone_low": "Zone low",
            "distance_to_break_pct": "Distance to break (%)",
            "consecutive_weeks": "Consolidation weeks",
            "zone_start_week": "First zone week ending",
            "zone_end_week": "Last zone week ending",
            "zone_period_start": "Zone period start (Monday)",
            "zone_period_end": "Zone period end (Friday)",
            "zone_period": "Zone period",
            "setup_score": "Setup score",
        }
    )
    if not signals_frame.empty:
        signal_times = pd.to_datetime(signals_frame["Time"], errors="coerce")
        signals_frame["Time"] = (
            signal_times.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("-")
        )

    recent_signal_id = STORE.latest_signal_id()
    last_audio_id = st.session_state.get("last_audio_signal_id")
    if last_audio_id is None:
        st.session_state["last_audio_signal_id"] = recent_signal_id
        baseline_id = recent_signal_id
        events: list[dict[str, Any]] = []
    elif audio_enabled:
        baseline_id = int(last_audio_id)
        events = STORE.get_signals_after(baseline_id)
        if events:
            st.session_state["last_audio_signal_id"] = max(
                int(event["id"]) for event in events
            )
    else:
        st.session_state["last_audio_signal_id"] = recent_signal_id
        baseline_id = recent_signal_id
        events = []
    _render_speech_alerts(audio_enabled, baseline_id, recent_signal_id, events)

    active_zone_count = sum(bool(zone_row["is_current"]) for zone_row in zones)
    if all_frame.empty:
        breakout_count = near_count = 0
    else:
        breakout_count = int(
            all_frame["state"].isin({BULLISH_BREAKOUT, BEARISH_BREAKDOWN}).sum()
        )
        near_count = int(all_frame["state"].isin({NEAR_BULLISH, NEAR_BEARISH}).sum())
    with st.container(horizontal=True):
        st.metric("Tracked tickers", len(all_frame), border=True)
        st.metric("Active zones (1+ week)", active_zone_count, border=True)
        st.metric("Breakouts", breakout_count, border=True)
        st.metric("Near-break states", near_count, border=True)

    with st.container(border=True):
        st.subheader("Current watchlist status")
        if all_frame.empty:
            st.caption("Status counts will appear when the engine publishes snapshots.")
        else:
            state_counts = (
                all_frame["display_state"]
                .value_counts()
                .rename_axis("Status")
                .reset_index(name="Count")
            )
            ticker_names = all_frame.groupby("display_state")["ticker"].agg(
                lambda tickers: ", ".join(sorted(tickers))
            )
            state_counts["Ticker(s)"] = state_counts["Status"].map(ticker_names)
            state_counts["Meaning"] = (
                state_counts["Status"]
                .map(STATE_DESCRIPTIONS)
                .fillna("No additional status details available.")
            )
            st.dataframe(
                state_counts[["Ticker(s)", "Status", "Count", "Meaning"]],
                column_config={
                    "Ticker(s)": st.column_config.TextColumn(width="large"),
                    "Status": st.column_config.TextColumn(width="medium"),
                    "Count": st.column_config.NumberColumn(width="small"),
                    "Meaning": st.column_config.TextColumn(width="large"),
                },
                hide_index=True,
                width="stretch",
                height=min(280, 38 + 35 * len(state_counts)),
                alt="Current ticker counts with a plain-language explanation of each status",
            )

    with st.container(border=True):
        st.subheader("Signal zone")
        active_report = report_frame[
            report_frame["Zone status"].str.startswith("Active")
        ].sort_values("Setup score", ascending=False)
        if active_report.empty:
            st.caption("No active 1-week-or-longer consolidation zones.")
        else:
            st.dataframe(
                active_report[
                    [
                        "Ticker",
                        "Zone weeks",
                        "Zone high",
                        "Zone low",
                        "Live signal",
                        "Distance to break (%)",
                        "Setup score",
                    ]
                ],
                column_config={
                    "Ticker": st.column_config.TextColumn(width="small"),
                    "Live signal": st.column_config.TextColumn(width="medium"),
                    "Zone high": st.column_config.NumberColumn(
                        width="small", format="$%.2f"
                    ),
                    "Zone low": st.column_config.NumberColumn(
                        width="small", format="$%.2f"
                    ),
                    "Distance to break (%)": st.column_config.NumberColumn(
                        width="small", format="%.2f%%"
                    ),
                    "Setup score": st.column_config.NumberColumn(
                        width="small", format="%d/100"
                    ),
                },
                hide_index=True,
                width="stretch",
                height=min(350, 38 + 35 * len(active_report)),
                alt="Active consolidation zones, breakout proximity, and setup scores",
            )
        st.caption(
            "Setup score is a rule-based heuristic, not a probability or "
            "performance forecast."
        )

    with st.container(border=True):
        st.subheader("Tickers by state")
        active_tickers = active_report[
            ["Ticker", "Zone weeks", "Zone high", "Zone low"]
        ].copy()
        breakout_tickers = all_frame.loc[
            all_frame["state"].isin({BULLISH_BREAKOUT, BEARISH_BREAKDOWN}),
            ["ticker", "state", "last_signal_time"],
        ].rename(
            columns={
                "ticker": "Ticker",
                "state": "State",
                "last_signal_time": "Signal date/time",
            }
        )
        near_tickers = all_frame.loc[
            all_frame["state"].isin({NEAR_BULLISH, NEAR_BEARISH}),
            ["ticker", "state", "last_signal_time"],
        ].rename(
            columns={
                "ticker": "Ticker",
                "state": "State",
                "last_signal_time": "Signal date/time",
            }
        )
        for ticker_frame in (breakout_tickers, near_tickers):
            if not ticker_frame.empty:
                ticker_frame["Signal date/time"] = (
                    pd.to_datetime(ticker_frame["Signal date/time"], errors="coerce")
                    .dt.strftime("%Y-%m-%d %H:%M:%S")
                    .fillna("-")
                )
        active_column, breakout_column, near_column = st.columns(3)
        with active_column:
            st.markdown("**Active zones**")
            if active_tickers.empty:
                st.caption("None")
            else:
                st.dataframe(
                    active_tickers,
                    hide_index=True,
                    width="stretch",
                    alt="Tickers with active consolidation zones",
                )
        with breakout_column:
            st.markdown("**Breakouts / breakdowns**")
            if breakout_tickers.empty:
                st.caption("None")
            else:
                st.dataframe(
                    breakout_tickers,
                    hide_index=True,
                    width="stretch",
                    alt="Tickers with confirmed breakout or breakdown signals",
                )
        with near_column:
            st.markdown("**Near break**")
            if near_tickers.empty:
                st.caption("None")
            else:
                st.dataframe(
                    near_tickers,
                    hide_index=True,
                    width="stretch",
                    alt="Tickers near their consolidation-zone boundary",
                )

    if st.toggle(
        "Show individual ticker ATR chart",
        value=False,
        key="show_atr_chart",
    ):
        _render_ticker_atr_chart(STORE.get_watchlist())

    report_tab, zones_tab, signals_tab = st.tabs(
        ["Consolidation report", "All zones", "All signals"]
    )
    with report_tab:
        st.caption(
            "One row per watchlist ticker. One completed consolidating week is "
            "shown as developing; two or more weeks are confirmed. Period dates "
            "are Monday through Friday; rows with no zone have no period dates."
        )
        st.dataframe(
            report_frame,
            column_config={
                "Consolidation start": st.column_config.DateColumn(
                    width="small", format="YYYY-MM-DD"
                ),
                "Consolidation end": st.column_config.DateColumn(
                    width="small", format="YYYY-MM-DD"
                ),
                "Current price": st.column_config.NumberColumn(
                    width="small", format="$%.2f"
                ),
                "Zone high": st.column_config.NumberColumn(
                    width="small", format="$%.2f"
                ),
                "Zone low": st.column_config.NumberColumn(
                    width="small", format="$%.2f"
                ),
                "Distance to break (%)": st.column_config.NumberColumn(
                    width="small", format="%.2f%%"
                ),
                "Setup score": st.column_config.NumberColumn(
                    width="small", format="%d/100"
                ),
            },
            hide_index=True,
            width="stretch",
            alt="Consolidation status, zone levels, live signal, and setup score by ticker",
        )
        workbook = io.BytesIO()
        with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
            report_frame.to_excel(writer, sheet_name="Screener Report", index=False)
            zones_frame.to_excel(writer, sheet_name="All Zones", index=False)
            signals_frame.to_excel(writer, sheet_name="All Signals", index=False)
        st.download_button(
            "Download Excel report",
            data=workbook.getvalue(),
            file_name="live_consolidation_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            icon=":material/download:",
        )
    with zones_tab:
        if zones_frame.empty:
            st.info("No completed 1-week-or-longer consolidation zones yet.")
        else:
            st.dataframe(
                zones_frame,
                hide_index=True,
                width="stretch",
                alt="Every detected consolidation zone in the current watchlist history",
            )
    with signals_tab:
        if signals_frame.empty:
            st.info("No signals have been recorded yet.")
        else:
            ticker_search = st.text_input(
                "Search signal ticker",
                placeholder="e.g. NVDA",
                key="signal_ticker_search",
            )
            filtered_signals = signals_frame
            if ticker_search.strip():
                search = ticker_search.strip().casefold()
                filtered_signals = filtered_signals[
                    filtered_signals["Ticker"].str.casefold().str.contains(search, regex=False)
                ]
            if filtered_signals.empty:
                st.info("No signals match that ticker.")
            else:
                st.dataframe(
                    filtered_signals,
                    hide_index=True,
                    width="stretch",
                    alt="Every recorded signal with the associated consolidation zone",
                )

    if frame.empty:
        st.info(
            "No ticker snapshots match these filters, or the engine has not returned "
            "data yet."
        )
        return

    frame = frame.copy()
    frame["state"] = [
        _display_signal_state(state, high, low)
        for state, high, low in zip(
            frame["state"],
            frame["zone_high"],
            frame["zone_low"],
        )
    ]
    display = frame.rename(
        columns={
            "ticker": "Ticker",
            "price": "Live Price",
            "state": "Current State",
            "zone_high": "Consolidation High",
            "zone_low": "Consolidation Low",
            "distance_to_break_pct": "Distance to Break (%)",
            "zone_width_pct": "Zone Width (%)",
            "last_signal_time": "Signal Date/Time",
        }
    )
    zone_periods = [
        consolidation_week_dates(start, end)
        if pd.notna(start) and pd.notna(end)
        else (None, None)
        for start, end in zip(
            frame["zone_start_week"],
            frame["zone_end_week"],
        )
    ]
    display["Zone Period"] = [
        f"{start.isoformat()} to {end.isoformat()}" if start and end else "-"
        for start, end in zone_periods
    ]
    signal_times = pd.to_datetime(display["Signal Date/Time"], errors="coerce")
    display["Signal Date/Time"] = (
        signal_times.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("-")
    )
    visible_columns = [
        "Ticker",
        "Live Price",
        "Current State",
        "Consolidation High",
        "Consolidation Low",
        "Distance to Break (%)",
        "Zone Width (%)",
        "Zone Period",
        "Signal Date/Time",
    ]
    display = display.reindex(columns=visible_columns)
    st.dataframe(
        display,
        column_config={
            "Ticker": st.column_config.TextColumn(width="small"),
            "Live Price": st.column_config.NumberColumn(width="small", format="$%.2f"),
            "Current State": st.column_config.TextColumn(width="medium"),
            "Consolidation High": st.column_config.NumberColumn(
                width="small", format="$%.2f"
            ),
            "Consolidation Low": st.column_config.NumberColumn(
                width="small", format="$%.2f"
            ),
            "Distance to Break (%)": st.column_config.NumberColumn(
                width="small", format="%.2f%%"
            ),
            "Zone Width (%)": st.column_config.NumberColumn(
                width="small", format="%.2f%%"
            ),
            "Signal Date/Time": st.column_config.TextColumn(width="medium"),
        },
        hide_index=True,
        width="stretch",
        height=min(600, 38 + 35 * max(1, len(display))),
        alt="Live equity watchlist with price, consolidation boundaries, and signals",
    )
    with st.expander("Chop duration and zone details"):
        chop_rows = all_frame.loc[
            all_frame["state"] == CHOP,
            ["ticker", "chop_bars", "chop_minutes", "zone_width_pct"],
        ].rename(
            columns={
                "ticker": "Ticker",
                "chop_bars": "Chop bars",
                "chop_minutes": "Chop minutes",
                "zone_width_pct": "Zone Width (%)",
            }
        )
        if chop_rows.empty:
            st.caption("No tickers are currently in Chop.")
        else:
            st.dataframe(
                chop_rows,
                column_config={
                    "Chop bars": st.column_config.NumberColumn(width="small"),
                    "Chop minutes": st.column_config.NumberColumn(width="small"),
                    "Zone Width (%)": st.column_config.NumberColumn(
                        width="small", format="%.2f%%"
                    ),
                },
                hide_index=True,
                width="stretch",
                alt="Current Chop duration and consolidation width by ticker",
            )


def main() -> None:
    _render_theme_controls()
    st.title("Live consolidation screener")
    st.caption(
        "Hourly-to-weekly consolidation logic with Yahoo Finance 1-minute polling. "
        "This is best-effort market data, not a guaranteed real-time feed."
    )

    if not STORE.get_watchlist():
        STORE.set_watchlist(DEFAULT_WATCHLIST)
    current_watchlist = STORE.get_watchlist()
    if "sector_universe_added" not in st.session_state:
        sector_universe = parse_tickers(
            """
            NBIS, CRWV, IREN, ORCL, DLR, APLD, CORZ,
            SNDK, MU, SKHY, WDC, STX,
            TSM, AVGO, SMH, INTC, AMD, SOXL, QCOM, CDNS, LRCX, AEHR,
            AFRM, TOST, TEM, XYZ, PYPL, NU,
            GS, SCHW, KRE, USB, NC, C, WFC, JPM, BAC,
            CRWD, OKTA, FTNT, PANW, S, ZS, NET, TENB, RBRK,
            GTLB, WDAY, NOW, TEAM, DT, MDB, SNOW, CRM, HUBS, PLTR, DDOG,
            COIN, MSTR, CRCL, HOOD, HUT, ISIT,
            BKSY, ASTS, RDW, LUNR, PL, DXYZ, RKLB, SPCX,
            AXTI, CRDO, LITE, POET, AAOI, GCW, FN, COHR
            """
        )
        sector_universe = sorted(set(current_watchlist).union(sector_universe))
        STORE.set_watchlist(sector_universe)
        if "Sector universe" not in STORE.list_watchlist_collections():
            STORE.save_watchlist_collection("Sector universe", sector_universe)
        st.session_state["sector_universe_added"] = True
        current_watchlist = STORE.get_watchlist()
    st.session_state.setdefault("watchlist_text", ", ".join(current_watchlist))
    st.session_state.setdefault("watchlist_upload_error", "")

    with st.container(border=True):
        st.subheader("Active watchlist")
        metric_column, ticker_column = st.columns([1, 4])
        with metric_column:
            st.metric("Tracked tickers", len(current_watchlist), border=True)
        with ticker_column:
            st.pills(
                "Watchlist tickers",
                current_watchlist,
                selection_mode="single",
                default=current_watchlist[0] if current_watchlist else None,
                key="active_watchlist_ticker",
            )

    with st.sidebar:
        st.header("Watchlist")
        st.multiselect(
            "Sector presets",
            list(PRESETS),
            key="sector_presets",
            help="Choose one or more sector groups to add to your saved watchlist.",
        )
        add_sectors = st.button(
            "Add sectors to saved watchlist",
            key="add_sector_watchlist",
            icon=":material/list:",
            width="stretch",
        )
        if add_sectors:
            sector_tickers = _selected_sector_tickers()
            if not sector_tickers:
                st.warning("Choose at least one sector before adding tickers.")
            else:
                saved_tickers = set(current_watchlist)
                new_tickers = [
                    ticker for ticker in sector_tickers if ticker not in saved_tickers
                ]
                current_watchlist.extend(new_tickers)
                STORE.set_watchlist(current_watchlist)
                current_watchlist = STORE.get_watchlist()
                st.session_state["watchlist_text"] = ", ".join(current_watchlist)
                if new_tickers:
                    st.success(
                        f"Added {len(new_tickers)} ticker(s); "
                        f"{len(current_watchlist)} total saved."
                    )
                else:
                    st.info("All selected sector tickers are already saved.")

        st.file_uploader(
            "Upload a TradingView CSV",
            type=["csv"],
            key="watchlist_upload",
            help="A Symbol or Ticker column is preferred; otherwise the first column is read.",
        )
        st.button(
            "Load uploaded symbols",
            on_click=_load_uploaded_watchlist,
            icon=":material/upload_file:",
            width="stretch",
        )
        if st.session_state["watchlist_upload_error"]:
            st.error(st.session_state["watchlist_upload_error"])

        with st.form("watchlist-form"):
            st.text_area(
                "Tickers (comma, space, or newline separated)",
                key="watchlist_text",
                height=140,
            )
            apply_watchlist = st.form_submit_button(
                "Apply watchlist", type="primary", width="stretch"
            )
        if apply_watchlist:
            tickers = parse_tickers(st.session_state["watchlist_text"])
            if not tickers:
                st.error("Enter at least one valid ticker symbol.")
            else:
                STORE.set_watchlist(tickers)
                st.success(
                    f"Applied {len(tickers)} ticker(s). The engine will update "
                    "its polling list on the next cycle."
                )

        st.subheader("Saved watchlists")
        collection_name = st.text_input(
            "Collection name",
            key="watchlist_collection_name",
            placeholder="Momentum list",
        )
        if st.button(
            "Save current watchlist",
            icon=":material/bookmark_add:",
            width="stretch",
        ):
            try:
                STORE.save_watchlist_collection(
                    st.session_state["watchlist_collection_name"],
                    STORE.get_watchlist(),
                )
                st.success("Watchlist collection saved.")
            except ValueError as exc:
                st.error(str(exc))

        collection_names = STORE.list_watchlist_collections()
        if collection_names:
            selected_collection = st.selectbox(
                "Saved collection",
                collection_names,
                key="saved_watchlist_collection",
            )
            load_collection, delete_collection = st.columns(2)
            with load_collection:
                if st.button(
                    "Load",
                    icon=":material/download:",
                    width="stretch",
                ):
                    tickers = STORE.get_watchlist_collection(selected_collection)
                    STORE.set_watchlist(tickers)
                    st.session_state["watchlist_text"] = ", ".join(tickers)
                    st.success(f"Loaded {len(tickers)} ticker(s).")
            with delete_collection:
                if st.button(
                    "Delete",
                    icon=":material/delete:",
                    width="stretch",
                ):
                    STORE.delete_watchlist_collection(selected_collection)
                    st.success(f"Deleted \"{selected_collection}\".")

        st.divider()
        filter_mode = st.segmented_control(
            "Quick filter",
            FILTERS,
            default="Show All",
            selection_mode="single",
        )
        max_zone_width_pct = st.slider(
            "Maximum zone width (%)",
            min_value=0.0,
            max_value=100.0,
            value=100.0,
            step=0.5,
            help="Lower this to focus on tighter consolidation coils.",
        )
        audio_enabled = st.toggle("Spoken breakout and near-break alerts", value=False)
        st.caption("Browser speech may require a user gesture and enabled audio.")

        try:
            csv_data = JOURNAL.read_bytes()
            st.download_button(
                "Download live weekly CSV",
                data=csv_data,
                file_name="live_weekly.csv",
                mime="text/csv",
                icon=":material/download:",
                width="stretch",
            )
        except Exception as exc:
            st.error(f"Could not read the CSV while it is being written: {exc}")

        st.caption("Yahoo Finance data may be delayed or incomplete.")

    render_live_panel(filter_mode or "Show All", audio_enabled, max_zone_width_pct)
    st.caption(
        "yfinance does not provide a live streaming API. Quotes and minute bars may "
        "be delayed, missing, or rate-limited; do not use this dashboard as an "
        "execution or risk-control system."
    )


if __name__ == "__main__":
    main()
