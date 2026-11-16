"""技术指标（纯 pandas 实现，不依赖 TA-Lib）。

输入约定：DataFrame 以日期为索引，包含 open/high/low/close/volume 列。
所有指标只使用当日及之前的数据，不存在未来函数。
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd


# --------------------------------------------------------------------- 基础
def ma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(span=window, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif.rename("dif"), dea.rename("dea"), hist.rename("macd")


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100).where(avg_gain.notna(), np.nan).rename(f"rsi{window}")


def kdj(df: pd.DataFrame, n: int = 9, k_period: int = 3,
        d_period: int = 3) -> tuple[pd.Series, pd.Series, pd.Series]:
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / k_period, adjust=False).mean()
    d = k.ewm(alpha=1 / d_period, adjust=False).mean()
    j = 3 * k - 2 * d
    return k.rename("k"), d.rename("d"), j.rename("j")


def boll(close: pd.Series, window: int = 20,
         width: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = ma(close, window)
    std = close.rolling(window, min_periods=window).std(ddof=0)
    return (mid + width * std).rename("boll_up"), mid.rename("boll_mid"), (mid - width * std).rename("boll_low")


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean().rename(f"atr{window}")


def annualized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    return (np.log(close / close.shift(1)).rolling(window).std() * np.sqrt(252)).rename("vol20")


# --------------------------------------------------------------------- 组合
def add_indicators(df: pd.DataFrame, ma_windows: Iterable[int] = (5, 10, 20, 60),
                   macd_params: tuple[int, int, int] = (12, 26, 9),
                   rsi_window: int = 14, boll_window: int = 20,
                   atr_window: int = 14) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    for window in ma_windows:
        out[f"ma{window}"] = ma(out["close"], window)
    dif, dea, hist = macd(out["close"], *macd_params)
    out["dif"], out["dea"], out["macd"] = dif, dea, hist
    out[f"rsi{rsi_window}"] = rsi(out["close"], rsi_window)
    k, d, j = kdj(out)
    out["k"], out["d"], out["j"] = k, d, j
    up, mid, low = boll(out["close"], boll_window)
    out["boll_up"], out["boll_mid"], out["boll_low"] = up, mid, low
    out[f"atr{atr_window}"] = atr(out, atr_window)
    out["vol_ma5"] = ma(out["volume"], 5)
    out["vol_ma10"] = ma(out["volume"], 10)
    out["pct"] = out["close"].pct_change() * 100
    out["high20"] = out["high"].rolling(20, min_periods=1).max()
    out["low20"] = out["low"].rolling(20, min_periods=1).min()
    out["high60"] = out["high"].rolling(60, min_periods=1).max()
    return out


def cross_up(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """fast 上穿 slow（当日交叉）。"""
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def cross_down(fast: pd.Series, slow: pd.Series) -> pd.Series:
    return (fast < slow) & (fast.shift(1) >= slow.shift(1))


def _val(row: pd.Series, key: str) -> float | None:
    if key not in row:
        return None
    value = row[key]
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latest_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    """取最后一根 K 线的指标快照 + 人类可读的状态描述。"""
    if df is None or df.empty:
        return {}
    enriched = add_indicators(df)
    row = enriched.iloc[-1]
    prev = enriched.iloc[-2] if len(enriched) >= 2 else row
    close = _val(row, "close")
    snap: dict[str, Any] = {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": close,
        "pct": _val(row, "pct"),
        "ma5": _val(row, "ma5"), "ma10": _val(row, "ma10"),
        "ma20": _val(row, "ma20"), "ma60": _val(row, "ma60"),
        "dif": _val(row, "dif"), "dea": _val(row, "dea"), "macd": _val(row, "macd"),
        "rsi": _val(row, "rsi14"),
        "k": _val(row, "k"), "d": _val(row, "d"), "j": _val(row, "j"),
        "boll_up": _val(row, "boll_up"), "boll_mid": _val(row, "boll_mid"),
        "boll_low": _val(row, "boll_low"),
        "atr": _val(row, "atr14"),
        "vol_ratio5": (_val(row, "volume") / _val(row, "vol_ma5")) if _val(row, "vol_ma5") else None,
        "high20": _val(row, "high20"), "low20": _val(row, "low20"),
        "high60": _val(row, "high60"),
        "signals": [],
    }
    signals: list[str] = []
    ma5, ma10, ma20, ma60 = snap["ma5"], snap["ma10"], snap["ma20"], snap["ma60"]
    if all(v is not None for v in (ma5, ma10, ma20, ma60)):
        if ma5 > ma10 > ma20 > ma60:  # type: ignore[operator]
            signals.append("均线多头排列")
        elif ma5 < ma10 < ma20 < ma60:  # type: ignore[operator]
            signals.append("均线空头排列")
    if all(v is not None for v in (snap["dif"], snap["dea"])):
        dif_s, dea_s = enriched["dif"], enriched["dea"]
        if bool(cross_up(dif_s, dea_s).iloc[-1]):
            signals.append("MACD 金叉")
        elif bool(cross_down(dif_s, dea_s).iloc[-1]):
            signals.append("MACD 死叉")
        elif snap["dif"] > snap["dea"]:  # type: ignore[operator]
            signals.append("MACD 多头")
        else:
            signals.append("MACD 空头")
    rsi_v = snap["rsi"]
    if rsi_v is not None:
        if rsi_v >= 70:
            signals.append(f"RSI 超买({rsi_v:.1f})")
        elif rsi_v <= 30:
            signals.append(f"RSI 超卖({rsi_v:.1f})")
    if all(v is not None for v in (snap["k"], snap["d"])):
        if bool(cross_up(enriched["k"], enriched["d"]).iloc[-1]):
            signals.append("KDJ 金叉")
        elif bool(cross_down(enriched["k"], enriched["d"]).iloc[-1]):
            signals.append("KDJ 死叉")
    if close is not None and snap["high60"]:
        drawdown = (close / snap["high60"] - 1) * 100
        snap["drawdown_from_high60"] = drawdown
        if drawdown > -3:
            signals.append("接近 60 日高点")
    if close is not None and snap["boll_up"] and snap["boll_low"]:
        if close >= snap["boll_up"]:
            signals.append("触及布林上轨")
        elif close <= snap["boll_low"]:
            signals.append("触及布林下轨")
    ratio = snap["vol_ratio5"]
    if ratio and ratio >= 2:
        signals.append(f"放量({ratio:.1f}倍)")
    snap["signals"] = signals
    return snap
