"""Yfinance-backed polling engine for the weekly consolidation screener.

Yahoo Finance is polled for recent one-minute bars; it is not a streaming
feed and may be delayed, incomplete, or rate-limited.
"""

from __future__ import annotations

import os
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from indicators import (
    AWAITING_ZONE,
    SignalTracker,
    build_weekly_table,
    calculate_setup_score,
    completed_weekly_rows,
    distance_to_break_percent,
    find_consolidation_zones,
    zone_width_percent,
)
from storage import CsvJournal, DashboardStore


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
EASTERN = ZoneInfo("America/New_York")
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]
DEFAULT_DATA_DIR = ROOT / "data"


@dataclass(frozen=True)
class EngineSettings:
    poll_seconds: int = 60
    history_period: str = "1y"
    proximity_pct: float = 0.3
    hold_bars: int = 2
    cooldown_minutes: int = 30
    batch_size: int = 40

    @classmethod
    def from_environment(cls) -> "EngineSettings":
        settings = cls(
            poll_seconds=int(os.getenv("YFINANCE_POLL_SECONDS", "60")),
            history_period=os.getenv("YFINANCE_HISTORY_PERIOD", "1y"),
            proximity_pct=float(os.getenv("NEAR_BREAK_PCT", "0.3")),
            hold_bars=int(os.getenv("BREAKOUT_HOLD_HOURS", "2")),
            cooldown_minutes=int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30")),
        )
        if settings.poll_seconds < 15:
            raise ValueError("YFINANCE_POLL_SECONDS must be at least 15 seconds.")
        if settings.proximity_pct <= 0:
            raise ValueError("NEAR_BREAK_PCT must be greater than zero.")
        if settings.hold_bars < 1:
            raise ValueError("BREAKOUT_HOLD_HOURS must be at least one.")
        if settings.cooldown_minutes < 0:
            raise ValueError("SIGNAL_COOLDOWN_MINUTES cannot be negative.")
        return settings


def local_now() -> datetime:
    return datetime.now(EASTERN)


def is_market_polling_window(now: datetime | None = None) -> bool:
    """Include a ten-minute closeout window so the final hourly bar is collected."""
    local = now or local_now()
    if local.tzinfo is None:
        local = local.replace(tzinfo=EASTERN)
    else:
        local = local.astimezone(EASTERN)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(16, 10)


def normalize_yfinance_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize yfinance OHLCV columns and timestamps to naive New York time."""
    if frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = frame.copy()
    result.columns = [str(column).strip().title() for column in result.columns]
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = set(required).difference(result.columns)
    if missing:
        raise ValueError(f"Yfinance response is missing OHLCV columns: {sorted(missing)}")
    result = result[required]

    index = pd.DatetimeIndex(pd.to_datetime(result.index))
    if index.tz is None:
        index = index.tz_localize(EASTERN)
    else:
        index = index.tz_convert(EASTERN)
    result.index = index.tz_localize(None)
    result.index.name = "datetime"
    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["Open", "High", "Low", "Close"])
    return result[~result.index.duplicated(keep="last")].sort_index()


def extract_symbol_frame(
    frame: pd.DataFrame, ticker: str, requested_tickers: list[str]
) -> pd.DataFrame:
    """Select a ticker from either flat or yfinance multi-index columns."""
    if frame.empty:
        return pd.DataFrame()
    if not isinstance(frame.columns, pd.MultiIndex):
        return frame.copy() if len(requested_tickers) == 1 else pd.DataFrame()

    ticker_key = ticker.casefold()
    selected: dict[str, Any] = {}
    for column in frame.columns:
        parts = column if isinstance(column, tuple) else (column,)
        if not any(str(part).casefold() == ticker_key for part in parts):
            continue
        field_name = next(
            (
                str(part).title()
                for part in parts
                if str(part).casefold()
                in {"open", "high", "low", "close", "volume"}
            ),
            None,
        )
        if field_name:
            selected[field_name] = frame[column]
    if not selected:
        return pd.DataFrame()
    return pd.DataFrame(selected, index=frame.index)


def aggregate_minutes_to_hours(minutes: pd.DataFrame) -> pd.DataFrame:
    """Aggregate one-minute OHLCV rows on the backtest's one-hour clock grid."""
    if minutes.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    return (
        minutes.sort_index()
        .resample("1h", label="left", closed="left")
        .agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        .dropna(subset=["Open", "High", "Low", "Close"])
    )


class YahooFinanceProvider:
    """Batch yfinance requests with per-batch failure recovery."""

    def __init__(self, batch_size: int = 40, timeout_seconds: int = 20) -> None:
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds

    def fetch(
        self, tickers: list[str], period: str, interval: str
    ) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}

        for offset in range(0, len(tickers), self.batch_size):
            batch = tickers[offset : offset + self.batch_size]

            try:
                response = yf.download(
                    tickers=" ".join(batch),
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    auto_adjust=False,
                    prepost=False,
                    progress=False,
                    threads=False,
                    timeout=self.timeout_seconds,
                )

                if response is None or response.empty:
                    print(f"[WARN] Empty Yahoo response for batch: {batch}")
                    continue

                for ticker in batch:
                    try:
                        selected = extract_symbol_frame(response, ticker, batch)

                        if selected.empty:
                            print(f"[WARN] No {interval} bars for {ticker}")
                            continue

                        normalized = normalize_yfinance_frame(selected)

                        if normalized.empty:
                            print(f"[WARN] No usable OHLCV rows for {ticker}")
                            continue

                        frames[ticker] = normalized

                    except Exception as exc:
                        print(
                            f"[WARN] {ticker} processing failed: "
                            f"{type(exc).__name__}: {exc}"
                        )

            except Exception as exc:
                print(
                    f"[WARN] Yahoo batch failed ({batch}): "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

        return frames


@dataclass
class TickerMonitor:
    ticker: str
    hourly: pd.DataFrame
    tracker: SignalTracker
    last_bar_time: pd.Timestamp | None = None
    last_weekly_logged: pd.Timestamp | None = None
    latest_price: float | None = None
    quote_time: datetime | None = None
    weekly_table: pd.DataFrame = field(default_factory=pd.DataFrame)
    closed_weekly: pd.DataFrame = field(default_factory=pd.DataFrame)
    zone: dict[str, Any] | None = None
    current_zone: dict[str, Any] | None = None
    zones: list[dict[str, Any]] = field(default_factory=list)
    zone_history_loaded: bool = False
    last_signal_time: str | None = None


class LiveScreenerEngine:
    def __init__(
        self,
        store: DashboardStore,
        journal: CsvJournal,
        provider: YahooFinanceProvider | None = None,
        settings: EngineSettings | None = None,
    ) -> None:
        self.store = store
        self.journal = journal
        self.settings = settings or EngineSettings.from_environment()
        self.provider = provider or YahooFinanceProvider(self.settings.batch_size)
        self.monitors: dict[str, TickerMonitor] = {}
        self._history_retry_after: dict[str, datetime] = {}

    def _metrics(self, monitor: TickerMonitor) -> dict[str, Any]:
        if monitor.closed_weekly.empty:
            return {}
        return monitor.closed_weekly.iloc[-1].to_dict()

    def _zone_from_history(self, monitor: TickerMonitor, now: datetime) -> bool:
        previous_zones = [
            (
                zone["zone_start_week"],
                zone["zone_end_week"],
                zone["consecutive_weeks"],
                zone["zone_high"],
                zone["zone_low"],
                zone["is_current"],
            )
            for zone in monitor.zones
        ]
        monitor.weekly_table = build_weekly_table(monitor.hourly, monitor.ticker)
        monitor.closed_weekly = completed_weekly_rows(monitor.weekly_table, now)
        all_zones = find_consolidation_zones(monitor.closed_weekly, min_weeks=1)
        confirmed_zones = find_consolidation_zones(monitor.closed_weekly)
        monitor.zone = (
            confirmed_zones.iloc[-1].to_dict()
            if not confirmed_zones.empty
            else None
        )

        current_start_week = None
        monitor.current_zone = None
        if (
            not all_zones.empty
            and not monitor.closed_weekly.empty
            and bool(monitor.closed_weekly["IS_CONSOLIDATING"].iloc[-1])
        ):
            latest_week = pd.Timestamp(
                monitor.closed_weekly["week_ending"].iloc[-1]
            )
            candidate = all_zones.iloc[-1].to_dict()
            if pd.Timestamp(candidate["zone_end_week"]) == latest_week:
                monitor.current_zone = candidate
                current_start_week = pd.Timestamp(candidate["zone_start_week"])

        if not all_zones.empty:
            all_zones["is_current"] = all_zones["zone_start_week"].eq(
                current_start_week
            )
        monitor.zones = all_zones.to_dict("records")
        current_zones = [
            (
                zone["zone_start_week"],
                zone["zone_end_week"],
                zone["consecutive_weeks"],
                zone["zone_high"],
                zone["zone_low"],
                bool(zone.get("is_current", False)),
            )
            for zone in monitor.zones
        ]
        changed = current_zones != previous_zones
        if changed or not monitor.zone_history_loaded:
            self.store.replace_zones(monitor.ticker, monitor.zones)
            monitor.zone_history_loaded = True
        if monitor.current_zone is not None:
            monitor.current_zone["is_current"] = True
        return changed

    def _print_ticker_report(self, monitor: TickerMonitor) -> None:
        current = monitor.current_zone
        if current:
            metrics = self._metrics(monitor)
            distance = distance_to_break_percent(
                monitor.latest_price,
                float(current["zone_high"]),
                float(current["zone_low"]),
            )
            score = calculate_setup_score(
                int(current["consecutive_weeks"]),
                distance,
                metrics.get("atr"),
                metrics.get("atr_avg"),
                metrics.get("weekly_rv"),
                metrics.get("rv_avg"),
                self.settings.proximity_pct,
            )
            phase = (
                "developing"
                if int(current["consecutive_weeks"]) == 1
                else "confirmed"
            )
            print(
                f"[REPORT] {monitor.ticker} | {int(current['consecutive_weeks'])} "
                f"consolidating week(s), {phase} | Zone: "
                f"{float(current['zone_low']):.4f}-"
                f"{float(current['zone_high']):.4f} | Signal: "
                f"{monitor.tracker.state} | Setup score: {score}/100 "
                "(heuristic, not probability)."
            )
        else:
            print(
                f"[REPORT] {monitor.ticker} | No current 1-week consolidation | "
                f"Signal: {monitor.tracker.state}."
            )

        if not monitor.zones:
            print(f"[ZONE] {monitor.ticker} | No completed consolidation zones.")
            return
        for zone in monitor.zones:
            status = "CURRENT" if zone.get("is_current") else "HISTORICAL"
            print(
                f"[ZONE] {status} {monitor.ticker} | "
                f"{pd.Timestamp(zone['zone_start_week']).date()} to "
                f"{pd.Timestamp(zone['zone_end_week']).date()} | "
                f"{int(zone['consecutive_weeks'])} week(s) | "
                f"{float(zone['zone_low']):.4f}-"
                f"{float(zone['zone_high']):.4f}"
            )

    def _add_tickers(self, tickers: list[str], now: datetime) -> None:
        added = [
            ticker
            for ticker in tickers
            if ticker not in self.monitors
            and now >= self._history_retry_after.get(ticker, now)
        ]
        if not added:
            return
        history = self.provider.fetch(
            added, period=self.settings.history_period, interval="1h"
        )
        current_hour = pd.Timestamp(now.replace(tzinfo=None)).floor("1h")
        for ticker in added:
            hourly = history.get(
                ticker,
                pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]),
            )
            if not hourly.empty:
                hourly = aggregate_minutes_to_hours(hourly)
                hourly = hourly.loc[hourly.index < current_hour]
            if hourly.empty:
                self._history_retry_after[ticker] = now + timedelta(minutes=5)
                print(
                    f"[WARN] Skipping {ticker}: no historical hourly bars for "
                    "indicator warm-up; retrying in five minutes."
                )
                continue
            self._history_retry_after.pop(ticker, None)
            tracker = SignalTracker(
                ticker=ticker,
                proximity_pct=self.settings.proximity_pct,
                hold_bars=self.settings.hold_bars,
                cooldown_minutes=self.settings.cooldown_minutes,
            )
            monitor = TickerMonitor(ticker=ticker, hourly=hourly, tracker=tracker)
            persisted_snapshot = self.store.get_snapshot(ticker)
            if persisted_snapshot:
                monitor.last_signal_time = persisted_snapshot.get("last_signal_time")
            if not hourly.empty:
                monitor.latest_price = float(hourly["Close"].iloc[-1])
            monitor.last_bar_time = max(
                pd.Timestamp(hourly.index[-1]) if not hourly.empty else current_hour,
                current_hour - pd.Timedelta(hours=1),
            )
            self._zone_from_history(monitor, now)
            tracker.seed(
                monitor.latest_price,
                monitor.zone,
                now=now.replace(tzinfo=None),
            )
            if not monitor.closed_weekly.empty:
                monitor.last_weekly_logged = pd.Timestamp(
                    monitor.closed_weekly["week_ending"].iloc[-1]
                )
            self.monitors[ticker] = monitor
            print(
                f"[ENGINE] Initialized {ticker} with {len(hourly)} hourly bars; "
                f"confirmed zone: {'yes' if monitor.zone else 'no'}."
            )
            self._print_ticker_report(monitor)

    def _remove_tickers(self, tickers: list[str]) -> None:
        for ticker in list(self.monitors):
            if ticker not in tickers:
                del self.monitors[ticker]
                print(f"[ENGINE] Unsubscribed {ticker}.")

    def _snapshot_row(
        self,
        monitor: TickerMonitor,
        record_type: str,
        timestamp: datetime,
        signal: str = "",
        bar: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metrics = self._metrics(monitor)
        zone = monitor.zone or {}
        upper = zone.get("zone_high")
        lower = zone.get("zone_low")
        state = monitor.tracker.state
        price = monitor.latest_price
        row: dict[str, Any] = {
            "record_type": record_type,
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "ticker": monitor.ticker,
            "signal": signal or state,
            "price": price,
            "bar_timeframe": "",
            "bar_timestamp": "",
            "bar_open": None,
            "bar_high": None,
            "bar_low": None,
            "bar_close": None,
            "bar_volume": None,
            "week_ending": metrics.get("week_ending"),
            "week_open": None,
            "week_high": metrics.get("week_high"),
            "week_low": metrics.get("week_low"),
            "week_close": metrics.get("close"),
            "week_volume": None,
            "atr": metrics.get("atr"),
            "atr_avg": metrics.get("atr_avg"),
            "atr_in_range": metrics.get("atr_in_range"),
            "weekly_rv": metrics.get("weekly_rv"),
            "rv_avg": metrics.get("rv_avg"),
            "rv_below_avg": metrics.get("rv_below_avg"),
            "prev_week_high": metrics.get("prev_week_high"),
            "prev_week_low": metrics.get("prev_week_low"),
            "is_consolidating": metrics.get("IS_CONSOLIDATING"),
            "zone_start_week": zone.get("zone_start_week"),
            "zone_end_week": zone.get("zone_end_week"),
            "consecutive_weeks": zone.get("consecutive_weeks"),
            "zone_high": upper,
            "zone_low": lower,
            "distance_to_break_pct": (
                min(abs(float(upper) - price), abs(price - float(lower))) / price * 100
                if price and upper is not None and lower is not None
                else None
            ),
            "zone_width_pct": zone_width_percent(upper, lower),
            "chop_bars": monitor.tracker.chop_bars,
            "chop_minutes": monitor.tracker.chop_minutes(timestamp),
        }
        if bar:
            row.update(
                {
                    "bar_timeframe": "1h",
                    "bar_timestamp": bar["timestamp"],
                    "bar_open": bar["open"],
                    "bar_high": bar["high"],
                    "bar_low": bar["low"],
                    "bar_close": bar["close"],
                    "bar_volume": bar["volume"],
                }
            )
        return row

    def _publish_signal(
        self,
        monitor: TickerMonitor,
        event: Any,
        bar: dict[str, Any] | None,
    ) -> None:
        timestamp = event.timestamp
        metrics = self._metrics(monitor)
        consecutive_weeks = int(monitor.zone.get("consecutive_weeks", 0))
        setup_score = calculate_setup_score(
            consecutive_weeks,
            event.distance_to_break_pct,
            metrics.get("atr"),
            metrics.get("atr_avg"),
            metrics.get("weekly_rv"),
            metrics.get("rv_avg"),
            self.settings.proximity_pct,
        )
        print(
            f"[SIGNAL] [{timestamp.strftime('%H:%M:%S')}] {monitor.ticker} | "
            f"{event.state.upper()} | Price: {event.price:.4f} | "
            f"Zone: {event.zone_low:.4f}-{event.zone_high:.4f} | "
            f"Consolidation: {consecutive_weeks} week(s) | "
            f"Setup score: {setup_score}/100 (heuristic, not probability)"
        )
        payload = {
            "ticker": monitor.ticker,
            "state": event.state,
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "price": event.price,
            "zone_high": event.zone_high,
            "zone_low": event.zone_low,
            "distance_to_break_pct": event.distance_to_break_pct,
            "zone_width_pct": event.zone_width_pct,
            "consecutive_weeks": consecutive_weeks,
            "zone_start_week": monitor.zone.get("zone_start_week"),
            "zone_end_week": monitor.zone.get("zone_end_week"),
            "setup_score": setup_score,
        }
        row = self._snapshot_row(
            monitor,
            record_type="signal",
            timestamp=timestamp,
            signal=event.state,
            bar=bar,
        )
        self.journal.append(row)
        self.store.record_signal(payload)
        monitor.last_signal_time = timestamp.isoformat(timespec="seconds")

    def _publish_hourly_bar(
        self, monitor: TickerMonitor, bar: dict[str, Any], timestamp: datetime
    ) -> None:
        row = self._snapshot_row(
            monitor,
            record_type="hourly_snapshot",
            timestamp=timestamp,
            bar=bar,
        )
        self.journal.append(row)

    def _publish_weekly_bar(
        self, monitor: TickerMonitor, week: dict[str, Any], timestamp: datetime
    ) -> None:
        row = self._snapshot_row(
            monitor,
            record_type="weekly_snapshot",
            timestamp=timestamp,
        )
        row.update(
            {
                "bar_timeframe": "1w",
                "week_ending": week.get("week_ending"),
                "week_high": week.get("week_high"),
                "week_low": week.get("week_low"),
                "week_close": week.get("close"),
                "atr": week.get("atr"),
                "atr_avg": week.get("atr_avg"),
                "atr_in_range": week.get("atr_in_range"),
                "weekly_rv": week.get("weekly_rv"),
                "rv_avg": week.get("rv_avg"),
                "rv_below_avg": week.get("rv_below_avg"),
                "prev_week_high": week.get("prev_week_high"),
                "prev_week_low": week.get("prev_week_low"),
                "is_consolidating": week.get("IS_CONSOLIDATING"),
            }
        )
        self.journal.append(row)

    def _update_ticker(
        self, monitor: TickerMonitor, minute_data: pd.DataFrame, now: datetime
    ) -> None:
        if minute_data.empty:
            return

        session_date = (
            now.astimezone(EASTERN).date() if now.tzinfo else now.date()
        )
        minutes = minute_data.loc[
            minute_data.index.date == session_date
        ]
        minutes = minutes.loc[
            minutes.index + pd.Timedelta(minutes=1)
            <= pd.Timestamp(now.replace(tzinfo=None))
        ]
        if minutes.empty:
            return
        monitor.latest_price = float(minutes["Close"].iloc[-1])
        monitor.quote_time = minutes.index[-1].to_pydatetime()

        current_hour = pd.Timestamp(now.replace(tzinfo=None)).floor("1h")
        completed_hours = aggregate_minutes_to_hours(minutes)
        completed_hours = completed_hours.loc[completed_hours.index < current_hour]
        new_bars = (
            completed_hours.loc[completed_hours.index > monitor.last_bar_time]
            if monitor.last_bar_time is not None
            else completed_hours
        )

        if not new_bars.empty:
            monitor.hourly = pd.concat([monitor.hourly, new_bars])
            monitor.hourly = monitor.hourly[
                ~monitor.hourly.index.duplicated(keep="last")
            ].sort_index()
            monitor.last_bar_time = pd.Timestamp(new_bars.index[-1])

        zones_changed = self._zone_from_history(monitor, now)
        for bar_time, bar in new_bars.iterrows():
            bar_payload = {
                "timestamp": pd.Timestamp(bar_time).isoformat(),
                "open": float(bar["Open"]),
                "high": float(bar["High"]),
                "low": float(bar["Low"]),
                "close": float(bar["Close"]),
                "volume": int(bar["Volume"]) if pd.notna(bar["Volume"]) else 0,
            }
            close_time = pd.Timestamp(bar_time) + pd.Timedelta(hours=1)
            close_dt = close_time.to_pydatetime()
            event = monitor.tracker.update(
                float(bar["Close"]),
                monitor.zone,
                now=close_dt,
                completed_bar={"timestamp": bar_time, "close": float(bar["Close"])},
            )
            self._publish_hourly_bar(monitor, bar_payload, close_dt)
            if event:
                self._publish_signal(monitor, event, bar_payload)

        event = monitor.tracker.update(
            monitor.latest_price,
            monitor.zone,
            now=now.replace(tzinfo=None),
        )
        if event:
            self._publish_signal(monitor, event, None)

        if not monitor.closed_weekly.empty:
            latest_week = monitor.closed_weekly.iloc[-1].to_dict()
            week_ending = pd.Timestamp(latest_week["week_ending"])
            if (
                monitor.last_weekly_logged is None
                or week_ending > monitor.last_weekly_logged
            ):
                self._publish_weekly_bar(monitor, latest_week, now.replace(tzinfo=None))
                monitor.last_weekly_logged = week_ending
        if zones_changed:
            self._print_ticker_report(monitor)

        self.store.upsert_snapshot(self._dashboard_snapshot(monitor, now))

    def _dashboard_snapshot(
        self, monitor: TickerMonitor, now: datetime
    ) -> dict[str, Any]:
        metrics = self._metrics(monitor)
        zone = monitor.zone or {}
        upper = zone.get("zone_high")
        lower = zone.get("zone_low")
        price = monitor.latest_price
        distance = (
            min(abs(float(upper) - price), abs(price - float(lower))) / price * 100
            if price and upper is not None and lower is not None
            else None
        )
        return {
            "ticker": monitor.ticker,
            "price": price,
            "state": monitor.tracker.state or AWAITING_ZONE,
            "zone_high": upper,
            "zone_low": lower,
            "distance_to_break_pct": distance,
            "zone_width_pct": zone_width_percent(upper, lower),
            "last_signal_time": monitor.last_signal_time,
            "quote_time": (
                monitor.quote_time.isoformat(timespec="seconds")
                if monitor.quote_time
                else None
            ),
            "updated_at": now.isoformat(timespec="seconds"),
            "zone_start_week": zone.get("zone_start_week"),
            "zone_end_week": zone.get("zone_end_week"),
            "consecutive_weeks": zone.get("consecutive_weeks"),
            "atr": metrics.get("atr"),
            "atr_avg": metrics.get("atr_avg"),
            "weekly_rv": metrics.get("weekly_rv"),
            "rv_avg": metrics.get("rv_avg"),
            "chop_bars": monitor.tracker.chop_bars,
            "chop_minutes": monitor.tracker.chop_minutes(now.replace(tzinfo=None)),
        }

    def poll_once(self, now: datetime | None = None) -> None:
        local = now or local_now()
        watchlist = self.store.get_watchlist()
        if not watchlist:
            self.store.set_watchlist(DEFAULT_WATCHLIST)
            watchlist = self.store.get_watchlist()
        self._remove_tickers(watchlist)
        self._add_tickers(watchlist, local)

        if not is_market_polling_window(local):
            self.store.set_status("state", "market_closed")
            waiting = [ticker for ticker in watchlist if ticker not in self.monitors]
            message = "Outside the US equity polling window; waiting for the next cycle."
            if waiting:
                message += " Historical warm-up is pending for: " + ", ".join(waiting)
            self.store.set_status("message", message)
            return

        self.store.set_status("state", "polling")
        frames = self.provider.fetch(watchlist, period="1d", interval="1m")
        for ticker in watchlist:
            if ticker not in self.monitors:
                continue
            monitor = self.monitors[ticker]
            frame = frames.get(ticker)
            if frame is None:
                continue
            self._update_ticker(monitor, frame, local)
        waiting = [ticker for ticker in watchlist if ticker not in self.monitors]
        self.store.set_status("state", "degraded" if waiting else "running")
        self.store.set_status("last_poll", local.isoformat(timespec="seconds"))
        message = (
            f"Yahoo Finance polling active; bars returned for "
            f"{len(frames)} of {len(watchlist)} tickers."
        )
        if waiting:
            message += " Historical warm-up pending for: " + ", ".join(waiting)
        self.store.set_status("message", message)

    def run_forever(self) -> None:
        self.store.set_watchlist(
            self.store.get_watchlist() or DEFAULT_WATCHLIST
        )
        print(
            "[ENGINE] Starting Yahoo Finance polling engine "
            f"(interval={self.settings.poll_seconds}s, "
            f"near={self.settings.proximity_pct:.2f}%, "
            f"hold={self.settings.hold_bars} hourly closes)."
        )
        print(
            "[ENGINE] Yahoo Finance is not a streaming feed; quotes may be delayed "
            "or unavailable."
        )
        self.store.set_status("state", "starting")
        self.store.set_status("message", "Initializing historical hourly data.")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                self.store.set_status("state", "stopped")
                print("\n[ENGINE] Stopped.")
                return
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.store.set_status("state", "error")
                self.store.set_status("message", message)
                print(f"[ERROR] Polling failed: {message}")
            time_module.sleep(self.settings.poll_seconds)


def main() -> None:
    data_dir = Path(os.getenv("SCREENER_DATA_DIR", str(DEFAULT_DATA_DIR)))
    store = DashboardStore(data_dir / "live_screener.sqlite3")
    journal = CsvJournal(data_dir / "live_weekly.csv")
    engine = LiveScreenerEngine(store, journal)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        store.set_status("state", "stopped")
        print("\n[ENGINE] Stopped.")


if __name__ == "__main__":
    main()
