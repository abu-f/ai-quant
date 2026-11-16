"""向量化 + 逐日撮合的回测引擎（仅用于研究，不产生任何交易指令）。

成本模型：
    佣金 双向，默认万 2.5，最低 5 元
    印花税 仅卖出，默认万 5
    过户费 双向，默认万 0.1
    滑点 单边，默认 2bp
交易约束：
    T+1（信号在收盘确定，次日开盘成交，天然满足）
    100 股整数倍
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..utils import fmt_num


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage_bps: float = 2.0
    risk_free_rate: float = 0.02
    lot_size: int = 100

    @classmethod
    def from_config(cls, cfg: Any) -> "BacktestConfig":
        return cls(
            initial_capital=float(cfg.get("backtest.initial_capital", 100_000)),
            commission_rate=float(cfg.get("backtest.commission_rate", 0.00025)),
            min_commission=float(cfg.get("backtest.min_commission", 5)),
            stamp_tax=float(cfg.get("backtest.stamp_tax", 0.0005)),
            transfer_fee=float(cfg.get("backtest.transfer_fee", 0.00001)),
            slippage_bps=float(cfg.get("backtest.slippage_bps", 2)),
            risk_free_rate=float(cfg.get("backtest.risk_free_rate", 0.02)),
            lot_size=int(cfg.get("backtest.lot_size", 100)),
        )


@dataclass
class Trade:
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    hold_days: int
    cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_date": self.entry_date, "exit_date": self.exit_date,
            "entry_price": round(self.entry_price, 3), "exit_price": round(self.exit_price, 3),
            "shares": self.shares, "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct * 100, 2), "hold_days": self.hold_days,
        }


@dataclass
class BacktestResult:
    code: str
    strategy: str
    params: dict[str, Any]
    equity: pd.Series
    benchmark: pd.Series
    trades: list[Trade]
    metrics: dict[str, Any]
    position: pd.Series = field(default_factory=lambda: pd.Series(dtype=int))
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "strategy": self.strategy, "params": self.params,
            "metrics": self.metrics,
            "warnings": self.warnings,
            "trades": [t.to_dict() for t in self.trades],
            "equity": [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
                       for d, v in self.equity.items()],
            "benchmark": [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
                          for d, v in self.benchmark.items()],
        }


# ------------------------------------------------------------------ 撮合
def _buy_cost(shares: int, price: float, cfg: BacktestConfig) -> float:
    amount = shares * price
    commission = max(amount * cfg.commission_rate, cfg.min_commission)
    transfer = amount * cfg.transfer_fee
    return amount + commission + transfer


def run_single(df: pd.DataFrame, position: pd.Series, code: str, strategy: str,
               params: dict[str, Any] | None = None,
               cfg: BacktestConfig | None = None) -> BacktestResult:
    cfg = cfg or BacktestConfig()
    if df is None or df.empty:
        raise ValueError("回测数据为空")
    df = df.dropna(subset=["open", "close"]).sort_index()
    if len(df) < 3:
        raise ValueError("回测数据不足（至少 3 个交易日）")

    pos = position.reindex(df.index).fillna(0).astype(int)
    exec_pos = pos.shift(1).fillna(0).astype(int)  # 次日开盘执行

    slip = cfg.slippage_bps / 10000.0
    cash = float(cfg.initial_capital)
    shares = 0
    entry_price = 0.0
    entry_date = ""
    entry_cash_out = 0.0
    trades: list[Trade] = []
    equity_values: list[float] = []
    holding_days = 0
    warnings: list[str] = []
    capital_short = False

    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    wants = exec_pos.to_numpy(dtype=int)
    dates = df.index

    for i in range(len(df)):
        price_open = opens[i]
        want = wants[i]

        if want and shares == 0 and price_open > 0:
            fill = price_open * (1 + slip)
            budget = cash
            lots = int(budget // (fill * cfg.lot_size))
            lots = max(lots, 0)
            while lots > 0 and _buy_cost(lots * cfg.lot_size, fill, cfg) > cash:
                lots -= 1
            if lots > 0:
                qty = lots * cfg.lot_size
                cost = _buy_cost(qty, fill, cfg)
                cash -= cost
                shares = qty
                entry_price = fill
                entry_date = dates[i].strftime("%Y-%m-%d")
                entry_cash_out = cost
            else:
                capital_short = True
        elif not want and shares > 0 and price_open > 0:
            fill = price_open * (1 - slip)
            amount = shares * fill
            commission = max(amount * cfg.commission_rate, cfg.min_commission)
            stamp = amount * cfg.stamp_tax
            transfer = amount * cfg.transfer_fee
            proceeds = amount - commission - stamp - transfer
            cash += proceeds
            pnl = proceeds - entry_cash_out
            hold_days = (dates[i] - pd.Timestamp(entry_date)).days if entry_date else 0
            trades.append(Trade(
                entry_date=entry_date, exit_date=dates[i].strftime("%Y-%m-%d"),
                entry_price=entry_price, exit_price=fill, shares=shares,
                pnl=pnl, pnl_pct=pnl / entry_cash_out if entry_cash_out else 0.0,
                hold_days=hold_days,
                cost=(entry_cash_out - shares * entry_price) + (amount - proceeds),
            ))
            shares = 0
            entry_price = 0.0
            entry_date = ""
            entry_cash_out = 0.0

        if shares > 0:
            holding_days += 1
        equity_values.append(cash + shares * closes[i])

    equity = pd.Series(equity_values, index=df.index, name="equity")
    if capital_short:
        min_lot_cost = float(df["open"].max()) * cfg.lot_size
        warnings.append(
            f"初始资金 {cfg.initial_capital:,.0f} 元不足以买入 1 手（100 股），"
            f"期间最高价需约 {min_lot_cost:,.0f} 元，故未产生交易；"
            f"可用 --capital 提高初始资金后重试。"
        )
    benchmark = df["close"] / df["close"].iloc[0] * cfg.initial_capital
    metrics = compute_metrics(equity, benchmark, trades, cfg,
                              exposure=holding_days / len(df))
    return BacktestResult(
        code=code, strategy=strategy, params=params or {},
        equity=equity, benchmark=benchmark, trades=trades,
        metrics=metrics, position=pos, warnings=warnings,
    )


# ------------------------------------------------------------------ 组合
def run_portfolio(data: dict[str, tuple[pd.DataFrame, pd.Series]], strategy: str,
                  params: dict[str, Any] | None = None,
                  cfg: BacktestConfig | None = None) -> BacktestResult:
    """等权资金分配的多标的回测（每只标的独立撮合，权益相加）。"""
    cfg = cfg or BacktestConfig()
    if not data:
        raise ValueError("没有可用于回测的标的")
    per_capital = cfg.initial_capital / len(data)
    sub_cfg = BacktestConfig(**{**cfg.__dict__, "initial_capital": per_capital})
    results = [run_single(df, pos, code, strategy, params, sub_cfg)
               for code, (df, pos) in data.items()]

    all_index = sorted(set().union(*[r.equity.index for r in results]))
    combined = pd.Series(0.0, index=pd.DatetimeIndex(all_index))
    bench = pd.Series(0.0, index=pd.DatetimeIndex(all_index))
    for res in results:
        combined = combined.add(res.equity.reindex(combined.index).ffill().fillna(per_capital), fill_value=0)
        bench = bench.add(res.benchmark.reindex(combined.index).ffill().fillna(per_capital), fill_value=0)

    trades = [t for r in results for t in r.trades]
    warnings = [f"{r.code}: {w}" for r in results for w in r.warnings]
    metrics = compute_metrics(combined, bench, trades, cfg,
                              exposure=float(np.mean([r.metrics["exposure"] for r in results])))
    return BacktestResult(
        code="+".join(sorted(data.keys()))[:40], strategy=strategy, params=params or {},
        equity=combined, benchmark=bench, trades=trades, metrics=metrics,
        warnings=warnings,
    )


# ------------------------------------------------------------------ 指标
def compute_metrics(equity: pd.Series, benchmark: pd.Series, trades: list[Trade],
                    cfg: BacktestConfig, exposure: float = 0.0) -> dict[str, Any]:
    equity = equity.dropna()
    if equity.empty:
        return {}
    start_value = float(equity.iloc[0]) or cfg.initial_capital
    end_value = float(equity.iloc[-1])
    total_return = end_value / start_value - 1

    daily = equity.pct_change().dropna()
    days = max(len(equity), 1)
    years = days / 252.0
    cagr = (end_value / start_value) ** (1 / years) - 1 if years > 0 and start_value > 0 else 0.0
    ann_vol = float(daily.std(ddof=0) * math.sqrt(252)) if len(daily) > 1 else 0.0
    rf_daily = cfg.risk_free_rate / 252
    excess = daily - rf_daily
    sharpe = float(excess.mean() / daily.std(ddof=0) * math.sqrt(252)) if daily.std(ddof=0) > 0 else 0.0

    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    max_dd = float(drawdown.min())
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else 0.0

    # 最大回撤持续天数
    dd_duration = 0
    current = 0
    for value in drawdown:
        if value < 0:
            current += 1
            dd_duration = max(dd_duration, current)
        else:
            current = 0

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win = float(np.mean([t.pnl_pct for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_hold = float(np.mean([t.hold_days for t in trades])) if trades else 0.0

    bench_return = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1) if len(benchmark) > 1 else 0.0

    return {
        "start": equity.index[0].strftime("%Y-%m-%d"),
        "end": equity.index[-1].strftime("%Y-%m-%d"),
        "days": days,
        "initial": round(start_value, 2),
        "final": round(end_value, 2),
        "total_return": round(total_return * 100, 2),
        "cagr": round(cagr * 100, 2),
        "ann_vol": round(ann_vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd * 100, 2),
        "max_dd_days": dd_duration,
        "calmar": round(calmar, 2),
        "trades": len(trades),
        "win_rate": round(win_rate * 100, 2),
        "avg_win": round(avg_win * 100, 2),
        "avg_loss": round(avg_loss * 100, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "avg_hold_days": round(avg_hold, 1),
        "exposure": round(exposure * 100, 2),
        "benchmark_return": round(bench_return * 100, 2),
        "excess_return": round((total_return - bench_return) * 100, 2),
    }


def metrics_lines(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    """把指标整理成 (名称, 值) 列表，便于终端/网页展示。"""
    if not metrics:
        return []
    pf = metrics.get("profit_factor")
    return [
        ("回测区间", f"{metrics['start']} ~ {metrics['end']}（{metrics['days']} 个交易日）"),
        ("期初 / 期末资金", f"{fmt_num(metrics['initial'])} → {fmt_num(metrics['final'])} 元"),
        ("累计收益", f"{metrics['total_return']:+.2f}%"),
        ("年化收益", f"{metrics['cagr']:+.2f}%"),
        ("年化波动", f"{metrics['ann_vol']:.2f}%"),
        ("夏普比率", f"{metrics['sharpe']:.2f}"),
        ("最大回撤", f"{metrics['max_drawdown']:.2f}%（持续 {metrics['max_dd_days']} 天）"),
        ("Calmar", f"{metrics['calmar']:.2f}"),
        ("交易次数", f"{metrics['trades']}"),
        ("胜率", f"{metrics['win_rate']:.2f}%"),
        ("平均盈利 / 亏损", f"{metrics['avg_win']:+.2f}% / {metrics['avg_loss']:+.2f}%"),
        ("盈亏比", f"{pf:.2f}" if pf is not None else "∞"),
        ("平均持仓", f"{metrics['avg_hold_days']:.1f} 天"),
        ("持仓时间占比", f"{metrics['exposure']:.1f}%"),
        ("同期买入持有", f"{metrics['benchmark_return']:+.2f}%"),
        ("超额收益", f"{metrics['excess_return']:+.2f}%"),
    ]
