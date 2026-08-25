"""区间解析与头尾分段规则的接缝测试（纯逻辑，不触库不触网）。

契约（issue #3 及 #1 user story 5）：自愈补抓只发生在请求区间的头部和尾部缺口，
中段缺口视为停牌/非交易日，不补抓。
"""

import pytest

from qstock_mcp.dates import normalize_date, resolve_range
from qstock_mcp.gaps import head_tail_gaps


class TestNormalizeDate:
    def test_compact_passthrough(self):
        assert normalize_date("20240105") == "20240105"

    def test_dashed_to_compact(self):
        assert normalize_date("2024-01-05") == "20240105"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize_date("2024/01/05")


class TestResolveRange:
    def test_start_end_passthrough(self):
        assert resolve_range(None, "20240101", "20240131") == ("20240101", "20240131")

    def test_start_only_defaults_end_to_today(self):
        assert resolve_range(None, "20240101", None, today="20240201") == (
            "20240101",
            "20240201",
        )

    def test_days_ends_at_today_with_trading_day_buffer(self):
        start, end = resolve_range(10, None, None, today="20240120")
        assert end == "20240120"
        # 10 个交易日 ≈ 14 个自然日 + buffer，起始日必须明显早于 10 个自然日前
        assert start <= "20240101"

    def test_days_conflicts_with_start_end(self):
        with pytest.raises(ValueError):
            resolve_range(10, "20240101", None)

    def test_neither_days_nor_start_raises(self):
        with pytest.raises(ValueError):
            resolve_range(None, None, None)

    def test_start_after_end_raises(self):
        with pytest.raises(ValueError):
            resolve_range(None, "20240201", "20240101")


class TestHeadTailGaps:
    def test_empty_db_whole_range_is_gap(self):
        assert head_tail_gaps(set(), "20240101", "20240131") == [("20240101", "20240131")]

    def test_full_coverage_no_gap(self):
        existing = {"20240101", "20240115", "20240131"}
        assert head_tail_gaps(existing, "20240101", "20240131") == []

    def test_head_gap_only(self):
        existing = {"20240110", "20240131"}
        assert head_tail_gaps(existing, "20240101", "20240131") == [
            ("20240101", "20240109")
        ]

    def test_tail_gap_only(self):
        existing = {"20240101", "20240120"}
        assert head_tail_gaps(existing, "20240101", "20240131") == [
            ("20240121", "20240131")
        ]

    def test_both_head_and_tail_gaps(self):
        existing = {"20240110", "20240120"}
        assert head_tail_gaps(existing, "20240101", "20240131") == [
            ("20240101", "20240109"),
            ("20240121", "20240131"),
        ]

    def test_mid_gap_treated_as_suspension_not_fetched(self):
        # 01-05 与 01-20 之间的缺口是停牌/非交易日，不补抓
        existing = {"20240105", "20240120"}
        assert head_tail_gaps(existing, "20240105", "20240120") == []
