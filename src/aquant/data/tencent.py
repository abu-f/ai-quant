"""腾讯行情接口（日/周/月 K 线，支持前复权）。

接口：https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
返回行格式：[日期, 开, 收, 高, 低, 成交量(手), ...]
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from ..symbols import Symbol
from ..utils import retry
from .base import http_get

BASE_URLS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
MKLINES_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
PERIOD_MAP = {"day": "day", "week": "week", "month": "month",
              "d": "day", "w": "week", "m": "month"}
MINUTE_MAP = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


@retry(times=2, delay=0.8)
def daily(symbol: Symbol | str, period: str = "day", adjust: str = "qfq",
          count: int = 800, start: str = "", end: str = "",
          timeout: float = 12, min_interval_ms: int = 250) -> pd.DataFrame:
    sym = Symbol.parse(symbol)
    per = PERIOD_MAP.get(period.lower(), "day")
    fq = "" if adjust in ("bfq", "none", "") else adjust
    param = f"{sym.tencent},{per},{start},{end},{count},{fq}"

    payload: dict[str, Any] | None = None
    errors: list[str] = []
    for base in BASE_URLS:
        try:
            text = http_get(base, {"param": param}, timeout=timeout,
                            min_interval_ms=min_interval_ms)
            payload = json.loads(text)  # 被 WAF 拦截时会返回 HTML，解析失败即换域名
            if payload.get("data"):
                break
        except Exception as exc:
            errors.append(f"{base.split('//')[1].split('/')[0]}: {exc}")
            payload = None
    if not payload or not payload.get("data"):
        raise RuntimeError(f"腾讯 K 线接口不可用（{'; '.join(errors) or '空数据'}）")

    node = (payload.get("data") or {}).get(sym.tencent) or {}

    frames: list[list[Any]] = []
    for key in (f"{fq}{per}", per, f"qfq{per}", f"hfq{per}"):
        value = node.get(key)
        if isinstance(value, list) and value:
            frames = value
            break
    if not frames:
        return _empty()

    rows = []
    for item in frames:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        date = str(item[0])[:10]
        try:
            o, c, h, low = (float(item[1]), float(item[2]), float(item[3]), float(item[4]))
            vol = float(item[5]) * 100.0  # 手 -> 股
        except (TypeError, ValueError):
            continue
        rows.append((date, o, h, low, c, vol))
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["amount"] = None
    return df[["open", "high", "low", "close", "volume", "amount"]].astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"],
                        index=pd.DatetimeIndex([], name="date"))


@retry(times=2, delay=0.8)
def intraday(symbol: Symbol | str, interval: str = "5m", count: int = 320,
             timeout: float = 12, min_interval_ms: int = 250) -> pd.DataFrame:
    """分钟级 K 线备用源。返回行：[时间, 开, 收, 高, 低, 成交量(手)]。"""
    sym = Symbol.parse(symbol)
    interval_key = MINUTE_MAP.get(interval.lower())
    if interval_key is None:
        raise ValueError(f"不支持的分钟周期：{interval}")
    text = http_get(MKLINES_URL, {"param": f"{sym.tencent},{interval_key},,{count}"},
                    timeout=timeout, min_interval_ms=min_interval_ms)
    payload: dict[str, Any] = json.loads(text)
    node = (payload.get("data") or {}).get(sym.tencent) or {}
    frames = node.get(interval_key) or []
    rows = []
    for item in frames:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        try:
            stamp = pd.to_datetime(str(item[0]), format="%Y%m%d%H%M", errors="coerce")
            o, c, h, low = float(item[1]), float(item[2]), float(item[3]), float(item[4])
            vol = float(item[5]) * 100.0  # 手 -> 股
        except (TypeError, ValueError):
            continue
        if stamp is pd.NaT:
            continue
        rows.append((stamp, o, h, low, c, vol))
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df = df.set_index("date").sort_index()
    df["amount"] = None
    return df[["open", "high", "low", "close", "volume", "amount"]]
