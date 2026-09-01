"""mx_search 接缝测试（issue #24/T2）：fake MX client + 临时配额 + 真实 PG 测试库。

mx-search 契约：搜索资讯并落库 news_items（业务键 (news_code, subject_type,
subject_code)，幂等 upsert）；subject 缺省 market/_market；响应内按 news_code
去重（保留首次出现）；配额/记账/错误语义与 mx_query 一致（触顶不调上游，
上游触达即记账，code!=0 与 MXError 走统一 error 契约）；任何路径不抛异常。
PG 不可达时落库测试自动 skip（见 conftest.pg_test）。
"""

import psycopg2
import pytest

from qstock_mcp.db import ensure_schema
from qstock_mcp.mx_client import MXError
from qstock_mcp.mx_quota import MxQuota
from qstock_mcp.repository import upsert_news_items
from qstock_mcp.tools_mx import extract_news_items, mx_search

from fakes import MX_SEARCH_BODY, MX_SEARCH_ITEMS, FakeMxClient


@pytest.fixture
def quota(tmp_path):
    return MxQuota(tmp_path / "quota.json", today="2026-09-01")


# ------------------------------------------------------- extract_news_items（纯函数，零 PG）


def test_extract_dedups_by_news_code_keeping_first():
    items = extract_news_items(MX_SEARCH_BODY)
    assert len(items) == 3  # 4 条响应条目去重后 3 条
    assert [i["code"] for i in items] == [
        "NW202608283858550632_1",
        "AN202608311828758731",
        "WCYQIN2026082817072776999521_2",
    ]
    # 重复 code 保留首次出现的那条
    assert items[0]["title"] == MX_SEARCH_ITEMS[0]["title"]


def test_extract_tolerates_missing_inner_structure():
    assert extract_news_items({"code": 0}) == []
    assert extract_news_items({"code": 0, "data": {"data": {"llmSearchResponse": {"data": None}}}}) == []


# ------------------------------------------------------- upsert_news_items（真实 PG 测试库）


def _conn(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    return conn


def _rows(conn, subject_type="market", subject_code="_market"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT news_code, subject_type, subject_code, information_type, title,"
            " content, publish_time, source, url, author, ins_name, rating, raw"
            " FROM news_items WHERE subject_type = %s AND subject_code = %s"
            " ORDER BY news_code",
            (subject_type, subject_code),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_upsert_maps_upstream_fields(pg_test):
    conn = _conn(pg_test)
    report = upsert_news_items(conn, "market", "_market", MX_SEARCH_ITEMS)
    assert report == {"inserted": 3, "updated": 0, "skipped": 1}  # 重复 code 计入 skipped
    rows = _rows(conn)
    assert [r["news_code"] for r in rows] == [
        "AN202608311828758731",
        "NW202608283858550632_1",
        "WCYQIN2026082817072776999521_2",
    ]
    notice = rows[0]
    assert notice["information_type"] == "NOTICE"
    assert notice["url"] == "https://pdf.dfcfw.com/pdf/H2_AN202608311828758731_1.PDF"
    assert notice["publish_time"].strftime("%Y-%m-%d %H:%M:%S") == "2026-08-31 09:18:09"
    assert notice["raw"]["code"] == "AN202608311828758731"  # 条目原文进 raw
    news = rows[1]
    assert news["title"] == MX_SEARCH_ITEMS[0]["title"]  # 重复 code 保留首次出现
    # 可选字段全缺 → None
    assert (news["source"], news["url"], news["author"], news["ins_name"], news["rating"]) == (
        None,
        None,
        None,
        None,
        None,
    )
    conn.close()


def test_upsert_is_idempotent_second_pass_all_updated(pg_test):
    conn = _conn(pg_test)
    upsert_news_items(conn, "market", "_market", MX_SEARCH_ITEMS)
    report = upsert_news_items(conn, "market", "_market", MX_SEARCH_ITEMS)
    assert report == {"inserted": 0, "updated": 3, "skipped": 1}
    assert len(_rows(conn)) == 3  # 无重复行
    conn.close()


def test_upsert_skips_bad_date_and_unkeyable_items(pg_test):
    conn = _conn(pg_test)
    items = [
        # 无 code 且无 title：业务键兜底也凑不出 → 跳过
        {"date": "2026-08-28 19:22:00", "informationType": "NEWS"},
        {"code": "NW_BAD_DATE", "title": "坏日期", "date": "不是日期", "informationType": "NEWS"},
        MX_SEARCH_ITEMS[0],
    ]
    report = upsert_news_items(conn, "market", "_market", items)
    assert report == {"inserted": 1, "updated": 0, "skipped": 2}
    assert len(_rows(conn)) == 1
    conn.close()


def test_upsert_fallback_key_from_title_and_date(pg_test):
    """上游缺条目 id：标题+发布时间复合兜底（ticket #24 AC），二次写入为 updated。"""
    conn = _conn(pg_test)
    items = [
        {
            "title": "无 code 条目",
            "date": "2026-08-28 19:22:00",
            "informationType": "NEWS",
            "content": "x",
        }
    ]
    first = upsert_news_items(conn, "market", "_market", items)
    assert first == {"inserted": 1, "updated": 0, "skipped": 0}
    rows = _rows(conn)
    assert rows[0]["news_code"] == "fb:无 code 条目|2026-08-28 19:22:00"
    second = upsert_news_items(conn, "market", "_market", items)
    assert second == {"inserted": 0, "updated": 1, "skipped": 0}
    assert len(_rows(conn)) == 1  # 无重复行
    conn.close()


def test_upsert_publish_time_is_east8(pg_test):
    """上游 naive 时间按东八区落 timestamptz（不按会话时区解释）。"""
    conn = _conn(pg_test)
    upsert_news_items(conn, "market", "_market", [MX_SEARCH_ITEMS[0]])
    row = _rows(conn)[0]
    assert row["publish_time"].utcoffset().total_seconds() == 8 * 3600
    conn.close()


def test_same_news_code_under_multiple_subjects(pg_test):
    """业务键含 subject：同一资讯可挂 market 与个股两个主体，各一行。"""
    conn = _conn(pg_test)
    upsert_news_items(conn, "market", "_market", MX_SEARCH_ITEMS[:1])
    report = upsert_news_items(conn, "stock", "000338", MX_SEARCH_ITEMS[:1])
    assert report == {"inserted": 1, "updated": 0, "skipped": 0}
    assert len(_rows(conn, "market", "_market")) == 1
    assert len(_rows(conn, "stock", "000338")) == 1
    conn.close()


# ------------------------------------------------------- mx_search 工具层


def _ensure(pg_test):
    conn = psycopg2.connect(pg_test)
    ensure_schema(conn)
    conn.close()


def test_search_success_persists_deduped_items(pg_test, quota):
    _ensure(pg_test)
    client = FakeMxClient()
    result = mx_search("潍柴动力最新消息", client=client, quota=quota)
    assert result["status"] == "ok"
    assert result["tool"] == "mx_search"
    # subject 缺省 market/_market
    assert result["params"] == {
        "query": "潍柴动力最新消息",
        "subject_type": "market",
        "subject_code": "_market",
    }
    assert result["rows"] == 3  # 4 条响应条目去重后 3 条
    assert result["inserted"] == 3
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert [i["code"] for i in result["items"]] == [
        "NW202608283858550632_1",
        "AN202608311828758731",
        "WCYQIN2026082817072776999521_2",
    ]
    assert result["quota"] == {"skill": "mx-search", "used": 1, "limit": 20}
    assert client.search_calls == ["潍柴动力最新消息"]
    # 落库验证：默认主体 market/_market 下 3 行
    conn = psycopg2.connect(pg_test)
    assert len(_rows(conn)) == 3
    conn.close()


def test_search_custom_subject(pg_test, quota):
    _ensure(pg_test)
    result = mx_search(
        "潍柴动力公告",
        subject_type="stock",
        subject_code="000338",
        client=FakeMxClient(),
        quota=quota,
    )
    assert result["status"] == "ok"
    conn = psycopg2.connect(pg_test)
    assert len(_rows(conn, "stock", "000338")) == 3
    assert len(_rows(conn, "market", "_market")) == 0
    conn.close()


def test_search_second_call_reports_updated(pg_test, quota):
    _ensure(pg_test)
    client = FakeMxClient()
    mx_search("问句", client=client, quota=quota)
    result = mx_search("问句", client=client, quota=quota)
    assert result["status"] == "ok"
    assert (result["inserted"], result["updated"]) == (0, 3)
    conn = psycopg2.connect(pg_test)
    assert len(_rows(conn)) == 3  # 幂等，无重复行
    conn.close()


def test_search_upstream_business_error_code(quota):
    body = {"success": False, "code": 113, "message": "配额上限", "data": None}
    client = FakeMxClient(search_body=body)
    result = mx_search("问句", client=client, quota=quota)
    assert result["status"] == "error"
    assert "113" in result["error"]
    assert result["upstream_code"] == 113
    assert "items" not in result  # 不伪造数据
    assert result["quota"] == {"skill": "mx-search", "used": 1, "limit": 20}
    assert client.search_calls == ["问句"]


def test_search_transport_error_records_quota(quota):
    client = FakeMxClient(search_error=MXError("MX 传输错误：连接超时"))
    result = mx_search("问句", client=client, quota=quota)
    assert result["status"] == "error"
    assert "连接超时" in result["error"]
    # 上游已触达 → 配额照样记账（与 mx_query 同一语义）
    assert result["quota"] == {"skill": "mx-search", "used": 1, "limit": 20}


def test_search_missing_api_key_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("MX_APIKEY", raising=False)
    result = mx_search("问句", quota=MxQuota(tmp_path / "quota.json"))
    assert result["status"] == "error"
    assert "MX_APIKEY" in result["error"]
    assert "items" not in result


def test_search_exhausted_quota_skips_upstream(tmp_path):
    quota = MxQuota(
        tmp_path / "quota.json", limits={"mx-search": 1}, today="2026-09-01"
    )
    client = FakeMxClient(search_error=MXError("boom"))  # 无需 PG 也能耗光配额
    first = mx_search("q1", client=client, quota=quota)
    assert first["quota"] == {"skill": "mx-search", "used": 1, "limit": 1}
    second = mx_search("q2", client=client, quota=quota)
    assert second["status"] == "error"
    assert "配额" in second["error"]
    assert second["quota"] == {"skill": "mx-search", "used": 1, "limit": 1}
    assert client.search_calls == ["q1"]  # 触顶后不再调上游


def test_search_quota_echo_counts_each_call(quota):
    client = FakeMxClient(search_error=MXError("boom"))
    mx_search("q1", client=client, quota=quota)
    result = mx_search("q2", client=client, quota=quota)
    assert result["quota"]["used"] == 2


def test_search_persist_failure_returns_error_without_raising(quota, monkeypatch):
    """落库异常不抛出工具层：统一 error 契约（上游已触达、配额已记）。"""
    import qstock_mcp.tools_mx as tools_mx

    class _FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(tools_mx, "connect", lambda: (_FakeConn(), None))

    def _boom(*args, **kwargs):
        raise RuntimeError("PG 断连")

    monkeypatch.setattr(tools_mx, "upsert_news_items", _boom)
    result = tools_mx.mx_search(
        "潍柴动力最新消息",
        client=FakeMxClient(search_body=MX_SEARCH_BODY),
        quota=quota,
    )
    assert result["status"] == "error"
    assert "落库失败" in result["error"]
    assert "items" not in result  # 不伪造数据
    assert result["quota"] == {"skill": "mx-search", "used": 1, "limit": 20}
