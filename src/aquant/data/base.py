"""行情数据结构与 HTTP 基础工具。"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..utils import now, safe_float, ts_text

log = logging.getLogger("aquant.data")

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

_session: requests.Session | None = None
_last_request_at = 0.0


def _build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        # 连接级重试交给 http_get 统一处理（便于重置会话、控制次数），
        # 这里只依赖连接池，避免两层重试叠加导致失败时特别慢。
        total=0, connect=0, read=0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session()
    return _session


def reset_session() -> None:
    """连接被对端重置后换一条干净的连接池。"""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:  # 关闭失败无所谓
            pass
    _session = None


def http_get(url: str, params: dict[str, Any] | None = None, *,
             timeout: float = 12, headers: dict[str, str] | None = None,
             min_interval_ms: int = 250, encoding: str | None = None,
             attempts: int = 3) -> str:
    """带全局限速 + 连接级重试的 GET，返回文本。

    公开行情接口经常出现 RemoteDisconnected / 连接被重置，这类错误重试即可恢复；
    因此这里在连接层做 3 次退避重试，失败时重建会话，避免复用到坏连接。
    """
    global _last_request_at
    gap = min_interval_ms / 1000.0
    last_error: Exception | None = None
    for attempt in range(1, max(attempts, 1) + 1):
        elapsed = time.time() - _last_request_at
        if elapsed < gap:
            time.sleep(gap - elapsed)
        try:
            resp = session().get(url, params=params, timeout=timeout, headers=headers)
            _last_request_at = time.time()
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
            return resp.text
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            last_error = exc
            _last_request_at = time.time()
            reset_session()
            if attempt == attempts:
                break
            wait = 0.6 * (2 ** (attempt - 1))
            log.debug("请求失败（%s），%.1fs 后重试 %d/%d：%s", type(exc).__name__, wait, attempt, attempts, url)
            time.sleep(wait)
    assert last_error is not None
    raise last_error


@dataclass
class Quote:
    code: str                       # 6 位代码
    name: str = ""
    price: float | None = None
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    change: float | None = None
    pct: float | None = None
    volume: float | None = None     # 股
    amount: float | None = None     # 元
    turnover: float | None = None   # 换手率 %
    volume_ratio: float | None = None
    pe: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    total_mv: float | None = None
    float_mv: float | None = None
    ts: str = field(default_factory=ts_text)
    source: str = ""
    market: str = ""

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}" if self.market else self.code

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Quote":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def age_minutes(self) -> float:
        from datetime import datetime
        try:
            then = datetime.strptime(self.ts, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return 1e9
        return (now() - then).total_seconds() / 60.0


def clean(value: Any) -> float | None:
    """东财在无数据时返回 '-'，统一处理。"""
    return safe_float(value)
