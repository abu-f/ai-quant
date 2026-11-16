"""东方财富接口：实时快照、全市场列表、财务摘要。

- 单只快照：https://push2.eastmoney.com/api/qt/stock/get
- 全市场列表：https://push2.eastmoney.com/api/qt/clist/get
- 财务摘要：https://datacenter-web.eastmoney.com/api/data/v1/get
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pandas as pd

from ..symbols import Symbol
from ..utils import retry, safe_float, ts_text
from .base import Quote, clean, http_get

log = logging.getLogger("aquant.data")

PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 沪深主板 + 创业板 + 科创板 + 北交所
MARKETS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

QUOTE_FIELDS = ",".join([
    "f43", "f44", "f45", "f46", "f47", "f48", "f50", "f57", "f58", "f60",
    "f107", "f116", "f117", "f162", "f167", "f168", "f169", "f170", "f115",
])

LIST_FIELDS = ",".join([
    "f2", "f3", "f4", "f5", "f6", "f8", "f9", "f10", "f12", "f13", "f14",
    "f15", "f16", "f17", "f18", "f20", "f21", "f23", "f115",
])


@retry(times=3, delay=0.6)
def quote(symbol: Symbol | str, timeout: float = 10, min_interval_ms: int = 250) -> Quote:
    sym = Symbol.parse(symbol)
    text = http_get(PUSH2, {
        "secid": sym.secid, "fltt": 2, "invt": 2, "fields": QUOTE_FIELDS,
    }, timeout=timeout, min_interval_ms=min_interval_ms)
    payload = json.loads(text)
    data = payload.get("data") or {}
    if not data:
        raise ValueError(f"东方财富未返回 {sym.code} 的行情")
    return _quote_from_em(data, sym)


def _quote_from_em(data: dict[str, Any], sym: Symbol) -> Quote:
    price = clean(data.get("f43"))
    prev = clean(data.get("f60"))
    change = clean(data.get("f169"))
    pct = clean(data.get("f170"))
    volume = clean(data.get("f47"))
    return Quote(
        code=sym.code,
        name=str(data.get("f58") or ""),
        price=price,
        prev_close=prev,
        open=clean(data.get("f46")),
        high=clean(data.get("f44")),
        low=clean(data.get("f45")),
        change=change,
        pct=pct,
        volume=volume * 100 if volume is not None else None,  # 手 -> 股
        amount=clean(data.get("f48")),
        turnover=clean(data.get("f168")),
        volume_ratio=clean(data.get("f50")),
        pe=clean(data.get("f162")),
        pe_ttm=clean(data.get("f115")),
        pb=clean(data.get("f167")),
        total_mv=clean(data.get("f116")),
        float_mv=clean(data.get("f117")),
        ts=ts_text(),
        source="eastmoney",
        market=sym.market,
    )


def _clist_page(page: int, page_size: int, timeout: float,
                min_interval_ms: int, attempts: int = 3) -> tuple[int | None, list[dict[str, Any]]] | None:
    """抓取单页；失败重试本页（而不是从头再来），仍失败返回 None。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            text = http_get(CLIST, {
                "pn": page, "pz": page_size, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": MARKETS, "fields": LIST_FIELDS,
            }, timeout=timeout, min_interval_ms=min_interval_ms)
            payload = json.loads(text)
            data = payload.get("data") or {}
            diff = data.get("diff") or []
            total = int(data.get("total") or 0)
            return total, list(diff)
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(0.5 * attempt)
    log.warning("东财全市场第 %d 页抓取失败：%s", page, last_error)
    return None


def market_snapshot(page_size: int = 200, timeout: float = 8,
                    min_interval_ms: int = 300, max_pages: int = 40,
                    min_coverage: float = 0.9) -> pd.DataFrame:
    """全市场 A 股快照（约 5000+ 只），用于选股与基本面速查。

    - 单页失败只跳过该页并重试本页，不会整次从头再来；
    - 连续 2 页失败即快速放弃（接口被限流时不要死磕）；
    - 覆盖率不足 min_coverage 视为失败，避免把残缺数据写进缓存。
    """
    rows: list[dict[str, Any]] = []
    total: int | None = None
    consecutive_failures = 0
    total_failures = 0
    for page in range(1, max_pages + 1):
        result = _clist_page(page, page_size, timeout, min_interval_ms, attempts=2)
        if result is None:
            consecutive_failures += 1
            total_failures += 1
            if consecutive_failures >= 2 or total_failures >= 3 or not rows:
                break
            continue
        consecutive_failures = 0
        page_total, diff = result
        if total is None:
            total = page_total
        if not diff:
            break
        rows.extend(_row_of(item) for item in diff)
        if total and len(rows) >= total:
            break

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("东方财富全市场接口无响应（连接被重置）")
    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
    if total and len(df) < total * min_coverage:
        raise RuntimeError(f"东方财富全市场快照不完整（{len(df)}/{total}），已放弃本次结果")
    return df


def _row_of(item: dict[str, Any]) -> dict[str, Any]:
    market_flag = str(item.get("f13"))
    code = str(item.get("f12") or "")
    if market_flag == "1":
        market = "sh"
    elif code.startswith(("43", "83", "87", "88", "92")):
        market = "bj"
    else:
        market = "sz"
    return {
        "code": code,
        "name": str(item.get("f14") or ""),
        "price": clean(item.get("f2")),
        "pct": clean(item.get("f3")),
        "change": clean(item.get("f4")),
        "volume": (clean(item.get("f5")) or 0) * 100,
        "amount": clean(item.get("f6")),
        "turnover": clean(item.get("f8")),
        "pe": clean(item.get("f9")),
        "pe_ttm": clean(item.get("f115")),
        "volume_ratio": clean(item.get("f10")),
        "high": clean(item.get("f15")),
        "low": clean(item.get("f16")),
        "open": clean(item.get("f17")),
        "prev_close": clean(item.get("f18")),
        "total_mv": clean(item.get("f20")),
        "float_mv": clean(item.get("f21")),
        "pb": clean(item.get("f23")),
        "market": market,
    }


FIN_FIELDS = ",".join([
    "SECUCODE", "SECURITY_NAME_ABBR", "REPORT_DATE", "REPORT_TYPE", "REPORT_DATE_NAME",
    "EPSJB", "BPS", "TOTALOPERATEREVE", "TOTALOPERATEREVETZ",
    "PARENTNETPROFIT", "PARENTNETPROFITTZ", "ROEJQ", "XSMLL", "XSJLL",
    "ZCFZL", "MGJYXJJE", "ROIC",
])


@retry(times=2, delay=0.8)
def financials(symbol: Symbol | str, periods: int = 8, timeout: float = 15,
               min_interval_ms: int = 300) -> list[dict[str, Any]]:
    sym = Symbol.parse(symbol)
    text = http_get(DATACENTER, {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": FIN_FIELDS,
        "filter": f'(SECUCODE="{sym.em_code}")',
        "pageNumber": 1,
        "pageSize": periods,
        "sortTypes": -1,
        "sortColumns": "REPORT_DATE",
        "source": "WEB",
        "client": "WEB",
    }, timeout=timeout, min_interval_ms=min_interval_ms)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    result = payload.get("result") or {}
    data = result.get("data") or []
    out: list[dict[str, Any]] = []
    for row in data:
        out.append({
            "report_date": str(row.get("REPORT_DATE") or "")[:10],
            "report_name": row.get("REPORT_DATE_NAME") or row.get("REPORT_TYPE") or "",
            "eps": safe_float(row.get("EPSJB")),
            "bps": safe_float(row.get("BPS")),
            "revenue": safe_float(row.get("TOTALOPERATEREVE")),
            "revenue_yoy": safe_float(row.get("TOTALOPERATEREVETZ")),
            "net_profit": safe_float(row.get("PARENTNETPROFIT")),
            "net_profit_yoy": safe_float(row.get("PARENTNETPROFITTZ")),
            "roe": safe_float(row.get("ROEJQ")),
            "gross_margin": safe_float(row.get("XSMLL")),
            "net_margin": safe_float(row.get("XSJLL")),
            "debt_ratio": safe_float(row.get("ZCFZL")),
            "cash_per_share": safe_float(row.get("MGJYXJJE")),
            "roic": safe_float(row.get("ROIC")),
        })
    return out
