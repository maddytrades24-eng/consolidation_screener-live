#!/usr/bin/env python3
import os
import getpass
from pathlib import Path
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import databento as db


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and not os.getenv(key):
            os.environ[key] = value


load_env_file(Path(__file__).resolve().parent / ".env")

# ============================================================
# CONFIG
# ============================================================

API_KEY = (
    os.getenv("DATABENTO_API_KEY")
    or os.getenv("API_KEY")
    or os.getenv("DATABENTO_KEY")
)

if not API_KEY:
    print("No Databento API key found.")
    API_KEY = getpass.getpass("Enter your Databento API key: ").strip()

if not API_KEY:
    raise SystemExit("Databento API key is required to fetch data.")

SYMBOLS_FILE = r"C:\Users\Admin\Desktop\pp\symbols.csv"

DATASET = "EQUS.MINI"
SCHEMA = "ohlcv-1h"
MARKET_OPEN_ET = time(9, 30)
MARKET_CLOSE_ET = time(16, 0)

EXPORT_FOLDER = Path("ticker_exports")

# ============================================================
# READ SYMBOL UNIVERSE
# ============================================================

symbols_df = pd.read_csv(SYMBOLS_FILE)
TICKERS = symbols_df["Symbol"].dropna().astype(str).str.strip().tolist()

if not TICKERS:
    raise SystemExit(f"No symbols found in {SYMBOLS_FILE}")

# ============================================================
# DATABENTO CLIENT
# ============================================================

client = db.Historical(key=API_KEY)

# ============================================================
# DATE RANGE (LAST 1 YEAR)
# ============================================================

# Databento hourly data is only available up to the latest completed hour.
# Using the current time directly can cause a 422 error for the last partial hour.
now_utc = datetime.now(timezone.utc)
end_date = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
start_date = end_date - timedelta(days=365)


def fetch_symbol(symbol: str) -> None:
    """
    Fetches, cleans, and saves 1 year of hourly OHLCV data for a single
    ticker. Raises on failure so the caller's try/except can handle it.
    """

    print(f"Fetching {symbol} data...")
    print(f"From: {start_date}")
    print(f"To  : {end_date}")

    # --------------------------------------------------------
    # FETCH DATA
    # --------------------------------------------------------

    try:
        data = client.timeseries.get_range(
            dataset=DATASET,
            schema=SCHEMA,
            symbols=[symbol],
            start=start_date,
            end=end_date,
        )
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "auth" in msg.lower():
            print("Authentication failed. Verify that your Databento API key is valid and active.")
            print("For this PowerShell session, run:")
            print("  $env:DATABENTO_API_KEY='your_key_here'")
            print("For future sessions, run:")
            print("  setx DATABENTO_API_KEY 'your_key_here'")
        raise

    # --------------------------------------------------------
    # CONVERT TO DATAFRAME
    # --------------------------------------------------------

    df = data.to_df()

    if df.empty:
        raise ValueError("No data returned.")

    # --------------------------------------------------------
    # CLEAN DATA
    # --------------------------------------------------------

    df = df.reset_index()

    # Keep only OHLCV columns
    columns_to_keep = [
        "ts_event",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]

    df = df[columns_to_keep]

    # Rename timestamp column
    df.rename(columns={"ts_event": "datetime"}, inplace=True)

    # Keep only U.S. regular market hours: weekdays, 9:30 AM to 4:00 PM ET
    if "datetime" in df.columns:
        df["datetime"] = df["datetime"].dt.tz_convert(ZoneInfo("America/New_York"))

        market_hours_mask = (
            (df["datetime"].dt.weekday < 5)
            & (df["datetime"].dt.time >= MARKET_OPEN_ET)
            & (df["datetime"].dt.time <= MARKET_CLOSE_ET)
        )
        df = df.loc[market_hours_mask].copy()

        # Keep the timestamp column in Eastern Time for the Excel export
        df["datetime"] = df["datetime"].dt.tz_localize(None)

    # --------------------------------------------------------
    # SAVE TO EXCEL
    # --------------------------------------------------------

    output_file = EXPORT_FOLDER / f"{symbol}_1H_OHLCV_1YEAR.xlsx"

    # If file already exists, create a new numbered filename
    if output_file.exists():
        base_name = output_file.stem
        extension = output_file.suffix
        counter = 1

        while True:
            candidate = EXPORT_FOLDER / f"{base_name}_{counter}{extension}"
            if not candidate.exists():
                output_file = candidate
                break
            counter += 1

    try:
        df.to_excel(output_file, index=False)
    except PermissionError:
        print(
            f"Could not write to '{output_file}'. "
            "Close any open Excel workbook using this file and try again."
        )
        raise

    print(f"Saved: {output_file.resolve()} ({len(df):,} rows)")


# ============================================================
# MAIN LOOP
# ============================================================

def main() -> None:
    # Create export folder once, before the loop starts
    EXPORT_FOLDER.mkdir(parents=True, exist_ok=True)

    total = len(TICKERS)
    failed = []

    for i, symbol in enumerate(TICKERS, start=1):
        print(f"\n[{i}/{total}] Fetching {symbol}...")

        try:
            fetch_symbol(symbol)
        except Exception as exc:
            print(f"  Failed {symbol}: {exc}")
            failed.append(symbol)
            continue

    print("\n" + "=" * 50)
    print("Download Complete")
    print("=" * 50)
    print(f"Total Symbols : {total}")
    print(f"Successful    : {total - len(failed)}")
    print(f"Failed        : {len(failed)}")

    if failed:
        print("\nFailed Symbols")
        for symbol in failed:
            print(symbol)

    print("=" * 50)


if __name__ == "__main__":
    main()