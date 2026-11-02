"""腾讯指数面（issue #117 / spec #111）：批量快照解析 + 指数日线 K 线解析。

fixture 为 2026-10-23 收盘后实抓响应（tests/fixtures/tencent/）：
- index_batch_quote.txt：qt.gtimg.cn 7 大指数批量报价（GBK 原始字节）；
- sh000001/sh000688_index_kline.js：指数日线 K 线（含当日 T+0 行；科创50
  上游只回 `day` 键）。

字段列位于当日收盘数据逐字段验算（模块 docstring 记录）：量比=[49]、
振幅=[43]、成交额=[37]（万元原生）、成交量=[36]（手 ×100）、行级日期取
行情时间戳 [30]。测试不触网。
"""

from pathlib import Path

import pytest

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.tencent_adapter import (
    MAJOR_INDEX_TX_CODES,
    TencentAdapter,
    map_tencent_rows,
    parse_kline_response,
    parse_tx_index_quotes,
    to_tx_index_code,
)
from qstock_mcp.tools_init import MAJOR_INDICES

FIXTURES = Path(__file__).parent / "fixtures" / "tencent"


@pytest.fixture(autouse=True)
def _no_chunk_pause(monkeypatch):
    """分段限速在测试里置零，避免多段回溯用例真实等待。"""
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.CHUNK_PAUSE_S", 0.0
    )


def _quote_rows() -> list[dict]:
    raw = (FIXTURES / "index_batch_quote.txt").read_bytes().decode("gbk")
    return parse_tx_index_quotes(raw)


# ---------------------------------------------------------------- 白名单一致性


def test_major_index_codes_match_tools_init_whitelist():
    """腾讯批量请求的 7 指数与 MAJOR_INDICES 白名单逐一等价（防清单漂移）。"""
    assert tuple(c[2:] for c in MAJOR_INDEX_TX_CODES) == tuple(
        c for c, _ in MAJOR_INDICES
    )


def test_to_tx_index_code_routing():
    assert to_tx_index_code("000001") == "sh000001"
    assert to_tx_index_code("000688") == "sh000688"
    assert to_tx_index_code("399001") == "sz399001"
    assert to_tx_index_code("399006") == "sz399006"
    with pytest.raises(FetchError) as ei:
        to_tx_index_code("899001")
    assert ei.value.kind == "unsupported"


# ---------------------------------------------------------------- 批量快照解析


def test_parse_batch_quotes_fields_verified_against_close():
    """收盘实抓数据逐字段验算：量比 [49]、量手×100、额万元原生、日期取时间戳。"""
    rows = _quote_rows()
    assert [r["index_code"] for r in rows] == [c for c, _ in MAJOR_INDICES]
    sh = rows[0]
    assert sh["index_name"] == "上证指数"
    assert sh["trade_date"] == "20261023"  # 行情时间戳 [30]
    assert sh["latest_price"] == pytest.approx(3690.48)
    assert sh["pre_close"] == pytest.approx(3658.03)
    assert sh["open"] == pytest.approx(3623.57)
    assert sh["high"] == pytest.approx(3691.40)
    assert sh["low"] == pytest.approx(3574.27)
    assert sh["change_amount"] == pytest.approx(32.45)
    assert sh["change_percent"] == pytest.approx(0.89)
    assert sh["volume"] == 55_284_291_200  # 552842912 手 ×100（ADR 0008）
    assert sh["amount"] == pytest.approx(89_866_687.0)  # 原生万元（ADR 0006）
    assert sh["amplitude"] == pytest.approx(3.20)
    assert sh["volume_ratio"] == pytest.approx(1.31)  # [49]，全候选源唯一含量比


def test_volume_ratio_is_field_49_not_turnover_field_38():
    """回归守护：调研报告曾把 [38] 标为量比，实测 [38] 是换手率（与 K 线
    turnover 列逐日一致），量比在 [49]——两字段数值不得混同。"""
    rows = {r["index_code"]: r for r in _quote_rows()}
    # 000001 换手 1.14（K 线同值）≠ 量比 1.31；000688 换手 1.58 ≠ 量比 1.36
    assert rows["000001"]["volume_ratio"] == pytest.approx(1.31)
    assert rows["000688"]["volume_ratio"] == pytest.approx(1.36)
    assert rows["000001"]["volume_ratio"] != pytest.approx(1.14)
    assert rows["399001"]["volume_ratio"] == pytest.approx(1.38)


def test_parse_skips_empty_and_malformed_statements():
    text = (
        'v_sh000001="";'
        "v_bad=not-a-quote;"
        'v_sh000001="1~上证指数~000001~1.0~1.0~1.0~100";'  # 字段数不足
    )
    assert parse_tx_index_quotes(text) == []


def test_fetch_indices_and_spot(monkeypatch):
    raw = (FIXTURES / "index_batch_quote.txt").read_bytes().decode("gbk")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_tx_index_quotes",
        lambda codes: calls.append(tuple(codes)) or raw,
    )
    adapter = TencentAdapter()
    rows = adapter.fetch_indices("20261023")
    assert len(rows) == 7
    assert calls[0] == MAJOR_INDEX_TX_CODES
    spot = adapter.fetch_index_spot("000688")
    assert [r["index_code"] for r in spot] == ["000688"]
    assert calls[1] == ("sh000688",)
    # 补漏专用空结果语义：上游无该指数 → []（工具层转下一源），不抛错
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_tx_index_quotes", lambda codes: ""
    )
    assert adapter.fetch_index_spot("000688") == []


def test_fetch_indices_empty_raises_fetch_error(monkeypatch):
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_tx_index_quotes", lambda codes: ""
    )
    with pytest.raises(FetchError, match="为空"):
        TencentAdapter().fetch_indices("20261023")


# ---------------------------------------------------------------- 指数日线 K 线


def _index_bars(symbol: str, fixture: str) -> list[dict]:
    rows, used_key = parse_kline_response(
        (FIXTURES / fixture).read_text(encoding="utf-8"), symbol, ""
    )
    assert used_key == "day"  # 指数无复权概念，上游只回 day 键
    return map_tencent_rows(symbol, rows)


def test_index_kline_t0_row_and_units():
    bars = _index_bars("sh000001", "sh000001_index_kline.js")
    assert [b["trade_date"] for b in bars][-1] == "20261023"  # T+0 当日行
    last = bars[-1]
    assert last["close"] == pytest.approx(3690.48)
    assert last["volume"] == 55_284_291_200  # 手 ×100（ADR 0008）
    assert last["amount"] == pytest.approx(89_866_687.49)  # 原生万元（ADR 0006）
    assert last["turnover_rate"] == pytest.approx(1.14)  # 原生 turnover 列
    assert last["change_percent"] == pytest.approx(0.8871, abs=1e-3)
    assert last["amplitude"] == pytest.approx(3.2020, abs=1e-3)
    assert bars[0]["change_percent"] is None  # 序列首行无前收不伪造


def test_kc50_index_kline_covers_gap_window():
    """科创50（000688）腾讯可取（baostock 实测 0 行）——断档回填数据源。"""
    bars = _index_bars("sh000688", "sh000688_index_kline.js")
    dates = [b["trade_date"] for b in bars]
    assert "20261013" in dates  # 断档起点行在场
    last = bars[-1]
    assert last["trade_date"] == "20261023"
    assert last["close"] == pytest.approx(1345.51)
    assert last["turnover_rate"] == pytest.approx(1.58)
    assert last["amount"] == pytest.approx(9_690_272.34)


def _raw_row(day: str, close: float = 10.0) -> list:
    return [day, str(close), str(close), str(close), str(close), "100.00", {},
            "1.00", "100.00", ""]


def test_fetch_index_daily_chunks_and_clips(monkeypatch):
    # 只放交易日（12/13-16/19-23），模拟上游行为
    rows_all = [_raw_row(f"2026-10-{d:02d}", 100 + d)
                for d in (12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]
    calls: list[str] = []

    def fake_rows(symbol, end_date, adjust):
        calls.append(end_date)
        eligible = [r for r in rows_all if r[0].replace("-", "") <= end_date]
        return eligible[-4:], False  # 每次回溯 4 根

    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_kline_rows", fake_rows
    )
    adapter = TencentAdapter()
    bars = adapter.fetch_index_daily("000001", "20261013", "20261020")
    assert [b["trade_date"] for b in bars] == [
        "20261013", "20261014", "20261015", "20261016",
        "20261019", "20261020",
    ]
    # 区间首行（10-13）前收 = 10-12 close=112 → 涨跌幅可算
    assert bars[0]["change_percent"] == pytest.approx(1 / 112 * 100, abs=1e-6)
    assert calls[0] == "20261020"  # 游标从 end 起
    # 指数无复权：请求复权口径为空，且不复权回落标记不置位
    assert adapter.adjust_fallback is False


def test_fetch_index_daily_empty_window_is_not_error(monkeypatch):
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_kline_rows",
        lambda symbol, end_date, adjust: ([], False),
    )
    assert TencentAdapter().fetch_index_daily("399006", "20261020", "20261020") == []
