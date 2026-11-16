"""SQLite 存储层：日线缓存、行情快照、基本面、预警记录。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .utils import now, ts_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    code   TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT 'day',
    adjust TEXT NOT NULL DEFAULT 'qfq',
    date   TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    PRIMARY KEY (code, period, adjust, date)
);
CREATE INDEX IF NOT EXISTS idx_bars_code ON bars(code, adjust, date);

CREATE TABLE IF NOT EXISTS quotes (
    code     TEXT PRIMARY KEY,
    ts       TEXT,
    name     TEXT,
    price    REAL,
    pct      REAL,
    payload  TEXT
);

CREATE TABLE IF NOT EXISTS fundamentals (
    code       TEXT PRIMARY KEY,
    updated_at TEXT,
    payload    TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    rule       TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ts         TEXT,
    price      REAL,
    message    TEXT,
    UNIQUE(code, rule, trade_date)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS backtests (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    code     TEXT,
    strategy TEXT,
    params   TEXT,
    metrics  TEXT
);
"""

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


class Store:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    # ---------- 基础 ----------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def close(self) -> None:  # 便于测试
        pass

    # ---------- 日线 ----------
    def upsert_bars(self, code: str, df: pd.DataFrame, period: str = "day",
                    adjust: str = "qfq") -> int:
        if df is None or df.empty:
            return 0
        rows: list[tuple] = []
        for idx, row in df.iterrows():
            date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            rows.append((
                code, period, adjust, date,
                _num(row.get("open")), _num(row.get("high")),
                _num(row.get("low")), _num(row.get("close")),
                _num(row.get("volume")), _num(row.get("amount")),
            ))
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO bars(code,period,adjust,date,open,high,low,close,volume,amount) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code,period,adjust,date) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume, amount=excluded.amount",
                rows,
            )
        return len(rows)

    def load_bars(self, code: str, period: str = "day", adjust: str = "qfq",
                  start: str | None = None, end: str | None = None,
                  limit: int | None = None) -> pd.DataFrame:
        sql = ("SELECT date, open, high, low, close, volume, amount FROM bars "
               "WHERE code=? AND period=? AND adjust=?")
        params: list[Any] = [code, period, adjust]
        if start:
            sql += " AND date>=?"
            params.append(start)
        if end:
            sql += " AND date<=?"
            params.append(end)
        sql += " ORDER BY date"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            return pd.DataFrame(columns=BAR_COLUMNS, index=pd.DatetimeIndex([], name="date"))
        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        if limit:
            df = df.tail(limit)
        return df

    def latest_bar_date(self, code: str, period: str = "day", adjust: str = "qfq") -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM bars WHERE code=? AND period=? AND adjust=?",
                (code, period, adjust),
            ).fetchone()
        return row["d"] if row and row["d"] else None

    def bar_count(self, code: str, period: str = "day", adjust: str = "qfq") -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM bars WHERE code=? AND period=? AND adjust=?",
                (code, period, adjust),
            ).fetchone()
        return int(row["c"]) if row else 0

    def cached_codes(self, period: str = "day", adjust: str = "qfq") -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT code FROM bars WHERE period=? AND adjust=? ORDER BY code",
                (period, adjust),
            ).fetchall()
        return [r["code"] for r in rows]

    # ---------- 行情快照 ----------
    def save_quotes(self, quotes: Iterable[Any]) -> None:
        ts = ts_text()
        rows = []
        for q in quotes:
            rows.append((q.code, ts, q.name, q.price,
                         q.pct, json.dumps(q.to_dict(), ensure_ascii=False)))
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO quotes(code,ts,name,price,pct,payload) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET ts=excluded.ts, name=excluded.name, "
                "price=excluded.price, pct=excluded.pct, payload=excluded.payload",
                rows,
            )

    def load_quotes(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT code, ts, payload FROM quotes").fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            payload["_ts"] = r["ts"]
            out.append(payload)
        return out

    def quote_age_minutes(self) -> float:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(ts) AS t FROM quotes").fetchone()
        if not row or not row["t"]:
            return 1e9
        try:
            then = datetime.strptime(row["t"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 1e9
        return (now() - then).total_seconds() / 60.0

    # ---------- 基本面 ----------
    def save_fundamentals(self, code: str, payload: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fundamentals(code,updated_at,payload) VALUES(?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload",
                (code, ts_text(), json.dumps(payload, ensure_ascii=False)),
            )

    def load_fundamentals(self, code: str, max_age_minutes: int = 720) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT updated_at, payload FROM fundamentals WHERE code=?", (code,)
            ).fetchone()
        if not row:
            return None
        try:
            then = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None
        if (now() - then).total_seconds() / 60 > max_age_minutes:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    # ---------- 预警 ----------
    def record_alert(self, code: str, rule: str, trade_date: str, message: str,
                     price: float | None) -> bool:
        """尝试写入预警；已存在同 (code, rule, date) 则返回 False（用于去重）。"""
        with self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO alerts(code,rule,trade_date,ts,price,message) VALUES(?,?,?,?,?,?)",
                    (code, rule, trade_date, ts_text(), price, message),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def recent_alerts(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def save_backtest(self, code: str, strategy: str, params: dict, metrics: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO backtests(ts,code,strategy,params,metrics) VALUES(?,?,?,?,?)",
                (ts_text(), code, strategy,
                 json.dumps(params, ensure_ascii=False, default=str),
                 json.dumps(metrics, ensure_ascii=False, default=str)),
            )

    # ---------- meta ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            bars = conn.execute("SELECT COUNT(*) c FROM bars").fetchone()["c"]
            codes = conn.execute("SELECT COUNT(DISTINCT code) c FROM bars").fetchone()["c"]
            alerts = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
        size = self.path.stat().st_size / 1024 / 1024 if self.path.exists() else 0
        return {"bars": bars, "codes": codes, "alerts": alerts, "size_mb": round(size, 2)}


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        num = float(value)
        return None if num != num else num
    except (TypeError, ValueError):
        return None
