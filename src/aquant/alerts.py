"""预警规则引擎。

规则写在 config/watchlist.yaml 里，例如：

  - code: "600519"
    name: 贵州茅台
    rules:
      - {type: price_above, value: 1600}
      - {type: pct_change_below, value: -3}
      - {type: ma_cross, fast: 5, slow: 20, direction: up}
      - {type: rsi_threshold, period: 14, op: below, value: 30}
      - {type: breakout, window: 20, direction: up}
      - {type: volume_spike, ratio: 2.0, window: 5}

所有判断只基于已收盘的日线数据，不预测、不产生任何交易指令。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import indicators as ind
from .config import Config
from .data.provider import MarketData
from .store import Store
from .symbols import Symbol
from .utils import fmt_num, now, safe_float

log = logging.getLogger("aquant.alerts")

RULE_TYPES = {
    "price_above", "price_below", "price_near",
    "pct_change_above", "pct_change_below",
    "ma_cross", "price_cross_ma", "macd_cross", "kdj_cross",
    "rsi_threshold", "breakout", "volume_spike", "drawdown_from_high",
}


@dataclass
class Alert:
    code: str
    name: str
    rule: str
    message: str
    price: float | None = None
    date: str = ""
    ts: str = field(default_factory=lambda: now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "rule": self.rule,
            "message": self.message, "price": self.price, "date": self.date, "ts": self.ts,
        }


def rule_key(rule: dict[str, Any]) -> str:
    rtype = str(rule.get("type", "")).strip()
    params = ",".join(f"{k}={v}" for k, v in sorted(rule.items()) if k != "type")
    return f"{rtype}({params})" if params else rtype


REQUIRED_PARAMS = {
    "price_above": ("value",),
    "price_below": ("value",),
    "price_near": ("value",),
    "pct_change_above": ("value",),
    "pct_change_below": ("value",),
}


def validate_rule(rule: dict[str, Any]) -> str | None:
    """返回错误信息，None 表示合法。"""
    if not isinstance(rule, dict):
        return "规则必须是映射（type: xxx）"
    rtype = rule.get("type")
    if rtype not in RULE_TYPES:
        return f"未知规则类型 {rtype!r}，可选：{', '.join(sorted(RULE_TYPES))}"
    for key in REQUIRED_PARAMS.get(str(rtype), ()):
        if rule.get(key) is None:
            return f"规则 {rtype} 缺少参数 {key}"
    if rtype == "ma_cross":
        fast = safe_float(rule.get("fast", 5), 5) or 5
        slow = safe_float(rule.get("slow", 20), 20) or 20
        if fast >= slow:
            return f"ma_cross 的 fast({fast}) 必须小于 slow({slow})"
    if rtype == "rsi_threshold" and str(rule.get("op", "below")).lower() not in ("below", "above"):
        return "rsi_threshold 的 op 只能是 below 或 above"
    if rtype in ("ma_cross", "price_cross_ma", "macd_cross", "kdj_cross", "breakout"):
        direction = str(rule.get("direction", "up")).lower()
        if direction not in ("up", "down"):
            return f"{rtype} 的 direction 只能是 up 或 down"
    return None


def evaluate(code: str, name: str, df: pd.DataFrame,
             rules: list[dict[str, Any]]) -> list[Alert]:
    """对单只股票评估全部规则，返回命中的提醒。"""
    if df is None or len(df) < 5:
        return []
    enriched = ind.add_indicators(df)
    last = enriched.iloc[-1]
    prev = enriched.iloc[-2]
    date = df.index[-1].strftime("%Y-%m-%d")
    close = safe_float(last.get("close"))
    pct = safe_float(last.get("pct"))
    alerts: list[Alert] = []

    for rule in rules or []:
        if validate_rule(rule):
            continue
        rtype = rule["type"]
        try:
            hit, message = _check(rtype, rule, enriched, last, prev, close, pct)
        except Exception as exc:  # 单条规则异常不影响其它规则
            log.debug("规则 %s 计算失败：%s", rule_key(rule), exc)
            continue
        if hit:
            alerts.append(Alert(code=code, name=name, rule=rule_key(rule),
                                message=message, price=close, date=date))
    return alerts


def _check(rtype: str, rule: dict[str, Any], df: pd.DataFrame, last: pd.Series,
           prev: pd.Series, close: float | None, pct: float | None) -> tuple[bool, str]:
    if rtype == "price_above":
        target = safe_float(rule.get("value"))
        if target is None or close is None:
            return False, ""
        return close >= target, f"价格 {close:.2f} 已上穿 {target:.2f}"

    if rtype == "price_below":
        target = safe_float(rule.get("value"))
        if target is None or close is None:
            return False, ""
        return close <= target, f"价格 {close:.2f} 已下穿 {target:.2f}"

    if rtype == "price_near":
        target = safe_float(rule.get("value"))
        band = safe_float(rule.get("pct", 1.0), 1.0) or 1.0
        if target is None or close is None or target == 0:
            return False, ""
        diff = abs(close / target - 1) * 100
        return diff <= band, f"价格 {close:.2f} 距 {target:.2f} 仅 {diff:.2f}%"

    if rtype == "pct_change_above":
        target = safe_float(rule.get("value"))
        if target is None or pct is None:
            return False, ""
        return pct >= target, f"当日涨幅 {pct:+.2f}% ≥ {target:+.2f}%"

    if rtype == "pct_change_below":
        target = safe_float(rule.get("value"))
        if target is None or pct is None:
            return False, ""
        return pct <= target, f"当日涨幅 {pct:+.2f}% ≤ {target:+.2f}%"

    if rtype == "ma_cross":
        fast_n = int(rule.get("fast", 5))
        slow_n = int(rule.get("slow", 20))
        direction = str(rule.get("direction", "up")).lower()
        fast_s = df.get(f"ma{fast_n}")
        slow_s = df.get(f"ma{slow_n}")
        if fast_s is None or slow_s is None:
            return False, ""
        if direction == "up":
            hit = bool(ind.cross_up(fast_s, slow_s).iloc[-1])
            return hit, f"MA{fast_n} 上穿 MA{slow_n}（金叉）"
        hit = bool(ind.cross_down(fast_s, slow_s).iloc[-1])
        return hit, f"MA{fast_n} 下穿 MA{slow_n}（死叉）"

    if rtype == "price_cross_ma":
        n = int(rule.get("ma", 20))
        direction = str(rule.get("direction", "up")).lower()
        ma_s = df.get(f"ma{n}")
        if ma_s is None:
            return False, ""
        close_s = df["close"]
        if direction == "up":
            hit = bool(ind.cross_up(close_s, ma_s).iloc[-1])
            return hit, f"收盘价上穿 MA{n}"
        hit = bool(ind.cross_down(close_s, ma_s).iloc[-1])
        return hit, f"收盘价下穿 MA{n}"

    if rtype == "macd_cross":
        direction = str(rule.get("direction", "up")).lower()
        if direction == "up":
            hit = bool(ind.cross_up(df["dif"], df["dea"]).iloc[-1])
            return hit, "MACD 金叉（DIF 上穿 DEA）"
        hit = bool(ind.cross_down(df["dif"], df["dea"]).iloc[-1])
        return hit, "MACD 死叉（DIF 下穿 DEA）"

    if rtype == "kdj_cross":
        direction = str(rule.get("direction", "up")).lower()
        if direction == "up":
            hit = bool(ind.cross_up(df["k"], df["d"]).iloc[-1])
            return hit, "KDJ 金叉"
        hit = bool(ind.cross_down(df["k"], df["d"]).iloc[-1])
        return hit, "KDJ 死叉"

    if rtype == "rsi_threshold":
        period = int(rule.get("period", 14))
        op = str(rule.get("op", "below")).lower()
        target = safe_float(rule.get("value", 30), 30) or 30
        value = safe_float(last.get(f"rsi{period}"))
        if value is None:
            return False, ""
        hit = value <= target if op == "below" else value >= target
        label = "低于" if op == "below" else "高于"
        return hit, f"RSI({period}) {value:.1f} {label} {target:.0f}"

    if rtype == "breakout":
        window = int(rule.get("window", 20))
        direction = str(rule.get("direction", "up")).lower()
        if direction == "up":
            prior_high = df["high"].shift(1).rolling(window, min_periods=window).max()
            ref = safe_float(prior_high.iloc[-1])
            if ref is None or close is None:
                return False, ""
            return close > ref, f"收盘 {close:.2f} 突破 {window} 日新高（前高 {ref:.2f}）"
        prior_low = df["low"].shift(1).rolling(window, min_periods=window).min()
        ref_low = safe_float(prior_low.iloc[-1])
        if ref_low is None or close is None:
            return False, ""
        return close < ref_low, f"收盘 {close:.2f} 跌破 {window} 日新低（前低 {ref_low:.2f}）"

    if rtype == "volume_spike":
        window = int(rule.get("window", 5))
        ratio = safe_float(rule.get("ratio", 2.0), 2.0) or 2.0
        vol_ma = safe_float(df["volume"].rolling(window, min_periods=window).mean().iloc[-1])
        volume = safe_float(last.get("volume"))
        if not vol_ma or volume is None:
            return False, ""
        actual = volume / vol_ma
        return actual >= ratio, f"成交量为 {window} 日均量的 {actual:.1f} 倍"

    if rtype == "drawdown_from_high":
        window = int(rule.get("window", 60))
        threshold = safe_float(rule.get("pct", 10), 10) or 10
        high = safe_float(df["high"].iloc[-window:].max())
        if not high or close is None:
            return False, ""
        drawdown = (1 - close / high) * 100
        return drawdown >= threshold, f"距 {window} 日高点回撤 {drawdown:.1f}%"

    return False, ""


# ------------------------------------------------------------------ 批量检查
def check_watchlist(cfg: Config, md: MarketData, store: Store,
                    items: list[dict[str, Any]] | None = None,
                    refresh: bool = False) -> tuple[list[Alert], list[str]]:
    """返回 (新命中的提醒, 错误信息列表)。同一规则同一交易日只提醒一次。"""
    items = items if items is not None else cfg.watchlist()
    new_alerts: list[Alert] = []
    errors: list[str] = []
    for item in items:
        if not item.get("rules"):
            continue
        sym = Symbol.parse(item["code"])
        try:
            df = md.bars(sym, days=300, refresh=refresh)
        except Exception as exc:
            errors.append(f"{sym.tencent}: {exc}")
            continue
        if df.empty:
            errors.append(f"{sym.tencent}: 无历史数据")
            continue
        name = item.get("name") or sym.code
        for alert in evaluate(sym.tencent, name, df, item["rules"]):
            if store.record_alert(alert.code, alert.rule, alert.date,
                                  alert.message, alert.price):
                new_alerts.append(alert)
    return new_alerts, errors


def notify(alerts: list[Alert], desktop: bool = False) -> None:
    """控制台 + 文件 + 可选桌面通知（WSLg 下的 notify-send）。"""
    if not alerts:
        return
    for alert in alerts:
        print(f"[预警] {alert.ts} {alert.name}({alert.code}) {alert.message}")
    if desktop and shutil.which("notify-send"):
        for alert in alerts[:5]:
            try:
                subprocess.run(
                    ["notify-send", "-a", "aquant", "-t", "8000",
                     f"{alert.name} {alert.code}", alert.message],
                    check=False, timeout=5,
                )
            except Exception:  # 桌面通知失败不影响主流程
                break


def close_price_text(price: float | None) -> str:
    return fmt_num(price, 2) if price is not None else "-"
