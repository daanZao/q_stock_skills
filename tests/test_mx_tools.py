"""mx_query 接缝测试：fake MX client + 临时配额文件，零网络零数据库（issue #23/T1）。

proxy 面契约：原样透传 mx-data 响应 body（不规格化、不落库、不进 fallback 链）；
配额触顶不调上游；上游业务码 code!=0 走统一 error 契约并回显 quota；MXError
（key 缺失/传输错误）报明确原因；工具层任何路径不抛异常。
"""

import pytest

from qstock_mcp.mx_client import MXError
from qstock_mcp.mx_quota import MxQuota
from qstock_mcp.tools_mx import mx_query

from fakes import MX_BODY, FakeMxClient


@pytest.fixture
def quota(tmp_path):
    return MxQuota(tmp_path / "quota.json", today="2026-09-01")


def test_passthrough_returns_body_verbatim(quota):
    client = FakeMxClient()
    result = mx_query("潍柴动力2024年第一季度净利润", client=client, quota=quota)
    assert result["status"] == "ok"
    assert result["tool"] == "mx_query"
    assert result["params"] == {"tool_query": "潍柴动力2024年第一季度净利润"}
    assert result["data"] == MX_BODY  # 原样透传，键名不动
    assert result["quota"] == {"skill": "mx-data", "used": 1, "limit": 20}
    assert client.calls == ["潍柴动力2024年第一季度净利润"]


def test_missing_api_key_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("MX_APIKEY", raising=False)
    # 不注入 client → 构造真实 MxClient，key 缺失应懒加载报明确错误
    result = mx_query("任意问句", quota=MxQuota(tmp_path / "quota.json"))
    assert result["status"] == "error"
    assert "MX_APIKEY" in result["error"]
    assert "data" not in result  # 不伪造数据


def test_upstream_business_error_code(quota):
    body = {"success": False, "code": 114, "message": "密钥无效", "data": None}
    client = FakeMxClient(body=body)
    result = mx_query("问句", client=client, quota=quota)
    assert result["status"] == "error"
    assert "114" in result["error"]
    assert "密钥无效" in result["error"]
    assert result["upstream_code"] == 114
    assert result["upstream_message"] == "密钥无效"
    assert "data" not in result
    # 已调上游 → 配额已计数并回显
    assert result["quota"] == {"skill": "mx-data", "used": 1, "limit": 20}
    assert client.calls == ["问句"]


def test_transport_error_returns_clear_error(quota):
    client = FakeMxClient(error=MXError("MX 传输错误：连接超时"))
    result = mx_query("问句", client=client, quota=quota)
    assert result["status"] == "error"
    assert "连接超时" in result["error"]
    assert "data" not in result
    # 上游已触达 → 配额照样计数（防止失败调用绕过每日上限）
    assert result["quota"] == {"skill": "mx-data", "used": 1, "limit": 20}


def test_quota_record_failure_does_not_break_passthrough(tmp_path):
    """ledger 写盘失败降级为日志：透传结果照常返回，工具层不抛异常。"""

    class _BrokenQuota(MxQuota):
        def record(self, skill: str) -> None:
            raise OSError("disk full")

    quota = _BrokenQuota(tmp_path / "quota.json", today="2026-09-01")
    result = mx_query("问句", client=FakeMxClient(), quota=quota)
    assert result["status"] == "ok"
    assert result["data"] == MX_BODY


def test_exhausted_quota_skips_upstream(tmp_path):
    quota = MxQuota(
        tmp_path / "quota.json", limits={"mx-data": 1}, today="2026-09-01"
    )
    client = FakeMxClient()
    first = mx_query("问句1", client=client, quota=quota)
    assert first["status"] == "ok"
    assert first["quota"] == {"skill": "mx-data", "used": 1, "limit": 1}
    second = mx_query("问句2", client=client, quota=quota)
    assert second["status"] == "error"
    assert "配额" in second["error"]
    assert second["quota"] == {"skill": "mx-data", "used": 1, "limit": 1}
    assert client.calls == ["问句1"]  # 触顶后不再调上游


def test_quota_echo_counts_each_call(quota):
    client = FakeMxClient()
    mx_query("q1", client=client, quota=quota)
    result = mx_query("q2", client=client, quota=quota)
    assert result["quota"]["used"] == 2


def test_does_not_touch_database(quota, monkeypatch):
    """proxy 面不落库：任何数据库连接企图都使测试失败（等价保证）。"""
    import psycopg2

    def _boom(*args, **kwargs):
        raise AssertionError("proxy 面不得连接数据库")

    monkeypatch.setattr(psycopg2, "connect", _boom)
    monkeypatch.delenv("PG_DSN", raising=False)
    result = mx_query("问句", client=FakeMxClient(), quota=quota)
    assert result["status"] == "ok"
