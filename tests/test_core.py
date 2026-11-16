"""核心模块单元测试（全部离线，用合成数据）。

运行：cd /path/to/quant && PYTHONPATH=src ~/.venvs/aquant/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from aquant import indicators as ind
from aquant.alerts import evaluate, rule_key, validate_rule
from aquant.backtest import BacktestConfig, build_position, run_portfolio, run_single
from aquant.store import Store
from aquant.symbols import Symbol, infer_market


def make_bars(n: int = 300, seed: int = 7, trend: float = 0.0006) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ret = rng.normal(trend, 0.018, n)
    close = 100 * np.cumprod(1 + ret)
    idx = pd.bdate_range("2023-01-02", periods=n)
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    vol = rng.integers(2_000_000, 8_000_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol, "amount": None}, index=idx)


class TestSymbols(unittest.TestCase):
    def test_parse_forms(self):
        cases = {
            "600519": ("600519", "sh"),
            "sh600519": ("600519", "sh"),
            "600519.SH": ("600519", "sh"),
            "000001": ("000001", "sz"),
            "300750": ("300750", "sz"),
            "510300": ("510300", "sh"),
            "688981": ("688981", "sh"),
            "830799": ("830799", "bj"),
        }
        for raw, (code, market) in cases.items():
            sym = Symbol.parse(raw)
            self.assertEqual((sym.code, sym.market), (code, market), raw)

    def test_helpers(self):
        sym = Symbol.parse("600519")
        self.assertEqual(sym.secid, "1.600519")
        self.assertEqual(sym.tencent, "sh600519")
        self.assertEqual(Symbol.parse("000001").secid, "0.000001")
        with self.assertRaises(ValueError):
            Symbol.parse("abc")

    def test_infer_market(self):
        self.assertEqual(infer_market("601398"), "sh")
        self.assertEqual(infer_market("002594"), "sz")
        self.assertEqual(infer_market("159915"), "sz")


class TestIndicators(unittest.TestCase):
    def setUp(self):
        self.df = make_bars(120)

    def test_add_indicators_columns(self):
        out = ind.add_indicators(self.df)
        for col in ("ma5", "ma20", "ma60", "dif", "dea", "macd", "rsi14",
                    "k", "d", "j", "boll_up", "boll_low", "atr14", "vol_ma5"):
            self.assertIn(col, out.columns)
        self.assertTrue(out["ma20"].iloc[-1] > 0)
        self.assertFalse(np.isnan(out["rsi14"].iloc[-1]))

    def test_rsi_bounds(self):
        rsi = ind.rsi(self.df["close"], 14).dropna()
        self.assertTrue(((rsi >= 0) & (rsi <= 100)).all())

    def test_latest_snapshot(self):
        snap = ind.latest_snapshot(self.df)
        self.assertIn("signals", snap)
        self.assertIsNotNone(snap["close"])
        self.assertEqual(snap["date"], self.df.index[-1].strftime("%Y-%m-%d"))

    def test_no_lookahead(self):
        """截断后重新计算，历史指标值应保持一致（无未来函数）。"""
        full = ind.add_indicators(self.df)
        partial = ind.add_indicators(self.df.iloc[:-5])
        col = "macd"
        self.assertAlmostEqual(float(full[col].iloc[-6]), float(partial[col].iloc[-1]), places=8)


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.df = make_bars(200, trend=0.002)

    def test_validate(self):
        self.assertIsNone(validate_rule({"type": "price_above", "value": 10}))
        self.assertIsNotNone(validate_rule({"type": "nope"}))
        self.assertIsNotNone(validate_rule({"type": "price_above"}))
        self.assertIsNotNone(validate_rule({"type": "ma_cross", "fast": 20, "slow": 5}))
        self.assertIsNotNone(validate_rule({"type": "ma_cross", "direction": "sideways"}))
        self.assertEqual(rule_key({"type": "rsi_threshold", "period": 14}), "rsi_threshold(period=14)")

    def test_price_rules(self):
        last = float(self.df["close"].iloc[-1])
        alerts = evaluate("sh600519", "测试", self.df,
                          [{"type": "price_above", "value": last * 0.9},
                           {"type": "price_below", "value": last * 0.5}])
        self.assertEqual(len(alerts), 1)
        self.assertIn("上穿", alerts[0].message)

    def test_pct_rule(self):
        alerts = evaluate("sh600519", "测试", self.df,
                          [{"type": "pct_change_above", "value": -99}])
        self.assertEqual(len(alerts), 1)

    def test_rsi_and_volume(self):
        alerts = evaluate("sh600519", "测试", self.df,
                          [{"type": "rsi_threshold", "period": 14, "op": "above", "value": 0},
                           {"type": "volume_spike", "ratio": 0.1, "window": 5}])
        self.assertEqual(len(alerts), 2)

    def test_bad_rule_ignored(self):
        alerts = evaluate("sh600519", "测试", self.df, [{"type": "unknown_rule"}])
        self.assertEqual(alerts, [])


class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.df = make_bars(400, trend=0.001)
        self.cfg = BacktestConfig(initial_capital=100_000)

    def test_buy_hold_tracks_price(self):
        pos = build_position(self.df, "buy_hold")
        result = run_single(self.df, pos, "sh600519", "buy_hold", {}, self.cfg)
        # 首个交易日收盘出信号、次日开盘成交，故与「收盘到收盘」涨幅存在缺口差异
        price_ret = (self.df["close"].iloc[-1] / self.df["close"].iloc[0] - 1) * 100
        self.assertAlmostEqual(result.metrics["total_return"], price_ret, delta=6.0)
        self.assertEqual(result.metrics["trades"], 0)  # 未平仓不计入交易
        self.assertGreater(result.metrics["final"], 0)

    def test_costs_reduce_return(self):
        pos = pd.Series(1, index=self.df.index)
        cheap = BacktestConfig(initial_capital=100_000, commission_rate=0,
                              min_commission=0, stamp_tax=0, transfer_fee=0, slippage_bps=0)
        expensive = BacktestConfig(initial_capital=100_000, commission_rate=0.003,
                                   min_commission=5, stamp_tax=0.001, transfer_fee=0.0001,
                                   slippage_bps=10)
        a = run_single(self.df, pos, "x", "buy_hold", {}, cheap)
        b = run_single(self.df, pos, "x", "buy_hold", {}, expensive)
        self.assertLess(b.metrics["final"], a.metrics["final"])

    def test_trades_and_metrics(self):
        pos = build_position(self.df, "ma_cross", fast=5, slow=20)
        result = run_single(self.df, pos, "sh600519", "ma_cross", {"fast": 5, "slow": 20}, self.cfg)
        self.assertGreaterEqual(result.metrics["trades"], 1)
        for key in ("cagr", "sharpe", "max_drawdown", "win_rate", "exposure"):
            self.assertIn(key, result.metrics)
        self.assertLessEqual(result.metrics["max_drawdown"], 0)
        for trade in result.trades:
            self.assertLessEqual(trade.entry_date, trade.exit_date)
            self.assertGreater(trade.shares, 0)

    def test_all_strategies_run(self):
        for name in ("ma_cross", "macd", "momentum", "boll_break", "breakout",
                     "rsi_reversion", "kdj", "buy_hold"):
            pos = build_position(self.df, name)
            self.assertEqual(len(pos), len(self.df))
            self.assertTrue(set(pos.unique()).issubset({0, 1}))
            result = run_single(self.df, pos, "x", name, {}, self.cfg)
            self.assertTrue(np.isfinite(result.metrics["final"]))

    def test_portfolio(self):
        df2 = make_bars(400, seed=11, trend=0.0005)
        data = {
            "sh600519": (self.df, build_position(self.df, "ma_cross")),
            "sz000001": (df2, build_position(df2, "ma_cross")),
        }
        result = run_portfolio(data, "ma_cross", {}, self.cfg)
        self.assertGreater(len(result.equity), 100)
        self.assertAlmostEqual(result.metrics["initial"], 100_000, delta=1)

    def test_t_plus_one_no_intraday(self):
        """仓位在收盘确定、次日开盘成交，因此首日不会产生交易。"""
        pos = pd.Series(1, index=self.df.index)
        result = run_single(self.df, pos, "x", "buy_hold", {}, self.cfg)
        self.assertAlmostEqual(result.equity.iloc[0], 100_000, delta=1)


class TestStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            df = make_bars(30)
            n = store.upsert_bars("sh600519", df, "day", "qfq")
            self.assertEqual(n, 30)
            loaded = store.load_bars("sh600519")
            self.assertEqual(len(loaded), 30)
            self.assertAlmostEqual(float(loaded["close"].iloc[-1]),
                                   float(df["close"].iloc[-1]), places=6)
            # 幂等：重复写入不产生重复行
            store.upsert_bars("sh600519", df, "day", "qfq")
            self.assertEqual(store.bar_count("sh600519"), 30)
            self.assertEqual(store.latest_bar_date("sh600519"),
                             df.index[-1].strftime("%Y-%m-%d"))
            # 预警去重
            self.assertTrue(store.record_alert("sh600519", "r1", "2024-01-02", "msg", 1.0))
            self.assertFalse(store.record_alert("sh600519", "r1", "2024-01-02", "msg", 1.0))
            self.assertTrue(store.record_alert("sh600519", "r1", "2024-01-03", "msg", 1.0))


class TestIntraday(unittest.TestCase):
    """分钟线解析（用固定报文，不联网）。"""

    def test_sina_intraday(self):
        import json
        from unittest.mock import patch as mpatch

        from aquant.data import sina

        payload = json.dumps([
            {"day": "2026-11-16 14:50:00", "open": "1449.91", "high": "1454.80",
             "low": "1449.03", "close": "1454.80", "volume": "209700", "amount": "304123456.0"},
            {"day": "2026-11-16 14:55:00", "open": "1454.00", "high": "1454.93",
             "low": "1451.50", "close": "1454.70", "volume": "215100", "amount": "312000000.0"},
        ])
        with mpatch("aquant.data.sina.http_get", return_value=payload):
            df = sina.intraday("600519", "5m", count=10)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["close"].iloc[-1], 1454.70)
        self.assertEqual(df["amount"].iloc[0], 304123456.0)
        self.assertTrue(isinstance(df.index, pd.DatetimeIndex))
        self.assertEqual(df.index[-1].strftime("%H:%M"), "14:55")

    def test_tencent_intraday(self):
        import json
        from unittest.mock import patch as mpatch

        from aquant.data import tencent

        payload = json.dumps({"code": 0, "data": {"sh600519": {"m5": [
            ["202611161455", "1454.00", "1454.80", "1454.88", "1451.56", "2033.00", {}, "1.63"],
        ]}}})
        with mpatch("aquant.data.tencent.http_get", return_value=payload):
            df = tencent.intraday("600519", "5m", count=5)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["volume"].iloc[0]), 203300.0)  # 手转股
        self.assertAlmostEqual(float(df["high"].iloc[0]), 1454.88)
        self.assertEqual(df.index[0].strftime("%Y-%m-%d %H:%M"), "2026-11-16 14:55")

    def test_bad_period(self):
        from unittest.mock import patch as mpatch

        from aquant.data import sina
        with mpatch("aquant.data.sina.http_get", return_value="[]"):
            with self.assertRaises(ValueError):
                sina.intraday("600519", "7m")


class TestWatchlistEdit(unittest.TestCase):
    def test_add_and_remove_roundtrip(self):
        from aquant.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cfg.watchlist_path = Path(tmp) / "watchlist.yaml"
            cfg.save_watchlist([
                {"code": "600519", "name": "贵州茅台", "rules": [{"type": "price_above", "value": 10}]},
            ])
            items = cfg.watchlist()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["name"], "贵州茅台")
            self.assertEqual(len(items[0]["rules"]), 1)
            # 补一只后再删
            items.append({"code": "000001", "name": "平安银行", "rules": []})
            cfg.save_watchlist(items)
            self.assertEqual(len(cfg.watchlist()), 2)
            keep = [i for i in cfg.watchlist() if i["code"] != "000001"]
            cfg.save_watchlist(keep)
            self.assertEqual([i["code"] for i in cfg.watchlist()], ["600519"])


class TestSnapshotResilience(unittest.TestCase):
    """全市场快照的分页容错（用桩替代网络）。"""

    @staticmethod
    def _row(code: str, name: str = "测试") -> dict:
        return {"f12": code, "f14": name, "f13": "1", "f2": 10.0, "f3": 1.0, "f4": 0.1,
                "f5": 1000, "f6": 1e7, "f8": 1.0, "f9": 10.0, "f10": 1.1, "f15": 10.2,
                "f16": 9.8, "f17": 10.0, "f18": 9.9, "f20": 1e9, "f21": 1e9,
                "f23": 1.2, "f115": 9.5}

    def test_partial_page_failure_tolerated(self):
        from unittest.mock import patch as mpatch

        from aquant.data import eastmoney
        seq = [(2, [self._row("600000")]), None, (2, [self._row("600001")])]
        with mpatch("aquant.data.eastmoney._clist_page", side_effect=seq):
            df = eastmoney.market_snapshot(page_size=1, max_pages=3)
        self.assertEqual(len(df), 2)
        self.assertEqual(set(df["code"]), {"600000", "600001"})
        self.assertEqual(df.iloc[0]["market"], "sh")

    def test_all_pages_fail_raises_readable_error(self):
        from unittest.mock import patch as mpatch

        from aquant.data import eastmoney
        with mpatch("aquant.data.eastmoney._clist_page", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                eastmoney.market_snapshot(page_size=1, max_pages=2)
        self.assertIn("东方财富", str(ctx.exception))

    def test_provider_falls_back_to_stale_cache(self):
        from unittest.mock import patch as mpatch

        from aquant.config import Config
        from aquant.data import MarketData

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cache = cfg.cache_dir / "snapshot.pkl"
            pd.DataFrame([{"code": "600000", "name": "浦发银行", "price": 10.0}]).to_pickle(cache)
            md = MarketData(cfg)
            boom = RuntimeError("Remote end closed connection")
            with mpatch("aquant.data.eastmoney.market_snapshot", side_effect=boom), \
                    mpatch("aquant.data.sina.market_snapshot", side_effect=boom):
                df = md.snapshot(refresh=True)
            self.assertEqual(df.iloc[0]["code"], "600000")

    def test_provider_switches_to_backup_source(self):
        from unittest.mock import patch as mpatch

        from aquant.config import Config
        from aquant.data import MarketData

        stub = pd.DataFrame([{"code": "600000", "name": "浦发银行", "price": 10.0,
                              "pe_ttm": 5.0, "pb": 0.6, "total_mv": 1e11}])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            md = MarketData(cfg)
            with mpatch("aquant.data.eastmoney.market_snapshot",
                        side_effect=RuntimeError("blocked")), \
                    mpatch("aquant.data.sina.market_snapshot", return_value=stub):
                df = md.snapshot(refresh=True)
            self.assertEqual(len(df), 1)
            self.assertEqual(md.store.get_meta("snapshot_source"), "sina")
            # 熔断标记应写入，下一次优先走备份源
            self.assertTrue(md.store.get_meta("snapshot_block_until:eastmoney"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
