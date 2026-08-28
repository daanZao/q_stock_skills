"""get_fundamentals 接缝测试：fake 适配器，零数据库（issue #6）。

proxy 能力面契约：原样透传上游 payload（不规格化、不落库），输出自描述 JSON
（含实际数据源 source 与 attempted_sources）；上游失败时报 status:error 与明确
原因，绝不伪造数据。
"""

from qstock_mcp.tools_fundamentals import get_fundamentals

from fakes import FUNDAMENTALS_PAYLOAD, FakeFundamentalsAdapter


def test_passthrough_returns_payload_with_actual_source():
    ak = FakeFundamentalsAdapter("akshare")
    result = get_fundamentals("600519", adapters=[ak])
    assert result["status"] == "ok"
    assert result["tool"] == "get_fundamentals"
    assert result["params"] == {"stock_code": "600519"}
    assert result["source"] == "akshare"
    assert result["data"] == FUNDAMENTALS_PAYLOAD  # 原样透传，键名不动
    assert result["attempted_sources"] == []
    assert ak.calls == ["600519"]


def test_fallback_to_next_source_when_first_fails():
    ak = FakeFundamentalsAdapter("akshare", fail_times=99)
    ef = FakeFundamentalsAdapter("efinance")
    result = get_fundamentals("600519", adapters=[ak, ef])
    assert result["status"] == "ok"
    assert result["source"] == "efinance"
    assert result["data"] == FUNDAMENTALS_PAYLOAD
    # akshare 失败记录（每源最多重试 2 次，共 3 次尝试）
    assert [a["source"] for a in result["attempted_sources"]] == ["akshare"]
    assert result["attempted_sources"][0]["attempts"] == 3
    assert "boom" in result["attempted_sources"][0]["error"]
    assert ak.calls == ["600519"] * 3


def test_all_sources_failed_returns_error_without_fabricated_data():
    adapters = [
        FakeFundamentalsAdapter(n, fail_times=99)
        for n in ("akshare", "efinance", "baostock")
    ]
    result = get_fundamentals("600519", adapters=adapters)
    assert result["status"] == "error"
    assert result["tool"] == "get_fundamentals"
    assert result["params"] == {"stock_code": "600519"}
    assert result["error"]  # 明确原因
    assert [a["source"] for a in result["attempted_sources"]] == [
        "akshare",
        "efinance",
        "baostock",
    ]
    assert all(a["attempts"] == 3 for a in result["attempted_sources"])
    assert "data" not in result  # 不伪造数据


def test_does_not_touch_database(monkeypatch):
    """proxy 面不落库：任何数据库连接企图都使测试失败（等价保证）。"""
    import psycopg2

    def _boom(*args, **kwargs):
        raise AssertionError("proxy 面不得连接数据库")

    monkeypatch.setattr(psycopg2, "connect", _boom)
    monkeypatch.delenv("PG_DSN", raising=False)
    result = get_fundamentals("600519", adapters=[FakeFundamentalsAdapter("akshare")])
    assert result["status"] == "ok"


# ---------------------------------------------------------------- json_safe（透传的序列化步骤）


class _NumpyScalar:
    """模拟 numpy 标量的鸭子类型（.item() → Python 标量）。"""

    def __init__(self, value):
        self._value = value

    def item(self):
        return self._value


class _NaT:
    """模拟 pd.NaT：isoformat 抛 ValueError。"""

    def isoformat(self):
        raise ValueError("NaTType does not support isoformat")


def test_json_safe_converts_upstream_types_without_renaming_keys():
    import json
    from datetime import date

    from qstock_mcp.adapters import json_safe

    payload = {
        "指标": "营业总收入",
        "20251231": _NumpyScalar(1.7e11),
        "报告日": date(2025, 12, 31),
        "缺失": float("nan"),
        "无限": float("inf"),
        "空值": _NaT(),
        "嵌套": [{"pe": _NumpyScalar(30)}],
    }
    safe = json_safe(payload)
    assert safe == {
        "指标": "营业总收入",
        "20251231": 1.7e11,
        "报告日": "2025-12-31",
        "缺失": None,
        "无限": None,
        "空值": None,
        "嵌套": [{"pe": 30}],
    }
    json.dumps(safe)  # 必须可直接 JSON 序列化
