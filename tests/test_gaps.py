"""区间解析与头尾分段规则的接缝测试（纯逻辑，不触库不触网）。

契约（issue #3 及 #1 user story 5）：自愈补抓只发生在请求区间的头部和尾部缺口，
中段缺口视为停牌/非交易日，不补抓。
"""

import pytest

from qstock_mcp.dates import (
    is_trading_day,
    next_trading_day,
    normalize_date,
    resolve_range,
)
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


class TestTradingDay:
    """issue #51：交易日判定（周末 + 交易所公告休市日，2026 年日历取自上证公告〔2025〕45号）。"""

    def test_plain_weekday_is_trading(self):
        assert is_trading_day("20261014")  # 周三

    def test_weekend_not_trading(self):
        assert not is_trading_day("20260927")  # 周日

    def test_holiday_weekday_not_trading(self):
        assert not is_trading_day("20261005")  # 周一，国庆休市（10/1~10/7）

    def test_next_trading_day_passthrough(self):
        assert next_trading_day("20261014") == "20261014"

    def test_next_trading_day_skips_weekend(self):
        assert next_trading_day("20260927") == "20260928"

    def test_next_trading_day_skips_holiday_week(self):
        assert next_trading_day("20261001") == "20261008"  # 国庆连休后 10/8 开市
        assert next_trading_day("20260220") == "20260224"  # 春节连休后 2/24 开市


class TestDaysStartAlignsToTradingDay:
    """issue #51：days=N 换算的 start 落在非交易日时向前对齐到最近交易日。"""

    def test_start_off_weekend_aligns_to_monday(self):
        # 工单取证场景：today=20261014、days=5 自然日换算落点 20260927（周日）
        start, end = resolve_range(5, None, None, today="20261014")
        assert (start, end) == ("20260928", "20261014")

    def test_start_in_holiday_week_aligns_to_open_day(self):
        # 落点 20261005 在国庆休市内，对齐到 10/8 开市日
        start, _ = resolve_range(5, None, None, today="20261022")
        assert start == "20261008"

    def test_start_in_spring_festival_closure_aligns(self):
        # 落点 20260220 在春节连休（2/15~2/23）内，对齐到 2/24 开市
        start, _ = resolve_range(5, None, None, today="20260309")
        assert start == "20260224"

    def test_start_already_trading_day_unchanged(self):
        start, _ = resolve_range(5, None, None, today="20261015")
        assert start == "20260928"  # 周一，本身即交易日

    def test_explicit_start_not_aligned(self):
        # 显式 start/end 语义不变，不做交易日对齐
        assert resolve_range(None, "20260927", "20261014") == ("20260927", "20261014")


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
