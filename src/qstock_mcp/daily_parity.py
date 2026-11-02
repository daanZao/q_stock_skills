"""个股日线三源对拍（issue #112 / spec #111）：逐源直取、字段级比较、不落库。

与 fallback 不同，每个源都被独立调用：失败/无数据只记入该源，不中断其他源。
用途是换源/迁移后的数据质量验证，固定 CLI 入口 `qstock-scheduler daily --check`
（手册 `docs/daily-source-cli.md`）。

复权口径与可比字段：
- `qfq` / `none`：各源价格口径一致，价格、成交量、成交额、换手率全字段比对；
- `hfq`：各源后复权基准不同（实测 600519 腾讯/新浪/baostock 绝对值互不相同），
  价格类字段（OHLC/涨跌幅）不可直接比，只比对成交量/成交额/换手率，并在
  输出带 `note` 说明。

复权回落可见化：源缺复权键按 day 返回时（适配器 `adjust_fallback` 标记），
该源条目带 `note` 标注。

容差：价格绝对差（qfq 两位小数口径）；成交量绝对差（腾讯手→股按整数手
四舍五入，单行误差 < 1 手 = 100 股）；成交额相对差；换手率绝对差（腾讯
百分数两位小数）。
"""

from typing import Sequence

from .adapters import DailyAdapter, default_daily_adapters, fetch_failure_kind
from .dates import normalize_date
from .output import error as _error

_PARITY_TOLERANCES: dict[str, tuple[str, float]] = {
    "open": ("abs", 0.011),
    "high": ("abs", 0.011),
    "low": ("abs", 0.011),
    "close": ("abs", 0.011),
    "change_percent": ("abs", 0.05),
    "volume": ("abs", 100.0),
    "amount": ("rel", 0.005),
    "turnover_rate": ("abs", 0.02),
}

# 后复权基准因源而异，价格类字段不参与 hfq 对拍
_HFQ_SKIP_FIELDS = ("open", "high", "low", "close", "change_percent")
_HFQ_NOTE = "后复权基准因源而异，价格类字段不参与对拍（只比量/额/换手率）"

# 复权回落可见化（issue #112）：源缺复权键按 day 返回时在该源条目上标注
_ADJUST_FALLBACK_NOTE = "上游缺复权键，本次取数为不复权（day）回落"


def _parity_fields(adj: str) -> dict[str, tuple[str, float]]:
    if adj != "hfq":
        return dict(_PARITY_TOLERANCES)
    return {k: v for k, v in _PARITY_TOLERANCES.items() if k not in _HFQ_SKIP_FIELDS}


def _within_tolerance(reference: float, value: float, kind: str, tol: float) -> bool:
    if kind == "abs":
        return abs(value - reference) <= tol
    if reference == 0:
        return abs(value) <= tol
    return abs(value - reference) / abs(reference) <= tol


def _parity_bar(bar: dict) -> dict:
    """对外保留全部可比值（便于人工核对）；比较范围由 compared_fields 限定。"""
    return {field: bar.get(field) for field in _PARITY_TOLERANCES}


def _compare_code(
    code: str, trade_date: str, adj: str, adapters: Sequence[DailyAdapter]
) -> dict:
    """单 code 逐源直取并对拍；适配器顺序即参考源优先级。"""
    fields = _parity_fields(adj)
    result: dict = {
        "stock_code": code,
        "status": "ok",
        "sources": {},
        "compared_fields": list(fields),
        "diffs": [],
    }
    for adapter in adapters:
        try:
            rows = adapter.fetch_daily(code, trade_date, trade_date, adj)
        except Exception as e:  # noqa: BLE001 - 单源失败只记该源，不中断对拍
            kind = fetch_failure_kind(e)
            result["sources"][adapter.name] = {
                "status": "unsupported" if kind == "unsupported" else "error",
                "failure_kind": kind,
                "error": str(e),
            }
            continue
        bar = next((r for r in rows if r["trade_date"] == trade_date), None)
        entry = (
            {"status": "ok", "bar": _parity_bar(bar)}
            if bar is not None
            else {"status": "no_data"}
        )
        if getattr(adapter, "adjust_fallback", False):
            entry["note"] = _ADJUST_FALLBACK_NOTE
        result["sources"][adapter.name] = entry
    available = [name for name, v in result["sources"].items() if v["status"] == "ok"]
    if not available:
        result["status"] = "no_data"
        return result
    if len(available) == 1:
        result["status"] = "single_source"
        return result
    reference_name = available[0]
    reference = result["sources"][reference_name]["bar"]
    for name in available[1:]:
        other = result["sources"][name]["bar"]
        for field, (kind, tol) in fields.items():
            ref_val, val = reference.get(field), other.get(field)
            if ref_val is None or val is None:
                continue
            if not _within_tolerance(ref_val, val, kind, tol):
                result["diffs"].append(
                    {
                        "field": field,
                        "reference": {"source": reference_name, "value": ref_val},
                        "other": {"source": name, "value": val},
                        "tolerance": {"kind": kind, "value": tol},
                    }
                )
    if result["diffs"]:
        result["status"] = "mismatch"
    return result


def compare_daily_sources(
    codes: Sequence[str],
    trade_date: str,
    adj: str = "qfq",
    adapters: Sequence[DailyAdapter] | None = None,
) -> dict:
    """三源对拍：逐源直取同一交易日个股日线并做字段级比对（不落库）。

    by-design 不支持的源记 `unsupported`（如 baostock 对北交所），不算对拍
    失败；仅一个源有值时记 `single_source` 警告。两源以上有值时按适配器顺序
    取参考源。`hfq` 只比量/额/换手率（基准不同），输出带 `note`。
    """
    tool = "compare_daily_sources"
    params = {"codes": list(codes), "trade_date": trade_date, "adj": adj}
    try:
        trade_date = normalize_date(trade_date)
    except ValueError as e:
        return _error(tool, params, str(e))
    if adj not in ("qfq", "hfq", "none"):
        return _error(tool, params, f"无法识别的复权口径: {adj!r}")
    codes = [c for c in codes if c]
    if not codes:
        return _error(tool, params, "股票代码列表为空")
    if adapters is None:
        adapters = default_daily_adapters()
    results = [_compare_code(c, trade_date, adj, adapters) for c in codes]
    mismatched = [r for r in results if r["status"] == "mismatch"]
    no_data = [r for r in results if r["status"] == "no_data"]
    params["trade_date"] = trade_date
    out = {
        "status": (
            "mismatch"
            if mismatched
            else ("error" if len(no_data) == len(results) else "ok")
        ),
        "tool": tool,
        "params": params,
        "results": results,
        "mismatch_count": len(mismatched),
        "no_data_count": len(no_data),
        "single_source_count": sum(
            1 for r in results if r["status"] == "single_source"
        ),
    }
    if adj == "hfq":
        out["note"] = _HFQ_NOTE
    return out
