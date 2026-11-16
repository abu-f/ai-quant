"""本地可视化工作台（Flask + ECharts）。

默认只监听 127.0.0.1，数据全部来自本地 SQLite/公开行情接口，无任何交易接口。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .. import indicators as ind
from ..backtest import BacktestConfig, build_position, run_portfolio, run_single
from ..backtest.engine import metrics_lines
from ..config import Config
from ..data import INTRADAY_PERIODS, MarketData
from ..screener import PRESETS, screen
from ..store import Store
from ..symbols import Symbol
from ..utils import fmt_big, fmt_num, safe_float

HERE = Path(__file__).resolve().parent


def create_app(cfg: Config, store: Store | None = None,
               md: MarketData | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(HERE / "static"),
                template_folder=str(HERE / "templates"))
    app.json.ensure_ascii = False
    store = store or Store(cfg.db_path)
    md = md or MarketData(cfg, store)
    app.config["AQUANT_CFG"] = cfg

    # ---------------------------------------------------------------- 统一错误响应
    def wants_json() -> bool:
        return request.path.startswith("/api/")

    @app.errorhandler(HTTPException)
    def on_http_error(exc: HTTPException):  # type: ignore[misc]
        if wants_json():
            return jsonify({"error": f"{exc.name}（HTTP {exc.code}）",
                            "code": exc.code}), exc.code or 500
        return exc

    @app.errorhandler(Exception)
    def on_error(exc: Exception):  # type: ignore[misc]
        if isinstance(exc, ValueError):
            # 代码/周期等参数问题属于用户输入错误，返回 400 而不是 500
            if wants_json():
                return jsonify({"error": str(exc)}), 400
            return (f"<h1>参数错误</h1><pre>{html.escape(str(exc))}</pre>"), 400
        app.logger.exception("接口异常：%s", exc)
        if wants_json():
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        return (f"<h1>服务内部错误</h1><pre>{html.escape(str(exc))}</pre>"
                f"<p>详细日志见数据目录 logs/aquant.log</p>"), 500

    # ---------------------------------------------------------------- 页面
    @app.get("/")
    def index() -> str:
        return render_template("index.html", version="0.1.0",
                               presets=[{"key": k, "title": v.title, "desc": v.description}
                                        for k, v in PRESETS.items()])

    # ---------------------------------------------------------------- API
    @app.get("/api/watchlist")
    def api_watchlist() -> Any:
        items = cfg.watchlist()
        if not items:
            return jsonify({"items": [], "message": "自选股为空，请编辑 config/watchlist.yaml"})
        codes = [i["code"] for i in items]
        try:
            quotes = {q.code: q for q in md.quotes(codes, refresh=request.args.get("refresh") == "1")}
            quotes = {q.code: q for q in md.enrich_fundamentals(list(quotes.values()))}
        except Exception as exc:
            quotes = {}
            app.logger.warning("行情获取失败：%s", exc)
        rows = []
        for item in items:
            sym = Symbol.parse(item["code"])
            q = quotes.get(sym.code)
            snap: dict[str, Any] = {}
            try:
                bars = md.bars(sym, days=200, refresh=False)
                if not bars.empty:
                    snap = ind.latest_snapshot(bars)
            except Exception:
                snap = {}
            rows.append({
                "code": sym.tencent,
                "raw_code": sym.code,
                "market": sym.market,
                "name": (q.name if q else "") or item.get("name") or sym.code,
                "price": q.price if q else snap.get("close"),
                "pct": q.pct if q else snap.get("pct"),
                "amount": q.amount if q else None,
                "turnover": q.turnover if q else None,
                "pe": (q.pe_ttm if q.pe_ttm is not None else q.pe) if q else None,
                "pb": q.pb if q else None,
                "mv": q.total_mv if q else None,
                "ma20": snap.get("ma20"),
                "rsi": snap.get("rsi"),
                "signals": snap.get("signals", []),
                "note": item.get("note", ""),
            })
        return jsonify({"items": rows, "ts": pd.Timestamp.now().strftime("%H:%M:%S")})

    @app.get("/api/kline")
    def api_kline() -> Any:
        raw = request.args.get("code", "600519")
        period = (request.args.get("period") or "day").lower()
        days = int(request.args.get("days", 250))
        adjust = request.args.get("adjust") or None
        sym = Symbol.parse(raw)
        is_intraday = period in INTRADAY_PERIODS
        if is_intraday:
            df = md.intraday(sym, interval=period, count=max(days, 120))
        else:
            df = md.bars(sym, period=period, adjust=adjust, days=max(days, 120), refresh=False)
        if df.empty:
            return jsonify({"error": f"{sym.tencent} 无 {period} 数据，请先执行 sync 或稍后重试"}), 404
        df = df.tail(days)
        enriched = ind.add_indicators(df)
        name = ""
        try:
            q = md.quotes([sym.tencent], refresh=False)
            name = q[0].name if q else ""
        except Exception:
            name = ""

        def series(column: str) -> list[float | None]:
            if column not in enriched:
                return []
            return [None if pd.isna(v) else round(float(v), 4) for v in enriched[column]]

        stamp = "%m-%d %H:%M" if is_intraday else "%Y-%m-%d"
        payload = {
            "code": sym.tencent,
            "name": name,
            "period": period,
            "is_intraday": is_intraday,
            "dates": [d.strftime(stamp) for d in enriched.index],
            "candles": [[round(float(r["open"]), 3), round(float(r["close"]), 3),
                         round(float(r["low"]), 3), round(float(r["high"]), 3)]
                        for _, r in enriched.iterrows()],
            "volumes": [[i, round(float(r["volume"]) / 100, 0),
                         "up" if r["close"] >= r["open"] else "down"]
                        for i, (_, r) in enumerate(enriched.iterrows())],
            "ma": {f"MA{n}": series(f"ma{n}") for n in (5, 10, 20, 60)},
            "macd": {"DIF": series("dif"), "DEA": series("dea"), "MACD": series("macd")},
            "rsi": series("rsi14"),
            "snapshot": ind.latest_snapshot(df),
        }
        return jsonify(payload)

    @app.get("/api/watchlist/codes")
    def api_watch_codes() -> Any:
        items = cfg.watchlist()
        return jsonify({
            "codes": [Symbol.parse(i["code"]).tencent for i in items],
            "items": [{"code": Symbol.parse(i["code"]).tencent,
                       "name": i.get("name") or Symbol.parse(i["code"]).code}
                      for i in items],
        })

    @app.post("/api/watchlist/add")
    def api_watch_add() -> Any:
        data = request.get_json(silent=True) or {}
        raw = str(data.get("code") or "").strip()
        if not raw:
            return jsonify({"error": "缺少 code"}), 400
        sym = Symbol.parse(raw)
        items = cfg.watchlist()
        if any(Symbol.parse(i["code"]).tencent == sym.tencent for i in items):
            return jsonify({"ok": True, "message": f"{sym.tencent} 已在自选股中"})
        name = str(data.get("name") or "").strip()
        if not name:
            try:
                quotes = md.quotes([sym.tencent], refresh=True)
                name = quotes[0].name if quotes else ""
            except Exception:
                name = ""
        items.append({"code": sym.code, "name": name or sym.code, "note": "", "rules": []})
        cfg.save_watchlist(items)
        try:  # 顺手把日线拉下来，页面立刻有指标
            md.sync([sym.tencent], days=400)
        except Exception as exc:
            app.logger.warning("新增自选后同步失败：%s", exc)
        return jsonify({"ok": True, "count": len(items), "name": name or sym.code})

    @app.post("/api/watchlist/remove")
    def api_watch_remove() -> Any:
        data = request.get_json(silent=True) or {}
        raw = str(data.get("code") or "").strip()
        if not raw:
            return jsonify({"error": "缺少 code"}), 400
        target = Symbol.parse(raw).tencent
        items = cfg.watchlist()
        keep = [i for i in items if Symbol.parse(i["code"]).tencent != target]
        if len(keep) == len(items):
            return jsonify({"error": f"{target} 不在自选股中"}), 404
        cfg.save_watchlist(keep)
        return jsonify({"ok": True, "count": len(keep), "removed": target})

    @app.get("/api/financials")
    def api_financials() -> Any:
        sym = Symbol.parse(request.args.get("code", "600519"))
        rows = md.financials(sym, periods=8)
        quotes = md.quotes([sym.tencent], refresh=False)
        quote = quotes[0].to_dict() if quotes else {}
        return jsonify({"code": sym.tencent, "quote": quote, "rows": rows})

    @app.get("/api/backtest")
    def api_backtest() -> Any:
        raw = request.args.get("code", "600519")
        strategy = request.args.get("strategy", "ma_cross")
        days = int(request.args.get("days", 500))
        params_raw = request.args.get("params", "{}")
        try:
            params = json.loads(params_raw) if params_raw else {}
        except json.JSONDecodeError:
            return jsonify({"error": "params 必须是 JSON 字符串"}), 400
        codes = [c for c in raw.split(",") if c.strip()]
        bt_cfg = BacktestConfig.from_config(cfg)
        capital = request.args.get("capital")
        if capital:
            try:
                bt_cfg.initial_capital = float(capital)
            except ValueError:
                return jsonify({"error": "capital 必须是数字"}), 400
        data: dict[str, tuple[pd.DataFrame, pd.Series]] = {}
        for code in codes:
            sym = Symbol.parse(code)
            df = md.bars(sym, days=max(days, 250))
            if df.empty:
                continue
            try:
                pos = build_position(df, strategy, **params)
            except Exception as exc:
                return jsonify({"error": f"策略参数错误：{exc}"}), 400
            data[sym.tencent] = (df, pos)
        if not data:
            return jsonify({"error": "没有可用数据，请先 sync"}), 404
        if len(data) == 1:
            first = list(data)[0]
            result = run_single(data[first][0], data[first][1], first, strategy, params, bt_cfg)
        else:
            result = run_portfolio(data, strategy, params, bt_cfg)
        payload = result.to_dict()
        payload["metrics_lines"] = metrics_lines(result.metrics)
        return jsonify(payload)

    @app.get("/api/screen")
    def api_screen() -> Any:
        preset = request.args.get("preset", "value")
        limit = int(request.args.get("limit", 20))
        pool = int(request.args.get("pool", 150))
        try:
            df, preset_obj = screen(cfg, md, preset_key=preset, limit=limit, pool=pool)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.warning("选股失败：%s", exc)
            return jsonify({"error": f"选股暂时失败：{exc}", "retryable": True}), 503
        if df is None or df.empty:
            return jsonify({"preset": preset_obj.title, "desc": preset_obj.description, "items": []})
        items = []
        for _, row in df.iterrows():
            items.append({
                "code": f"{row.get('market', '')}{row.get('code', '')}",
                "name": row.get("name", ""),
                "price": _num(row.get("price")),
                "pct": _num(row.get("pct")),
                "pe_ttm": _num(row.get("pe_ttm")),
                "pb": _num(row.get("pb")),
                "total_mv_text": fmt_big(row.get("total_mv")),
                "amount_text": fmt_big(row.get("amount")),
                "turnover": _num(row.get("turnover")),
                "volume_ratio": _num(row.get("volume_ratio")),
                "rsi": _num(row.get("rsi")),
            })
        return jsonify({"preset": preset_obj.title, "desc": preset_obj.description, "items": items})

    @app.get("/api/alerts")
    def api_alerts() -> Any:
        limit = int(request.args.get("limit", 30))
        return jsonify({"items": store.recent_alerts(limit)})

    @app.get("/api/stats")
    def api_stats() -> Any:
        stats = dict(store.stats())
        stats["home"] = str(cfg.home)
        stats["db"] = str(store.path)
        return jsonify(stats)

    @app.post("/api/sync")
    def api_sync() -> Any:
        items = cfg.watchlist()
        codes = [i["code"] for i in items] or ["600519"]
        days = int(request.args.get("days", 500))
        results = md.sync(codes, days=days)
        return jsonify({"items": [r.__dict__ for r in results]})

    return app


def _num(value: Any) -> float | None:
    num = safe_float(value)
    return round(num, 2) if num is not None else None
