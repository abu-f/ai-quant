"""选股筛选：基本面条件走全市场快照，技术条件走本地日线。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import indicators as ind
from .config import Config
from .data.provider import MarketData
from .symbols import Symbol

log = logging.getLogger("aquant.screener")


@dataclass
class Preset:
    key: str
    title: str
    description: str
    filters: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    sort_by: str = "pct"
    ascending: bool = False
    technical: str | None = None
    need_bars: bool = False


PRESETS: dict[str, Preset] = {
    "value": Preset(
        "value", "低估值", "PE(TTM) 0~25、PB 0~3、总市值 ≥ 50 亿",
        {"pe_ttm": (0.01, 25), "pb": (0.01, 3.0), "total_mv": (5e9, None)},
        sort_by="pe_ttm", ascending=True,
    ),
    "momentum": Preset(
        "momentum", "短线强势", "涨幅 ≥ 3%、换手 1%~20%、成交额 ≥ 2 亿",
        {"pct": (3, 10.5), "turnover": (1, 20), "amount": (2e8, None)},
        sort_by="pct", ascending=False,
    ),
    "volume": Preset(
        "volume", "放量异动", "量比 ≥ 2、涨幅 0~9%、成交额 ≥ 1 亿",
        {"volume_ratio": (2, None), "pct": (0, 9), "amount": (1e8, None)},
        sort_by="volume_ratio", ascending=False,
    ),
    "active": Preset(
        "active", "成交活跃", "成交额 ≥ 5 亿，按成交额排序",
        {"amount": (5e8, None)}, sort_by="amount", ascending=False,
    ),
    "oversold": Preset(
        "oversold", "技术超卖", "日线 RSI(14) < 35，且收盘价高于 5 日均线 3% 以内",
        {}, sort_by="rsi", ascending=True, technical="oversold", need_bars=True,
    ),
    "breakout": Preset(
        "breakout", "创阶段新高", "收盘价突破 20 日最高价，且成交额 ≥ 1 亿",
        {"amount": (1e8, None)}, sort_by="pct", ascending=False,
        technical="breakout", need_bars=True,
    ),
    "ma_bull": Preset(
        "ma_bull", "均线多头", "MA5 > MA10 > MA20 > MA60，且站上 MA20",
        {"amount": (5e7, None)}, sort_by="pct", ascending=False,
        technical="ma_bull", need_bars=True,
    ),
}


def apply_filters(df: pd.DataFrame, filters: dict[str, tuple[float | None, float | None]]) -> pd.DataFrame:
    out = df.copy()
    for column, (low, high) in filters.items():
        if column not in out.columns:
            continue
        series = pd.to_numeric(out[column], errors="coerce")
        if series.notna().sum() == 0:
            # 当前数据源没有这个字段（例如新浪列表没有量比），跳过而不是筛成空
            log.warning("字段 %s 在当前数据源不可用，已跳过该条件", column)
            continue
        mask = series.notna()
        if low is not None:
            mask &= series >= low
        if high is not None:
            mask &= series <= high
        out = out[mask]
    return out


def _technical_ok(df: pd.DataFrame, kind: str) -> tuple[bool, dict[str, Any]]:
    if df is None or len(df) < 65:
        return False, {}
    enriched = ind.add_indicators(df)
    last = enriched.iloc[-1]
    close = float(last["close"])
    rsi_val = float(last.get("rsi14")) if pd.notna(last.get("rsi14")) else None
    ma5, ma10, ma20, ma60 = (float(last.get(f"ma{n}")) if pd.notna(last.get(f"ma{n}")) else None
                             for n in (5, 10, 20, 60))
    info = {"rsi": rsi_val, "ma20": ma20}

    if kind == "oversold":
        if rsi_val is None or rsi_val >= 35:
            return False, info
        if ma5 and close > ma5 * 1.03:
            return False, info
        return True, info
    if kind == "breakout":
        prior_high = df["high"].shift(1).rolling(20, min_periods=20).max().iloc[-1]
        if pd.isna(prior_high):
            return False, info
        return close > float(prior_high), info
    if kind == "ma_bull":
        if None in (ma5, ma10, ma20, ma60):
            return False, info
        return bool(ma5 > ma10 > ma20 > ma60 and close > ma20), info
    return False, info


def screen(cfg: Config, md: MarketData, preset_key: str = "value", limit: int = 20,
           pool: int = 200, min_mv: float | None = None, max_pe: float | None = None,
           progress: Any = None) -> tuple[pd.DataFrame, Preset]:
    if preset_key not in PRESETS:
        raise ValueError(f"未知预设：{preset_key}，可选：{', '.join(PRESETS)}")
    preset = PRESETS[preset_key]
    snapshot = md.snapshot()
    if snapshot is None or snapshot.empty:
        raise RuntimeError("无法获取全市场快照，请检查网络或稍后重试")

    df = snapshot[snapshot["price"].notna()].copy()
    df = df[~df["name"].str.contains("退|ST", na=False)]

    filters = dict(preset.filters)
    if min_mv is not None:
        filters["total_mv"] = (min_mv * 1e8, filters.get("total_mv", (None, None))[1])
    if max_pe is not None:
        filters["pe_ttm"] = (filters.get("pe_ttm", (None, None))[0], max_pe)
    df = apply_filters(df, filters)

    if preset.need_bars:
        # 技术条件需要逐只拉日线：先用成交额缩小候选池，控制请求量
        df = df.sort_values("amount", ascending=False).head(max(pool, limit * 3))
        rows: list[dict[str, Any]] = []
        symbols = [Symbol.parse(str(c)) for c in df["code"]]
        lookup = df.set_index("code")

        def work(sym: Symbol) -> tuple[str, bool, dict[str, Any]]:
            try:
                bars = md.bars(sym, days=160)
                ok, info = _technical_ok(bars, preset.technical or "")
                return sym.code, ok, info
            except Exception as exc:
                log.debug("%s 技术筛选失败：%s", sym.tencent, exc)
                return sym.code, False, {}

        total = len(symbols)
        done = 0
        # 并发不要太高：公开接口对短时间内的大量连接比较敏感
        with ThreadPoolExecutor(max_workers=3) as pool_exec:
            futures = {pool_exec.submit(work, s): s for s in symbols}
            for future in as_completed(futures):
                code, ok, info = future.result()
                done += 1
                if progress and (done % 20 == 0 or done == total):
                    progress(done, total)
                if ok and code in lookup.index:
                    row = lookup.loc[code].to_dict()
                    row["code"] = code  # index 列不会出现在 to_dict 里，显式补回
                    row.update(info)
                    rows.append(row)
        result = pd.DataFrame(rows)
        if result.empty:
            return result, preset
        if preset.technical == "oversold":
            result = result.sort_values("rsi", ascending=True)
        else:
            result = result.sort_values("pct", ascending=False)
    else:
        sort_by = preset.sort_by if preset.sort_by in df.columns else "amount"
        result = df.sort_values(sort_by, ascending=preset.ascending)

    return result.head(limit), preset


SCREEN_COLUMNS = [
    ("code", "代码"), ("name", "名称"), ("price", "现价"), ("pct", "涨跌幅%"),
    ("pe_ttm", "PE(TTM)"), ("pb", "PB"), ("total_mv", "总市值"), ("turnover", "换手%"),
    ("volume_ratio", "量比"), ("amount", "成交额"), ("rsi", "RSI"),
]
