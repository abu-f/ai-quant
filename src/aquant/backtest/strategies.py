"""回测策略库：每个策略把 K 线转换成「目标仓位」序列（1=持有，0=空仓）。

约定：仓位由当日收盘及之前的数据决定，引擎在下一交易日开盘执行，因此不含未来函数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from .. import indicators as ind


@dataclass
class Strategy:
    name: str
    func: Callable[..., pd.Series]
    defaults: dict[str, Any] = field(default_factory=dict)
    description: str = ""


def _series(df: pd.DataFrame, cond: pd.Series) -> pd.Series:
    return cond.fillna(False).astype(int).rename("position")


def ma_cross(df: pd.DataFrame, fast: int = 5, slow: int = 20, **_: Any) -> pd.Series:
    fast = int(fast)
    slow = int(slow)
    if fast >= slow:
        raise ValueError("fast 必须小于 slow")
    return _series(df, df["close"].rolling(fast).mean() > df["close"].rolling(slow).mean())


def macd_trend(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9,
               **_: Any) -> pd.Series:
    dif, dea, _ = ind.macd(df["close"], int(fast), int(slow), int(signal))
    return _series(df, (dif > dea) & (dif > 0))


def momentum(df: pd.DataFrame, window: int = 20, threshold: float = 0.0,
             **_: Any) -> pd.Series:
    window = int(window)
    ret = df["close"] / df["close"].shift(window) - 1
    return _series(df, ret > float(threshold))


def boll_break(df: pd.DataFrame, window: int = 20, width: float = 2.0,
               **_: Any) -> pd.Series:
    up, mid, _ = ind.boll(df["close"], int(window), float(width))
    return _series(df, df["close"] > up)


def breakout(df: pd.DataFrame, window: int = 20, exit_window: int = 10,
             **_: Any) -> pd.Series:
    """唐奇安通道：突破 N 日高点入场，跌破 M 日低点离场。"""
    window, exit_window = int(window), int(exit_window)
    prior_high = df["high"].shift(1).rolling(window, min_periods=window).max()
    prior_low = df["low"].shift(1).rolling(exit_window, min_periods=exit_window).min()
    pos = np.zeros(len(df), dtype=int)
    holding = False
    close = df["close"].to_numpy()
    highs = prior_high.to_numpy()
    lows = prior_low.to_numpy()
    for i in range(len(df)):
        if not holding:
            if not np.isnan(highs[i]) and close[i] > highs[i]:
                holding = True
        else:
            if not np.isnan(lows[i]) and close[i] < lows[i]:
                holding = False
        pos[i] = 1 if holding else 0
    return pd.Series(pos, index=df.index, name="position")


def rsi_reversion(df: pd.DataFrame, window: int = 14, buy: float = 30,
                  sell: float = 70, **_: Any) -> pd.Series:
    """RSI 超卖买入、超买卖出（状态机，需要逐日推进）。"""
    window, buy, sell = int(window), float(buy), float(sell)
    values = ind.rsi(df["close"], window).to_numpy()
    pos = np.zeros(len(df), dtype=int)
    holding = False
    for i in range(len(df)):
        val = values[i]
        if not np.isnan(val):
            if not holding and val <= buy:
                holding = True
            elif holding and val >= sell:
                holding = False
        pos[i] = 1 if holding else 0
    return pd.Series(pos, index=df.index, name="position")


def kdj_cross(df: pd.DataFrame, n: int = 9, **_: Any) -> pd.Series:
    k, d, _ = ind.kdj(df, int(n))
    return _series(df, k > d)


def buy_hold(df: pd.DataFrame, **_: Any) -> pd.Series:
    return pd.Series(1, index=df.index, name="position")


STRATEGIES: dict[str, Strategy] = {
    "ma_cross": Strategy("ma_cross", ma_cross, {"fast": 5, "slow": 20},
                         "双均线：快线上穿慢线持有，下穿离场"),
    "macd": Strategy("macd", macd_trend, {"fast": 12, "slow": 26, "signal": 9},
                     "MACD：DIF 在 DEA 上方且为正时持有"),
    "momentum": Strategy("momentum", momentum, {"window": 20, "threshold": 0.0},
                         "动量：N 日涨幅为正时持有"),
    "boll_break": Strategy("boll_break", boll_break, {"window": 20, "width": 2.0},
                           "布林带：收盘突破上轨持有"),
    "breakout": Strategy("breakout", breakout, {"window": 20, "exit_window": 10},
                         "通道突破：突破 N 日高点入场，跌破 M 日低点离场"),
    "rsi_reversion": Strategy("rsi_reversion", rsi_reversion,
                              {"window": 14, "buy": 30, "sell": 70},
                              "RSI 均值回归：超卖买入、超买卖出"),
    "kdj": Strategy("kdj", kdj_cross, {"n": 9}, "KDJ：K 在 D 上方时持有"),
    "buy_hold": Strategy("buy_hold", buy_hold, {}, "买入持有（基准对照）"),
}


def build_position(df: pd.DataFrame, name: str, **params: Any) -> pd.Series:
    if name not in STRATEGIES:
        raise ValueError(f"未知策略：{name}，可选：{', '.join(STRATEGIES)}")
    strategy = STRATEGIES[name]
    merged = {**strategy.defaults, **{k: v for k, v in params.items() if v is not None}}
    return strategy.func(df, **merged)
