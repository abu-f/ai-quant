"""每日观察报告（Markdown）。"""

from __future__ import annotations

from pathlib import Path

from . import indicators as ind
from .alerts import evaluate
from .config import Config
from .data.provider import MarketData
from .store import Store
from .symbols import Symbol
from .utils import fmt_big, fmt_num, fmt_pct, now

DISCLAIMER = (
    "本报告由本地工具自动生成，仅用于个人研究记录：不含投资建议，不含任何下单、"
    "委托或交易执行功能，所有决策与操作均由使用者本人完成。行情数据来自公开接口，"
    "可能存在延迟或缺失。"
)


def build_daily_report(cfg: Config, md: MarketData, store: Store) -> tuple[str, Path]:
    items = cfg.watchlist()
    lines: list[str] = []
    today = now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"# A 股观察日报 · {today}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")

    if not items:
        lines.append("自选股为空，请先编辑 `config/watchlist.yaml`。")
        text = "\n".join(lines)
        path = _write(cfg, text)
        return text, path

    codes = [item["code"] for item in items]
    quotes = md.quotes(codes, refresh=True)
    quote_map = {q.code: q for q in quotes}

    lines.append("## 一、自选股行情")
    lines.append("")
    lines.append("| 代码 | 名称 | 现价 | 涨跌幅 | 成交额 | 换手率 | 更新时间 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
    for item in items:
        sym = Symbol.parse(item["code"])
        q = quote_map.get(sym.code)
        name = (q.name if q and q.name else item.get("name")) or sym.code
        if q:
            lines.append(
                f"| {sym.code} | {name} | {fmt_num(q.price)} | {fmt_pct(q.pct)} | "
                f"{fmt_big(q.amount)} | {fmt_num(q.turnover)}% | {q.ts} |"
            )
        else:
            lines.append(f"| {sym.code} | {name} | - | - | - | - | 获取失败 |")
    lines.append("")

    lines.append("## 二、技术指标状态")
    lines.append("")
    lines.append("| 代码 | 名称 | 收盘 | MA20 | RSI14 | MACD | 60日回撤 | 状态 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    alert_lines: list[str] = []
    for item in items:
        sym = Symbol.parse(item["code"])
        q = quote_map.get(sym.code)
        display_name = (q.name if q and q.name else item.get("name")) or sym.code
        try:
            bars = md.bars(sym, days=200)
        except Exception:
            continue
        if bars.empty:
            continue
        snap = ind.latest_snapshot(bars)
        macd_state = "多头" if (snap.get("dif") or 0) > (snap.get("dea") or 0) else "空头"
        dd = snap.get("drawdown_from_high60")
        lines.append(
            f"| {sym.code} | {display_name} | {fmt_num(snap.get('close'))} | "
            f"{fmt_num(snap.get('ma20'))} | {fmt_num(snap.get('rsi'), 1)} | {macd_state} | "
            f"{fmt_num(dd, 1)}% | {'、'.join(snap.get('signals', [])) or '-'} |"
        )
        for alert in evaluate(sym.tencent, display_name, bars, item.get("rules", [])):
            alert_lines.append(f"- **{alert.name}({alert.code})** {alert.message}（{alert.date}）")
    lines.append("")

    lines.append("## 三、规则命中")
    lines.append("")
    lines.extend(alert_lines or ["- 今日无规则命中。"])
    lines.append("")

    stats = store.stats()
    lines.append("## 四、数据概况")
    lines.append("")
    lines.append(f"- 本地日线：{stats['bars']:,} 条，覆盖 {stats['codes']} 只标的")
    lines.append(f"- 历史预警记录：{stats['alerts']} 条")
    lines.append(f"- 数据库：`{store.path}`")
    lines.append("")
    text = "\n".join(lines)
    return text, _write(cfg, text)


def _write(cfg: Config, text: str) -> Path:
    path = cfg.report_dir / f"{now().strftime('%Y-%m-%d')}-daily.md"
    path.write_text(text, encoding="utf-8")
    latest = cfg.report_dir / "latest.md"
    latest.write_text(text, encoding="utf-8")
    return path
