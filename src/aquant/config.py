"""配置加载：默认值 + config/config.yaml 覆盖 + 环境变量。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "aquant",
        # 数据目录：留空则使用 $AQUANT_HOME 或 ~/.local/share/aquant
        "home": None,
    },
    "data": {
        # 日线主源 / 备用源；行情快照优先东财
        "bar_priority": ["tencent", "sina"],
        "quote_priority": ["eastmoney", "sina"],
        "adjust": "qfq",            # qfq 前复权 / hfq 后复权 / bfq 不复权
        "history_days": 800,        # 默认拉取的日线根数
        "request_timeout": 12,
        "retry": 3,
        "min_interval_ms": 250,     # 请求间隔，避免触发风控
        "cache_ttl_minutes": 30,    # 行情缓存有效期
        "snapshot_ttl_minutes": 10, # 全市场快照缓存
        "snapshot_priority": ["eastmoney", "sina"],  # 全市场列表源，失败自动切换
        "snapshot_cooldown_minutes": 15,  # 某个源失败后的熔断时长
        "intraday_ttl_seconds": 30, # 分钟线内存缓存（不落库）
    },
    "backtest": {
        "initial_capital": 100000.0,
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_tax": 0.0005,        # 卖出印花税
        "transfer_fee": 0.00001,    # 过户费
        "slippage_bps": 2.0,        # 单边滑点（万分之）
        "risk_free_rate": 0.02,
        "lot_size": 100,            # A 股一手 = 100 股
    },
    "alerts": {
        "check_interval": 60,       # watch 循环间隔（秒）
        "cooldown_minutes": 240,    # 同一规则重复提醒冷却时间
        "notify_desktop": False,    # 是否尝试 notify-send（WSLg 可用时）
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8770,
        "open_browser": False,
    },
    "ui": {
        "top_n": 20,
        "color": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _default_home() -> Path:
    env = os.environ.get("AQUANT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "aquant"


class Config:
    """点号取值的配置对象：cfg.get("data.adjust", "qfq")。"""

    def __init__(self, path: Path | None = None, home: Path | None = None):
        env_path = os.environ.get("AQUANT_CONFIG")
        self.path = Path(path or env_path or PROJECT_ROOT / "config" / "config.yaml")
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件格式错误（应为映射）：{self.path}")
            self.data = _deep_merge(self.data, loaded)

        configured_home = self.data.get("app", {}).get("home")
        self.home = Path(home or configured_home or "").expanduser() if (home or configured_home) else _default_home()
        self.home.mkdir(parents=True, exist_ok=True)

        self.db_path = self.home / "aquant.db"
        self.report_dir = self.home / "reports"
        self.log_dir = self.home / "logs"
        self.cache_dir = self.home / "cache"
        for d in (self.report_dir, self.log_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.watchlist_path = PROJECT_ROOT / "config" / "watchlist.yaml"

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ---- 自选股 ----
    def watchlist(self) -> list[dict[str, Any]]:
        if not self.watchlist_path.exists():
            return []
        raw = yaml.safe_load(self.watchlist_path.read_text(encoding="utf-8")) or {}
        items = raw.get("watchlist", raw) if isinstance(raw, dict) else raw
        out: list[dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, str):
                item = {"code": item}
            if not isinstance(item, dict) or not item.get("code"):
                continue
            item = dict(item)
            item["code"] = str(item["code"]).strip()
            item.setdefault("rules", [])
            item.setdefault("name", "")
            out.append(item)
        return out

    def save_watchlist(self, items: list[dict[str, Any]]) -> None:
        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 自选股与预警规则（aquant 自动维护，手工编辑同样生效）\n"
            "# 代码支持 600519 / sh600519 / 600519.SH\n"
            "# 规则说明：./quant alert rules\n\n"
        )
        body = yaml.safe_dump({"watchlist": items}, allow_unicode=True, sort_keys=False)
        self.watchlist_path.write_text(header + body, encoding="utf-8")


def load_config(path: Path | None = None) -> Config:
    return Config(path)
