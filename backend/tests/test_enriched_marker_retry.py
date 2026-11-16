"""Windows 读锁竞态下 enriched 世代标记原子替换的重试测试。

根因: ``_write_marker`` 用临时文件 + ``os.replace`` 原子替换 ``.matrix_generation_*.json``。
Windows 的 ``MoveFileEx(REPLACE_EXISTING)`` 在目标文件仍被任何句柄打开时返回
ERROR_ACCESS_DENIED (WinError 5), 而 CPython 的 ``open()`` / ``Path.read_text()``
不带 ``FILE_SHARE_DELETE``。世代标记被 ``_read_marker`` 在本进程内高频读取
(行情线程、各 HTTP 请求的 ``get_enriched_generation_snapshot``), 全市场补数又要
逐分区 ``commit()`` —— 两者重叠即稳定失败。

实测表现: 按需补齐日K 在写第 48 个 enriched 分区时抛
``PermissionError: [WinError 5] ... .matrix_generation_stock.json.*.tmp ->
.matrix_generation_stock.json``, 接口 502, 标的从此只剩半截历史 (前端显示 1 根K)。

修复: ``_replace_with_retry`` 短退避穿过读窗口; 重试耗尽仍抛原始异常,
不做静默降级 (世代标记与实际分区必须一致)。
"""
from __future__ import annotations

import json
import os

import pytest

from app import enriched_generation


def _flaky_replace(monkeypatch, fail_times: int) -> dict:
    """os.replace 前 fail_times 次 raise PermissionError, 之后正常执行。"""
    real_replace = os.replace
    state = {"calls": 0}

    def _flaky(src, dst):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(enriched_generation.os, "replace", _flaky)
    return state


# ---------- _replace_with_retry 本体 ----------

def test_replace_retry_succeeds_after_transient_blocks(tmp_path, monkeypatch):
    out = tmp_path / "marker.json"
    out.write_text("old", encoding="utf-8")
    src = tmp_path / "marker.json.tmp"
    src.write_text("new", encoding="utf-8")
    state = _flaky_replace(monkeypatch, fail_times=3)

    enriched_generation._replace_with_retry(src, out, attempts=6, base_delay=0)

    assert out.read_text(encoding="utf-8") == "new"
    assert state["calls"] == 4
    assert not src.exists()


def test_replace_retry_no_block_single_attempt(tmp_path, monkeypatch):
    out = tmp_path / "marker.json"
    out.write_text("old", encoding="utf-8")
    src = tmp_path / "marker.json.tmp"
    src.write_text("new", encoding="utf-8")
    state = _flaky_replace(monkeypatch, fail_times=0)

    enriched_generation._replace_with_retry(src, out, attempts=6, base_delay=0)

    assert state["calls"] == 1
    assert out.read_text(encoding="utf-8") == "new"


def test_replace_retry_exhausted_raises_last_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        enriched_generation.os, "replace",
        lambda s, d: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )
    src = tmp_path / "a.tmp"
    src.write_text("x", encoding="utf-8")

    with pytest.raises(PermissionError, match="拒绝访问"):
        enriched_generation._replace_with_retry(src, tmp_path / "a.json", attempts=3, base_delay=0)
    assert src.exists()  # 未被消费


# ---------- _write_marker 接入 ----------

def test_write_marker_survives_transient_lock(tmp_path, monkeypatch):
    target = tmp_path / ".matrix_generation_stock.json"
    state = _flaky_replace(monkeypatch, fail_times=2)

    enriched_generation._write_marker(target, {"generation": "g1"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": "g1"}
    assert state["calls"] == 3
    # 临时文件不得残留 (否则下次 recover 会看到孤儿 tmp)
    assert list(tmp_path.glob(".*tmp")) == []


def test_write_marker_overwrites_with_retry(tmp_path, monkeypatch):
    target = tmp_path / ".matrix_generation_stock.json"
    enriched_generation._write_marker(target, {"generation": "g1"})

    _flaky_replace(monkeypatch, fail_times=1)
    enriched_generation._write_marker(target, {"generation": "g2"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": "g2"}


def test_write_marker_failure_leaves_previous_marker_intact(tmp_path, monkeypatch):
    """重试耗尽必须抛错且不破坏已有标记 —— 静默降级会让读取方看到假世代。"""
    target = tmp_path / ".matrix_generation_stock.json"
    enriched_generation._write_marker(target, {"generation": "g1"})

    monkeypatch.setattr(
        enriched_generation.os, "replace",
        lambda s, d: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。")),
    )
    with pytest.raises(PermissionError):
        enriched_generation._write_marker(target, {"generation": "g2"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": "g1"}
    assert list(tmp_path.glob(".*tmp")) == []
