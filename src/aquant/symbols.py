"""证券代码规范化。

支持的输入形式：
    600519 / sh600519 / 600519.SH / SH600519 / 000001.SZ
    指数可显式带前缀：sh000001（上证指数）、sz399001（深证成指）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CODE_RE = re.compile(r"(\d{6})")


@dataclass(frozen=True)
class Symbol:
    code: str      # 6 位数字
    market: str    # sh / sz / bj

    @property
    def tencent(self) -> str:
        return f"{self.market}{self.code}"

    @property
    def sina(self) -> str:
        return f"{self.market}{self.code}"

    @property
    def secid(self) -> str:
        """东方财富 secid：1=沪市，0=深市/北交所。"""
        return f"{'1' if self.market == 'sh' else '0'}.{self.code}"

    @property
    def std(self) -> str:
        return f"{self.code}.{self.market.upper()}"

    @property
    def em_code(self) -> str:
        return f"{self.code}.{self.market.upper()}"

    def __str__(self) -> str:  # 便于打印
        return self.tencent

    @classmethod
    def parse(cls, raw: str | "Symbol") -> "Symbol":
        if isinstance(raw, Symbol):
            return raw
        text = str(raw).strip().lower().replace(" ", "")
        if not text:
            raise ValueError("空的证券代码")
        # 带后缀：600519.sh
        if "." in text:
            body, _, suffix = text.rpartition(".")
            suffix = suffix.upper()
            if suffix in ("SH", "SZ", "BJ"):
                digits = _CODE_RE.search(body)
                if not digits:
                    raise ValueError(f"无法识别的代码：{raw}")
                return cls(digits.group(1), suffix.lower())
        # 带前缀：sh600519
        for prefix in ("sh", "sz", "bj"):
            if text.startswith(prefix):
                digits = _CODE_RE.search(text)
                if not digits:
                    raise ValueError(f"无法识别的代码：{raw}")
                return cls(digits.group(1), prefix)
        digits = _CODE_RE.search(text)
        if not digits:
            raise ValueError(f"无法识别的代码：{raw}")
        code = digits.group(1)
        return cls(code, infer_market(code))


def infer_market(code: str) -> str:
    # 上交所：主板 60x、科创板 688、B 股 900、沪市基金/ETF 5xx、可转债 11x/13x
    if code.startswith(("60", "68", "90", "11", "13", "50", "51", "52", "56", "58")):
        return "sh"
    # 深交所：主板 00x、创业板 30x、B 股 200、深市基金/ETF 15x/16x、可转债 12x
    if code.startswith(("00", "30", "20", "12", "15", "16", "18")):
        return "sz"
    # 北交所：43x / 83x / 87x / 88x / 92x
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    # 其它（含指数）默认深市
    return "sz"


def detect_index(code: str) -> bool:
    """粗略判断是否为指数代码（sh000xxx / sz399xxx）。"""
    return code.startswith("000") and len(code) == 6 and code not in ("000001",)


def normalize_codes(raws: list[str] | tuple[str, ...]) -> list[Symbol]:
    out: list[Symbol] = []
    seen: set[str] = set()
    for raw in raws:
        sym = Symbol.parse(raw)
        if sym.tencent not in seen:
            seen.add(sym.tencent)
            out.append(sym)
    return out
