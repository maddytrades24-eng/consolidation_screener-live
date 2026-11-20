import os
import glob
import numpy as np
import pandas as pd
from tqdm import tqdm

# ----------------------------- CONFIG ---------------------------------
# Folder where all 700+ Excel files live (one file per stock)
DATA_FOLDER = r"C:\Users\Admin\Desktop\pp\ticker_exports"

# Where to save the final report
OUTPUT_FILE = r"C:\Users\Admin\Desktop\pp\consolidation_report.xlsx"

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


def build_weekly_table(file_path: str, ticker: str) -> pd.DataFrame | None:
    """
    Same logic as before, but now reads a single Excel file (one stock)
    instead of a hardcoded CSV path. The 'ticker' name is passed in
    separately (we take it from the filename).
    """

    # Read the stock's Excel file instead of a CSV
    data = pd.read_excel(file_path)

    data.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume"
    }, inplace=True)

    # Check that all required columns exist before doing anything else.
    # If a file is missing a column, this will raise an error, which the
    # calling code will catch and skip.
    required_columns = ["datetime", "Open", "High", "Low", "Close", "Volume"]
    for col in required_columns:
        if col not in data.columns:
            raise ValueError(f"Missing required column: {col}")

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


def get_current_streak_info(weekly_table: pd.DataFrame) -> dict:
    """
    Walks backward from the latest week and counts how many consecutive
    weeks in a row have IS_CONSOLIDATING == True. This is used only when
    the stock is currently consolidating, so we can report the current
    zone's start date/length/high/low even if the streak so far is
    shorter than MIN_CONSECUTIVE_WEEKS (i.e. it hasn't been confirmed
    as an official "zone" by find_consolidation_zones() yet).
    """

    df = weekly_table.reset_index(drop=True)

    end_idx = len(df) - 1
    start_idx = end_idx

    # Step backwards one week at a time while the previous week is
    # also consolidating
    while start_idx > 0 and df.loc[start_idx - 1, "IS_CONSOLIDATING"]:
        start_idx -= 1

    current_streak = df.loc[start_idx:end_idx]

    return {
        "start_date": current_streak["week_ending"].iloc[0],
        "weeks": len(current_streak),
        "high": current_streak["week_high"].max(),
        "low": current_streak["week_low"].min(),
    }


def build_summary_row(ticker: str, weekly_table: pd.DataFrame, zones_df: pd.DataFrame) -> dict:
    """
    Builds one summary row for a single stock, using the weekly table
    and the consolidation zones we already calculated for it.
    This is new code (not part of the original math) -- it just reads
    off the results and packages them for the Summary worksheet.

    The "Consolidation ..." columns always show the most relevant zone:
      - If the stock is consolidating right now, they show the zone
        that is currently in progress, with the End Date left blank
        since it hasn't finished yet.
      - If the stock is not consolidating right now, they show the
        most recently completed consolidation zone instead.
    """

    # The most recent week of data for this stock
    latest_week = weekly_table.iloc[-1]
    still_in_consolidation = bool(latest_week["IS_CONSOLIDATING"])

    # Default values -- used if there's no relevant zone to report at all
    consolidation_start = None
    consolidation_end = None
    consolidation_weeks = None
    consolidation_high = None
    consolidation_low = None

    if still_in_consolidation:
        # Case 1: currently consolidating -- use the live streak so far,
        # leave the end date blank since it hasn't ended
        streak_info = get_current_streak_info(weekly_table)
        consolidation_start = streak_info["start_date"]
        consolidation_end = None
        consolidation_weeks = streak_info["weeks"]
        consolidation_high = streak_info["high"]
        consolidation_low = streak_info["low"]

    elif not zones_df.empty:
        # Case 2: not consolidating right now -- report the most recent
        # completed zone (zones_df is sorted chronologically, so the
        # last row is the most recent one, and it's guaranteed to have
        # already ended since the latest week itself is not consolidating)
        last_zone = zones_df.iloc[-1]
        consolidation_start = last_zone["zone_start_week"]
        consolidation_end = last_zone["zone_end_week"]
        consolidation_weeks = last_zone["consecutive_weeks"]
        consolidation_high = last_zone["zone_high"]
        consolidation_low = last_zone["zone_low"]

    summary_row = {
        "Stock Name": ticker,
        "Total Number of Consolidation Weeks": int(weekly_table["IS_CONSOLIDATING"].sum()),
        "Still in Consolidation (YES/NO)": "YES" if still_in_consolidation else "NO",
        "Consolidation Start Date": consolidation_start,
        "Consolidation End Date": consolidation_end,
        "Consolidation Weeks": consolidation_weeks,
        "Consolidation Highest High": consolidation_high,
        "Consolidation Lowest Low": consolidation_low,
        "Current Close": latest_week["close"],
        "Current ATR": latest_week["atr"],
        "ATR Average": latest_week["atr_avg"],
        "ATR Below Average": bool(latest_week["atr_in_range"]),
        "Current Weekly RV": latest_week["weekly_rv"],
        "RV Average": latest_week["rv_avg"],
        "RV Below Average": bool(latest_week["rv_below_avg"]),
    }

    return summary_row


def build_zone_rows(ticker: str, zones_df: pd.DataFrame) -> list:
    """
    Turns this stock's zones_df (from the unchanged find_consolidation_zones)
    into a list of rows for the "All Consolidation Zones" worksheet --
    one row per zone, numbered in order, with the total zone count
    repeated on every row so it's easy to filter/skim in Excel.
    """

    zone_rows = []

    if zones_df.empty:
        return zone_rows

    total_zones = len(zones_df)

    zone_number = 1
    for _, zone in zones_df.iterrows():
        zone_rows.append({
            "Stock Name": ticker,
            "Zone Number": zone_number,
            "Zone Start Date": zone["zone_start_week"],
            "Zone End Date": zone["zone_end_week"],
            "Consecutive Weeks": zone["consecutive_weeks"],
            "Total Consolidation Zones Found": total_zones,
        })
        zone_number += 1

    return zone_rows


def get_ticker_name(file_path: str) -> str:
    """Turns 'C:\\...\\NVDA_1H_OHLCV_1YEAR.xlsx' into 'NVDA_1H_OHLCV_1YEAR'."""
    file_name = os.path.basename(file_path)
    ticker_name, _ = os.path.splitext(file_name)
    return ticker_name


def run_full_scan():
    """
    Loops through every Excel file in DATA_FOLDER, one stock at a time.
    For each stock we:
      1. Build the weekly table
      2. Find consolidation zones
      3. Save a summary row
      4. Save the weekly rows
      5. Let Python throw away that stock's data before moving to the next one
    """

    # Find every .xlsx file in the folder
    file_list = glob.glob(os.path.join(DATA_FOLDER, "*.xlsx"))

    if not file_list:
        print(f"No .xlsx files found in {DATA_FOLDER}")
        return

    summary_rows = []          # one row per stock, for Worksheet 1
    all_weekly_tables = []     # every weekly row, for Worksheet 2
    all_zone_rows = []         # every consolidation zone, for Worksheet 3

    # tqdm draws the progress bar for us as we loop through the files
    progress_bar = tqdm(file_list, desc="Scanning Stocks", unit="stock")

    for file_path in progress_bar:

        ticker = get_ticker_name(file_path)

        # Show which file is currently being processed under the bar
        progress_bar.set_postfix_str(f"Currently Processing: {ticker}")

        try:
            weekly_table = build_weekly_table(file_path, ticker)

            if weekly_table is None or weekly_table.empty:
                print(f"Skipping {ticker}: not enough data")
                continue

            zones_df = find_consolidation_zones(weekly_table)

            summary_row = build_summary_row(ticker, weekly_table, zones_df)
            summary_rows.append(summary_row)

            zone_rows = build_zone_rows(ticker, zones_df)
            all_zone_rows.extend(zone_rows)

            all_weekly_tables.append(weekly_table)

        except Exception as e:
            # If anything goes wrong with this one file, skip it and
            # keep going -- never let one bad file stop the whole scan
            print(f"Skipping {ticker}: {e}")
            continue

        finally:
            # Free this stock's data before moving to the next one.
            # weekly_table and zones_df go out of scope automatically,
            # this just makes sure we don't keep any extra references.
            if "weekly_table" in locals():
                del weekly_table
            if "zones_df" in locals():
                del zones_df

    if not summary_rows:
        print("No stocks were successfully processed. Nothing to save.")
        return

    # Build the two final tables
    summary_df = pd.DataFrame(summary_rows)
    weekly_df = pd.concat(all_weekly_tables, ignore_index=True)

    # week_ending marks the Friday each weekly candle closes on, so the
    # candle's start (Saturday of the previous week) is 6 days earlier
    weekly_df["week_start"] = weekly_df["week_ending"] - pd.Timedelta(days=6)

    # Only keep the columns requested, in the requested order
    weekly_columns = [
        "ticker",
        "week_start",
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
    weekly_df = weekly_df[weekly_columns]

    # Build the "All Consolidation Zones" table (one row per zone found)
    zone_columns = [
        "Stock Name",
        "Zone Number",
        "Zone Start Date",
        "Zone End Date",
        "Consecutive Weeks",
        "Total Consolidation Zones Found",
    ]

    if all_zone_rows:
        zones_summary_df = pd.DataFrame(all_zone_rows)[zone_columns]
    else:
        # No zones found across any stock -- still write an empty sheet
        # with the correct headers so the workbook structure stays the same
        zones_summary_df = pd.DataFrame(columns=zone_columns)

    # Save everything to one Excel workbook with three sheets, only once
    # all 700+ stocks have been processed
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        weekly_df.to_excel(writer, sheet_name="Weekly Analysis", index=False)
        zones_summary_df.to_excel(writer, sheet_name="All Consolidation Zones", index=False)

    print(f"\nDone! Report saved to: {OUTPUT_FILE}")
    print(f"Stocks processed successfully: {len(summary_rows)} / {len(file_list)}")


if __name__ == "__main__":
    run_full_scan()
