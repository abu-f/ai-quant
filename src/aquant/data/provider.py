"""统一数据门面：本地缓存 + 多源回退 + 限速。

设计原则：
1. 日线落地到 SQLite，日常读取不重复请求网络；
2. 主源失败自动切换备用源，并把失败原因写进日志；
3. 全部为只读行情数据，不涉及任何交易指令。
"""

from __future__ import annotations

import logging
import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ..config import Config
from ..store import Store
from ..symbols import Symbol
from ..utils import now, recent_trade_date_guess, retry
from . import eastmoney, sina, tencent
from .base import Quote

log = logging.getLogger("aquant.data")

DAILY_PERIODS = {"day", "week", "month"}
INTRADAY_PERIODS = {"1m", "5m", "15m", "30m", "60m"}
PERIODS = DAILY_PERIODS | INTRADAY_PERIODS


@dataclass
class SyncResult:
    code: str
    rows: int = 0
    latest: str | None = None
    source: str = ""
    error: str | None = None


class MarketData:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.timeout = float(cfg.get("data.request_timeout", 12))
        self.retry_times = int(cfg.get("data.retry", 3))
        self.interval = int(cfg.get("data.min_interval_ms", 250))
        self.default_adjust = str(cfg.get("data.adjust", "qfq"))
        self.history_days = int(cfg.get("data.history_days", 800))
        self.bar_priority = list(cfg.get("data.bar_priority", ["tencent", "sina"]))
        self.quote_priority = list(cfg.get("data.quote_priority", ["eastmoney", "sina"]))
        self._snapshot_cache: pd.DataFrame | None = None
        self._intraday_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    # ------------------------------------------------------------------ 日线
    def bars(self, code: str | Symbol, period: str = "day", adjust: str | None = None,
             days: int | None = None, refresh: bool = False) -> pd.DataFrame:
        sym = Symbol.parse(code)
        period = period.lower()
        if period in INTRADAY_PERIODS:
            return self.intraday(sym, interval=period, count=days or 480, refresh=refresh)
        if period not in DAILY_PERIODS:
            raise ValueError(f"不支持的周期：{period}（可选 day/week/month/1m/5m/15m/30m/60m）")
        adjust = adjust or self.default_adjust
        days = days or self.history_days

        cached = self.store.load_bars(sym.tencent, period, adjust)
        need_fetch = refresh or cached.empty or self._is_stale(sym.tencent, period, adjust, cached)

        if need_fetch:
            try:
                fresh = self._fetch_bars(sym, period, adjust, days)
                if not fresh.empty:
                    self.store.upsert_bars(sym.tencent, fresh, period, adjust)
                    self.store.set_meta(f"last_bar_check:{sym.tencent}:{period}:{adjust}",
                                        now().isoformat(timespec="seconds"))
                    cached = self.store.load_bars(sym.tencent, period, adjust)
            except Exception as exc:  # 网络问题不应让命令崩溃
                log.warning("拉取 %s 日线失败，改用本地缓存：%s", sym.tencent, exc)
                if cached.empty:
                    raise
        result = self._drop_partial_bar(cached)
        if days and len(result) > days:
            result = result.tail(days)
        return result

    def _drop_partial_bar(self, df: pd.DataFrame) -> pd.DataFrame:
        """盘中未收盘的当日 K 线不参与指标计算，避免信号反复跳动。"""
        if df.empty or not self.cfg.get("data.exclude_intraday_bar", True):
            return df
        current = now()
        if current.hour < 15 or (current.hour == 15 and current.minute < 10):
            last = df.index[-1]
            if last.date() == current.date() and len(df) > 1:
                return df.iloc[:-1]
        return df

    def _is_stale(self, tencent_code: str, period: str, adjust: str,
                  cached: pd.DataFrame) -> bool:
        if cached.empty:
            return True
        ttl = int(self.cfg.get("data.cache_ttl_minutes", 30))
        stamp = self.store.get_meta(f"last_bar_check:{tencent_code}:{period}:{adjust}")
        if stamp:
            try:
                age = (now() - datetime.fromisoformat(stamp)).total_seconds() / 60
                if age < ttl:
                    return False
            except ValueError:
                pass
        latest = cached.index[-1].strftime("%Y-%m-%d")
        return latest < recent_trade_date_guess()

    def _fetch_bars(self, sym: Symbol, period: str, adjust: str, days: int) -> pd.DataFrame:
        errors: list[str] = []
        for name in self.bar_priority:
            try:
                if name == "tencent":
                    df = tencent.daily(sym, period=period, adjust=adjust, count=min(days, 1200),
                                       timeout=self.timeout, min_interval_ms=self.interval)
                elif name == "sina":
                    if period != "day":
                        continue
                    df = sina.daily(sym, count=min(days, 1023), timeout=self.timeout,
                                    min_interval_ms=self.interval)
                else:
                    continue
                if not df.empty:
                    log.debug("%s %s 数据来自 %s，共 %d 根", sym.tencent, period, name, len(df))
                    return df
                errors.append(f"{name}: 空数据")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                log.debug("%s 源失败：%s", name, exc)
        raise RuntimeError(f"{sym.tencent} 所有行情源均失败（{'; '.join(errors)}）")

    # ------------------------------------------------------------------ 分钟线
    def intraday(self, code: str | Symbol, interval: str = "5m", count: int = 480,
                 refresh: bool = False) -> pd.DataFrame:
        """分钟级 K 线（1/5/15/30/60 分钟）。带 30 秒内存缓存，不落库。"""
        sym = Symbol.parse(code)
        interval = interval.lower()
        if interval not in INTRADAY_PERIODS:
            raise ValueError(f"不支持的分钟周期：{interval}")
        ttl = float(self.cfg.get("data.intraday_ttl_seconds", 30))
        key = (sym.tencent, interval)
        cached = self._intraday_cache.get(key)
        if cached and not refresh and time.time() - cached[0] < ttl:
            return cached[1].tail(count)

        errors: list[str] = []
        for name in ("sina", "tencent"):
            try:
                if name == "sina":
                    df = sina.intraday(sym, interval=interval, count=min(count, 1023),
                                       timeout=self.timeout, min_interval_ms=self.interval)
                else:
                    df = tencent.intraday(sym, interval=interval, count=min(count, 800),
                                          timeout=self.timeout, min_interval_ms=self.interval)
                if not df.empty:
                    self._intraday_cache[key] = (time.time(), df)
                    log.debug("%s %s 分钟线来自 %s，共 %d 根", sym.tencent, interval, name, len(df))
                    return df
                errors.append(f"{name}: 空数据")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if cached:  # 全部失败时退回上一次的内存缓存
            log.warning("分钟线刷新失败，使用内存缓存：%s", "; ".join(errors))
            return cached[1].tail(count)
        raise RuntimeError(f"{sym.tencent} {interval} 分钟线获取失败（{'; '.join(errors)}）")

    # ------------------------------------------------------------------ 快照
    def quotes(self, codes: Iterable[str | Symbol], refresh: bool = False) -> list[Quote]:
        syms = [Symbol.parse(c) for c in codes]
        if not syms:
            return []
        ttl = float(self.cfg.get("data.cache_ttl_minutes", 30))
        cached = {q["code"]: q for q in self.store.load_quotes()}
        if not refresh and syms:
            hit = [cached.get(s.code) for s in syms]
            fresh = [q for q in hit if q and
                     (now() - datetime.strptime(q["_ts"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60 < ttl]
            if len(fresh) == len(syms):
                return [Quote.from_dict(q) for q in fresh]

        out: list[Quote] = []
        errors: list[str] = []
        # 单只查询走东财（带 PE/PB/市值）；多只走新浪批量（一次请求）
        if len(syms) == 1:
            for name in self.quote_priority:
                try:
                    if name == "eastmoney":
                        out = [eastmoney.quote(syms[0], timeout=self.timeout,
                                               min_interval_ms=self.interval)]
                    elif name == "sina":
                        out = sina.quotes(syms, timeout=self.timeout,
                                          min_interval_ms=self.interval)
                    if out:
                        break
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        else:
            for name in self.quote_priority:
                try:
                    if name == "sina":
                        out = sina.quotes(syms, timeout=self.timeout,
                                          min_interval_ms=self.interval)
                    elif name == "eastmoney":
                        out = [eastmoney.quote(s, timeout=self.timeout,
                                               min_interval_ms=self.interval) for s in syms]
                    if out:
                        break
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        if not out:
            log.warning("实时行情获取失败（%s），使用缓存", "; ".join(errors))
            return [Quote.from_dict(q) for q in (cached.get(s.code) for s in syms) if q]
        self.store.save_quotes(out)
        return out

    # ------------------------------------------------------------------ 全市场
    def enrich_fundamentals(self, quotes: list[Quote], limit: int = 30) -> list[Quote]:
        """为缺少估值字段的行情补充 PE/PB/市值（东财单只接口）。

        新浪批量接口只提供价格与成交信息，这里按需补齐；结果写回缓存，
        因此下一次刷新不会重复请求。
        """
        out: list[Quote] = []
        budget = limit
        for quote in quotes:
            if quote.pb is not None or budget <= 0:
                out.append(quote)
                continue
            budget -= 1
            try:
                detail = eastmoney.quote(f"{quote.market}{quote.code}",
                                         timeout=self.timeout,
                                         min_interval_ms=self.interval)
                detail.name = detail.name or quote.name
                self.store.save_quotes([detail])
                out.append(detail)
            except Exception as exc:
                log.debug("%s 估值字段补充失败：%s", quote.code, exc)
                out.append(quote)
        return out

    def snapshot(self, refresh: bool = False) -> pd.DataFrame:
        if self._snapshot_cache is not None and not refresh:
            return self._snapshot_cache
        ttl = float(self.cfg.get("data.snapshot_ttl_minutes", 10))
        cache_file = self.cfg.cache_dir / "snapshot.pkl"
        if not refresh and cache_file.exists():
            age = (now().timestamp() - cache_file.stat().st_mtime) / 60
            if age < ttl:
                try:
                    self._snapshot_cache = pd.read_pickle(cache_file)
                    return self._snapshot_cache
                except Exception:  # 缓存损坏则忽略
                    pass
        priority = list(self.cfg.get("data.snapshot_priority", ["eastmoney", "sina"]))
        # 熔断：某个源刚失败过，短时间内不再优先尝试，避免每次筛选都先等一轮超时
        blocked_until = self.store.get_meta(f"snapshot_block_until:{priority[0] if priority else ''}")
        if priority and blocked_until:
            try:
                if datetime.fromisoformat(blocked_until) > now():
                    log.debug("主快照源处于熔断期，直接使用备用源")
                    priority = priority[1:] + priority[:1]
            except ValueError:
                pass

        errors: list[str] = []
        for name in priority:
            try:
                if name == "eastmoney":
                    df = eastmoney.market_snapshot(timeout=max(self.timeout, 8),
                                                   min_interval_ms=self.interval)
                elif name == "sina":
                    df = sina.market_snapshot(min_interval_ms=max(self.interval, 220))
                else:
                    continue
                if df is not None and not df.empty:
                    df.to_pickle(cache_file)
                    self._snapshot_cache = df
                    self.store.set_meta("snapshot_source", name)
                    self.store.set_meta(f"snapshot_block_until:{name}", "")
                    if errors:
                        log.warning("全市场快照改由 %s 提供（%s）", name, "; ".join(errors))
                    else:
                        log.debug("全市场快照来自 %s，共 %d 只", name, len(df))
                    return df
                errors.append(f"{name}: 空数据")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                cooldown = int(self.cfg.get("data.snapshot_cooldown_minutes", 15))
                self.store.set_meta(
                    f"snapshot_block_until:{name}",
                    (now() + timedelta(minutes=cooldown)).isoformat(timespec="seconds"),
                )
                log.warning("全市场快照源 %s 失败：%s", name, exc)

        # 所有源都失败时退回本地缓存（哪怕已过期），保证选股功能仍可使用
        if cache_file.exists():
            try:
                self._snapshot_cache = pd.read_pickle(cache_file)
                stamp = datetime.fromtimestamp(cache_file.stat().st_mtime).strftime("%m-%d %H:%M")
                log.warning("全市场快照全部失败，改用 %s 的本地缓存（%s）",
                            stamp, "; ".join(errors))
                return self._snapshot_cache
            except Exception:
                pass
        raise RuntimeError(
            "全市场快照获取失败：东方财富与新浪接口均不可用（连接被重置/限流）。"
            "请稍后重试；技术类选股可改用 ./quant bars 查看单只数据。"
        )

    # ------------------------------------------------------------------ 财务
    def financials(self, code: str | Symbol, periods: int = 8,
                   refresh: bool = False) -> list[dict[str, Any]]:
        sym = Symbol.parse(code)
        cached = self.store.load_fundamentals(sym.tencent, max_age_minutes=12 * 60)
        if cached and not refresh:
            return cached.get("rows", [])
        try:
            rows = eastmoney.financials(sym, periods=periods, timeout=max(self.timeout, 15),
                                        min_interval_ms=self.interval)
            self.store.save_fundamentals(sym.tencent, {"rows": rows})
            return rows
        except Exception as exc:
            log.warning("%s 财务数据获取失败：%s", sym.tencent, exc)
            return cached.get("rows", []) if cached else []

    # ------------------------------------------------------------------ 批量同步
    def sync(self, codes: list[str], days: int | None = None,
             period: str = "day", adjust: str | None = None) -> list[SyncResult]:
        results: list[SyncResult] = []
        adjust = adjust or self.default_adjust
        for raw in codes:
            sym = Symbol.parse(raw)
            try:
                df = self._fetch_bars(sym, period, adjust, days or self.history_days)
                rows = self.store.upsert_bars(sym.tencent, df, period, adjust)
                self.store.set_meta(f"last_bar_check:{sym.tencent}:{period}:{adjust}",
                                    now().isoformat(timespec="seconds"))
                latest = df.index[-1].strftime("%Y-%m-%d") if not df.empty else None
                results.append(SyncResult(sym.tencent, rows, latest, _source_of(df)))
            except Exception as exc:
                results.append(SyncResult(sym.tencent, 0, None, "", str(exc)))
        return results


def _source_of(df: pd.DataFrame) -> str:
    return str(df.attrs.get("source", "")) or "network"
