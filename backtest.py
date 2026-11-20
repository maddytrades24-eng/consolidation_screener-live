import numpy as np
import pandas as pd

# ----------------------------- CONFIG ---------------------------------
# ----------------------------- CONFIG ---------------------------------

import os
import glob

DATA_FOLDER = r"C:\Users\Admin\Downloads\ticker_exports-20260729T112336Z-1-001\ticker_exports"

FILES = glob.glob(os.path.join(DATA_FOLDER, "*.csv"))

CANDLE_INTERVAL = "1h"
LOOKBACK_PERIOD = "1y"
ATR_PERIOD = 14
ATR_AVG_PERIOD = 20

RV_WEEKS_WINDOW = 4
RV_AVG_WEEKS = 10
WEEKS_PER_YEAR = 52

SHOW_ONLY_CONSOLIDATING = True
MIN_CONSECUTIVE_WEEKS = 2
# ------------------------------------------------------------------------


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)

    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def compute_weekly_rv(df: pd.DataFrame, window_weeks: int = RV_WEEKS_WINDOW) -> pd.Series:
    close = df["Close"]
    candle_log_returns = np.log(close / close.shift(1))

    weekly_returns = candle_log_returns.resample("W-FRI").sum()
    weekly_rv = weekly_returns.rolling(window_weeks).std() * np.sqrt(WEEKS_PER_YEAR)
    return weekly_rv


def build_weekly_table(file_path: str) -> pd.DataFrame | None:

    data = pd.read_csv(file_path)

    ticker = os.path.splitext(os.path.basename(file_path))[0]
    data.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume"
    }, inplace=True)

    data["datetime"] = pd.to_datetime(data["datetime"])
    data.set_index("datetime", inplace=True)

    hourly = data.resample("1h").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum"
    })

    hourly.dropna(inplace=True)

    if hourly.empty or len(hourly) < ATR_AVG_PERIOD + ATR_PERIOD:
        return None

    atr = compute_atr(hourly)
    atr_avg = atr.rolling(ATR_AVG_PERIOD).mean()

    weekly_atr = atr.resample("W-FRI").last()
    weekly_atr_avg = atr_avg.resample("W-FRI").last()

    weekly_rv = compute_weekly_rv(hourly)
    weekly_rv_avg = weekly_rv.rolling(RV_AVG_WEEKS).mean()

    weekly_hl = hourly.resample("W-FRI").agg({
        "High": "max",
        "Low": "min",
        "Close": "last"
    })

    table = pd.DataFrame({
        "week_ending": weekly_hl.index,
        "close": weekly_hl["Close"],
        "week_high": weekly_hl["High"],
        "week_low": weekly_hl["Low"],
        "atr": weekly_atr,
        "atr_avg": weekly_atr_avg,
        "weekly_rv": weekly_rv,
        "rv_avg": weekly_rv_avg,
    }).reset_index(drop=True)

    table["prev_week_high"] = table["week_high"].shift(1)
    table["prev_week_low"] = table["week_low"].shift(1)

    table["atr_in_range"] = table["atr"] < table["atr_avg"]

    table["rv_below_avg"] = table["weekly_rv"] < table["rv_avg"]

    table["inside_prev_week_range"] = (
        (table["close"] >= table["prev_week_low"]) &
        (table["close"] <= table["prev_week_high"])
    )

    table["IS_CONSOLIDATING"] = (
        table["atr_in_range"] &
        table["rv_below_avg"] &
        table["inside_prev_week_range"]
    )

    table = table.dropna(subset=[
        "atr",
        "atr_avg",
        "weekly_rv",
        "rv_avg",
        "prev_week_high"
    ])

    table.insert(0, "ticker", ticker)

    return table.round(4)


def find_consolidation_zones(
    weekly_table: pd.DataFrame,
    min_weeks: int = MIN_CONSECUTIVE_WEEKS
) -> pd.DataFrame:

    df = weekly_table.reset_index(drop=True).copy()

    df["streak_group"] = (~df["IS_CONSOLIDATING"]).cumsum()

    consolidating_weeks = df[df["IS_CONSOLIDATING"]]

    zones = []

    for _, group in consolidating_weeks.groupby("streak_group"):
        num_weeks = len(group)

        if num_weeks < min_weeks:
            continue

        zones.append({
            "ticker": group["ticker"].iloc[0],
            "zone_start_week": group["week_ending"].iloc[0],
            "zone_end_week": group["week_ending"].iloc[-1],
            "consecutive_weeks": num_weeks,
            "zone_high": group["week_high"].max(),
            "zone_low": group["week_low"].min(),
            "last_close": group["close"].iloc[-1],
            "breakout_level_up": group["week_high"].max(),
            "breakout_level_down": group["week_low"].min(),
        })

    return pd.DataFrame(zones)


def run_screen():

    all_tables = []

    total = len(FILES)

    for i, file_path in enumerate(FILES, start=1):

        ticker = os.path.splitext(os.path.basename(file_path))[0]

        print(f"Processing {i}/{total} : {ticker}")

        try:

            weekly_table = build_weekly_table(file_path)

            if weekly_table is not None and not weekly_table.empty:

                all_tables.append(weekly_table)

        except Exception as e:

            print(f"Skipping {ticker}: {e}")

    if not all_tables:
        return pd.DataFrame()

    return pd.concat(all_tables, ignore_index=True)

if __name__ == "__main__":
    df = run_screen()
    if df.empty:
        print("No results (check tickers / data availability).")

    else:
        display_df = df[df["IS_CONSOLIDATING"]] if SHOW_ONLY_CONSOLIDATING else df

        cols = [
            "ticker",
            "week_ending",
            "close",
            "prev_week_low",
            "prev_week_high",
            "inside_prev_week_range",
            "atr",
            "atr_avg",
            "atr_in_range",
            "weekly_rv",
            "rv_avg",
            "rv_below_avg",
            "IS_CONSOLIDATING",
        ]

        print("=== Weekly detail ===")
        print(display_df[cols].to_string(index=False))

        print(
            f"\n{df['IS_CONSOLIDATING'].sum()} consolidating weeks found "
            f"out of {len(df)} total weeks checked."
        )

        all_zones = []

        for ticker in df["ticker"].unique():
            ticker_table = df[df["ticker"] == ticker]
            zones = find_consolidation_zones(ticker_table)

            if not zones.empty:
                all_zones.append(zones)

        print(f"\n=== Consolidation Zones (>= {MIN_CONSECUTIVE_WEEKS} back-to-back weeks) ===")

        if all_zones:
            zones_df = pd.concat(all_zones, ignore_index=True)

            print(zones_df.round(2).to_string(index=False))
            summary = []

for ticker in df["ticker"].unique():

    ticker_table = df[df["ticker"] == ticker]

    zones = find_consolidation_zones(ticker_table)

    last = ticker_table.iloc[-1]

    if not zones.empty:

        latest = zones.iloc[-1]

        zone_high = latest["zone_high"]
        zone_low = latest["zone_low"]
        weeks = latest["consecutive_weeks"]

    else:

        zone_high = np.nan
        zone_low = np.nan
        weeks = 0

    summary.append({

        "Stock Name": ticker,

        "No. of Consolidation Weeks": weeks,

        "Still in Consolidation": last["IS_CONSOLIDATING"],

        "Consolidation Highest High": zone_high,

        "Consolidation Lowest Low": zone_low,

        "Current ATR": last["atr"],

        "ATR Average": last["atr_avg"],

        "Current RV": last["weekly_rv"],

        "RV Average": last["rv_avg"]

    })

summary_df = pd.DataFrame(summary)

summary_df.to_excel("Consolidation_Summary.xlsx", index=False)

print("\nExcel saved as Consolidation_Summary.xlsx")

            