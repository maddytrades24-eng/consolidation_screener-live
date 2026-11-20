"""Consolidation indicators and signal-state logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ATR_PERIOD = 14
ATR_AVG_PERIOD = 20
RV_WEEKS_WINDOW = 4
RV_AVG_WEEKS = 10
WEEKS_PER_YEAR = 52
MIN_CONSECUTIVE_WEEKS = 2
EASTERN = ZoneInfo("America/New_York")

AWAITING_ZONE = "Awaiting Confirmed Zone"
BULLISH_BREAKOUT = "Bullish Breakout"
BEARISH_BREAKDOWN = "Bearish Breakdown"
NEAR_BULLISH = "Near Bullish Break"
NEAR_BEARISH = "Near Bearish Break"
CHOP = "Chop"


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Match the backtest's Wilder-style exponential ATR calculation."""
    high, low, close = df["High"], df["Low"], df["Close"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def compute_weekly_rv(
    df: pd.DataFrame, window_weeks: int = RV_WEEKS_WINDOW
) -> pd.Series:
    """Match the backtest's weekly summed log-return volatility calculation."""
    log_returns = np.log(df["Close"] / df["Close"].shift(1))
    weekly_returns = log_returns.resample("W-FRI").sum()
    return weekly_returns.rolling(window_weeks).std() * np.sqrt(WEEKS_PER_YEAR)


def build_weekly_table(
    hourly_data: pd.DataFrame,
    ticker: str,
    atr_period: int = ATR_PERIOD,
    atr_avg_period: int = ATR_AVG_PERIOD,
    rv_weeks_window: int = RV_WEEKS_WINDOW,
    rv_avg_weeks: int = RV_AVG_WEEKS,
) -> pd.DataFrame:
    """Build the same weekly consolidation table as the supplied backtest."""
    if hourly_data.empty:
        return pd.DataFrame()

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required.difference(hourly_data.columns)
    if missing:
        raise ValueError(f"Hourly data is missing columns: {sorted(missing)}")

    hourly = hourly_data.sort_index().resample("1h").agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    hourly = hourly.dropna(subset=["Open", "High", "Low", "Close"])
    if hourly.empty:
        return pd.DataFrame()

    atr = compute_atr(hourly, period=atr_period)
    atr_avg = atr.rolling(atr_avg_period).mean()
    weekly_atr = atr.resample("W-FRI").last()
    weekly_atr_avg = atr_avg.resample("W-FRI").last()
    weekly_rv = compute_weekly_rv(hourly, window_weeks=rv_weeks_window)
    weekly_rv_avg = weekly_rv.rolling(rv_avg_weeks).mean()
    weekly_high_low = hourly.resample("W-FRI").agg(
        {"High": "max", "Low": "min", "Close": "last"}
    )

    table = pd.DataFrame(
        {
            "week_ending": weekly_high_low.index,
            "close": weekly_high_low["Close"],
            "week_high": weekly_high_low["High"],
            "week_low": weekly_high_low["Low"],
            "atr": weekly_atr,
            "atr_avg": weekly_atr_avg,
            "weekly_rv": weekly_rv,
            "rv_avg": weekly_rv_avg,
        }
    ).reset_index(drop=True)

    table["prev_week_high"] = table["week_high"].shift(1)
    table["prev_week_low"] = table["week_low"].shift(1)
    table["atr_in_range"] = table["atr"] < table["atr_avg"]
    table["rv_below_avg"] = table["weekly_rv"] < table["rv_avg"]
    table["inside_prev_week_range"] = (
        (table["close"] >= table["prev_week_low"])
        & (table["close"] <= table["prev_week_high"])
    )
    table["IS_CONSOLIDATING"] = (
        table["atr_in_range"]
        & table["rv_below_avg"]
        & table["inside_prev_week_range"]
    )
    table = table.dropna(
        subset=["atr", "atr_avg", "weekly_rv", "rv_avg", "prev_week_high"],
    )
    table.insert(0, "ticker", ticker)
    numeric_columns = table.select_dtypes(include="number").columns
    table[numeric_columns] = table[numeric_columns].round(4)
    return table.reset_index(drop=True)


def find_consolidation_zones(
    weekly_table: pd.DataFrame,
    min_weeks: int = MIN_CONSECUTIVE_WEEKS,
) -> pd.DataFrame:
    """Return confirmed consecutive consolidation runs using the backtest math."""
    if weekly_table.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "zone_start_week",
                "zone_end_week",
                "consecutive_weeks",
                "zone_high",
                "zone_low",
                "last_close",
                "breakout_level_up",
                "breakout_level_down",
            ]
        )

    df = weekly_table.reset_index(drop=True).copy()
    df["streak_group"] = (~df["IS_CONSOLIDATING"]).cumsum()
    zones: list[dict[str, Any]] = []
    for _, group in df[df["IS_CONSOLIDATING"]].groupby("streak_group"):
        if len(group) < min_weeks:
            continue
        high = float(group["week_high"].max())
        low = float(group["week_low"].min())
        zones.append(
            {
                "ticker": group["ticker"].iloc[0],
                "zone_start_week": group["week_ending"].iloc[0],
                "zone_end_week": group["week_ending"].iloc[-1],
                "consecutive_weeks": int(len(group)),
                "zone_high": high,
                "zone_low": low,
                "last_close": float(group["close"].iloc[-1]),
                "breakout_level_up": high,
                "breakout_level_down": low,
            }
        )
    return pd.DataFrame(zones)


def consolidation_week_dates(
    first_week_ending: date | datetime | str | pd.Timestamp,
    last_week_ending: date | datetime | str | pd.Timestamp,
) -> tuple[date, date]:
    """Convert Friday-labeled resample weeks to their Monday-Friday trading span."""
    first_friday = pd.Timestamp(first_week_ending).date()
    last_friday = pd.Timestamp(last_week_ending).date()
    return first_friday - timedelta(days=4), last_friday


def last_completed_week_ending(now: datetime) -> date:
    """Return the latest Friday whose regular session has ended in New York."""
    local_now = now.replace(tzinfo=EASTERN) if now.tzinfo is None else now.astimezone(EASTERN)
    today = local_now.date()
    weekday = today.weekday()
    if weekday < 4:
        days_back = weekday + 3
    elif weekday == 4:
        days_back = 0 if local_now.time() >= time(16, 0) else 7
    else:
        days_back = weekday - 4
    return today - timedelta(days=days_back)


def completed_weekly_rows(weekly_table: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Exclude the still-forming week so zone boundaries cannot repaint intrawweek."""
    if weekly_table.empty:
        return weekly_table.copy()
    last_friday = last_completed_week_ending(now)
    labels = pd.to_datetime(weekly_table["week_ending"]).dt.date
    return weekly_table.loc[labels <= last_friday].copy().reset_index(drop=True)


def zone_width_percent(upper: float | None, lower: float | None) -> float | None:
    if upper is None or lower is None or upper <= 0 or lower <= 0:
        return None
    midpoint = (upper + lower) / 2
    return (upper - lower) / midpoint * 100 if midpoint else None


def distance_to_break_percent(
    price: float | None, upper: float | None, lower: float | None
) -> float | None:
    if price is None or price <= 0 or upper is None or lower is None:
        return None
    return min(abs(upper - price), abs(price - lower)) / price * 100


def calculate_setup_score(
    consecutive_weeks: int,
    distance_pct: float | None,
    atr: float | None,
    atr_avg: float | None,
    weekly_rv: float | None,
    rv_avg: float | None,
    proximity_pct: float = 0.3,
) -> int:
    """Return a transparent heuristic score, not a breakout probability."""
    if consecutive_weeks < 1:
        return 0

    score = min(40, consecutive_weeks * 10)
    if atr is not None and atr_avg is not None and atr < atr_avg:
        score += 15
    if weekly_rv is not None and rv_avg is not None and weekly_rv < rv_avg:
        score += 15
    if distance_pct is not None and proximity_pct > 0:
        score += round(30 * max(0.0, 1.0 - distance_pct / proximity_pct))
    return min(100, score)


def classify_price_state(
    price: float,
    upper: float,
    lower: float,
    proximity_pct: float = 0.3,
) -> str:
    """Classify a quote without treating an unconfirmed cross as a breakout."""
    if price <= 0 or upper <= lower:
        return AWAITING_ZONE
    upper_distance = abs(upper - price) / price * 100
    lower_distance = abs(price - lower) / price * 100
    if price > upper:
        return NEAR_BULLISH if upper_distance <= proximity_pct else AWAITING_ZONE
    if price < lower:
        return NEAR_BEARISH if lower_distance <= proximity_pct else AWAITING_ZONE
    if upper_distance <= proximity_pct:
        return NEAR_BULLISH
    if lower_distance <= proximity_pct:
        return NEAR_BEARISH
    return CHOP


@dataclass(frozen=True)
class SignalEvent:
    state: str
    timestamp: datetime
    price: float
    zone_high: float
    zone_low: float
    distance_to_break_pct: float | None
    zone_width_pct: float | None


class SignalTracker:
    """Track state transitions, held hourly closes, chop duration, and cooldown."""

    def __init__(
        self,
        ticker: str,
        proximity_pct: float = 0.3,
        hold_bars: int = 2,
        cooldown_minutes: int = 30,
    ) -> None:
        if hold_bars < 1:
            raise ValueError("hold_bars must be at least one")
        self.ticker = ticker
        self.proximity_pct = proximity_pct
        self.hold_bars = hold_bars
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.state = AWAITING_ZONE
        self.pending_direction: str | None = None
        self.pending_bars = 0
        self.last_bar_time: pd.Timestamp | None = None
        self.last_signal_state: str | None = None
        self.last_signal_time: datetime | None = None
        self._last_signal_by_state: dict[str, datetime] = {}
        self.chop_bars = 0
        self.chop_started_at: datetime | None = None
        self._initialized = False

    def seed(
        self,
        price: float | None,
        zone: dict[str, Any] | None,
        now: datetime | None = None,
    ) -> None:
        """Set a quiet initial state from historical data without emitting alerts."""
        if price is not None and zone:
            self.state = classify_price_state(
                price,
                float(zone["zone_high"]),
                float(zone["zone_low"]),
                self.proximity_pct,
            )
        else:
            self.state = AWAITING_ZONE
        if self.state == CHOP:
            self.chop_started_at = now
        self._initialized = True

    def update(
        self,
        price: float,
        zone: dict[str, Any] | None,
        now: datetime,
        completed_bar: dict[str, Any] | None = None,
    ) -> SignalEvent | None:
        if zone is None:
            self.state = AWAITING_ZONE
            self.pending_direction = None
            self.pending_bars = 0
            self.chop_bars = 0
            self.chop_started_at = None
            self._initialized = True
            return None

        upper = float(zone["zone_high"])
        lower = float(zone["zone_low"])
        previous_state = self.state
        new_bar = False
        bar_close = price
        if completed_bar is not None:
            bar_time = pd.Timestamp(completed_bar["timestamp"])
            if self.last_bar_time is None or bar_time > self.last_bar_time:
                new_bar = True
                self.last_bar_time = bar_time
                bar_close = float(completed_bar["close"])

        breakout_state: str | None = None
        if new_bar:
            if bar_close > upper:
                direction = BULLISH_BREAKOUT
                self.pending_bars = (
                    self.pending_bars + 1 if self.pending_direction == direction else 1
                )
                self.pending_direction = direction
                if self.pending_bars >= self.hold_bars:
                    breakout_state = direction
            elif bar_close < lower:
                direction = BEARISH_BREAKDOWN
                self.pending_bars = (
                    self.pending_bars + 1 if self.pending_direction == direction else 1
                )
                self.pending_direction = direction
                if self.pending_bars >= self.hold_bars:
                    breakout_state = direction
            else:
                self.pending_direction = None
                self.pending_bars = 0

        if breakout_state is not None:
            next_state = breakout_state
        elif previous_state in {BULLISH_BREAKOUT, BEARISH_BREAKDOWN} and not (
            new_bar
            and (
                lower <= bar_close <= upper
                or (
                    previous_state == BULLISH_BREAKOUT
                    and bar_close < lower
                )
                or (
                    previous_state == BEARISH_BREAKDOWN
                    and bar_close > upper
                )
            )
        ):
            next_state = previous_state
        else:
            next_state = classify_price_state(
                price, upper, lower, self.proximity_pct
            )

        if next_state == CHOP:
            if previous_state != CHOP:
                self.chop_bars = 0
                self.chop_started_at = now
            if new_bar and lower <= bar_close <= upper:
                self.chop_bars += 1
        else:
            self.chop_bars = 0
            self.chop_started_at = None

        self.state = next_state
        if not self._initialized:
            self._initialized = True
            return None
        if next_state == previous_state or next_state == AWAITING_ZONE:
            return None
        state_last_signal = self._last_signal_by_state.get(next_state)
        if state_last_signal is not None and now - state_last_signal < self.cooldown:
            return None

        self.last_signal_state = next_state
        self.last_signal_time = now
        self._last_signal_by_state[next_state] = now
        return SignalEvent(
            state=next_state,
            timestamp=now,
            price=float(price),
            zone_high=upper,
            zone_low=lower,
            distance_to_break_pct=distance_to_break_percent(price, upper, lower),
            zone_width_pct=zone_width_percent(upper, lower),
        )

    def chop_minutes(self, now: datetime) -> int:
        if self.chop_started_at is None:
            return 0
        return max(0, int((now - self.chop_started_at).total_seconds() // 60))
