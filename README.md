# Stock Consolidation Scanner

A Python-based stock market analysis toolkit that downloads one year of hourly
OHLCV data, identifies weekly consolidation patterns, and produces ranked Excel
reports.

## Features

- Downloads hourly OHLCV data for US equities through Databento.
- Filters downloaded data to regular US market hours on weekdays.
- Calculates ATR, rolling ATR, weekly realized volatility, and prior-week
  support/resistance ranges.
- Detects consecutive weeks of low volatility and range-bound price action.
- Exports detailed and ranked Excel workbooks.

## Project Files

| File | Purpose |
| --- | --- |
| `data_fetch.py` | Downloads hourly stock data and exports one Excel file per symbol. |
| `consolidation_scanner_v2.py` | Scans exported data and creates the main consolidation report. |
| `Ranking.py` | Ranks stocks and creates a prioritized copy of the report. |
| `backtest.py` | Legacy CSV-based analysis script. |
| `symbols.csv` | Symbol universe used by the data downloader. |
| `consolidation_report.xlsx` | Main scan output. |
| `consolidation_report_ranked.xlsx` | Ranked scan output. |
| `ticker_exports/` | Downloaded hourly OHLCV Excel files. |

## Requirements

- Windows with Python 3.10 or newer
- A Databento account and API key
- Python packages:
  - `pandas`
  - `numpy`
  - `databento`
  - `tqdm`
  - `openpyxl`

Install the packages with:

```powershell
python -m pip install pandas numpy databento tqdm openpyxl
```

## Configuration

Set a Databento API key as an environment variable:

```powershell
$env:DATABENTO_API_KEY="your_api_key"
```

You can also create a `.env` file in the project directory:

```text
DATABENTO_API_KEY=your_api_key
```

Update the symbol universe in `symbols.csv`. The file must contain a `Symbol`
column.

The scripts use Windows absolute paths for their default input and output
locations. Update the path constants near the top of each script when running
from a different location:

- `data_fetch.py`: `SYMBOLS_FILE`, `EXPORT_FOLDER`
- `consolidation_scanner_v2.py`: `DATA_FOLDER`, `OUTPUT_FILE`
- `Ranking.py`: `INPUT_FILE`, `OUTPUT_FILE`

## Usage

Run the scripts in this order:

```powershell
# 1. Download one year of hourly OHLCV data
python data_fetch.py

# 2. Scan all ticker exports and create the consolidation report
python consolidation_scanner_v2.py

# 3. Rank the report by priority
python Ranking.py
```

The download step may take time depending on the number of symbols and data
availability.

## Consolidation Report

`consolidation_report.xlsx` contains:

- `Summary`: One row per successfully processed stock.
- `Weekly Analysis`: Weekly ATR, volatility, range, and consolidation fields.
- `All Consolidation Zones`: Confirmed consolidation streaks with start and end
  dates.

`consolidation_report_ranked.xlsx` preserves those worksheets and adds ranking
columns to `Summary`:

- `1`: Currently consolidating
- `2`: Historical consolidation found
- `3`: No consolidation found

Stocks ranked `1` are sorted before ranked `2`, and ranked `2` before ranked
`3`. Ties are sorted by the total number of consolidating weeks.

## Consolidation Criteria

A week is considered consolidating when all of the following are true:

1. Current ATR is below its 20-period average.
2. Four-week realized volatility is below its 10-period average.
3. The closing price remains within the prior week's high-to-low range.

The default scanner requires at least two consecutive qualifying weeks for a
confirmed zone. These settings can be changed in
`consolidation_scanner_v2.py`:

- `ATR_PERIOD`
- `ATR_AVG_PERIOD`
- `RV_WEEKS_WINDOW`
- `RV_AVG_WEEKS`
- `MIN_CONSECUTIVE_WEEKS`

## Output Data

- Hourly ticker exports use the name `<SYMBOL>_1H_OHLCV_1YEAR.xlsx`.
- Excel files that already exist during a download are not overwritten; the
  downloader creates a numbered alternative file name.
- Failed ticker downloads are reported after the download loop completes.

## Notes

- Market data availability and Databento licensing determine which symbols can
  be downloaded.
- Consolidation detection is for research and analysis only. It is not financial
  advice or a guarantee of future price movement.

## Live consolidation screener

The separate `screener_engine.py` and `app.py` tools use Yahoo Finance through
`yfinance`. This is a **polling** implementation, not a WebSocket/live stream:
Yahoo bars may be delayed, incomplete, unavailable, or rate-limited. No
Databento API key is used by this screener. The existing Databento download and
backtest scripts above are unchanged.

The screener seeds its indicators with up to one year of Yahoo Finance hourly
bars, then polls one-minute bars during regular US equity hours and for a
ten-minute closeout window. It rolls those bars into hourly data and applies the
supplied backtest's hourly ATR, weekly
realized-volatility, previous-week-range, and two-consecutive-week consolidation
math. A boundary is only updated from completed weeks; breakout confirmation
requires two consecutive completed hourly closes outside it by default. Near
breaks use a 0.3% default proximity threshold. The live consolidation report
also lists completed one-week runs as developing zones, while retaining the
backtest's two-week minimum for confirmed zones and breakout signals. The
dashboard includes all stored zones and signals plus an Excel report download.
Zone periods show the Monday-Friday span for the first through last qualifying
weeks in the report, all-zones table, and signal export. The current-status
table shows ticker counts alongside plain-language explanations, including
whether there is no confirmed two-week zone or price is away from an existing
zone.
Its 0-100 setup score is a rule-based heuristic, not a probability.

The engine and dashboard share `data/live_screener.sqlite3`. One
`data/live_weekly.csv` contains `hourly_snapshot`, `weekly_snapshot`, and
`signal` rows using a live-specific schema. CSV reads and writes use a
cross-process file lock. Select one or more sector presets and click Add sectors
to saved watchlist; duplicates are removed, and the engine uses the updated list
on its next poll. TradingView CSV uploads use the `Symbol`/`Ticker` column when
present.
Quick filters include a maximum zone-width slider and a Chop-duration detail
view. The terminal prints consolidation-zone summaries on initialization and
when zone history changes. Use the Appearance control to select a preset
palette or set custom colors, and switch between dark and light modes. The
selected appearance updates Streamlit's native charts and tables as well as
the dashboard styling. Choices are saved under `data/theme.json` and
`data/mode.txt`; the local Streamlit configuration is updated as well.
The optional individual-ticker ATR chart defaults to a three-month view and
lets you select the ticker, time window, and overlays, including weekly
high/low ranges, close ± ATR envelope, ATR/ATR average, and consolidation
zones.

### Windows PowerShell setup and run

Open PowerShell in the project folder:

```powershell
Set-Location 'C:\Users\Admin\OneDrive\Desktop\Consolidation Zone Screener'
py -3 -m venv .venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

No API key is required for yfinance. The optional `.env` settings are documented
in `.env.example`; preserve any existing `.env` file rather than replacing it.
Run the focused tests:

```powershell
python -m unittest discover -s tests -v
```

Start the polling engine in the first PowerShell window:

```powershell
Set-Location 'C:\Users\Admin\OneDrive\Desktop\Consolidation Zone Screener'
.\.venv\Scripts\Activate.ps1
python .\screener_engine.py
```

Start the dashboard in a second PowerShell window:

```powershell
Set-Location 'C:\Users\Admin\OneDrive\Desktop\Consolidation Zone Screener'
.\.venv\Scripts\Activate.ps1
python -m streamlit run .\app.py
```

The default polling interval is 60 seconds. Adjust `YFINANCE_POLL_SECONDS`,
`YFINANCE_HISTORY_PERIOD`, `NEAR_BREAK_PCT`, `BREAKOUT_HOLD_HOURS`, and
`SIGNAL_COOLDOWN_MINUTES` in `.env` as needed. This is a research screener, not
financial advice or an execution/risk-control system.
