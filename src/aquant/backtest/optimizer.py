"""参数寻优与 Walk-forward 验证。

为什么需要这一层：
    单次「调好参数再回测」几乎必然高估收益——你在同一段历史上反复挑选参数，
    等于偷看了答案。这里提供两条防线：

1. Walk-forward：把历史切成若干折，每折只在训练段挑参数，在紧邻的测试段做样本外
   (OOS) 回测，滚动前移，把各折 OOS 净值拼接。样本内漂亮、样本外崩溃，就是过拟合。
2. Deflated Sharpe Ratio (DSR)：搜索了 N 组参数后，最高夏普天然偏高。DSR 用试验次数、
   收益偏度/峰度校正，回答「这个夏普有多少概率是真本事」。

参考思路来自开源项目 shy3130/tick-stock-panel 的 walk-forward 与 metrics_v2 设计
（MIT 协议），本项目为独立实现，未复制其源码。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from .engine import BacktestConfig, BacktestResult, run_single
from .strategies import STRATEGIES, build_position

OBJECTIVES = {
    "sharpe": "夏普比率",
    "cagr": "年化收益%",
    "total_return": "累计收益%",
    "calmar": "Calmar",
    "excess_return": "超额收益%",
}


# --------------------------------------------------------------------- 参数网格
def parse_grid(spec: str) -> dict[str, list[Any]]:
    """解析 "fast=3,5,8;slow=10,20,40" → {"fast": [3,5,8], "slow": [10,20,40]}。"""
    if not spec or not spec.strip():
        raise ValueError("参数网格为空，示例：--optimize \"fast=3,5,8;slow=10,20,40\"")
    grid: dict[str, list[Any]] = {}
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"参数片段格式错误：{chunk}（应为 key=v1,v2）")
        key, _, values = chunk.partition("=")
        key = key.strip()
        parsed: list[Any] = []
        for raw in values.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed.append(int(raw))
            except ValueError:
                try:
                    parsed.append(float(raw))
                except ValueError:
                    parsed.append(raw)
        if not key or not parsed:
            raise ValueError(f"参数片段格式错误：{chunk}")
        grid[key] = parsed
    if not grid:
        raise ValueError("参数网格为空")
    return grid


def grid_size(grid: dict[str, Sequence[Any]]) -> int:
    return int(np.prod([len(v) for v in grid.values()])) if grid else 0


def valid_params(strategy: str, params: dict[str, Any]) -> bool:
    """过滤明显非法的组合（如 fast >= slow），避免浪费回测。"""
    if strategy == "ma_cross":
        fast, slow = params.get("fast"), params.get("slow")
        if fast is not None and slow is not None and int(fast) >= int(slow):
            return False
    if strategy == "rsi_reversion":
        buy, sell = params.get("buy"), params.get("sell")
        if buy is not None and sell is not None and float(buy) >= float(sell):
            return False
    return True


# --------------------------------------------------------------------- 网格搜索
@dataclass
class Trial:
    params: dict[str, Any]
    score: float
    metrics: dict[str, Any]
    result: BacktestResult | None = field(default=None, repr=False)


def grid_search(df: pd.DataFrame, strategy: str, grid: dict[str, Sequence[Any]],
                cfg: BacktestConfig | None = None, objective: str = "sharpe",
                min_trades: int = 1, code: str = "") -> list[Trial]:
    """穷举参数组合，按目标指标降序返回。"""
    if objective not in OBJECTIVES:
        raise ValueError(f"未知目标 {objective}，可选：{', '.join(OBJECTIVES)}")
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略：{strategy}")

    keys = list(grid.keys())
    trials: list[Trial] = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        if not valid_params(strategy, params):
            continue
        try:
            position = build_position(df, strategy, **params)
            result = run_single(df, position, code or "backtest", strategy, params, cfg)
        except Exception:  # 参数组合不可用（数据不足等）直接跳过
            continue
        metrics = result.metrics
        if metrics.get("trades", 0) < min_trades:
            continue
        score = metrics.get(objective)
        if score is None or not np.isfinite(score):
            continue
        trials.append(Trial(params=params, score=float(score), metrics=metrics, result=result))

    trials.sort(key=lambda t: t.score, reverse=True)
    return trials


# --------------------------------------------------------------------- Walk-forward
@dataclass
class Fold:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_params: dict[str, Any] = field(default_factory=dict)
    is_metrics: dict[str, Any] = field(default_factory=dict)
    oos_metrics: dict[str, Any] = field(default_factory=dict)
    degradation: float | None = None  # 样本外夏普 - 样本内夏普


@dataclass
class WalkForwardResult:
    folds: list[Fold]
    equity: pd.Series
    oos_metrics: dict[str, Any]
    is_mean_sharpe: float
    oos_mean_sharpe: float
    consistency: float  # OOS 夏普 > 0 的折数占比


def walk_forward(df: pd.DataFrame, strategy: str, grid: dict[str, Sequence[Any]],
                 cfg: BacktestConfig | None = None, train_days: int = 250,
                 test_days: int = 60, step_days: int = 60, objective: str = "sharpe",
                 min_trades: int = 1, code: str = "") -> WalkForwardResult:
    """滚动窗口：训练段选参 → 紧邻测试段做 OOS，拼接各折净值。"""
    if train_days < 30 or test_days < 10 or step_days < 10:
        raise ValueError("train_days/test_days/step_days 太小（建议 250/60/60）")
    if len(df) < train_days + test_days:
        raise ValueError(
            f"数据不足：{len(df)} 根，至少需要 train({train_days}) + test({test_days}) 根；"
            f"可用 --days 拉长区间"
        )

    folds: list[Fold] = []
    equity_parts: list[pd.Series] = []
    prev_end = float(cfg.initial_capital) if cfg else 100_000.0
    total = len(df)
    start_idx = 0
    index = 0

    while start_idx + train_days + test_days <= total:
        train = df.iloc[start_idx: start_idx + train_days]
        test = df.iloc[start_idx + train_days: start_idx + train_days + test_days]
        start_idx += step_days
        index += 1

        train_trials = grid_search(train, strategy, grid, cfg, objective, min_trades, code)
        if not train_trials:
            folds.append(Fold(
                index=index,
                train_start=train.index[0].strftime("%Y-%m-%d"),
                train_end=train.index[-1].strftime("%Y-%m-%d"),
                test_start=test.index[0].strftime("%Y-%m-%d"),
                test_end=test.index[-1].strftime("%Y-%m-%d"),
            ))
            continue

        best = train_trials[0]
        try:
            position = build_position(test, strategy, **best.params)
            oos = run_single(test, position, code or "backtest", strategy, best.params, cfg)
        except Exception:
            continue

        # 各折资金衔接：把本折净值按上一折期末资金缩放
        scaled = oos.equity / float(oos.equity.iloc[0]) * prev_end
        equity_parts.append(scaled)
        prev_end = float(scaled.iloc[-1])

        is_sharpe = float(best.metrics.get("sharpe") or 0.0)
        oos_sharpe = float(oos.metrics.get("sharpe") or 0.0)
        folds.append(Fold(
            index=index,
            train_start=train.index[0].strftime("%Y-%m-%d"),
            train_end=train.index[-1].strftime("%Y-%m-%d"),
            test_start=test.index[0].strftime("%Y-%m-%d"),
            test_end=test.index[-1].strftime("%Y-%m-%d"),
            best_params=best.params,
            is_metrics=best.metrics,
            oos_metrics=oos.metrics,
            degradation=round(oos_sharpe - is_sharpe, 2),
        ))

    if not equity_parts:
        raise ValueError("没有成功完成的折，请放宽参数或缩短窗口")

    equity = pd.concat(equity_parts).sort_index()
    equity = equity[~equity.index.duplicated(keep="last")]
    from .engine import compute_metrics

    oos_metrics = compute_metrics(equity, equity, [], cfg or BacktestConfig())
    oos_metrics["benchmark_return"] = round(
        (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100, 2)
    oos_metrics["excess_return"] = round(oos_metrics["total_return"] - oos_metrics["benchmark_return"], 2)

    done = [f for f in folds if f.is_metrics]
    is_mean = float(np.mean([f.is_metrics.get("sharpe", 0) for f in done])) if done else 0.0
    oos_mean = float(np.mean([f.oos_metrics.get("sharpe", 0) for f in done])) if done else 0.0
    positive = sum(1 for f in done if (f.oos_metrics.get("sharpe") or 0) > 0)
    consistency = positive / len(done) if done else 0.0

    return WalkForwardResult(
        folds=folds, equity=equity, oos_metrics=oos_metrics,
        is_mean_sharpe=round(is_mean, 2), oos_mean_sharpe=round(oos_mean, 2),
        consistency=round(consistency, 3),
    )


# --------------------------------------------------------------------- 统计校正
EULER_GAMMA = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 有理逼近，误差 < 1.2e-9），避免引入 scipy。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必须在 (0,1) 内")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(returns: Iterable[float], trial_sharpes: Sequence[float],
                    annualize: float = 252.0) -> dict[str, float] | None:
    """Deflated Sharpe Ratio：校正「搜索了 N 组参数」带来的夏普虚高。

    `returns` 为逐期（日）收益；`trial_sharpes` 为**年化**夏普（本项目 metrics 口径），
    内部会除以 sqrt(annualize) 换算成与 `returns` 一致的逐期尺度，避免量纲错配。
    返回 DSR（0~1，越大越可信）、期望最大夏普门槛 SR0 与逐期夏普。
    """
    arr = np.asarray([r for r in returns if r is not None and np.isfinite(r)], dtype=float)
    n = arr.size
    raw_sharpes = np.asarray([s for s in trial_sharpes if s is not None and np.isfinite(s)], dtype=float)
    if n < 30 or raw_sharpes.size < 2:
        return None

    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    if std <= 0:
        return None
    sr = mean / std  # 逐期（未年化）夏普
    skew = float(pd.Series(arr).skew())
    kurt = float(pd.Series(arr).kurt()) + 3.0  # 转成非超额峰度

    sharpes = raw_sharpes / math.sqrt(max(annualize, 1.0))  # 年化 -> 逐期
    trials = sharpes.size
    var_sr = float(sharpes.var(ddof=1))
    if var_sr <= 0:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var_sr) * (
            (1 - EULER_GAMMA) * _norm_ppf(1 - 1 / trials)
            + EULER_GAMMA * _norm_ppf(1 - 1 / (trials * math.e))
        )

    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    z = (sr - sr0) * math.sqrt(max(n - 1, 1)) / math.sqrt(denom)
    return {
        "dsr": round(_norm_cdf(z), 4),
        "sr0": round(sr0, 4),
        "sr": round(sr, 4),
        "trials": float(trials),
    }
