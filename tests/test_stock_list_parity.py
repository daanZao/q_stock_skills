"""股票清单多源互证（issue #122 长尾验收）：逐源直取、不落库。

覆盖：参考源优先级、代码集合差集（双向样本与计数）、北交所覆盖统计、
单源失败不中断、全源失败 error。不触真实网络（本地 fake）。
"""

from qstock_mcp.adapters.base import FetchError
from qstock_mcp.stock_list_parity import compare_stock_list_sources


class _Fake:
    def __init__(self, name, rows=None, exc=None):
        self.name = name
        self._rows = rows
        self._exc = exc

    def fetch_stock_list(self):
        if self._exc is not None:
            raise self._exc
        return self._rows


def _rows(*codes):
    return [{"stock_code": c, "stock_name": f"股{c}"} for c in codes]


def test_all_sources_agree_status_ok():
    rows = _rows("600519", "000001", "920001")
    out = compare_stock_list_sources(adapters=[
        _Fake("efinance", rows),
        _Fake("akshare", _rows("600519", "000001", "920001")),
        _Fake("baostock", _rows("600519", "000001", "920001")),
    ])
    assert out["status"] == "ok"
    assert out["reference_source"] == "efinance"
    assert out["diff_total"] == 0
    assert out["bse_coverage"] == {"efinance": 1, "akshare": 1, "baostock": 1}


def test_code_set_diff_is_mismatch_with_samples():
    out = compare_stock_list_sources(adapters=[
        _Fake("akshare", _rows("600519", "920001")),  # 参考源：含北交所
        _Fake("baostock", _rows("600519", "000001")),  # 兜底：无北交所
    ])
    assert out["status"] == "mismatch"
    assert out["diff_total"] == 2
    (cmp,) = out["comparisons"].values()
    assert cmp["reference_only_sample"] == ["920001"]
    assert cmp["other_only_sample"] == ["000001"]
    assert cmp["common_codes"] == 1
    assert "口径" in out["note"]


def test_single_source_failure_records_and_continues():
    conn_exc = FetchError("efinance 连接重置")
    conn_exc.__cause__ = ConnectionResetError("reset")
    out = compare_stock_list_sources(adapters=[
        _Fake("efinance", exc=conn_exc),
        _Fake("baostock", _rows("600519")),
    ])
    assert out["status"] == "ok"  # 单源无对比对象
    assert out["reference_source"] == "baostock"
    assert out["sources"]["efinance"]["status"] == "error"
    assert out["sources"]["efinance"]["failure_kind"] == "connection"
    assert "bse_coverage" in out and out["bse_coverage"] == {"baostock": 0}


def test_all_sources_failed_is_error():
    out = compare_stock_list_sources(adapters=[
        _Fake(n, exc=FetchError(f"{n} boom")) for n in ("efinance", "akshare", "baostock")
    ])
    assert out["status"] == "error"
    assert "全部清单源无数据或失败" in out["error"]


def test_empty_source_is_no_data_not_failure():
    out = compare_stock_list_sources(adapters=[
        _Fake("akshare", _rows("600519")),
        _Fake("baostock", []),
    ])
    assert out["status"] == "ok"
    assert out["sources"]["baostock"]["status"] == "no_data"
