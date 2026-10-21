"""腾讯个股日线薄封装（issue #112 / spec #111）：录制响应解析 + 北交所归一化。

fixture 为 2026-10-20/21 实抓响应裁剪（tests/fixtures/tencent/*.js），覆盖
沪主板 / 深主板 / 科创板（成交量原生为股）/ 北交所 920 新码 / 旧码无复权键
回落 day。测试不触网。
"""

from pathlib import Path

import pytest

from qstock_mcp.adapters._bse_codes import (
    bse_code_map,
    market_of,
    normalize_stock_code,
    to_market_code,
)
from qstock_mcp.adapters.base import FetchError
from qstock_mcp.adapters.tencent_adapter import (
    TencentAdapter,
    map_tencent_rows,
    parse_kline_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tencent"


@pytest.fixture(autouse=True)
def _no_chunk_pause(monkeypatch):
    """分段限速在测试里置零，避免多段回溯用例真实等待。"""
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.CHUNK_PAUSE_S", 0.0
    )


def _parse(name: str, symbol: str, adjust: str = "qfq") -> list[list]:
    return parse_kline_response(
        (FIXTURES / name).read_text(encoding="utf-8"), symbol, adjust
    )


# ---------------------------------------------------------------- 北交所代码归一化


def test_bse_map_matches_official_table():
    """北交所官方新旧代码对照表：248 只，一码一映射。"""
    mapping = bse_code_map()
    assert len(mapping) == 248
    assert len(set(mapping.values())) == 248
    # 官方首批 + 撞码递进样例（后三位相同时第四位递进，不能按公式推算）
    assert mapping["833819"] == "920819"
    assert mapping["830799"] == "920799"
    assert mapping["832023"] == "920023"
    assert mapping["837023"] == "920123"
    assert mapping["831396"] == "920496"
    assert mapping["833171"] == "920571"


def test_code_normalization_and_market():
    assert normalize_stock_code("430047") == "920047"
    assert normalize_stock_code("872931") == "920931"
    assert normalize_stock_code("920001") == "920001"
    assert normalize_stock_code("600519") == "600519"
    assert market_of("600519") == "sh"
    assert market_of("000001") == "sz"
    assert market_of("300408") == "sz"
    assert market_of("688981") == "sh"
    assert market_of("920001") == "bj"
    assert market_of("430047") == "bj"
    assert to_market_code("600519") == "sh600519"
    assert to_market_code("000001") == "sz000001"
    assert to_market_code("430047") == "bj920047"
    assert to_market_code("bj920001") == "bj920001"  # 已带前缀
    assert to_market_code("sz000001") == "sz000001"


def test_unknown_codes_rejected():
    for code in ("900001", "200002", "110001", "60051"):
        with pytest.raises(FetchError):
            to_market_code(code)
    # 900xxx 沪 B 股不是北交所（is_bse_code 的 9 前缀口径在此收紧）
    with pytest.raises(FetchError) as ei:
        market_of("900001")
    assert ei.value.kind == "unsupported"


def test_unmapped_old_bse_code_kept():
    """对照表未覆盖的旧码原样请求（腾讯仍可能返回冻结历史，不臆造新码）。"""
    assert normalize_stock_code("430999") == "430999"
    assert to_market_code("430999") == "bj430999"


# ---------------------------------------------------------------- 响应解析


def test_parse_qfq_response():
    rows = _parse("sh600519_qfq.js", "sh600519")
    assert len(rows) == 5
    assert rows[0][0] == "2026-10-14"
    assert rows[-1][0] == "2026-10-20"


def test_parse_falls_back_to_day_key():
    """请求 qfq 但上游只给 day（无复权事件，如 bj920047）时仍可取数。"""
    rows = _parse("bj920047_qfq.js", "bj920047")
    assert len(rows) == 3
    assert rows[-1][0] == "2026-10-20"


def test_parse_errors_are_fetch_errors():
    with pytest.raises(FetchError, match="缺少 JS 赋值"):
        parse_kline_response("not-a-js-assignment", "sh600519", "qfq")
    with pytest.raises(FetchError, match="code=-1"):
        parse_kline_response(
            'k={"code":-1,"msg":"bad","data":{}}', "sh600519", "qfq"
        )
    with pytest.raises(FetchError, match="缺少"):
        parse_kline_response(
            'k={"code":0,"msg":"","data":{}}', "sh600519", "qfq"
        )
    with pytest.raises(FetchError, match="非法 JSON"):
        parse_kline_response("k={broken", "sh600519", "qfq")


# ---------------------------------------------------------------- 字段映射


def test_map_main_board_row_units_and_derived_fields():
    rows = _parse("sh600519_qfq.js", "sh600519")
    bars = map_tencent_rows("sh600519", rows)
    first, last = bars[0], bars[-1]
    # 序列首行无前收 → 涨跌幅/振幅/涨跌额 None（不伪造）
    assert first["change_percent"] is None and first["amplitude"] is None
    # 列序 open, close, high, low（非 OHLC）
    assert last["trade_date"] == "20261020"
    assert last["open"] == 1321.01
    assert last["close"] == 1331.88
    assert last["high"] == 1335.00
    assert last["low"] == 1316.14
    assert last["volume"] == 3_413_600  # 34136 手 × 100（ADR 0008）
    assert last["amount"] == 452539.90  # 原生万元（ADR 0006）
    assert last["turnover_rate"] == 0.27  # 原生百分数
    assert last["change_amount"] == pytest.approx(6.91)
    assert last["change_percent"] == pytest.approx(0.5215, abs=1e-3)
    assert last["amplitude"] == pytest.approx(1.4234, abs=1e-3)


def test_map_star_board_volume_already_shares():
    """科创板成交量实测原生为股，不再 ×100。"""
    rows = _parse("sh688981_qfq.js", "sh688981")
    last = map_tencent_rows("sh688981", rows)[-1]
    assert last["volume"] == 29_936_506
    assert last["amount"] == 300554.50


def test_map_bse_row_volume_lots():
    rows = _parse("bj920001_qfq.js", "bj920001")
    last = map_tencent_rows("bj920001", rows)[-1]
    assert last["volume"] == 1_910_300  # 19103 手 × 100
    assert last["close"] == 10.43
    assert last["turnover_rate"] == 2.12


def test_map_eleven_column_row():
    """不复权行带 11 列（尾部 0.00,0.00），按索引解析不受影响。"""
    rows = _parse("bj920047_qfq.js", "bj920047")
    last = map_tencent_rows("bj920047", rows)[-1]
    assert last["close"] == 13.63
    assert last["volume"] == 2_958_000


# ---------------------------------------------------------------- 分段抓取


def _raw_row(day: str, close: float = 10.0) -> list:
    return [day, str(close), str(close), str(close), str(close), "100.00", {},
            "1.00", "100.00", ""]


def test_fetch_daily_chunks_backwards_and_filters(monkeypatch):
    rows_all = [_raw_row(f"2026-01-{d:02d}", 10 + d) for d in range(1, 21)]
    calls: list[str] = []

    def fake_rows(symbol, end_date, adjust, count=640):
        calls.append(end_date)
        eligible = [
            r for r in rows_all if r[0].replace("-", "") <= end_date
        ]
        return eligible[-3:]  # 模拟小窗口，每次只回溯 3 根

    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_kline_rows", fake_rows
    )
    bars = TencentAdapter().fetch_daily("600519", "20260105", "20260110")
    assert [b["trade_date"] for b in bars] == [
        "20260105", "20260106", "20260107", "20260108", "20260109", "20260110"
    ]
    # 首行前收（01-04 close=14）随回溯取得 → 涨跌幅可算，不是 None
    assert bars[0]["change_percent"] == pytest.approx(1 / 14 * 100, abs=1e-6)
    assert calls == ["20260110", "20260107", "20260104"]


def test_fetch_daily_single_request_when_window_fits(monkeypatch):
    rows_all = [_raw_row(f"2025-12-{d:02d}") for d in range(1, 32)]
    rows_all += [_raw_row(f"2026-01-{d:02d}") for d in range(1, 11)]
    calls: list[str] = []

    def fake_rows(symbol, end_date, adjust, count=640):
        calls.append(end_date)
        return [r for r in rows_all if r[0].replace("-", "") <= end_date]

    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_kline_rows", fake_rows
    )
    bars = TencentAdapter().fetch_daily("600519", "20260105", "20260110")
    assert len(bars) == 6
    assert calls == ["20260110"]  # 首段已回溯到 start 之前，1 次请求


def test_fetch_daily_rejects_unknown_adjust():
    with pytest.raises(FetchError) as ei:
        TencentAdapter().fetch_daily("600519", "20260105", "20260110", adj="xx")
    assert ei.value.kind == "unsupported"


def test_parse_and_map_skip_malformed_rows():
    """上游偶发非列表/空行不应炸穿解析；有效行照常映射。"""
    text = (
        'k={"code":0,"msg":"","data":{"sh600519":{"qfqday":['
        '"not-a-row", [], '
        '["2026-10-20","1.00","1.10","1.20","0.90","100.00",{},"1.00","100.00",""]'
        "]}}}"
    )
    rows = parse_kline_response(text, "sh600519", "qfq")
    assert len(rows) == 2  # 字符串行被过滤，空列表保留由映射层跳过
    bars = map_tencent_rows("sh600519", rows)
    assert len(bars) == 1
    assert bars[0]["trade_date"] == "20261020"


def test_urllib_connection_errors_classified_as_connection():
    """issue #112：urllib.URLError（未拿到响应）判 connection；HTTPError 判 other。"""
    from urllib.error import HTTPError, URLError

    from qstock_mcp.adapters.base import fetch_failure_kind

    try:
        raise FetchError("腾讯K线传输错误") from URLError("connection reset")
    except FetchError as e:
        assert fetch_failure_kind(e) == "connection"
    try:
        raise FetchError("腾讯K线 HTTP 错误") from HTTPError(
            "http://example", 503, "unavailable", None, None
        )
    except FetchError as e:
        assert fetch_failure_kind(e) == "other"

def test_parse_all_adjust_modes_for_bse():
    """北交所 920xxx 三复权口径均可取（issue #112 验收）。"""
    qfq = _parse("bj920001_qfq.js", "bj920001", "qfq")
    hfq = _parse("bj920001_hfq.js", "bj920001", "hfq")
    none = _parse("bj920001_none.js", "bj920001", "none")
    assert [r[2] for r in qfq[-1:]] == ["10.430"]
    assert hfq[-1][2] == "10.930"
    assert none[-1][2] == "10.43"
    # 三口径成交量/成交额一致（量额本身不受复权影响）
    assert {r[5] for r in (qfq[-1], hfq[-1], none[-1])} == {"19103.00"}


def test_parse_hfq_and_none_for_main_board():
    hfq = _parse("sh600519_hfq.js", "sh600519", "hfq")
    none = _parse("sh600519_none.js", "sh600519", "none")
    assert hfq[-1][2] == "9298.83"
    assert none[-1][2] == "1331.88"
