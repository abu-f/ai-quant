"""公开行情数据源（腾讯 + 新浪，免 Key）。

设计取舍：
- 日K 走腾讯 fqkline 的**不复权**原始价（契约要求 provider 不得自行复权，
  复权交给 adj_factor + enriched 管道；本插件暂不提供 adj_factor，见 README）。
- 分钟K 走新浪，单标的最多 1023 根，含成交额；超出部分由多次调用/更短周期覆盖。
- 实时快照走腾讯批量报价：全市场 5578 只约 7 批、4 并发，一轮 4~6s，
  因此**内存缓存 20s**，避免 6s 轮询把公开接口打爆。
- 标的维表走新浪 Market_Center：全市场约 20s，缓存 12 小时，后台线程预热，
  不阻塞实时轮询线程。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import ClassVar

import httpx
import polars as pl

from app.data_providers.base import ProviderRequestError

from . import client as pub

logger = logging.getLogger(__name__)

DAILY_SCHEMA = {
    "symbol": pl.Utf8, "date": pl.Date,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}
MINUTE_SCHEMA = {
    "symbol": pl.Utf8, "datetime": pl.Datetime("ms"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}

SNAPSHOT_TTL_S = 20.0
UNIVERSE_TTL_S = 12 * 3600.0
DAILY_MAX_BARS = 800
MINUTE_MAX_BARS = 1023
REQUEST_GAP_S = 0.05


class _Config:
    """宿主用 ``dataset in provider.config.datasets`` 判断路由，value 内容不参与逻辑。"""

    datasets: ClassVar[dict[str, dict]] = {"daily": {}, "minute": {}, "realtime": {}}


class Provider:
    name = "public_cn"
    builtin = True

    def __init__(self) -> None:
        self.config = _Config()
        self._client: httpx.Client | None = None
        self._quote_cache: list[dict] = []
        self._quote_at = 0.0
        self._universe: list[dict] = []
        self._universe_at = 0.0
        self._universe_loading = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 基础
    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                finally:
                    self._client = None
            self._quote_cache = []
            self._quote_at = 0.0

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(
                    timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                    headers={"User-Agent": pub.UA},
                    follow_redirects=True,
                )
            return self._client

    # ------------------------------------------------------------------ 标的维表
    def _ensure_universe(self, background: bool = True) -> list[dict]:
        """返回标的列表；未就绪时可选在后台线程预热（构造必须快，不能在这里等 20s）。"""
        now = time.time()
        if self._universe and now - self._universe_at < UNIVERSE_TTL_S:
            return self._universe
        with self._lock:
            if self._universe_loading:
                return self._universe
            self._universe_loading = True

        def load() -> None:
            try:
                rows = pub.fetch_universe(self._http())
                with self._lock:
                    self._universe = rows
                    self._universe_at = time.time()
                logger.info("公开行情标的维表已加载：%d 只", len(rows))
            except Exception as exc:  # noqa: BLE001
                logger.warning("标的维表加载失败：%s", exc)
            finally:
                with self._lock:
                    self._universe_loading = False

        if background:
            threading.Thread(target=load, name="public_cn-universe", daemon=True).start()
            return self._universe
        load()
        return self._universe

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """标的维表（仅股票）。ETF/指数不在本插件覆盖范围，返回空由上层走内置源。"""
        if asset_type != "stock":
            return []
        rows = self._ensure_universe(background=False)
        logger.info("%s 标的维表：%d 只（asset_type=%s）", self.name, len(rows), asset_type)
        return rows

    # ------------------------------------------------------------------ 日K
    def iter_daily(self, symbols: list[str], start_time: datetime | None,
                   end_time: datetime | None, asset_type: str = "stock",
                   on_chunk_done: Callable[[int, int], None] | None = None
                   ) -> Iterator[pl.DataFrame]:
        """逐批返回日K（有界批大小，避免全市场同步时把结果全堆在内存里）。"""
        start = start_time.date() if isinstance(start_time, datetime) else start_time
        end = end_time.date() if isinstance(end_time, datetime) else end_time
        symbols = [s for s in symbols if pub.normalize_symbol(s)]
        total = len(symbols)
        if not total:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return

        batch: list[tuple] = []
        failed: list[str] = []
        for idx, symbol in enumerate(symbols, start=1):
            try:
                rows = pub.fetch_daily(self._http(), symbol, start, end, DAILY_MAX_BARS)
                if rows:
                    if start:
                        rows = [r for r in rows if r[1] >= start]
                    if end:
                        rows = [r for r in rows if r[1] <= end]
                    batch.extend(rows)
            except Exception as exc:  # noqa: BLE001
                failed.append(symbol)
                logger.debug("日K 拉取失败 %s：%s", symbol, exc)
            finally:
                time.sleep(REQUEST_GAP_S)

            if len(batch) >= 5000 or idx == total:
                if batch:
                    yield pl.DataFrame(batch, schema=DAILY_SCHEMA, orient="row")
                    batch = []
                if on_chunk_done:
                    on_chunk_done(idx, total)

        if failed:
            logger.warning("日K 抓取失败 %d/%d 只（示例：%s）", len(failed), total, failed[:5])
        if on_chunk_done:
            on_chunk_done(total, total)

    def get_daily(self, symbols: list[str], start_time: datetime | None,
                  end_time: datetime | None, asset_type: str = "stock",
                  on_chunk_done: Callable[[int, int], None] | None = None) -> pl.DataFrame:
        chunks = [df for df in self.iter_daily(symbols, start_time, end_time,
                                               asset_type, on_chunk_done) if not df.is_empty()]
        if not chunks:
            return pl.DataFrame(schema=DAILY_SCHEMA)
        return pl.concat(chunks, how="diagonal_relaxed")

    # ------------------------------------------------------------------ 分钟K
    def get_minute(self, symbols: list[str], start_time: datetime | None,
                   end_time: datetime | None, asset_type: str = "stock",
                   freq: str = "1m",
                   on_chunk_done: Callable[[int, int], None] | None = None) -> pl.DataFrame:
        """分钟K：单标的最多 1023 根；start/end 只做本地过滤（上游不支持区间参数）。"""
        symbols = [s for s in symbols if pub.normalize_symbol(s)]
        if not symbols:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return pl.DataFrame(schema=MINUTE_SCHEMA)

        rows: list[tuple] = []
        failed: list[str] = []
        for idx, symbol in enumerate(symbols, start=1):
            try:
                got = pub.fetch_minute(self._http(), symbol, freq=freq, count=MINUTE_MAX_BARS)
                if start_time:
                    got = [r for r in got if r[1] >= start_time.replace(tzinfo=None)]
                if end_time:
                    got = [r for r in got if r[1] <= end_time.replace(tzinfo=None)]
                rows.extend(got)
            except Exception as exc:  # noqa: BLE001
                failed.append(symbol)
                logger.debug("分钟K 拉取失败 %s：%s", symbol, exc)
            finally:
                time.sleep(REQUEST_GAP_S)
            if on_chunk_done:
                on_chunk_done(idx, len(symbols))

        if failed and len(failed) == len(symbols):
            raise ProviderRequestError(f"分钟K 全部失败（{len(failed)} 只，freq={freq}）")
        if failed:
            logger.warning("分钟K 部分失败 %d/%d（示例：%s）", len(failed), len(symbols), failed[:5])
        if not rows:
            return pl.DataFrame(schema=MINUTE_SCHEMA)
        return pl.DataFrame(rows, schema=MINUTE_SCHEMA, orient="row")

    # ------------------------------------------------------------------ 实时
    def get_realtime(self) -> list[dict]:
        """全市场实时快照：失败软返回 []（不阻断轮询线程），缓存 20s。"""
        now = time.time()
        if self._quote_cache and now - self._quote_at < SNAPSHOT_TTL_S:
            return self._quote_cache

        universe = self._ensure_universe(background=True)
        if not universe:
            logger.info("全市场快照等待标的维表就绪（后台加载中）")
            return self._quote_cache

        symbols = [row["symbol"] for row in universe]
        try:
            rows = pub.fetch_quotes(self._http(), symbols)
        except Exception as exc:  # noqa: BLE001
            logger.warning("全市场实时快照失败：%s", exc)
            return self._quote_cache
        if not rows:
            logger.warning("全市场实时快照为空（请求 %d 只）", len(symbols))
            return self._quote_cache

        self._quote_cache = rows
        self._quote_at = now
        logger.info("全市场实时快照：%d 条（请求 %d 只）", len(rows), len(symbols))
        return rows

    def get_realtime_symbols(self, symbols: list[str], *, asset_type: str = "stock"
                             ) -> list[dict] | None:
        """按码补拉（自选/详情），不走全市场缓存，保证最新。失败返回 None。"""
        wanted = [s for s in symbols if pub.normalize_symbol(s)]
        if not wanted:
            return []
        try:
            return pub.fetch_quotes(self._http(), wanted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 按码补拉行情失败：%s", self.name, exc)
            return None

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数快照（腾讯报价同样支持 sh000001 等）。失败返回 None 以保留上轮缓存。"""
        wanted = [s for s in symbols if pub.normalize_symbol(s)]
        if not wanted:
            return []
        try:
            return pub.fetch_quotes(self._http(), wanted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 指数行情失败：%s", self.name, exc)
            return None

    # ------------------------------------------------------------------ 试拉
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        sample = [pub.normalize_symbol(s) for s in (symbols or ["600519", "000001"])]
        sample = [s for s in sample if s][:3]
        try:
            if dataset == "daily":
                df = self.get_daily(sample, None, None)
            elif dataset == "minute":
                df = self.get_minute(sample, None, None, freq="1m")
            elif dataset == "realtime":
                rows = self.get_realtime_symbols(sample) or []
                df = pl.DataFrame(rows) if rows else pl.DataFrame()
            else:
                return {"provider": self.name, "dataset": dataset, "rows": 0,
                        "columns": [], "preview": [],
                        "error": f"本插件未声明数据集 {dataset}"}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [], "error": str(exc)}
        preview = df.head(3).to_dicts() if not df.is_empty() else []
        return {
            "provider": self.name, "dataset": dataset, "rows": df.height,
            "columns": df.columns, "preview": preview,
        }
