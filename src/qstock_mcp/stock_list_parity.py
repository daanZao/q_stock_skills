"""股票清单多源互证（issue #122，spec #111 长尾验收）：逐源直取、不落库。

与 fallback 不同，清单链每个源（efinance → akshare 交易所官网 → baostock）
都被独立调用：失败只记入该源，不中断其他源。用途是迁移期的清单链质量验证
——东财不可达（efinance 腿失败）、akshare 交易所官网源含北交所腿（bse.cn
WAF 拦截见 issue #108）、baostock 兜底（#100 调研：清单链实际已降级为
baostock 单源在跑）都如实呈现。固定 CLI 入口
`qstock-scheduler stock-list --check`（手册 `docs/stock-list-cli.md`）。

比对口径：行数、代码集合双向差集（A 股代码为稳定标识，名称不作为差异——
各源名称清洗口径不同）。差异为对拍产出非失败信号；全部源失败才 error。
"""

from typing import Sequence

from .adapters import DataAdapter, default_adapters, fetch_failure_kind
from .output import error as _error

_SET_SAMPLE_LIMIT = 20  # 代码集合差集样本上限（全市场差集可能上千，样本供人工核读）


def compare_stock_list_sources(
    adapters: Sequence[DataAdapter] | None = None,
) -> dict:
    """清单链逐源独立抓取并互证（不落库）。

    逐源调用 fetch_stock_list（失败只记该源）；以有数据的最高优先级源为
    参考，逐源比对行数与代码集合（双向差集样本）。全部源失败返回 error。
    """
    tool = "compare_stock_list_sources"
    params: dict = {}
    if adapters is None:
        adapters = default_adapters()
    sources: dict[str, dict] = {}
    for adapter in adapters:
        try:
            rows = adapter.fetch_stock_list()
        except Exception as e:  # noqa: BLE001 - 单源失败只记该源，不中断对拍
            sources[adapter.name] = {
                "status": "error",
                "failure_kind": fetch_failure_kind(e),
                "error": str(e)[:300],
            }
            continue
        by_code = {str(r.get("stock_code")): r for r in rows or [] if r.get("stock_code")}
        sources[adapter.name] = {
            "status": "ok" if by_code else "no_data",
            "rows": len(by_code),
            "codes": by_code,
        }
    ok_sources = [n for n, v in sources.items() if v.get("codes")]
    if not ok_sources:
        return {
            "status": "error",
            "tool": tool,
            "params": params,
            "sources": {n: {k: v for k, v in s.items() if k != "codes"}
                        for n, s in sources.items()},
            "error": "全部清单源无数据或失败",
        }
    reference_name = ok_sources[0]
    reference = sources[reference_name]["codes"]
    others = ok_sources[1:]
    ref_set = set(reference)
    comparisons: dict[str, dict] = {}
    diff_total = 0
    for name in others:
        other_set = set(sources[name]["codes"])
        only_ref = sorted(ref_set - other_set)
        only_other = sorted(other_set - ref_set)
        diff_total += len(only_ref) + len(only_other)
        comparisons[name] = {
            "rows": sources[name]["rows"],
            "common_codes": len(ref_set & other_set),
            "reference_only_sample": only_ref[:_SET_SAMPLE_LIMIT],
            "other_only_sample": only_other[:_SET_SAMPLE_LIMIT],
            "reference_only_count": len(only_ref),
            "other_only_count": len(only_other),
        }
    out = {
        "status": "mismatch" if diff_total else "ok",
        "tool": tool,
        "params": params,
        "reference_source": reference_name,
        "sources": {
            n: {k: v for k, v in s.items() if k != "codes"}
            for n, s in sources.items()
        },
        "bse_coverage": {
            n: sum(1 for c in s["codes"] if c[:1] in ("4", "8", "9"))
            for n, s in sources.items()
            if s.get("codes")
        },
        "comparisons": comparisons,
        "diff_total": diff_total,
    }
    if diff_total:
        out["note"] = (
            "代码集合差异为对拍产出非失败信号：各源清单口径不同"
            "（交易所官网含北交所腿、bse.cn WAF 拦截致 akshare 整函数失败、"
            "baostock 仅沪深主板+创业板+科创板等），详见 comparisons"
        )
    return out
