"""指数快照两源对拍（issue #122，spec #111 总验收）：逐源直取、字段级比较、不落库。

与 fallback 不同，指数快照链每个源（腾讯批量报价 / 新浪批量报价，issue #117）
都被独立调用：失败/无数据只记入该源，不中断其他源。用途是迁移后的数据质量
验证，固定 CLI 入口 `qstock-scheduler parity` 的 indices 面
（手册 `docs/parity-cli.md`）；指数日线侧的库内连续性由
`tools_index.verify_index_coverage` 承担，与本对拍并列于 indices 面。

结构性缺项（源 by-design 不提供，不是数据缺失，比对时跳过）：
- 新浪 hq.sinajs.cn：volume_ratio（无量比字段，issue #117）。

容差沿用 daily_parity 口径：价格/涨跌额绝对差、量/幅/量比绝对差、
成交额相对差。参考源 = 链首腾讯（issue #117 指数快照主承载）。
"""

from typing import Sequence

from .adapters import (
    BoardAdapter,
    default_index_spot_adapters,
    fetch_failure_kind,
)
from .adapters.base import INDEX_FIELDS
from .daily_parity import _within_tolerance
from .output import error as _error
from .tools_init import MAJOR_INDICES

# 对拍容差（比较函数 _within_tolerance 复用 daily_parity，同 tools_lhb 先例；
# 本表字段集为指数面定制，不下放 daily_parity）
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
    "volume": ("abs", 100.0),
    "amount": ("rel", 0.005),
}

# 结构性缺项：源 by-design 不提供的字段，不参与比对（非数据缺失）
STRUCTURAL_GAPS: dict[str, tuple[str, ...]] = {
    "sina": ("volume_ratio",),
}

_DIFF_SAMPLE_LIMIT = 20  # 差异样本上限（供人工核读）


def compare_index_spot_sources(
    codes: Sequence[str] | str | None = None,
    adapters: Sequence[BoardAdapter] | None = None,
) -> dict:
    """两源直取主要指数快照并做字段级对拍（不落库）。

    逐源独立逐 code 调用 fetch_index_spot（失败只记该源，不中断对拍）；
    codes 缺省 = MAJOR_INDICES 7 大指数白名单（单一事实来源）。按代码交集
    与参考源（有数据的最高优先级源）逐字段比对，结构性缺项跳过；差异超容差
    输出样本（上限 _DIFF_SAMPLE_LIMIT）与逐字段差异计数。全部源失败返回
    error。行级 trade_date（腾讯取行情时间戳）分源报告，供"同一交易日"
    判定。
    """
    tool = "compare_index_spot_sources"
    params: dict = {"codes": codes}
    if codes is None:
        selected = [c for c, _ in MAJOR_INDICES]
    elif isinstance(codes, str):
        selected = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        selected = [str(c).strip() for c in codes if str(c).strip()]
    if not selected:
        return _error(tool, params, "无指数代码")
    if adapters is None:
        adapters = default_index_spot_adapters()

    sources: dict[str, dict] = {}
    for adapter in adapters:
        by_code: dict[str, dict] = {}
        trade_dates: set[str] = set()
        errors: list[str] = []
        last_exc: Exception | None = None
        for code in selected:
            try:
                rows = adapter.fetch_index_spot(code)
            except Exception as e:  # noqa: BLE001 - 单源单 code 失败只记该源
                errors.append(f"{code}: {e}")
                last_exc = e
                continue
            for row in rows or []:
                by_code[row["index_code"]] = row
                if row.get("trade_date"):
                    trade_dates.add(row["trade_date"])
        entry: dict = {
            "indices": len(by_code),
            "by_code": by_code,
            "trade_dates": sorted(trade_dates),
        }
        if by_code:
            entry["status"] = "ok"
        else:
            entry["status"] = "error" if errors else "no_data"
            if errors:
                entry["error"] = "; ".join(errors[:5])
                entry["failure_kind"] = fetch_failure_kind(last_exc)
        sources[adapter.name] = entry

    ok_sources = [n for n, v in sources.items() if v.get("by_code")]
    if not ok_sources:
        return {
            "status": "error",
            "tool": tool,
            "params": params,
            "sources": {n: {k: v for k, v in s.items() if k != "by_code"}
                        for n, s in sources.items()},
            "error": "全部指数快照源无数据或失败",
        }
    reference_name = ok_sources[0]
    reference = sources[reference_name]["by_code"]
    others = ok_sources[1:]

    fields = [f for f in INDEX_FIELDS if f not in ("index_code", "index_name")]
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
                                "index_code": code,
                                "field": field,
                                "reference": {
                                    "source": reference_name,
                                    "value": ref_val,
                                },
                                "other": {"source": name, "value": val},
                            }
                        )
    total_diffs = sum(sum(c.values()) for c in field_diff_counts.values())
    all_dates = [d for s in sources.values() for d in s.get("trade_dates") or []]
    out = {
        "status": "mismatch" if total_diffs else "ok",
        "tool": tool,
        "params": params,
        "reference_source": reference_name,
        "sources": {
            n: {k: v for k, v in s.items() if k != "by_code"}
            for n, s in sources.items()
        },
        "same_day": bool(all_dates) and all(d == all_dates[0] for d in all_dates),
        "compared_fields": [
            f for f in fields
            if f not in STRUCTURAL_GAPS.get("sina", ())
        ],
        "structural_gaps": {
            n: list(g) for n, g in STRUCTURAL_GAPS.items() if n in sources
        },
        "common_indices": {
            n: len(set(reference) & set(sources[n]["by_code"])) for n in others
        },
        "compared_values": compared_counts,
        "field_diff_counts": field_diff_counts,
        "diff_total": total_diffs,
        "diff_samples": diffs,
    }
    if total_diffs:
        out["note"] = "差异为对拍产出非失败信号；逐字段差异计数见 field_diff_counts"
    return out
