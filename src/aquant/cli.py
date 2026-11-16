"""命令行入口：python -m aquant <command> / ./quant <command>。

所有命令都是「读数据 + 本地计算 + 展示」，没有任何下单、委托、交易接口。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__, indicators as ind
from .alerts import RULE_TYPES, check_watchlist, notify, validate_rule
from .backtest import STRATEGIES, BacktestConfig, build_position, run_portfolio, run_single
from .backtest.engine import metrics_lines
from .backtest.optimizer import (OBJECTIVES, deflated_sharpe, grid_search, grid_size,
                                 parse_grid, walk_forward)
from .config import PROJECT_ROOT, Config, load_config
from .data import INTRADAY_PERIODS, MarketData
from .report import build_daily_report
from .screener import PRESETS, SCREEN_COLUMNS, screen
from .store import Store
from .symbols import Symbol, normalize_codes
from .utils import fmt_big, fmt_num, fmt_pct, fmt_price, now, safe_float

log = logging.getLogger("aquant")
console = Console()
# 输出被重定向（非终端）时 Rich 默认按 80 列排版，会把表格挤成省略号；
# 这里给一个最小宽度，保证列内容完整，必要时可设 AQUANT_WIDTH 覆盖。
if console.width < 110:
    console.width = 110


# --------------------------------------------------------------------- 公共
class Ctx:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.md = MarketData(cfg, self.store)

    def codes_from_args(self, args: argparse.Namespace, default_watchlist: bool = True) -> list[str]:
        if getattr(args, "codes", None):
            return list(args.codes)
        if default_watchlist:
            items = self.cfg.watchlist()
            if items:
                return [i["code"] for i in items]
        return []


def _setup_logging(verbose: bool, cfg: Config) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(cfg.log_dir / "aquant.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def sparkline(values: Sequence[float], width: int = 60) -> str:
    vals = [v for v in values if v == v]
    if len(vals) < 2:
        return ""
    if len(vals) > width:
        step = len(vals) / width
        vals = [vals[min(int(i * step), len(vals) - 1)] for i in range(width)]
    low, high = min(vals), max(vals)
    if high == low:
        return "▄" * len(vals)
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(int((v - low) / (high - low) * (len(blocks) - 1)), len(blocks) - 1)]
                   for v in vals)


def _pct_style(value: float | None) -> str:
    """A 股习惯：涨红跌绿。"""
    if value is None:
        return "white"
    return "red" if value > 0 else ("green" if value < 0 else "white")


def _kv_table(title: str, pairs: list[tuple[str, str]]) -> Table:
    table = Table(title=title, title_justify="left", show_header=False, box=None, pad_edge=False)
    table.add_column("指标", style="cyan", no_wrap=True)
    table.add_column("数值", justify="right")
    for key, value in pairs:
        table.add_row(key, value)
    return table


def _save_csv(df: pd.DataFrame, path: str) -> None:
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, encoding="utf-8-sig")
    console.print(f"[green]已导出[/green] {len(df)} 行 → {out}")


# --------------------------------------------------------------------- init
def cmd_init(ctx: Ctx, args: argparse.Namespace) -> int:
    cfg = ctx.cfg
    console.print(Panel.fit(
        f"[bold]aquant {__version__}[/bold] 初始化完成\n"
        f"配置：{cfg.path}\n自选股：{cfg.watchlist_path}\n"
        f"数据目录：{cfg.home}\n数据库：{cfg.db_path}",
        title="初始化", border_style="green",
    ))
    if not cfg.watchlist_path.exists():
        cfg.save_watchlist([{"code": "600519", "name": "贵州茅台", "rules": [
            {"type": "pct_change_below", "value": -3},
            {"type": "ma_cross", "fast": 5, "slow": 20, "direction": "up"},
        ]}])
        console.print(f"[yellow]已生成示例自选股文件：{cfg.watchlist_path}[/yellow]")
    stats = ctx.store.stats()
    console.print(f"本地日线 {stats['bars']} 条 / {stats['codes']} 只，库大小 {stats['size_mb']} MB")
    console.print("下一步：[bold]./quant sync[/bold] 下载自选股历史数据")
    return 0


# --------------------------------------------------------------------- doctor
def cmd_doctor(ctx: Ctx, args: argparse.Namespace) -> int:
    from .data import eastmoney, sina, tencent

    table = Table(title="环境自检", title_justify="left")
    table.add_column("项目", style="cyan")
    table.add_column("结果")
    table.add_column("说明", style="dim")

    table.add_row("Python", sys.version.split()[0], sys.executable)
    try:
        import pandas as pd  # noqa: F401
        import numpy as np
        table.add_row("依赖", "OK", f"pandas {pd.__version__} / numpy {np.__version__}")
    except ImportError as exc:
        table.add_row("依赖", "[red]缺失[/red]", str(exc))

    table.add_row("数据目录", "OK" if ctx.cfg.home.exists() else "创建", str(ctx.cfg.home))
    try:
        ctx.store.set_meta("doctor", now().isoformat(timespec="seconds"))
        table.add_row("数据库", "[green]可写[/green]", str(ctx.store.path))
    except Exception as exc:
        table.add_row("数据库", "[red]不可写[/red]", str(exc))

    sym = Symbol.parse("600519")
    checks = [
        ("腾讯 K 线", lambda: tencent.daily(sym, count=5, timeout=8, min_interval_ms=0)),
        ("新浪 K 线", lambda: sina.daily(sym, count=5, timeout=8, min_interval_ms=0)),
        ("新浪分钟线", lambda: sina.intraday(sym, "5m", count=5, timeout=8, min_interval_ms=0)),
        ("腾讯分钟线", lambda: tencent.intraday(sym, "5m", count=5, timeout=8, min_interval_ms=0)),
        ("东财快照", lambda: eastmoney.quote(sym, timeout=8, min_interval_ms=0)),
    ]
    for label, func in checks:
        start = time.perf_counter()
        try:
            result = func()
            empty = getattr(result, "empty", False)
            ok = not empty
            cost = max(0.0, (time.perf_counter() - start) * 1000)
            table.add_row(label, "[green]可用[/green]" if ok else "[yellow]空数据[/yellow]",
                          f"{cost:.0f} ms")
        except Exception as exc:
            table.add_row(label, "[red]失败[/red]", str(exc)[:80])

    stats = ctx.store.stats()
    table.add_row("本地缓存", f"{stats['codes']} 只", f"{stats['bars']:,} 条日线")
    table.add_row("时区", now().strftime("%Z") or "-", now().strftime("%Y-%m-%d %H:%M:%S"))
    console.print(table)
    console.print("[dim]提示：数据源均为公开行情接口，偶发失败属正常，工具会自动回退到本地缓存。[/dim]")
    return 0


# --------------------------------------------------------------------- sync
def cmd_sync(ctx: Ctx, args: argparse.Namespace) -> int:
    codes = ctx.codes_from_args(args)
    if getattr(args, "all", False):
        cached = ctx.store.cached_codes("day", ctx.cfg.get("data.adjust", "qfq"))
        codes = list(dict.fromkeys(codes + cached))
    if not codes:
        console.print("[yellow]没有要同步的标的：请在 config/watchlist.yaml 配置，或指定代码。[/yellow]")
        return 1
    days = args.days or int(ctx.cfg.get("data.history_days", 800))
    adjust = args.adjust or ctx.cfg.get("data.adjust", "qfq")
    console.print(f"开始同步 {len(codes)} 只标的，约 {days} 个交易日…")
    results = ctx.md.sync(codes, days=days, period=args.period, adjust=adjust)

    table = Table(title=f"同步结果（{args.period} / {adjust}）", title_justify="left")
    table.add_column("代码", style="cyan")
    table.add_column("写入", justify="right")
    table.add_column("最新日期")
    table.add_column("状态")
    ok = 0
    for res in results:
        if res.error:
            table.add_row(res.code, "0", "-", f"[red]{res.error[:60]}[/red]")
        else:
            ok += 1
            table.add_row(res.code, str(res.rows), res.latest or "-", "[green]成功[/green]")
    console.print(table)
    console.print(f"[bold]{ok}/{len(results)}[/bold] 只同步成功")
    return 0 if ok else 1


# --------------------------------------------------------------------- quote
def cmd_quote(ctx: Ctx, args: argparse.Namespace) -> int:
    codes = ctx.codes_from_args(args)
    if not codes:
        console.print("[yellow]请指定代码，或在 config/watchlist.yaml 中配置自选股。[/yellow]")
        return 1
    quotes = ctx.md.quotes(codes, refresh=not args.cached)
    if not quotes:
        console.print("[red]未获取到行情。[/red]")
        return 1

    table = Table(title=f"实时行情 · {now().strftime('%Y-%m-%d %H:%M:%S')}", title_justify="left")
    for col, justify in [("代码", "left"), ("名称", "left"), ("现价", "right"),
                         ("涨跌幅", "right"), ("今开", "right"), ("最高", "right"),
                         ("最低", "right"), ("成交额", "right"), ("换手", "right")]:
        table.add_column(col, justify=justify, no_wrap=True)

    for q in quotes:
        style = _pct_style(q.pct)
        table.add_row(
            f"{q.market}{q.code}", q.name or "-", fmt_price(q.price),
            Text(fmt_pct(q.pct), style=style),
            fmt_price(q.open), fmt_price(q.high), fmt_price(q.low),
            fmt_big(q.amount), f"{fmt_num(q.turnover)}%" if q.turnover is not None else "-",
        )
    console.print(table)
    if quotes and quotes[0].total_mv:
        q = quotes[0]
        console.print(
            f"[dim]{q.name}：PE(动) {fmt_num(q.pe)} / PE(TTM) {fmt_num(q.pe_ttm or q.pe)} / "
            f"PB {fmt_num(q.pb)} / 总市值 {fmt_big(q.total_mv)} / 流通市值 {fmt_big(q.float_mv)}[/dim]"
        )
    return 0


# --------------------------------------------------------------------- bars
def cmd_bars(ctx: Ctx, args: argparse.Namespace) -> int:
    sym = Symbol.parse(args.code)
    adjust = args.adjust or ctx.cfg.get("data.adjust", "qfq")
    period = args.period
    is_intraday = period in INTRADAY_PERIODS
    df = ctx.md.bars(sym, period=period, adjust=adjust,
                     days=max(args.days, 60), refresh=args.refresh)
    if df.empty:
        console.print("[red]没有数据。[/red]")
        return 1
    pct_series = df["close"].pct_change() * 100
    view = df.tail(args.days)
    if args.csv:
        _save_csv(view, args.csv)

    period_text = period if is_intraday else f"{period} / {adjust}"
    table = Table(title=f"{sym.tencent} {period_text} K 线（最近 {len(view)} 根）",
                  title_justify="left")
    for col in ("时间" if is_intraday else "日期", "开盘", "最高", "最低", "收盘",
                "涨跌幅", "成交量(手)"):
        table.add_column(col, justify="right" if col != "日期" else "left", no_wrap=True)
    stamp = "%m-%d %H:%M" if is_intraday else "%Y-%m-%d"
    for date, row in view.iterrows():
        close = safe_float(row["close"])
        pct = safe_float(pct_series.get(date))
        style = _pct_style(pct)
        table.add_row(
            date.strftime(stamp),
            fmt_num(row["open"]), fmt_num(row["high"]), fmt_num(row["low"]),
            Text(fmt_num(close), style=style),
            Text(fmt_pct(pct), style=style),
            fmt_num((safe_float(row["volume"]) or 0) / 100, 0),
        )
    console.print(table)
    line = sparkline([float(v) for v in view["close"].tolist()])
    console.print(f"收盘走势 {line}")
    first, last = view.index[0], view.index[-1]
    fmt_edge = "%Y-%m-%d %H:%M" if is_intraday else "%Y-%m-%d"
    console.print(f"[dim]{first:{fmt_edge}} → {last:{fmt_edge}}，"
                  f"区间涨跌 {(view['close'].iloc[-1] / view['close'].iloc[0] - 1) * 100:+.2f}%[/dim]")
    return 0


# --------------------------------------------------------------------- ta
def cmd_ta(ctx: Ctx, args: argparse.Namespace) -> int:
    sym = Symbol.parse(args.code)
    df = ctx.md.bars(sym, days=max(args.days, 120))
    if df.empty:
        console.print("[red]没有数据，请先 sync。[/red]")
        return 1
    snap = ind.latest_snapshot(df)
    if not snap:
        console.print("[red]数据不足，无法计算指标。[/red]")
        return 1

    pairs = [
        ("数据日期", snap.get("date", "-")),
        ("收盘价", fmt_num(snap.get("close"))),
        ("当日涨跌", fmt_pct(snap.get("pct"))),
        ("MA5 / MA10", f"{fmt_num(snap.get('ma5'))} / {fmt_num(snap.get('ma10'))}"),
        ("MA20 / MA60", f"{fmt_num(snap.get('ma20'))} / {fmt_num(snap.get('ma60'))}"),
        ("MACD (DIF/DEA/柱)", f"{fmt_num(snap.get('dif'), 3)} / {fmt_num(snap.get('dea'), 3)} / "
                              f"{fmt_num(snap.get('macd'), 3)}"),
        ("RSI(14)", fmt_num(snap.get("rsi"), 1)),
        ("KDJ (K/D/J)", f"{fmt_num(snap.get('k'), 1)} / {fmt_num(snap.get('d'), 1)} / "
                        f"{fmt_num(snap.get('j'), 1)}"),
        ("布林带 (上/中/下)", f"{fmt_num(snap.get('boll_up'))} / {fmt_num(snap.get('boll_mid'))} / "
                             f"{fmt_num(snap.get('boll_low'))}"),
        ("ATR(14)", fmt_num(snap.get("atr"), 3)),
        ("量能 / 5日均量", f"{fmt_num(snap.get('vol_ratio5'), 2)} 倍"),
        ("20日高 / 低", f"{fmt_num(snap.get('high20'))} / {fmt_num(snap.get('low20'))}"),
        ("距60日高点", f"{fmt_num(snap.get('drawdown_from_high60'), 1)}%"),
        ("状态", "、".join(snap.get("signals", [])) or "-"),
    ]
    console.print(_kv_table(f"{sym.tencent} 技术指标", pairs))

    enriched = ind.add_indicators(df).tail(min(args.days, 30))
    table = Table(title="近期指标明细", title_justify="left")
    for col in ("日期", "收盘", "MA5", "MA20", "DIF", "DEA", "RSI14"):
        table.add_column(col, justify="right", no_wrap=True)
    for date, row in enriched.iterrows():
        table.add_row(
            date.strftime("%m-%d"), fmt_price(row["close"]), fmt_price(row.get("ma5")),
            fmt_price(row.get("ma20")), fmt_num(row.get("dif"), 2), fmt_num(row.get("dea"), 2),
            fmt_num(row.get("rsi14"), 1),
        )
    console.print(table)
    return 0


# --------------------------------------------------------------------- fund
def cmd_fund(ctx: Ctx, args: argparse.Namespace) -> int:
    sym = Symbol.parse(args.code)
    quotes = ctx.md.quotes([sym.tencent], refresh=not args.cached)
    if quotes:
        q = quotes[0]
        console.print(Panel.fit(
            f"[bold]{q.name}（{q.market}{q.code}）[/bold]  现价 {fmt_num(q.price)}  "
            f"{fmt_pct(q.pct)}\n"
            f"PE(动) {fmt_num(q.pe)}  PE(TTM) {fmt_num(q.pe_ttm or q.pe)}  PB {fmt_num(q.pb)}\n"
            f"总市值 {fmt_big(q.total_mv)}  流通市值 {fmt_big(q.float_mv)}  "
            f"换手 {fmt_num(q.turnover)}%  量比 {fmt_num(q.volume_ratio)}",
            border_style="cyan",
        ))
    rows = ctx.md.financials(sym, periods=args.periods, refresh=args.refresh)
    if not rows:
        console.print("[yellow]未获取到财务摘要（接口可能限流，可稍后重试）。[/yellow]")
        return 1
    table = Table(title=f"{sym.tencent} 财务摘要（近 {len(rows)} 期）", title_justify="left")
    for col in ("报告期", "营收", "营收同比", "归母净利", "净利同比", "ROE", "毛利率",
                "净利率", "资产负债率", "EPS"):
        table.add_column(col, justify="right" if col != "报告期" else "left", no_wrap=True)
    for row in rows:
        table.add_row(
            row.get("report_name") or row.get("report_date", ""),
            fmt_big(row.get("revenue")),
            fmt_pct(row.get("revenue_yoy")),
            fmt_big(row.get("net_profit")),
            fmt_pct(row.get("net_profit_yoy")),
            f"{fmt_num(row.get('roe'))}%",
            f"{fmt_num(row.get('gross_margin'))}%",
            f"{fmt_num(row.get('net_margin'))}%",
            f"{fmt_num(row.get('debt_ratio'))}%",
            fmt_num(row.get("eps")),
        )
    console.print(table)
    console.print("[dim]数据来源：东方财富公开财务摘要接口，报告期口径以披露为准。[/dim]")
    return 0


# --------------------------------------------------------------------- screen
def cmd_screen(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.list:
        table = Table(title="可用筛选预设", title_justify="left")
        table.add_column("预设", style="cyan")
        table.add_column("名称")
        table.add_column("说明", style="dim")
        for key, preset in PRESETS.items():
            table.add_row(key, preset.title, preset.description)
        console.print(table)
        return 0

    def progress(done: int, total: int) -> None:
        console.print(f"[dim]技术筛选进度 {done}/{total}[/dim]", end="\r")

    try:
        result, preset = screen(ctx.cfg, ctx.md, preset_key=args.preset, limit=args.limit,
                                pool=args.pool, min_mv=args.min_mv, max_pe=args.max_pe,
                                progress=progress if args.preset in ("oversold", "breakout", "ma_bull") else None)
    except Exception as exc:
        console.print(f"[red]筛选失败：{exc}[/red]")
        return 1
    if args.preset in ("oversold", "breakout", "ma_bull"):
        console.print(" " * 40, end="\r")
    if result is None or result.empty:
        console.print("[yellow]没有符合条件的标的。[/yellow]")
        return 0

    table = Table(title=f"{preset.title}（{preset.description}）", title_justify="left")
    for key, label in SCREEN_COLUMNS:
        if key not in result.columns:
            continue
        table.add_column(label, justify="left" if key in ("code", "name") else "right", no_wrap=True)
    for _, row in result.iterrows():
        cells = []
        for key, _label in SCREEN_COLUMNS:
            if key not in result.columns:
                continue
            value = row.get(key)
            if key in ("code", "name"):
                cells.append(str(value))
            elif key == "pct":
                cells.append(fmt_pct(value))
            elif key == "total_mv":
                cells.append(fmt_big(value))
            elif key == "amount":
                cells.append(fmt_big(value))
            elif key == "rsi":
                cells.append(fmt_num(value, 1))
            else:
                cells.append(fmt_num(value))
        table.add_row(*cells)
    console.print(table)
    if args.csv:
        _save_csv(result, args.csv)
    console.print("[dim]筛选结果仅为数据统计，不构成任何投资建议。[/dim]")
    return 0


# --------------------------------------------------------------------- 参数寻优
def _load_single_bars(ctx: Ctx, args: argparse.Namespace, raw: str) -> tuple[Symbol, pd.DataFrame]:
    sym = Symbol.parse(raw)
    df = ctx.md.bars(sym, days=max(args.days, 320), adjust=args.adjust)
    if args.start:
        df = df[df.index >= pd.Timestamp(args.start)]
    if args.end:
        df = df[df.index <= pd.Timestamp(args.end)]
    return sym, df


def _run_optimize(ctx: Ctx, args: argparse.Namespace, codes: list[str]) -> int:
    if len(codes) != 1:
        console.print("[yellow]参数寻优一次只支持一个标的（多标的请分别寻优后再组合）。[/yellow]")
        return 1
    sym, df = _load_single_bars(ctx, args, codes[0])
    if len(df) < 120:
        console.print(f"[red]{sym.tencent} 数据不足（{len(df)} 根），至少需要 120 根。[/red]")
        return 1

    try:
        grid = parse_grid(args.optimize)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    bt_cfg = BacktestConfig.from_config(ctx.cfg)
    if args.capital:
        bt_cfg.initial_capital = float(args.capital)
    if args.slippage is not None:
        bt_cfg.slippage_bps = float(args.slippage)

    if not args.json:
        console.print(
            f"[dim]{sym.tencent} · {args.strategy} · 网格 {grid_size(grid)} 组 · "
            f"目标 {OBJECTIVES.get(args.objective, args.objective)} · 样本 {len(df)} 根[/dim]"
        )

    try:
        if args.walkforward:
            wf = walk_forward(df, args.strategy, grid, bt_cfg,
                              train_days=args.train, test_days=args.test,
                              step_days=args.step, objective=args.objective,
                              min_trades=args.min_trades, code=sym.tencent)
        else:
            trials = grid_search(df, args.strategy, grid, bt_cfg, args.objective,
                                 args.min_trades, sym.tencent)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if args.json:
        if args.walkforward:
            payload = {
                "mode": "walkforward", "code": sym.tencent, "strategy": args.strategy,
                "grid": grid, "objective": args.objective,
                "is_mean_sharpe": wf.is_mean_sharpe, "oos_mean_sharpe": wf.oos_mean_sharpe,
                "consistency": wf.consistency, "oos_metrics": wf.oos_metrics,
                "folds": [{"index": f.index, "train_start": f.train_start, "train_end": f.train_end,
                           "test_start": f.test_start, "test_end": f.test_end,
                           "best_params": f.best_params, "is_sharpe": f.is_metrics.get("sharpe"),
                           "oos_sharpe": f.oos_metrics.get("sharpe"),
                           "degradation": f.degradation} for f in wf.folds],
            }
        else:
            payload = {
                "mode": "grid", "code": sym.tencent, "strategy": args.strategy,
                "grid": grid, "objective": args.objective,
                "trials": [{"params": t.params, "score": t.score,
                            "sharpe": t.metrics.get("sharpe"),
                            "total_return": t.metrics.get("total_return"),
                            "max_drawdown": t.metrics.get("max_drawdown"),
                            "trades": t.metrics.get("trades")} for t in trials[:args.top]],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.walkforward:
        _print_walkforward(wf, sym, args)
        return 0

    if not trials:
        console.print("[yellow]所有参数组合都不可用（可能是交易次数不足或数据太短）。"
                      "试试 --min-trades 0 或放宽网格。[/yellow]")
        return 1
    _print_grid(trials, sym, args)
    return 0


def _print_grid(trials: list[Any], sym: Symbol, args: argparse.Namespace) -> None:
    best = trials[0]
    keys = list(best.params.keys())
    table = Table(title=f"{sym.tencent} 参数寻优 Top {min(args.top, len(trials))}"
                        f"（按 {OBJECTIVES.get(args.objective, args.objective)} 排序）",
                  title_justify="left")
    table.add_column("#", justify="right", width=3)
    for key in keys:
        table.add_column(key, justify="right")
    for col in ("夏普", "年化%", "最大回撤%", "交易数"):
        table.add_column(col, justify="right")
    for rank, trial in enumerate(trials[:args.top], start=1):
        row = [str(rank)]
        row.extend(str(trial.params[k]) for k in keys)
        row.extend([
            fmt_num(trial.metrics.get("sharpe")),
            fmt_pct(trial.metrics.get("cagr")),
            fmt_num(trial.metrics.get("max_drawdown")),
            str(trial.metrics.get("trades")),
        ])
        table.add_row(*row)
    console.print(table)

    returns = best.result.equity.pct_change().dropna() if best.result else pd.Series(dtype=float)
    dsr = deflated_sharpe(returns, [t.metrics.get("sharpe") or 0 for t in trials])
    console.print(_kv_table(f"最优组合：{' '.join(f'{k}={v}' for k, v in best.params.items())}",
                            metrics_lines(best.metrics)))
    if dsr:
        verdict = "较可信" if dsr["dsr"] >= 0.95 else ("存疑" if dsr["dsr"] >= 0.8 else "很可能过拟合")
        console.print(
            f"[dim]Deflated Sharpe：DSR={dsr['dsr']:.3f}（门槛 SR0={dsr['sr0']:.3f}，"
            f"日夏普={dsr['sr']:.3f}，试验 {int(dsr['trials'])} 组）→ {verdict}[/dim]"
        )
        console.print("[dim]DSR 已按搜索次数校正：0.95 以上才谈得上统计显著；"
                      "参数越多、网格越密，门槛越高。[/dim]")
    console.print("[dim]提示：本表是同一段历史里挑出来的最好结果，务必用 --walkforward 做样本外验证。[/dim]")


def _print_walkforward(wf: Any, sym: Symbol, args: argparse.Namespace) -> None:
    table = Table(title=f"{sym.tencent} Walk-forward（训练 {args.train} 根 / 测试 {args.test} 根 / 步长 {args.step}）",
                  title_justify="left")
    for col in ("折", "训练区间", "测试区间", "最优参数", "样本内夏普", "样本外夏普", "退化"):
        table.add_column(col, justify="left" if col in ("训练区间", "测试区间", "最优参数") else "right")
    for fold in wf.folds:
        params_text = " ".join(f"{k}={v}" for k, v in fold.best_params.items()) if fold.best_params else "-"
        table.add_row(
            str(fold.index),
            f"{fold.train_start}~{fold.train_end}",
            f"{fold.test_start}~{fold.test_end}",
            params_text,
            fmt_num(fold.is_metrics.get("sharpe")),
            fmt_num(fold.oos_metrics.get("sharpe")),
            fmt_num(fold.degradation),
        )
    console.print(table)

    pairs = [
        ("折数", f"{len([f for f in wf.folds if f.is_metrics])} / {len(wf.folds)}"),
        ("样本内平均夏普", fmt_num(wf.is_mean_sharpe)),
        ("样本外平均夏普", fmt_num(wf.oos_mean_sharpe)),
        ("样本外正收益折占比", f"{wf.consistency * 100:.0f}%"),
        ("OOS 累计收益", fmt_pct(wf.oos_metrics.get("total_return"))),
        ("OOS 年化收益", fmt_pct(wf.oos_metrics.get("cagr"))),
        ("OOS 最大回撤", f"{fmt_num(wf.oos_metrics.get('max_drawdown'))}%"),
        ("同期买入持有", fmt_pct(wf.oos_metrics.get("benchmark_return"))),
    ]
    console.print(_kv_table("样本外汇总（各折拼接净值）", pairs))
    curve = sparkline([float(v) for v in wf.equity.tolist()], width=64)
    console.print(f"OOS 资金曲线 {curve}")

    drop = wf.is_mean_sharpe - wf.oos_mean_sharpe
    if wf.oos_mean_sharpe <= 0:
        console.print("[red]⚠ 样本外平均夏普 ≤ 0：这套参数在没见过的数据上没有优势，"
                      "典型的过拟合信号。[/red]")
    elif drop >= 0.5:
        console.print(f"[yellow]⚠ 样本内→样本外夏普下降 {drop:.2f}，衰减明显；"
                      f"建议减少参数、拉长训练窗口或换更简单的策略。[/yellow]")
    else:
        console.print("[green]样本内外表现相对一致，参数稳定性尚可。[/green]")
    console.print("[dim]说明：每折用紧邻的下一段数据做样本外回测，测试段不参与选参，不存在未来函数。[/dim]")


# --------------------------------------------------------------------- alert
def cmd_alert(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.alert_action == "list":
        rows = ctx.store.recent_alerts(args.limit)
        if not rows:
            console.print("[dim]暂无预警记录。[/dim]")
            return 0
        table = Table(title="历史预警", title_justify="left")
        for col in ("时间", "代码", "规则", "信息", "价格"):
            table.add_column(col, no_wrap=(col != "信息"))
        for row in rows:
            table.add_row(row["ts"] or "", row["code"], row["rule"],
                          row["message"] or "", fmt_num(row["price"]))
        console.print(table)
        return 0

    if args.alert_action == "rules":
        table = Table(title="支持的预警规则", title_justify="left")
        table.add_column("类型", style="cyan")
        table.add_column("说明")
        descriptions = {
            "price_above": "价格上穿/达到 value",
            "price_below": "价格下穿/低于 value",
            "price_near": "价格靠近 value（pct 为容差百分比，默认 1）",
            "pct_change_above": "当日涨幅 ≥ value",
            "pct_change_below": "当日涨幅 ≤ value（跌幅用负数）",
            "ma_cross": "均线交叉 fast/slow，direction=up/down",
            "price_cross_ma": "收盘价穿越 ma，direction=up/down",
            "macd_cross": "MACD 金叉/死叉，direction=up/down",
            "kdj_cross": "KDJ 金叉/死叉，direction=up/down",
            "rsi_threshold": "RSI 阈值 period/op(below|above)/value",
            "breakout": "突破 N 日高/低点 window/direction",
            "volume_spike": "放量 ratio 倍（对 window 日均量）",
            "drawdown_from_high": "距 window 日高点回撤 ≥ pct%",
        }
        for key in sorted(RULE_TYPES):
            table.add_row(key, descriptions.get(key, ""))
        console.print(table)
        return 0

    # 校验配置中的规则
    items = ctx.cfg.watchlist()
    problems = []
    for item in items:
        for rule in item.get("rules", []):
            err = validate_rule(rule)
            if err:
                problems.append(f"{item['code']}: {err}")
    for problem in problems:
        console.print(f"[red]规则错误[/red] {problem}")

    loop = args.loop or args.watch
    interval = args.interval or int(ctx.cfg.get("alerts.check_interval", 60))
    desktop = args.desktop or bool(ctx.cfg.get("alerts.notify_desktop", False))
    if not loop:
        alerts, errors = check_watchlist(ctx.cfg, ctx.md, ctx.store, refresh=args.refresh)
        for err in errors:
            console.print(f"[yellow]跳过[/yellow] {err}")
        if alerts:
            notify(alerts, desktop)
        else:
            console.print(f"[dim]{now():%H:%M:%S} 本轮无新预警（检查 {len(items)} 只自选股）[/dim]")
        return 0

    console.print(f"[bold]开始盯盘[/bold] 每 {interval}s 检查一次，Ctrl+C 退出。"
                  f"（仅为本地提醒，不会发出任何委托）")
    try:
        while True:
            alerts, errors = check_watchlist(ctx.cfg, ctx.md, ctx.store, refresh=True)
            for err in errors:
                console.print(f"[yellow]跳过[/yellow] {err}")
            if alerts:
                notify(alerts, desktop)
            else:
                console.print(f"[dim]{now():%H:%M:%S} 无新预警[/dim]", end="\r")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]已停止盯盘。[/dim]")
    return 0


# --------------------------------------------------------------------- backtest
def _parse_params(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            console.print(f"[yellow]忽略无效参数：{pair}（应为 key=value）[/yellow]")
            continue
        key, _, value = pair.partition("=")
        text = value.strip()
        try:
            out[key.strip()] = int(text)
        except ValueError:
            try:
                out[key.strip()] = float(text)
            except ValueError:
                out[key.strip()] = text
    return out


def cmd_backtest(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.list:
        table = Table(title="可用策略", title_justify="left")
        table.add_column("策略", style="cyan")
        table.add_column("默认参数")
        table.add_column("说明", style="dim")
        for key, st in STRATEGIES.items():
            table.add_row(key, ", ".join(f"{k}={v}" for k, v in st.defaults.items()) or "-",
                          st.description)
        console.print(table)
        return 0

    codes = ctx.codes_from_args(args, default_watchlist=False)
    if not codes:
        console.print("[yellow]请指定至少一个代码，例如：./quant backtest 600519[/yellow]")
        return 1

    if getattr(args, "optimize", None):
        return _run_optimize(ctx, args, codes)

    params = _parse_params(args.param)
    bt_cfg = BacktestConfig.from_config(ctx.cfg)
    if args.capital:
        bt_cfg.initial_capital = float(args.capital)
    if args.slippage is not None:
        bt_cfg.slippage_bps = float(args.slippage)

    data: dict[str, tuple[pd.DataFrame, pd.Series]] = {}
    for raw in codes:
        sym = Symbol.parse(raw)
        try:
            df = ctx.md.bars(sym, days=max(args.days, 250), adjust=args.adjust)
        except Exception as exc:
            console.print(f"[red]{sym.tencent} 数据获取失败：{exc}[/red]")
            continue
        if args.start:
            df = df[df.index >= pd.Timestamp(args.start)]
        if args.end:
            df = df[df.index <= pd.Timestamp(args.end)]
        if len(df) < 60:
            console.print(f"[yellow]{sym.tencent} 样本不足（{len(df)} 根），已跳过[/yellow]")
            continue
        try:
            pos = build_position(df, args.strategy, **params)
        except Exception as exc:
            console.print(f"[red]策略构建失败：{exc}[/red]")
            return 1
        data[sym.tencent] = (df, pos)

    if not data:
        console.print("[red]没有可用于回测的数据。[/red]")
        return 1

    result = (run_single(data[list(data)[0]][0], data[list(data)[0]][1], list(data)[0],
                         args.strategy, params, bt_cfg)
              if len(data) == 1 else
              run_portfolio(data, args.strategy, params, bt_cfg))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    title = f"{result.code} · {args.strategy} · " + ", ".join(f"{k}={v}" for k, v in params.items())
    console.print(_kv_table(f"回测结果：{title}", metrics_lines(result.metrics)))
    for warning in result.warnings:
        console.print(f"[yellow]⚠ {warning}[/yellow]")

    equity = result.equity
    curve = sparkline([float(v) for v in equity.tolist()], width=64)
    console.print(f"资金曲线 {curve}")
    console.print(f"[dim]{equity.index[0]:%Y-%m-%d} 起始 {equity.iloc[0]:,.0f} 元 → "
                  f"{equity.index[-1]:%Y-%m-%d} 期末 {equity.iloc[-1]:,.0f} 元[/dim]")

    if result.trades:
        table = Table(title=f"交易明细（最近 {min(args.trades, len(result.trades))} 笔 / 共 {len(result.trades)} 笔）",
                      title_justify="left")
        for col in ("买入日", "卖出日", "买入价", "卖出价", "股数", "盈亏", "收益率", "持仓天数"):
            table.add_column(col, justify="right" if col != "买入日" else "left", no_wrap=True)
        for trade in result.trades[-args.trades:]:
            style = _pct_style(trade.pnl)
            table.add_row(
                trade.entry_date, trade.exit_date, fmt_num(trade.entry_price),
                fmt_num(trade.exit_price), str(trade.shares),
                Text(fmt_num(trade.pnl), style=style),
                Text(f"{trade.pnl_pct * 100:+.2f}%", style=style),
                str(trade.hold_days),
            )
        console.print(table)

    if args.save:
        ctx.store.save_backtest(result.code, args.strategy, params, result.metrics)
        console.print("[dim]已写入回测记录表。[/dim]")
    console.print("[dim]回测为历史模拟，已计入佣金/印花税/滑点，但仍不代表未来收益。[/dim]")
    return 0


# --------------------------------------------------------------------- watch
def cmd_watch(ctx: Ctx, args: argparse.Namespace) -> int:
    cfg = ctx.cfg
    items = cfg.watchlist()

    if args.watch_action == "list":
        if not items:
            console.print("[dim]自选股为空。用 ./quant watch add 600519 添加。[/dim]")
            return 0
        table = Table(title=f"自选股（{len(items)} 只）", title_justify="left")
        for col in ("代码", "名称", "规则数", "备注"):
            table.add_column(col, justify="left" if col != "规则数" else "right")
        for item in items:
            sym = Symbol.parse(item["code"])
            table.add_row(sym.tencent, item.get("name") or "-",
                          str(len(item.get("rules", []))), item.get("note", ""))
        console.print(table)
        return 0

    if args.watch_action == "add":
        sym = Symbol.parse(args.code)
        existing = {Symbol.parse(i["code"]).tencent for i in items}
        if sym.tencent in existing:
            console.print(f"[yellow]{sym.tencent} 已在自选股中[/yellow]")
            return 0
        name = args.name
        if not name:
            try:
                quotes = ctx.md.quotes([sym.tencent])
                name = quotes[0].name if quotes else ""
            except Exception:
                name = ""
        items.append({"code": sym.code, "name": name or sym.code,
                      "note": args.note or "", "rules": []})
        cfg.save_watchlist(items)
        console.print(f"[green]已加入自选[/green] {sym.tencent} {name or ''}，"
                      f"现在共 {len(items)} 只")
        return 0

    # rm
    targets = {Symbol.parse(c).tencent for c in args.codes}
    keep = [i for i in items if Symbol.parse(i["code"]).tencent not in targets]
    removed = len(items) - len(keep)
    if removed == 0:
        console.print("[yellow]未找到匹配的自选股，未做修改。[/yellow]")
        return 1
    cfg.save_watchlist(keep)
    console.print(f"[green]已移除 {removed} 只[/green]，剩余 {len(keep)} 只")
    return 0


# --------------------------------------------------------------------- report
def cmd_report(ctx: Ctx, args: argparse.Namespace) -> int:
    text, path = build_daily_report(ctx.cfg, ctx.md, ctx.store)
    if args.print:
        console.print(text)
    console.print(f"[green]已生成日报[/green] {path}")
    console.print(f"[dim]最新报告副本：{ctx.cfg.report_dir / 'latest.md'}[/dim]")
    return 0


# --------------------------------------------------------------------- web
def cmd_web(ctx: Ctx, args: argparse.Namespace) -> int:
    from .web.app import create_app

    host = args.host or str(ctx.cfg.get("web.host", "127.0.0.1"))
    port = args.port or int(ctx.cfg.get("web.port", 8770))
    app = create_app(ctx.cfg)
    console.print(Panel.fit(
        f"本地工作台已启动：http://{host}:{port}\n"
        f"在 Windows 浏览器打开上述地址即可（WSL2 会自动转发 localhost）。\n"
        f"Ctrl+C 停止。",
        border_style="green", title="aquant web",
    ))
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    return 0


# --------------------------------------------------------------------- 解析
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aquant",
        description="A 股个人量化研究助手（只读行情 + 回测 + 预警，不含任何交易执行功能）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例：\n"
            "  ./quant init                        # 初始化\n"
            "  ./quant sync                        # 同步自选股日线\n"
            "  ./quant quote 600519 000001         # 实时行情\n"
            "  ./quant bars 600519 --period 5m     # 分钟 K 线\n"
            "  ./quant ta 600519                   # 技术指标\n"
            "  ./quant fund 600519                 # 基本面\n"
            "  ./quant watch add 600519            # 加入自选\n"
            "  ./quant watch rm 600519             # 移出自选\n"
            "  ./quant screen --preset value       # 低估值筛选\n"
            "  ./quant alert check                 # 检查预警\n"
            "  ./quant backtest 600519 --strategy ma_cross --param fast=5 --param slow=20\n"
            "  ./quant backtest 600519 --optimize \"fast=3,5,8;slow=10,20,40\" --walkforward\n"
            "  ./quant web                         # 本地可视化工作台\n"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"aquant {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--config", help="指定配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("init", help="初始化配置与数据库")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="环境与数据源自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("sync", help="下载/更新历史日线")
    p.add_argument("codes", nargs="*", help="股票代码，留空则用自选股")
    p.add_argument("--days", type=int, default=None, help="拉取多少个交易日（默认取配置）")
    p.add_argument("--period", default="day", choices=["day", "week", "month"])
    p.add_argument("--adjust", default=None, choices=["qfq", "hfq", "bfq"], help="复权方式")
    p.add_argument("--all", action="store_true", help="连同本地已有标的一起更新")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("quote", help="查看实时行情")
    p.add_argument("codes", nargs="*", help="股票代码，留空则用自选股")
    p.add_argument("--cached", action="store_true", help="只用本地缓存，不请求网络")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("bars", help="查看历史 K 线（含分钟线）")
    p.add_argument("code", help="股票代码")
    p.add_argument("--days", type=int, default=20, help="显示最近 N 根")
    p.add_argument("--period", default="day",
                   choices=["day", "week", "month", "1m", "5m", "15m", "30m", "60m"],
                   help="周期：日/周/月线，或 1/5/15/30/60 分钟线")
    p.add_argument("--adjust", default=None, choices=["qfq", "hfq", "bfq"])
    p.add_argument("--refresh", action="store_true", help="强制从网络刷新")
    p.add_argument("--csv", help="导出 CSV 路径")
    p.set_defaults(func=cmd_bars)

    p = sub.add_parser("ta", help="技术指标快照")
    p.add_argument("code")
    p.add_argument("--days", type=int, default=60, help="明细显示天数")
    p.set_defaults(func=cmd_ta)

    p = sub.add_parser("fund", help="基本面与财务摘要")
    p.add_argument("code")
    p.add_argument("--periods", type=int, default=8, help="显示近 N 个报告期")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--cached", action="store_true", help="行情用本地缓存")
    p.set_defaults(func=cmd_fund)

    p = sub.add_parser("screen", help="按预设条件选股")
    p.add_argument("--preset", default="value", help=f"预设：{', '.join(PRESETS)}")
    p.add_argument("--limit", type=int, default=20, help="返回条数")
    p.add_argument("--pool", type=int, default=200, help="技术筛选的候选池大小（按成交额）")
    p.add_argument("--min-mv", type=float, default=None, help="最小总市值（亿元）")
    p.add_argument("--max-pe", type=float, default=None, help="最大 PE(TTM)")
    p.add_argument("--list", action="store_true", help="列出所有预设")
    p.add_argument("--csv", help="导出 CSV 路径")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("alert", help="预警规则检查")
    alert_sub = p.add_subparsers(dest="alert_action", required=True, metavar="<action>")
    q = alert_sub.add_parser("check", help="执行一次预警检查")
    q.add_argument("--refresh", action="store_true", help="强制刷新行情")
    q.add_argument("--loop", action="store_true", help="持续轮询")
    q.add_argument("--watch", action="store_true", help="同 --loop")
    q.add_argument("--interval", type=int, default=None, help="轮询间隔（秒）")
    q.add_argument("--desktop", action="store_true", help="尝试桌面通知")
    q.set_defaults(func=cmd_alert)
    q = alert_sub.add_parser("list", help="查看历史预警")
    q.add_argument("--limit", type=int, default=30)
    q.set_defaults(func=cmd_alert)
    q = alert_sub.add_parser("rules", help="列出支持的规则类型")
    q.set_defaults(func=cmd_alert)

    p = sub.add_parser("backtest", help="历史回测")
    p.add_argument("codes", nargs="*", help="一个或多个代码（多个则等权组合）")
    p.add_argument("--strategy", default="ma_cross", help=f"策略：{', '.join(STRATEGIES)}")
    p.add_argument("--param", action="append", help="策略参数，可重复：--param fast=5")
    p.add_argument("--days", type=int, default=500, help="回看交易日数")
    p.add_argument("--start", help="起始日期 YYYY-MM-DD")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD")
    p.add_argument("--capital", type=float, default=None, help="初始资金")
    p.add_argument("--slippage", type=float, default=None, help="单边滑点（bp）")
    p.add_argument("--adjust", default=None, choices=["qfq", "hfq", "bfq"])
    p.add_argument("--trades", type=int, default=10, help="展示最近 N 笔交易")
    p.add_argument("--optimize", help='参数网格寻优，如 "fast=3,5,8;slow=10,20,40"')
    p.add_argument("--objective", default="sharpe", choices=list(OBJECTIVES),
                   help="寻优目标指标")
    p.add_argument("--walkforward", action="store_true",
                   help="滚动样本内选参 + 样本外验证（配合 --optimize）")
    p.add_argument("--train", type=int, default=250, help="walk-forward 训练窗口（交易日）")
    p.add_argument("--test", type=int, default=60, help="walk-forward 测试窗口（交易日）")
    p.add_argument("--step", type=int, default=60, help="walk-forward 滚动步长（交易日）")
    p.add_argument("--min-trades", type=int, default=1, help="参与排名的组合最少交易次数")
    p.add_argument("--top", type=int, default=10, help="展示前 N 组参数")
    p.add_argument("--json", action="store_true", help="输出 JSON（供脚本/网页使用）")
    p.add_argument("--save", action="store_true", help="写入回测记录")
    p.add_argument("--list", action="store_true", help="列出可用策略")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("watch", help="管理自选股（增删查）")
    watch_sub = p.add_subparsers(dest="watch_action", required=True, metavar="<action>")
    q = watch_sub.add_parser("list", help="列出自选股")
    q.set_defaults(func=cmd_watch)
    q = watch_sub.add_parser("add", help="加入自选股")
    q.add_argument("code")
    q.add_argument("--name", help="自定义名称")
    q.add_argument("--note", help="备注")
    q.set_defaults(func=cmd_watch)
    q = watch_sub.add_parser("rm", help="移除自选股（可多个）")
    q.add_argument("codes", nargs="+")
    q.set_defaults(func=cmd_watch)

    p = sub.add_parser("report", help="生成每日观察报告（Markdown）")
    p.add_argument("--print", action="store_true", help="同时在终端打印")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("web", help="启动本地可视化工作台")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config) if args.config else None)
    _setup_logging(args.verbose, cfg)
    ctx = Ctx(cfg)
    try:
        return int(args.func(ctx, args) or 0)
    except KeyboardInterrupt:
        console.print("\n[dim]已中断。[/dim]")
        return 130
    except Exception as exc:
        log.exception("命令执行失败")
        console.print(f"[red]出错：{exc}[/red]")
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
