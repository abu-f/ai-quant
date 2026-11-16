"""新浪财经接口：日线备用源 + 实时行情备用源。

- K 线：https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
- 快照：https://hq.sinajs.cn/list=sh600519 （需 Referer，GBK 编码）
"""

from __future__ import annotations

import json
import re
from typing import Iterable

import pandas as pd

from ..symbols import Symbol
from ..utils import now, retry, safe_float, ts_text
from .base import Quote, http_get

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
QUOTE_URL = "https://hq.sinajs.cn/list="
REFERER = {"Referer": "https://finance.sina.com.cn"}

LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
COUNT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeStockCount")

# 分钟级周期映射：aquant 写法 -> 新浪 scale 参数
SCALE_MAP = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}


@retry(times=2, delay=0.8)
def intraday(symbol: Symbol | str, interval: str = "5m", count: int = 480,
             timeout: float = 12, min_interval_ms: int = 250) -> pd.DataFrame:
    """分钟级 K 线（1/5/15/30/60 分钟），最多 1023 根。"""
    sym = Symbol.parse(symbol)
    scale = SCALE_MAP.get(interval.lower())
    if scale is None:
        raise ValueError(f"不支持的分钟周期：{interval}")
    text = http_get(KLINE_URL, {
        "symbol": sym.sina, "scale": scale, "ma": "no", "datalen": min(count, 1023),
    }, timeout=timeout, min_interval_ms=min_interval_ms)
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        return _empty_intraday()
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append((
            str(item.get("day")),
            safe_float(item.get("open")), safe_float(item.get("high")),
            safe_float(item.get("low")), safe_float(item.get("close")),
            safe_float(item.get("volume")), safe_float(item.get("amount")),
        ))
    if not rows:
        return _empty_intraday()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume", "amount"]]


def _empty_intraday() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"],
                        index=pd.DatetimeIndex([], name="date"))


@retry(times=2, delay=0.8)
def daily(symbol: Symbol | str, count: int = 800, timeout: float = 12,
          min_interval_ms: int = 250) -> pd.DataFrame:
    sym = Symbol.parse(symbol)
    text = http_get(KLINE_URL, {
        "symbol": sym.sina, "scale": 240, "ma": "no", "datalen": min(count, 1023),
    }, timeout=timeout, min_interval_ms=min_interval_ms)
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"],
                            index=pd.DatetimeIndex([], name="date"))
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append((
            str(item.get("day"))[:10],
            safe_float(item.get("open")), safe_float(item.get("high")),
            safe_float(item.get("low")), safe_float(item.get("close")),
            safe_float(item.get("volume")),
        ))
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).dropna()
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"],
                            index=pd.DatetimeIndex([], name="date"))
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["amount"] = None
    return df[["open", "high", "low", "close", "volume", "amount"]]


@retry(times=2, delay=0.6)
def quotes(symbols: Iterable[Symbol | str], timeout: float = 10,
           min_interval_ms: int = 250) -> list[Quote]:
    syms = [Symbol.parse(s) for s in symbols]
    if not syms:
        return []
    text = http_get(QUOTE_URL + ",".join(s.sina for s in syms), timeout=timeout,
                    headers=REFERER, encoding="gbk", min_interval_ms=min_interval_ms)
    out: list[Quote] = []
    for partial, sym in zip(re.findall(r'hq_str_[a-z]{2}\d{6}="([^"]*)"', text), syms):
        fields = partial.split(",")
        if len(fields) < 32:
            continue
        price = safe_float(fields[3])
        prev = safe_float(fields[2])
        change = None
        pct = None
        if price is not None and prev:
            change = price - prev
            pct = change / prev * 100
        out.append(Quote(
            code=sym.code,
            name=fields[0],
            price=price,
            prev_close=prev,
            open=safe_float(fields[1]),
            high=safe_float(fields[4]),
            low=safe_float(fields[5]),
            change=change,
            pct=pct,
            volume=safe_float(fields[8]),
            amount=safe_float(fields[9]),
            ts=ts_text(),
            source="sina",
            market=sym.market,
        ))
    return out


# --------------------------------------------------------------------- 全市场
def market_snapshot(page_size: int = 100, max_pages: int = 70,
                    timeout: float = 12, min_interval_ms: int = 220,
                    min_coverage: float = 0.9) -> "pd.DataFrame":
    """新浪全市场 A 股列表（东财 clist 被限流时的备用源）。

    与东财的差异：没有量比、PE 为动态口径（pe_ttm 用同一值兜底）、
    市值为万元换算后的元。字段名与东财版本保持一致，选股逻辑无需区分来源。
    """
    total: int | None = None
    try:
        raw = http_get(COUNT_URL, {"node": "hs_a"}, timeout=8, headers=REFERER,
                       min_interval_ms=min_interval_ms)
        total = int(json.loads(raw))
    except Exception:
        total = None

    rows: list[dict] = []
    consecutive_failures = 0
    for page in range(1, max_pages + 1):
        try:
            text = http_get(LIST_URL, {
                "page": page, "num": page_size, "sort": "symbol", "asc": 1,
                "node": "hs_a", "symbol": "", "_s_r_a": "page",
            }, timeout=timeout, headers=REFERER, min_interval_ms=min_interval_ms, attempts=2)
            data = json.loads(text)
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 2 or not rows:
                break
            continue
        if not data:
            break
        consecutive_failures = 0
        rows.extend(_row_of_sina(item) for item in data if isinstance(item, dict))
        if total and len(rows) >= total:
            break

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("新浪全市场列表无有效数据")
    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
    if total and len(df) < total * min_coverage:
        raise RuntimeError(f"新浪全市场列表不完整（{len(df)}/{total}）")
    return df


def _row_of_sina(item: dict) -> dict:
    symbol = str(item.get("symbol") or "")
    market = symbol[:2].lower() if symbol[:2].lower() in ("sh", "sz", "bj") else ""
    code = str(item.get("code") or symbol[2:])
    price = safe_float(item.get("trade"))
    prev = safe_float(item.get("settlement"))
    change = safe_float(item.get("pricechange"))
    pct = safe_float(item.get("changepercent"))
    pe = safe_float(item.get("per"))
    mktcap = safe_float(item.get("mktcap"))   # 万元
    nmc = safe_float(item.get("nmc"))         # 万元
    return {
        "code": code,
        "name": str(item.get("name") or ""),
        "price": price,
        "pct": pct,
        "change": change,
        "volume": safe_float(item.get("volume")),
        "amount": safe_float(item.get("amount")),
        "turnover": safe_float(item.get("turnoverratio")),
        "pe": pe,
        "pe_ttm": pe,          # 新浪只给动态 PE，这里兜底
        "volume_ratio": None,  # 新浪列表无量比
        "high": safe_float(item.get("high")),
        "low": safe_float(item.get("low")),
        "open": safe_float(item.get("open")),
        "prev_close": prev,
        "total_mv": mktcap * 1e4 if mktcap is not None else None,
        "float_mv": nmc * 1e4 if nmc is not None else None,
        "pb": safe_float(item.get("pb")),
        "market": market,
    }
