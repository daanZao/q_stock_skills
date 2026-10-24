"""全市场快照三层链适配器测试（issue #116 / spec #111）。

协议解析用录制的真实响应 fixture（2026-10-21 实录）：
- tests/fixtures/tencent/qt_batch_quotes.txt — qt.gtimg.cn 批量报价（GBK，
  沪主板/深主板/科创板/北交所各一）；
- tests/fixtures/tencent/rank_page1.json — 腾讯 rank 榜单 200 行/页（含 bj）；
- tests/fixtures/sina/spot_page1.json — 新浪 Market_Center.getHQNodeData
  原始接口（bj920000-920002，绕过 akshare 封装）。
抓取循环（分批/翻页）用 monkeypatch 驱动，不触网。工具层/落库行为在
test_fetch_snapshot.py / test_query_snapshot_aggregate.py；对拍在
test_snapshot_parity.py；CLI 在 test_scheduler.py。
"""

import json
from pathlib import Path

import pytest

from qstock_mcp.adapters import _snapshot_codes
from qstock_mcp.adapters.base import FetchError, SNAPSHOT_FIELDS
from qstock_mcp.adapters.sina_adapter import (
    SinaSnapshotAdapter,
    parse_sina_spot_rows,
)
from qstock_mcp.adapters.tencent_adapter import (
    TencentRankAdapter,
    TencentSnapshotAdapter,
    parse_qt_quotes,
    parse_rank_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
QT_TEXT = (FIXTURES / "tencent" / "qt_batch_quotes.txt").read_bytes().decode("gbk")
RANK_PAYLOAD = json.loads((FIXTURES / "tencent" / "rank_page1.json").read_text())
SINA_RECORDS = json.loads((FIXTURES / "sina" / "spot_page1.json").read_text())


# ---------------------------------------------------------------- qt 批量报价解析


def test_parse_qt_quotes_full_field_coverage_and_trade_date():
    rows, trade_date = parse_qt_quotes(QT_TEXT)
    assert trade_date == "20261021"  # 字段 30 时间戳含日期 → 可过 #78 日期防线
    assert [r["stock_code"] for r in rows] == ["000001", "600000", "688981", "920001"]
    for r in rows:
        assert set(r) == set(SNAPSHOT_FIELDS)  # 18 字段全覆盖，键集即落库列集


def test_parse_qt_quotes_main_board_units():
    row = next(r for r in parse_qt_quotes(QT_TEXT)[0] if r["stock_code"] == "600000")
    assert row["stock_name"] == "浦发银行"
    assert row["latest_price"] == 9.66
    assert row["pre_close"] == 9.76
    assert row["open"] == 9.72
    assert row["high"] == 9.76 and row["low"] == 9.61
    assert row["change_amount"] == -0.10 and row["change_percent"] == -1.02
    assert row["amplitude"] == 1.54
    assert row["volume"] == 46_256_200  # 462562 手 × 100（ADR 0008）
    assert row["amount"] == 44709.0  # 原生万元（ADR 0006；自洽 447085835 元）
    assert row["market_cap"] == pytest.approx(3217.34 * 1e4)  # 亿 → 万元
    assert row["float_cap"] == pytest.approx(3217.34 * 1e4)
    assert row["pe_ratio"] == 6.28
    assert row["pb_ratio"] == 0.43
    assert row["volume_ratio"] == 0.69
    assert row["turnover_rate"] == 0.14


def test_parse_qt_quotes_star_board_volume_native_shares():
    """科创板（sh688/sh689）成交量原生为股，不换算（与日线同规则）。"""
    row = next(r for r in parse_qt_quotes(QT_TEXT)[0] if r["stock_code"] == "688981")
    assert row["volume"] == 23_986_822  # 原生股（×100 会得 2.4B，实测 24M 手误）


def test_parse_qt_quotes_bse_negative_pe_kept():
    """北交所 920 新码行情齐全；亏损股 PE 负值正常透出（不清洗）。"""
    row = next(r for r in parse_qt_quotes(QT_TEXT)[0] if r["stock_code"] == "920001")
    assert row["stock_name"] == "纬达光电"
    assert row["latest_price"] == 10.31
    assert row["volume"] == 1_074_300  # 10743 手 × 100
    assert row["amount"] == 1109.55
    assert row["pe_ratio"] == -37.79
    assert row["amplitude"] == 2.30


def test_parse_qt_quotes_skips_malformed_lines():
    text = QT_TEXT + '\nv_sh000001="broken~\u3000~";\nv_sz000002="";\nnot-a-quote\n'
    rows, _ = parse_qt_quotes(text)
    assert len(rows) == 4  # 残缺/空体/非报价行全部跳过


def test_parse_qt_quotes_empty_fields_map_none():
    """停牌等空价格字段映射 None 不伪造（含日期时间戳的行仍贡献交易日）。"""
    fields = ["1", "停牌股", "600001", "-", "-", "", "0"] + [""] * 23 + [
        "20261021150000",
        "",
    ]
    text = "v_sh600001=" + '"' + "~".join(fields) + '"' + ";\n"
    rows, trade_date = parse_qt_quotes(text)
    assert trade_date == "20261021"
    assert len(rows) == 1
    row = rows[0]
    assert row["stock_code"] == "600001"
    assert row["latest_price"] is None and row["pre_close"] is None
    assert row["volume"] == 0 or row["volume"] is None
    assert row["pe_ratio"] is None
    assert row["stock_name"] == "停牌股"


# ---------------------------------------------------------------- 主源分批抓取


def _qt_adapter(provider, monkeypatch, batch_size=3):
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.QT_BATCH_SIZE", batch_size
    )
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.QT_BATCH_PAUSE_S", 0.0
    )
    calls: list[list[str]] = []

    def fake_fetch(symbols):
        calls.append(list(symbols))
        return QT_TEXT

    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_qt_quotes", fake_fetch
    )
    return TencentSnapshotAdapter(codes_provider=provider), calls


def test_tencent_snapshot_batches_and_normalizes_codes(monkeypatch):
    """清单驱动分批（含北交所旧码归一化到 920 新码）；交易日取全批最大时间戳。"""
    adapter, calls = _qt_adapter(
        lambda: ["600000", "000001", "688981", "920001", "430047"], monkeypatch
    )
    result = adapter.fetch_market_snapshot()
    assert calls == [
        ["bj920001", "bj920047", "sh600000"],  # 排序后 3 只/批；430047→bj920047
        ["sh688981", "sz000001"],
    ]
    assert result["trade_date"] == "20261021"
    assert len(result["rows"]) == 4 * 2  # 每批返回同一 fixture 4 行


def test_tencent_snapshot_empty_list_is_source_failure(monkeypatch):
    """清单为空不是"上游无数据"：抛 FetchError 由 fallback 链转 rank 榜单。"""
    adapter, _ = _qt_adapter(lambda: [], monkeypatch)
    with pytest.raises(FetchError, match="清单为空"):
        adapter.fetch_market_snapshot()


# ---------------------------------------------------------------- 腾讯 rank 榜单


def test_parse_rank_page_units_and_structural_gaps():
    rows = parse_rank_page(RANK_PAYLOAD)
    assert len(rows) == 200
    assert {r["stock_code"][:1] for r in rows} >= {"6", "0", "9"}  # 沪深京全含
    row = rows[0]  # sh688121 *ST卓然
    assert row["stock_code"] == "688121"
    assert row["latest_price"] == 0.67
    assert row["change_percent"] == -18.29
    assert row["change_amount"] == -0.15
    assert row["amplitude"] == 9.76  # zf 振幅%
    assert row["turnover_rate"] == 12.65  # hsl 换手率%
    assert row["volume_ratio"] == 2.02  # lb 量比
    assert row["pe_ratio"] == -0.55  # pe_ttm 口径
    assert row["pb_ratio"] == 0.07  # pn 市净率
    assert row["volume"] == 25_631_034.0  # 256310.34 手 × 100
    assert row["amount"] == 1764.0  # 原生万元
    assert row["market_cap"] == pytest.approx(1.57 * 1e4)  # zsz 亿 → 万元
    assert row["float_cap"] == pytest.approx(1.36 * 1e4)  # ltsz
    for f in ("open", "high", "low", "pre_close"):  # 榜单不带 OHLC
        assert row[f] is None
    for r in rows:
        assert set(r) == set(SNAPSHOT_FIELDS)


def test_parse_rank_page_bad_payload():
    with pytest.raises(FetchError, match="rank_list"):
        parse_rank_page({"code": 0, "data": {}})
    assert parse_rank_page({"data": {"rank_list": [None, {"code": ""}]}}) == []


def test_rank_adapter_paginates_by_total_and_dedups(monkeypatch):
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.RANK_PAGE_SIZE", 3
    )
    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.RANK_PAGE_PAUSE_S", 0.0
    )
    page_rows = RANK_PAYLOAD["data"]["rank_list"]
    offsets: list[int] = []

    def fake_page(offset):
        offsets.append(offset)
        if offset == 0:
            return {"code": 0, "data": {"total": 4, "rank_list": page_rows[:3]}}
        return {"code": 0, "data": {"total": 4, "rank_list": page_rows[2:4]}}

    monkeypatch.setattr(
        "qstock_mcp.adapters.tencent_adapter.fetch_rank_page", fake_page
    )
    result = TencentRankAdapter().fetch_market_snapshot()
    assert offsets == [0, 3]  # total=4、页大小 3 → 第 2 页 offset=3
    assert result["trade_date"] is None  # 榜单无时间戳
    codes = [r["stock_code"] for r in result["rows"]]
    assert codes == sorted(set(codes))
    assert len(codes) == 4  # 第 3 行重叠已去重


# ---------------------------------------------------------------- 新浪直连


def test_parse_sina_spot_rows_units_and_gaps():
    rows = parse_sina_spot_rows(SINA_RECORDS)
    assert [r["stock_code"] for r in rows] == ["920000", "920001", "920002"]
    row = rows[1]  # bj920001 纬达光电
    assert row["stock_name"] == "纬达光电"
    assert row["latest_price"] == 10.31
    assert row["pre_close"] == 10.43
    assert row["open"] == 10.43 and row["high"] == 10.45 and row["low"] == 10.21
    assert row["change_amount"] == -0.12
    assert row["change_percent"] == -1.151
    assert row["volume"] == 1_074_253  # 原生股（ADR 0008）
    assert row["amount"] == pytest.approx(11095511 / 1e4)  # 元 → 万元（ADR 0006）
    assert row["market_cap"] == pytest.approx(158409.133224)  # 原生万元
    assert row["float_cap"] == pytest.approx(92844.141934)
    assert row["pe_ratio"] == -79.308
    assert row["pb_ratio"] == 2.256
    assert row["turnover_rate"] == 1.19292
    assert row["amplitude"] is None  # 新浪缺振幅/量比
    assert row["volume_ratio"] is None
    for r in rows:
        assert set(r) == set(SNAPSHOT_FIELDS)


def test_sina_snapshot_paginates_by_count(monkeypatch):
    monkeypatch.setattr("qstock_mcp.adapters.sina_adapter.SINA_PAGE_SIZE", 2)
    monkeypatch.setattr("qstock_mcp.adapters.sina_adapter.SINA_PAGE_PAUSE_S", 0.0)
    pages: list[int] = []

    def fake_count():
        return 3

    def fake_page(page):
        pages.append(page)
        return SINA_RECORDS[:2] if page == 1 else SINA_RECORDS[2:]

    monkeypatch.setattr("qstock_mcp.adapters.sina_adapter.fetch_sina_spot_count", fake_count)
    monkeypatch.setattr("qstock_mcp.adapters.sina_adapter.fetch_sina_spot_page", fake_page)
    result = SinaSnapshotAdapter().fetch_market_snapshot()
    assert pages == [1, 2]  # total=3、页大小 2 → 2 页
    assert result["trade_date"] is None  # ticktime 无日期
    assert len(result["rows"]) == 3


# ---------------------------------------------------------------- 默认清单提供者


def test_default_codes_prefers_akshare(monkeypatch):
    monkeypatch.setattr(
        _snapshot_codes, "_codes_from_akshare", lambda: ["600519", "920001"]
    )
    assert _snapshot_codes.default_snapshot_codes() == ["600519", "920001"]


def test_default_codes_falls_back_to_db(monkeypatch, pg_test):
    import psycopg2

    from qstock_mcp.db import ensure_schema
    from qstock_mcp.repository import upsert_stock_list

    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    upsert_stock_list(
        conn, [{"stock_code": "600519", "stock_name": "贵州茅台"}], "test"
    )
    conn.close()

    def boom():
        raise RuntimeError("akshare 不可用")

    monkeypatch.setattr(_snapshot_codes, "_codes_from_akshare", boom)
    assert _snapshot_codes.default_snapshot_codes() == ["600519"]


def test_default_codes_both_paths_fail(monkeypatch):
    def ak_down():
        raise RuntimeError("ak down")

    def db_down():
        raise FetchError("db down")

    monkeypatch.setattr(_snapshot_codes, "_codes_from_akshare", ak_down)
    monkeypatch.setattr(_snapshot_codes, "_codes_from_db", db_down)
    with pytest.raises(FetchError, match="双路皆不可用"):
        _snapshot_codes.default_snapshot_codes()


# ---------------------------------------------------------------- tools 层落库（真实适配器 + 录制响应）


def test_tencent_snapshot_fixture_rows_land_in_pg(pg_test, monkeypatch):
    """真实 TencentSnapshotAdapter 解析录制响应 → tools 层按业务键落库（18 列对齐）。"""
    import psycopg2

    from qstock_mcp.db import ensure_schema
    from qstock_mcp.tools_snapshot import fetch_market_snapshot

    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()

    adapter, _ = _qt_adapter(lambda: ["600000", "920001"], monkeypatch)
    result = fetch_market_snapshot(adapters=[adapter])
    assert result["status"] == "ok"
    assert result["trade_date"] == "20261021"
    assert result["trade_date_origin"] == "api"
    assert result["source"] == "tencent"

    conn = psycopg2.connect(pg_test)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT latest_price, volume, amount, market_cap, pe_ratio, source"
                " FROM market_snapshot WHERE trade_date = '20261021' AND stock_code = '600000'"
            )
            latest, volume, amount, market_cap, pe, source = cur.fetchone()
            # latest_price/pe_ratio 为 real（float4），读回有精度噪声，用 approx
            assert latest == pytest.approx(9.66, abs=1e-4)
            assert volume == 46256200
            assert amount == pytest.approx(44709.0)
            assert market_cap == pytest.approx(3217.34 * 1e4)
            assert pe == pytest.approx(6.28, abs=1e-4)
            assert source == "tencent"
            cur.execute(
                "SELECT count(*) FROM market_snapshot WHERE trade_date = '20261021'"
            )
            assert cur.fetchone()[0] == 4  # fixture 4 行全落（含科创板/北交所）
    finally:
        conn.close()
