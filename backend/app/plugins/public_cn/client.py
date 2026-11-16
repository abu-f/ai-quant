"""公开行情 HTTP 客户端（腾讯 / 新浪）。

职责边界：只负责取数与字段搬运，不做业务判断、不读写配置。
所有对外函数返回 Python 原生结构（dict / list[tuple]），由 provider 层转 Polars。

数据来源与实测（2026-11）：
- 日K：腾讯 ``ifzq.gtimg.cn`` fqkline（``param=<code>,day,,,count,`` 不带复权参数 → 不复权原始价）
- 分钟K：新浪 ``CN_MarketDataService.getKLineData``（scale=1/5/15/30/60，最多 1023 根，含成交额）
- 实时：腾讯 ``qt.gtimg.cn`` 批量报价（单请求 800 只约 2.4s，字段含 OHLC/量额/换手）
- 标的维表：新浪 ``Market_Center.getHQNodeData``（hs_a 节点，5578 只，整轮约 20s）

单位约定：腾讯日K与报价的成交量已是「手」，新浪分钟K为「股」需 /100。
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

TX_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
TX_QUOTE = "https://qt.gtimg.cn/q="
SINA_KLINE = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
SINA_LIST = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

MINUTE_SCALE = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
QUOTE_BATCH = 800
QUOTE_WORKERS = 4
BEIJING = timezone(timedelta(hours=8))

_EXCHANGE_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
_PREFIX_EXCHANGE = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


# --------------------------------------------------------------------- 代码
def normalize_symbol(raw: str) -> str:
    """``600519`` / ``sh600519`` / ``600519.SH`` → ``600519.SH``。无法识别返回空串。"""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        code, _, exch = text.partition(".")
        if exch in _EXCHANGE_PREFIX and code.isdigit():
            return f"{code}.{exch}"
    for prefix, exch in _PREFIX_EXCHANGE.items():
        if text.startswith(prefix.upper()) and text[2:].isdigit():
            return f"{text[2:]}.{exch}"
    if text.isdigit() and len(text) == 6:
        if text[0] in "69":
            return f"{text}.SH"
        if text[0] in "48" or text.startswith("920"):
            return f"{text}.BJ"
        return f"{text}.SZ"
    return ""


def to_tencent(symbol: str) -> str:
    """``600519.SH`` → ``sh600519``（腾讯/新浪通用前缀）。"""
    norm = normalize_symbol(symbol)
    if not norm:
        return ""
    code, _, exch = norm.partition(".")
    return f"{_EXCHANGE_PREFIX.get(exch, 'sh')}{code}"


def from_tencent(code: str) -> str:
    """``sh600519`` → ``600519.SH``。"""
    text = str(code or "").strip().lower()
    if len(text) < 8:
        return ""
    prefix, digits = text[:2], text[2:]
    exch = _PREFIX_EXCHANGE.get(prefix)
    if not exch or not digits.isdigit():
        return ""
    return f"{digits}.{exch}"


def to_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def to_ms(text: str) -> int | None:
    """腾讯时间戳 ``20261116161323`` → epoch ms（北京时间）。"""
    try:
        dt = datetime.strptime(str(text), "%Y%m%d%H%M%S").replace(tzinfo=BEIJING)
    except (TypeError, ValueError):
        return None
    return int(dt.timestamp() * 1000)


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# --------------------------------------------------------------------- 日K
def fetch_daily(client: httpx.Client, symbol: str, start: date | None,
                end: date | None, count: int = 800) -> list[tuple]:
    """不复权日K：[(symbol, date, open, high, low, close, volume(手), amount)]。

    腾讯返回行序为 ``[日期, 开, 收, 高, 低, 成交量(手)]``——注意开收高低顺序与常见 OHLC 不同。
    """
    code = to_tencent(symbol)
    if not code:
        return []
    params = {"param": f"{code},day,{start.isoformat() if start else ''},"
                       f"{end.isoformat() if end else ''},{max(1, min(count, 800))},"}
    resp = client.get(TX_KLINE, params=params)
    resp.raise_for_status()
    payload = resp.json()
    node = (payload.get("data") or {}).get(code) or {}
    bars = node.get("day") or node.get("qfqday") or []
    rows: list[tuple] = []
    for bar in bars:
        if not isinstance(bar, (list, tuple)) or len(bar) < 6:
            continue
        day = to_date(bar[0])
        if day is None:
            continue
        open_, close_, high, low = _num(bar[1]), _num(bar[2]), _num(bar[3]), _num(bar[4])
        volume = _num(bar[5])
        if None in (open_, close_, high, low):
            continue
        rows.append((symbol, day, open_, high, low, close_,
                     math.floor(volume) if volume is not None else None, None))
    return rows


# --------------------------------------------------------------------- 分钟K
def fetch_minute(client: httpx.Client, symbol: str, freq: str = "1m",
                 count: int = 1023) -> list[tuple]:
    """分钟K：[(symbol, datetime(北京墙钟), open, high, low, close, volume(手), amount(元))]。"""
    scale = MINUTE_SCALE.get(freq.lower())
    code = to_tencent(symbol)
    if scale is None or not code:
        return []
    resp = client.get(SINA_KLINE, params={
        "symbol": code, "scale": scale, "ma": "no", "datalen": min(max(count, 1), 1023),
    }, headers={"Referer": "https://finance.sina.com.cn"})
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        return []
    rows: list[tuple] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            stamp = datetime.strptime(str(item.get("day")), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        open_, high, low, close_ = (_num(item.get("open")), _num(item.get("high")),
                                    _num(item.get("low")), _num(item.get("close")))
        if None in (open_, high, low, close_):
            continue
        volume = _num(item.get("volume"))
        rows.append((symbol, stamp, open_, high, low, close_,
                     math.floor(volume / 100.0) if volume is not None else None,
                     _num(item.get("amount"))))
    return rows


# --------------------------------------------------------------------- 实时
def _parse_quote_line(line: str) -> dict | None:
    """解析 ``v_sh600519="1~贵州茅台~600519~1453.90~..."``。

    字段位置（实测，1 基）：1 名称 / 2 代码 / 3 现价 / 4 昨收 / 5 今开 /
    6 成交量(手) / 30 时间戳 / 31 涨跌额 / 32 涨跌幅(%) / 33 最高 / 34 最低 /
    37 成交额(万元) / 38 换手率(%)。
    """
    if '="' not in line:
        return None
    head, _, rest = line.partition('="')
    raw_symbol = head.replace("v_", "").strip()
    symbol = from_tencent(raw_symbol)
    payload = rest.rstrip('"').rstrip(";")
    if not symbol or not payload:
        return None
    parts = payload.split("~")
    if len(parts) < 39:
        logger.debug("报价行字段不足（%d 段）：%s", len(parts), raw_symbol)
        return None

    last = _num(parts[3])
    prev = _num(parts[4])
    volume = _num(parts[6]) or _num(parts[36])
    amount_wan = _num(parts[37])
    pct = _num(parts[32])
    change = _num(parts[31])
    if change is None and last is not None and prev is not None:
        change = round(last - prev, 4)
    change_pct = pct / 100.0 if pct is not None else (
        change / prev if (change is not None and prev) else None)
    turnover = _num(parts[38])

    return {
        "symbol": symbol,
        "name": parts[1] or None,
        "last_price": last,
        "prev_close": prev,
        "open": _num(parts[5]),
        "high": _num(parts[33]),
        "low": _num(parts[34]),
        "volume": math.floor(volume) if volume is not None else None,
        "amount": round(amount_wan * 10_000, 2) if amount_wan is not None else None,
        "change_pct": change_pct,
        "change_amount": change,
        "amplitude": None,
        "turnover_rate": turnover / 100.0 if turnover is not None else None,
        "timestamp": to_ms(parts[30]),
        "session": None,
    }


def fetch_quotes(client: httpx.Client, symbols: Iterable[str]) -> list[dict]:
    """批量报价（腾讯），自动分批 + 有界并发。失败的批次记录告警但不影响其他批次。"""
    codes = [to_tencent(s) for s in symbols]
    codes = [c for c in codes if c]
    if not codes:
        return []
    batches = [codes[i:i + QUOTE_BATCH] for i in range(0, len(codes), QUOTE_BATCH)]

    def one(batch: list[str]) -> list[dict]:
        try:
            resp = client.get(TX_QUOTE + ",".join(batch),
                              headers={"Referer": "https://gu.qq.com/"})
            resp.encoding = "gbk"
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量报价失败（%d 只）：%s", len(batch), exc)
            return []
        out: list[dict] = []
        for line in resp.text.split(";"):
            line = line.strip()
            if not line:
                continue
            rec = _parse_quote_line(line)
            if rec is not None:
                out.append(rec)
        return out

    if len(batches) == 1:
        return one(batches[0])
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(QUOTE_WORKERS, len(batches))) as pool:
        for chunk in pool.map(one, batches):
            rows.extend(chunk)
    return rows


# --------------------------------------------------------------------- 标的维表
def _universe_page(client: httpx.Client, page: int) -> list[dict]:
    resp = client.get(SINA_LIST, params={
        "page": page, "num": 100, "sort": "symbol", "asc": 1,
        "node": "hs_a", "symbol": "", "_s_r_a": "page",
    }, headers={"Referer": "https://finance.sina.com.cn"})
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, list) else []


def fetch_universe(client: httpx.Client, max_pages: int = 60, workers: int = 8) -> list[dict]:
    """全市场 A 股（含北交所）标的列表 → [{symbol, name, code, exchange, ...}]。

    新浪单页 100 只、整轮约 20s；并发只略微改善（服务端单请求本身偏慢），
    因此调用方应缓存（标的同步每日一次，实时快照另有缓存）。
    """
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for page in pool.map(lambda p: _universe_page(client, p), range(1, max_pages + 1)):
            if not page:
                continue
            for item in page:
                if not isinstance(item, dict):
                    continue
                symbol = from_tencent(str(item.get("symbol") or ""))
                if not symbol:
                    continue
                code, _, exch = symbol.partition(".")
                rows.append({
                    "symbol": symbol,
                    "name": str(item.get("name") or "").strip() or None,
                    "code": code,
                    "exchange": exch,
                    "region": "CN",
                    "type": "stock",
                    "ext": {},
                })
    return rows
