"""THS 涨停揭秘适配器测试（issue #120）：录制响应 fixture 驱动协议解析。

fixture 取自社区互证的响应信封形态（docs/research/2026-10-20-zt-pool-
alternatives.md 实测字段全集；信封 status_code/data.info[...].items 为社区
实现互证形态，冒烟实测复核）。不触真实网络：HTTP 层以 monkeypatch 替换。
"""

import json
from pathlib import Path

import pytest

from qstock_mcp.adapters import FetchError
from qstock_mcp.adapters import ths_adapter
from qstock_mcp.adapters._board_ths import (
    map_ths_pool_rows,
    parse_high_days,
    parse_limit_time,
)
from qstock_mcp.adapters.ths_adapter import (
    ThsAdapter,
    _check_ban,
    parse_pool_payload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ths"


def _items(name: str) -> list[dict]:
    payload = json.loads((FIXTURES / name).read_text())
    out, _ = parse_pool_payload(payload, "zt")
    return out


class TestMapping:
    def test_zt_pool_fields(self):
        rows = map_ths_pool_rows(_items("limit_up_pool.json"), "zt")
        row = rows[0]
        assert row["pool_type"] == "zt"
        assert row["stock_code"] == "603123"
        assert row["stock_name"] == "示例涨停连板"
        assert row["consecutive_boards"] == 2  # "2天2板" → 连板数取板数
        assert row["sealed_amount"] == pytest.approx(12500.0)  # 元 → 万元
        assert row["limit_up_time"] == "093000"  # 2026-10-16 09:30 东八区
        assert row["limit_up_type"] == "换手板"
        assert row["reason_type"] == "医药+原料药"
        assert row["zb_info"] == "0"
        # THS 不提供：行业/成交额/成交量/跌停价 → None（不伪造）
        assert row["industry"] is None
        assert row["amount"] is None
        assert row["volume"] is None
        assert row["dt_price"] is None

    def test_first_board_and_open_num(self):
        rows = map_ths_pool_rows(_items("limit_up_pool.json"), "zt")
        second = rows[1]
        assert second["consecutive_boards"] == 1  # "首板"
        assert second["zb_info"] == "2"  # 炸板/开板次数
        assert second["limit_up_time"] == "145700"

    def test_zb_pool_maps_zt_price_and_open_num(self):
        (row,) = map_ths_pool_rows(_items("open_limit_pool.json"), "zb")
        assert row["pool_type"] == "zb"
        assert row["zt_price"] == pytest.approx(16.28)
        assert row["zb_info"] == "3"  # 炸板次数
        assert row["sealed_amount"] == pytest.approx(2100.0)

    def test_dt_pool_accepts_dict_info_envelope(self):
        rows = map_ths_pool_rows(_items("down_limit_pool.json"), "dt")
        row = rows[0]
        assert row["pool_type"] == "dt"
        assert row["consecutive_boards"] == 1
        assert row["change_percent"] == pytest.approx(-9.98)
        assert row["limit_up_time"] == "111000"  # first_limit_down_time → 首封列
        assert row["limit_up_type"] == "一字板"  # limit_down_type → 板型列

    @pytest.mark.parametrize(
        ("raw", "want"),
        [("2天2板", 2), ("首板", 1), ("4天2板", 2), ("11天6板", 6), (None, None), ("-", None)],
    )
    def test_parse_high_days(self, raw, want):
        assert parse_high_days(raw) == want

    @pytest.mark.parametrize(
        ("raw", "want"),
        [(1792114200, "093000"), (0, None), (None, None), ("abc", None)],
    )
    def test_parse_limit_time(self, raw, want):
        assert parse_limit_time(raw) == want


class TestEnvelopeAndBan:
    def test_status_code_nonzero_is_fetch_error(self):
        with pytest.raises(FetchError) as e:
            parse_pool_payload({"status_code": 1, "status_msg": "forbidden"}, "zt")
        assert e.value.kind is None  # 业务错误非连接级

    def test_empty_info_is_empty_list(self):
        assert parse_pool_payload({"status_code": 0, "data": {}}, "zt") == ([], None)

    def test_modern_row_list_envelope(self):
        """新版信封（社区互证 + 冒烟实测）：data.info 即行列表，无 pages 元数据。"""
        payload = {
            "status_code": 0,
            "data": {"info": [
                {"code": "600519", "name": "贵州茅台", "latest": 1700.0},
                {"code": "000001", "name": "平安银行", "latest": 10.0},
            ]},
        }
        items, pages = parse_pool_payload(payload, "zt")
        assert [r["code"] for r in items] == ["600519", "000001"]
        assert pages is None  # 无分页元数据 → 满页启发式续抓

    def test_forbidden_body_is_connection(self):
        body = (FIXTURES / "forbidden.html").read_text()
        with pytest.raises(FetchError) as e:
            _check_ban(200, body)
        assert e.value.kind == "connection"

    def test_403_is_connection(self):
        with pytest.raises(FetchError) as e:
            _check_ban(403, "")
        assert e.value.kind == "connection"

    def test_normal_body_passes(self):
        _check_ban(200, '{"status_code": 0}')


class TestDiscipline:
    """低频纪律：池间长退避、封禁即弃本轮、单池失败他池继续。"""

    def _script_pools(self, monkeypatch, script):
        """以脚本替换 fetch_pool_records（真实 ThsAdapter.fetch_zt_pool 驱动）。"""
        calls: list[str] = []

        def fake_records(endpoint, pool_type, trade_date, pause_s=None,
                         fields=None):
            calls.append(pool_type)
            outcome = script.get(pool_type, [])
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(ths_adapter, "fetch_pool_records", fake_records)
        return calls

    def test_pool_order_and_pause_between_pools(self, monkeypatch):
        calls = self._script_pools(monkeypatch, {
            "zt": _items("limit_up_pool.json"),
            "dt": _items("down_limit_pool.json"),
            "zb": _items("open_limit_pool.json"),
        })
        sleeps: list[float] = []
        monkeypatch.setattr(ths_adapter, "sleep", sleeps.append)
        adapter = ThsAdapter(pause_s=20.0)
        rows = adapter.fetch_zt_pool("20261016")
        assert calls == ["zt", "dt", "zb"]
        assert sleeps == [20.0, 20.0]  # 池间长退避恰好两次
        assert {r["pool_type"] for r in rows} == {"zt", "dt", "zb"}

    def test_single_pool_failure_continues_others(self, monkeypatch):
        self._script_pools(monkeypatch, {
            "zt": _items("limit_up_pool.json"),
            "dt": FetchError("THS down_limit_pool HTTP 错误：status=404"),
            "zb": _items("open_limit_pool.json"),
        })
        monkeypatch.setattr(ths_adapter, "sleep", lambda _s: None)
        adapter = ThsAdapter()
        rows = adapter.fetch_zt_pool("20261016")
        assert {r["pool_type"] for r in rows} == {"zt", "zb"}
        assert "dt" in adapter.pool_errors  # 失败原因供工具层 partial_error

    def test_ban_abandons_remaining_pools_same_run(self, monkeypatch):
        calls = self._script_pools(monkeypatch, {
            "zt": FetchError("同花顺 IP 级封禁", kind="connection"),
        })
        monkeypatch.setattr(ths_adapter, "sleep", lambda _s: None)
        adapter = ThsAdapter()
        with pytest.raises(FetchError) as e:
            adapter.fetch_zt_pool("20261016")
        assert e.value.kind == "connection"
        assert calls == ["zt"]  # 封禁后不再请求剩余池
        assert adapter.pool_errors["dt"].startswith("封禁探测命中")

    def test_all_failed_without_ban_is_not_connection(self, monkeypatch):
        self._script_pools(monkeypatch,
                           {k: FetchError(f"{k} boom")
                            for k in ("zt", "dt", "zb")})
        monkeypatch.setattr(ths_adapter, "sleep", lambda _s: None)
        adapter = ThsAdapter()
        with pytest.raises(FetchError) as e:
            adapter.fetch_zt_pool("20261016")
        assert e.value.kind is None  # 非连接级 → fetch_failed 语义

    def test_strong_stocks_is_unsupported(self):
        with pytest.raises(FetchError) as e:
            ThsAdapter().fetch_strong_stocks("20261016")
        assert e.value.kind == "unsupported"


class TestProbe:
    def test_probe_reports_banned(self, monkeypatch):
        body = (FIXTURES / "forbidden.html").read_bytes()

        class Resp:
            status = 403

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(ths_adapter.urllib.request, "urlopen",
                            lambda *a, **k: Resp())
        assert ths_adapter.probe()["banned"] is True

    def test_probe_unreachable(self, monkeypatch):
        import urllib.error

        def _boom(*a, **k):
            raise urllib.error.URLError("no route")

        monkeypatch.setattr(ths_adapter.urllib.request, "urlopen", _boom)
        result = ths_adapter.probe()
        assert result["reachable"] is False
        assert result["banned"] is False
