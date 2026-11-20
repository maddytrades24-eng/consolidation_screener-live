"""Shared SQLite state and a cross-process locked CSV journal."""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import portalocker


CSV_COLUMNS = [
    "record_type",
    "timestamp",
    "ticker",
    "signal",
    "price",
    "bar_timeframe",
    "bar_timestamp",
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "bar_volume",
    "week_ending",
    "week_open",
    "week_high",
    "week_low",
    "week_close",
    "week_volume",
    "atr",
    "atr_avg",
    "atr_in_range",
    "weekly_rv",
    "rv_avg",
    "rv_below_avg",
    "prev_week_high",
    "prev_week_low",
    "is_consolidating",
    "zone_start_week",
    "zone_end_week",
    "consecutive_weeks",
    "zone_high",
    "zone_low",
    "distance_to_break_pct",
    "zone_width_pct",
    "chop_bars",
    "chop_minutes",
]

SNAPSHOT_COLUMNS = [
    "ticker",
    "price",
    "state",
    "zone_high",
    "zone_low",
    "distance_to_break_pct",
    "zone_width_pct",
    "last_signal_time",
    "quote_time",
    "updated_at",
    "zone_start_week",
    "zone_end_week",
    "consecutive_weeks",
    "atr",
    "atr_avg",
    "weekly_rv",
    "rv_avg",
    "chop_bars",
    "chop_minutes",
]


class DashboardStore:
    """SQLite-backed state shared by the engine process and Streamlit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist_collections (
                    name TEXT PRIMARY KEY,
                    tickers TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    ticker TEXT PRIMARY KEY,
                    price REAL,
                    state TEXT NOT NULL,
                    zone_high REAL,
                    zone_low REAL,
                    distance_to_break_pct REAL,
                    zone_width_pct REAL,
                    last_signal_time TEXT,
                    quote_time TEXT,
                    updated_at TEXT NOT NULL,
                    zone_start_week TEXT,
                    zone_end_week TEXT,
                    consecutive_weeks INTEGER,
                    atr REAL,
                    atr_avg REAL,
                    weekly_rv REAL,
                    rv_avg REAL,
                    chop_bars INTEGER NOT NULL DEFAULT 0,
                    chop_minutes INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    state TEXT NOT NULL,
                    price REAL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consolidation_zones (
                    ticker TEXT NOT NULL,
                    zone_start_week TEXT NOT NULL,
                    zone_end_week TEXT NOT NULL,
                    consecutive_weeks INTEGER NOT NULL,
                    zone_high REAL NOT NULL,
                    zone_low REAL NOT NULL,
                    last_close REAL,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (ticker, zone_start_week)
                );
                CREATE TABLE IF NOT EXISTS engine_status (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_watchlist(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT ticker FROM watchlist ORDER BY ticker"
            ).fetchall()
        return [row["ticker"] for row in rows]

    def set_watchlist(self, tickers: list[str]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        normalized = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
        with self._connection() as connection:
            connection.execute("DELETE FROM watchlist")
            connection.executemany(
                "INSERT INTO watchlist (ticker, updated_at) VALUES (?, ?)",
                [(ticker, now) for ticker in normalized],
            )

    def list_watchlist_collections(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name FROM watchlist_collections ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]

    def save_watchlist_collection(self, name: str, tickers: list[str]) -> None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Collection name cannot be empty.")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        normalized = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
        if not normalized:
            raise ValueError("Collection must contain at least one ticker.")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO watchlist_collections (name, tickers, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    tickers=excluded.tickers,
                    updated_at=excluded.updated_at
                """,
                (normalized_name, json.dumps(normalized), now),
            )

    def get_watchlist_collection(self, name: str) -> list[str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT tickers FROM watchlist_collections WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return []
        return json.loads(row["tickers"])

    def delete_watchlist_collection(self, name: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM watchlist_collections WHERE name = ?", (name,)
            )

    def upsert_snapshot(self, snapshot: dict[str, Any]) -> None:
        row = {column: snapshot.get(column) for column in SNAPSHOT_COLUMNS}
        for column, value in row.items():
            if isinstance(value, (datetime, date, pd.Timestamp)):
                row[column] = value.isoformat()
            elif hasattr(value, "item"):
                row[column] = value.item()
        row["chop_bars"] = row["chop_bars"] if row["chop_bars"] is not None else 0
        row["chop_minutes"] = (
            row["chop_minutes"] if row["chop_minutes"] is not None else 0
        )
        row["updated_at"] = row["updated_at"] or datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        columns = ", ".join(SNAPSHOT_COLUMNS)
        placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in SNAPSHOT_COLUMNS if column != "ticker"
        )
        values = [row[column] for column in SNAPSHOT_COLUMNS]
        with self._connection() as connection:
            connection.execute(
                f"INSERT INTO snapshots ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(ticker) DO UPDATE SET {updates}",
                values,
            )

    def get_snapshots(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.* FROM snapshots AS s
                JOIN watchlist AS w ON w.ticker = s.ticker
                ORDER BY s.ticker
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_snapshot(self, ticker: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE ticker = ?", (ticker,)
            ).fetchone()
        return dict(row) if row else None

    def record_signal(self, signal: dict[str, Any]) -> int:
        payload = json.dumps(signal, default=str, separators=(",", ":"))
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO signals (ticker, timestamp, state, price, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    signal["ticker"],
                    signal["timestamp"],
                    signal["state"],
                    signal.get("price"),
                    payload,
                ),
            )
            return int(cursor.lastrowid)

    def latest_signal_id(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id), 0) AS id FROM signals").fetchone()
        return int(row["id"])

    def get_signals_after(self, signal_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM signals WHERE id > ? ORDER BY id",
                (signal_id,),
            ).fetchall()
        return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]

    def get_all_signals(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM signals ORDER BY id DESC"
            ).fetchall()
        return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]

    def get_recent_signals(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM signals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]

    @staticmethod
    def _week_date(value: Any) -> str:
        return pd.Timestamp(value).date().isoformat()

    def replace_zones(self, ticker: str, zones: list[dict[str, Any]]) -> None:
        rows = [
            (
                ticker,
                self._week_date(zone["zone_start_week"]),
                self._week_date(zone["zone_end_week"]),
                int(zone["consecutive_weeks"]),
                float(zone["zone_high"]),
                float(zone["zone_low"]),
                float(zone["last_close"]),
                int(bool(zone.get("is_current", False))),
            )
            for zone in zones
        ]
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM consolidation_zones WHERE ticker = ?", (ticker,)
            )
            connection.executemany(
                """
                INSERT INTO consolidation_zones (
                    ticker, zone_start_week, zone_end_week, consecutive_weeks,
                    zone_high, zone_low, last_close, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_zones(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT z.* FROM consolidation_zones AS z
                JOIN watchlist AS w ON w.ticker = z.ticker
                ORDER BY z.ticker, z.zone_end_week DESC, z.zone_start_week DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, key: str, value: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO engine_status (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def get_status(self) -> dict[str, dict[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key, value, updated_at FROM engine_status"
            ).fetchall()
        return {row["key"]: dict(row) for row in rows}


class CsvJournal:
    """Append and read the shared CSV while holding a Windows-safe file lock."""

    def __init__(self, path: str | Path, timeout: float = 10) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._thread_lock = threading.Lock()
        self._ensure_header()

    def _locked(self, mode: str):
        open_options: dict[str, Any] = {}
        if "b" not in mode:
            open_options.update(encoding="utf-8", newline="")
        return portalocker.Lock(
            str(self.path),
            mode=mode,
            timeout=self.timeout,
            **open_options,
        )

    def _ensure_header(self) -> None:
        with self._thread_lock, self._locked("a+") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                csv.writer(stream).writerow(CSV_COLUMNS)
                stream.flush()

    @staticmethod
    def _format_value(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, float):
            return f"{value:.6f}"
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value

    def append(self, row: dict[str, Any]) -> None:
        values = [self._format_value(row.get(column)) for column in CSV_COLUMNS]
        with self._thread_lock, self._locked("a+") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                csv.writer(stream).writerow(CSV_COLUMNS)
            csv.writer(stream).writerow(values)
            stream.flush()

    def read_dataframe(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=CSV_COLUMNS)
        with self._locked("r") as stream:
            return pd.read_csv(stream)

    def read_bytes(self) -> bytes:
        if not self.path.exists():
            return (",".join(CSV_COLUMNS) + "\n").encode("utf-8")
        with self._locked("rb") as stream:
            return stream.read()
