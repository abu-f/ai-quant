"""通用工具：时间、数值格式化、重试。"""

from __future__ import annotations

import functools
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Sequence, TypeVar

log = logging.getLogger("aquant")

T = TypeVar("T")


def now() -> datetime:
    """本地当前时间（WSL 时区应设为 Asia/Shanghai）。"""
    return datetime.now()


def ts_text(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def safe_float(value: Any, default: float | None = None) -> float | None:
    """把接口返回的空值/'-'/'--' 统一转成 None。"""
    if value is None or value == "" or value == "-" or value == "--":
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return num


def fmt_num(value: Any, digits: int = 2, dash: str = "-") -> str:
    num = safe_float(value)
    if num is None:
        return dash
    return f"{num:,.{digits}f}"


def fmt_price(value: Any, digits: int = 2, dash: str = "-") -> str:
    """价格不带千分位，避免表格被撑宽。"""
    num = safe_float(value)
    if num is None:
        return dash
    return f"{num:.{digits}f}"


def fmt_pct(value: Any, digits: int = 2, dash: str = "-") -> str:
    num = safe_float(value)
    if num is None:
        return dash
    return f"{num:+.{digits}f}%"


def fmt_big(value: Any, dash: str = "-") -> str:
    """大数字转 亿/万。"""
    num = safe_float(value)
    if num is None:
        return dash
    abs_num = abs(num)
    if abs_num >= 1e8:
        return f"{num / 1e8:,.2f}亿"
    if abs_num >= 1e4:
        return f"{num / 1e4:,.2f}万"
    return f"{num:,.2f}"


def retry(times: int = 3, delay: float = 0.6, backoff: float = 2.0,
          exceptions: tuple[type[Exception], ...] = (Exception,)) -> Callable:
    """轻量重试装饰器（指数退避）。"""

    def deco(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last: Exception | None = None
            wait = delay
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last = exc
                    if attempt == times:
                        break
                    log.debug("%s 第 %d 次失败：%s，%.1fs 后重试", func.__name__, attempt, exc, wait)
                    time.sleep(wait)
                    wait *= backoff
            assert last is not None
            raise last

        return wrapper

    return deco


def chunked(items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i: i + size]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_date(text: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期：{text}")


def recent_trade_date_guess() -> str:
    """粗略估计最近一个交易日（仅用于缓存过期判断，不参与交易逻辑）。"""
    dt = now()
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    if dt.hour < 16:
        dt -= timedelta(days=1)
        while dt.weekday() >= 5:
            dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")
