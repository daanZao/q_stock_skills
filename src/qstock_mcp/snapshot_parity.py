"""全市场快照三源对拍（issue #116 / spec #111）：逐源直取、字段级比较、不落库。

与 fallback 不同，快照链每个源（腾讯批量报价 / 腾讯 rank 榜单 / 新浪直连）
都被独立调用：失败/无数据只记入该源，不中断其他源。用途是迁移后的数据
质量验证，固定 CLI 入口 `qstock-scheduler snapshot --check`
（手册 `docs/snapshot-source-cli.md`）。

结构性缺项（源 by-design 不提供，不是数据缺失，比对时跳过）：
- 腾讯 rank 榜单：open / high / low / pre_close（榜单不带 OHLC）；
- 新浪直连：amplitude / volume_ratio（原始接口无此两字段）。

PE 口径提示：三源 PE 字段口径互不相同（腾讯字段 39 疑 TTM、rank 明示
pe_ttm、新浪 per 未验证，见 docs/research/2026-10-20-snapshot-alternatives.md
§7），pe_ratio 差异超出容差属已知口径切换信号，对拍输出如实呈现。

容差沿用 daily_parity 口径：价格绝对差、量/幅/换手绝对差、成交额相对差。
"""

from typing import Sequence

from .adapters import (
    SnapshotAdapter,
    default_snapshot_adapters,
    fetch_failure_kind,
)
from .adapters.base import SNAPSHOT_FIELDS
from .daily_parity import _within_tolerance
from .output import error as _error

# 对拍容差（比较函数 _within_tolerance 复用 daily_parity，同 tools_lhb 先例；
# 本表字段集为快照面超集，不下放 daily_parity）
_PARITY_TOLERANCES: dict[str, tuple[str, float]] = {
    "latest_price": ("abs", 0.011),
    "pre_close": ("abs", 0.011),
    "open": ("abs", 0.011),
    "high": ("abs", 0.011),
    "low": ("abs", 0.011),
    "change_percent": ("abs", 0.05),
    "change_amount": ("abs", 0.011),
    "amplitude": ("abs", 0.05),
    "volume_ratio": ("abs", 0.05),
    "turnover_rate": ("abs", 0.02),
    "pe_ratio": ("rel", 0.05),
    "pb_ratio": ("rel", 0.05),
    "volume": ("abs", 100.0),
    "amount": ("rel", 0.005),
    "market_cap": ("rel", 0.005),
    "float_cap": ("rel", 0.005),
}

# 结构性缺项：源 by-design 不提供这些字段，不参与比对（非数据缺失）
STRUCTURAL_GAPS: dict[str, tuple[str, ...]] = {
    "tencent_rank": ("open", "high", "low", "pre_close"),
    "sina": ("amplitude", "volume_ratio"),
}

_DIFF_SAMPLE_LIMIT = 20  # 对拍差异样本上限（全市场逐行差异可能上万，样本供人工核读）


def compare_snapshot_sources(
    adapters: Sequence[SnapshotAdapter] | None = None,
    codes_limit: int | None = None,
) -> dict:
    """三源直取全市场快照并做字段级对拍（不落库）。

    逐源独立调用（失败只记该源）；按代码交集与参考源（有数据的最高优先级
    源）逐字段比对，结构性缺项跳过；差异超容差输出样本（上限
    _DIFF_SAMPLE_LIMIT）与逐字段差异计数。全部源失败返回 error。
    """
    tool = "compare_snapshot_sources"
    params: dict = {"codes_limit": codes_limit}
    if codes_limit is not None and (
        isinstance(codes_limit, bool) or not isinstance(codes_limit, int) or codes_limit <= 0
    ):
        return _error(tool, params, f"codes_limit 必须为正整数: {codes_limit!r}")
    if adapters is None:
        adapters = default_snapshot_adapters()
    sources: dict[str, dict] = {}
    for adapter in adapters:
        try:
            r = adapter.fetch_market_snapshot()
        except Exception as e:  # noqa: BLE001 - 单源失败只记该源，不中断对拍
            sources[adapter.name] = {
                "status": "error",
                "failure_kind": fetch_failure_kind(e),
                "error": str(e),
            }
            continue
        rows = r.get("rows") or []
        if codes_limit is not None:
            rows = rows[:codes_limit]
        sources[adapter.name] = {
            "status": "ok" if rows else "no_data",
            "trade_date": r.get("trade_date"),
            "rows": len(rows),
            "by_code": {row["stock_code"]: row for row in rows},
        }
    ok_sources = [n for n, v in sources.items() if v.get("by_code")]
    if not ok_sources:
        return {
            "status": "error",
            "tool": tool,
            "params": params,
            "sources": {n: {k: v for k, v in s.items() if k != "by_code"}
                        for n, s in sources.items()},
            "error": "全部快照源无数据或失败",
        }
    reference_name = ok_sources[0]
    reference = sources[reference_name]["by_code"]
    others = [n for n in ok_sources[1:]]

    fields = [f for f in SNAPSHOT_FIELDS if f not in ("stock_code", "stock_name")]
    diffs: list[dict] = []
    field_diff_counts = {n: {f: 0 for f in fields} for n in others}
    compared_counts = {n: 0 for n in others}
    for code, ref_row in reference.items():
        for name in others:
            other_row = sources[name]["by_code"].get(code)
            if other_row is None:
                continue
            gaps = STRUCTURAL_GAPS.get(name, ())
            for field in fields:
                if field in gaps:
                    continue
                ref_val, val = ref_row.get(field), other_row.get(field)
                if ref_val is None or val is None:
                    continue
                kind, tol = _PARITY_TOLERANCES[field]
                compared_counts[name] += 1
                if not _within_tolerance(ref_val, val, kind, tol):
                    field_diff_counts[name][field] += 1
                    if len(diffs) < _DIFF_SAMPLE_LIMIT:
                        diffs.append(
                            {
                                "stock_code": code,
                                "field": field,
                                "reference": {
                                    "source": reference_name,
                                    "value": ref_val,
                                },
                                "other": {"source": name, "value": val},
                            }
                        )
    total_diffs = sum(sum(c.values()) for c in field_diff_counts.values())
    out = {
        "status": "mismatch" if total_diffs else "ok",
        "tool": tool,
        "params": params,
        "reference_source": reference_name,
        "sources": {
            n: {k: v for k, v in s.items() if k != "by_code"} for n, s in sources.items()
        },
        "compared_fields": [
            f
            for f in fields
            if f not in STRUCTURAL_GAPS.get("tencent_rank", ())
            or f not in STRUCTURAL_GAPS.get("sina", ())
        ],
        "structural_gaps": {
            n: list(g) for n, g in STRUCTURAL_GAPS.items() if n in sources
        },
        "common_codes": {n: len(set(reference) & set(sources[n]["by_code"])) for n in others},
        "compared_values": compared_counts,
        "field_diff_counts": field_diff_counts,
        "diff_total": total_diffs,
        "diff_samples": diffs,
    }
    if total_diffs:
        out["note"] = (
            "差异含已知口径切换信号：三源 PE 口径不同（腾讯疑 TTM / rank 明示 "
            "pe_ttm / 新浪 per 未验证）；逐字段差异计数见 field_diff_counts"
        )
    return out
