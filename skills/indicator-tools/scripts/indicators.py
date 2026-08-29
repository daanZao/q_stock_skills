"""indicator-tools（管道版）：stdin 吃 query_daily 输出 JSON，stdout 吐指标 JSON。

口径基线继承旧契约（旧系统 data_provider/base.py calculate_indicators）：
min_periods=窗口、MACD EMA(12/26,9) adjust=False、RSI Wilder 平滑、BOLL(20,2) ddof=0。
纯标准库实现，不直接访问数据库，指标现算现用、不物化（边界见 docs/adr/0001）。

用法：
    <query_daily JSON> | python indicators.py --indicator macd [--tail 60]
    <query_daily JSON> | python indicators.py --indicator ma --periods 5,10,20,60
    <query_daily JSON> | python indicators.py --indicator diff1 --on close

退出码：0 = ok / insufficient_data（数据不足非错误）；1 = 输入/运行时错误；
2 = 参数错误（argparse 或 --on 等取值非法）。
"""

import argparse
import json
import sys
from typing import Callable, Optional

Series = list[Optional[float]]


# ---------------------------------------------------------------- 最小长度

def required_length(indicator: str, params: dict) -> int:
    """各指标在默认/给定参数下需要的最小日线长度（与旧契约一致）。"""
    if indicator == "ma":
        return max(params["periods"])
    if indicator == "macd":
        return params["slow"] + params["signal"]  # DIF 需 slow，DEA 再需 signal 个有效 DIF
    if indicator == "boll":
        return params["period"]
    if indicator == "rsi":
        return max(params["periods"]) + 1  # 需先有一阶差分
    if indicator in ("diff1", "diff2"):
        return int(indicator[-1]) + 1
    if indicator in ("rolling_max", "rolling_min", "maxdd"):
        return params["window"]
    if indicator == "chg":
        return params["window"] + 1
    raise ValueError(f"未知指标: {indicator}")


# ---------------------------------------------------------------- 序列原语（min_periods=窗口，头部 null）

def sma(xs: Series, window: int) -> Series:
    out: Series = [None] * len(xs)
    acc = 0.0
    for i, x in enumerate(xs):
        acc += x  # type: ignore[operator]
        if i >= window:
            acc -= xs[i - window]  # type: ignore[operator]
        if i >= window - 1:
            out[i] = acc / window
    return out


def ema(xs: Series, span: int, min_periods: int) -> Series:
    """ewm(span, adjust=False, min_periods)：跳过头部 None 从首个有效值起递归。"""
    alpha = 2.0 / (span + 1)
    out: Series = [None] * len(xs)
    prev: Optional[float] = None
    valid = 0
    for i, x in enumerate(xs):
        if x is None:
            continue
        valid += 1
        prev = x if prev is None else alpha * x + (1 - alpha) * prev
        if valid >= min_periods:
            out[i] = prev
    return out


def _rolling(xs: Series, window: int, fn: Callable[[list], float],
             min_periods: int) -> Series:
    out: Series = [None] * len(xs)
    for i in range(len(xs)):
        chunk = [x for x in xs[max(0, i - window + 1): i + 1] if x is not None]
        if len(chunk) >= min_periods:
            out[i] = fn(chunk)
    return out


def rolling_std(xs: Series, window: int) -> Series:
    """rolling(window, min_periods=window).std(ddof=0)。"""
    def std(chunk: list) -> float:
        mean = sum(chunk) / len(chunk)
        return (sum((x - mean) ** 2 for x in chunk) / len(chunk)) ** 0.5
    return _rolling(xs, window, std, window)


def rolling_extreme(xs: Series, window: int, fn: Callable[[list], float]) -> Series:
    return _rolling(xs, window, fn, window)


def derivative(xs: Series, order: int) -> Series:
    """一阶/二阶导数（离散差分）。边界：diff1 首位 null，diff2 首两位 null。"""
    if order not in (1, 2):
        raise ValueError(f"仅支持一阶/二阶导数，got order={order}")
    out: Series = list(xs)
    for _ in range(order):
        nxt: Series = [None]
        nxt += [None if a is None or b is None else b - a
                for a, b in zip(out, out[1:])]
        out = nxt
    return out


# ---------------------------------------------------------------- 指标计算（与旧契约同口径）

def _map2(fn: Callable[[float, float], float], xs: Series, ys: Series) -> Series:
    """逐点二元运算，任一侧为 None 则该点为 None。"""
    return [None if a is None or b is None else fn(a, b) for a, b in zip(xs, ys)]


def calc_ma(base: Series, periods: list) -> dict:
    return {f"ma{p}": sma(base, p) for p in periods}


def calc_macd(close: Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    ema_fast = ema(close, fast, fast)
    ema_slow = ema(close, slow, slow)
    dif = _map2(lambda f, s: f - s, ema_fast, ema_slow)
    dea = ema(dif, signal, signal)
    bar = _map2(lambda d, e: (d - e) * 2, dif, dea)
    return {"dif": dif, "dea": dea, "bar": bar}


def calc_boll(close: Series, period: int = 20, k: float = 2.0) -> dict:
    mid = sma(close, period)
    std = rolling_std(close, period)
    return {"mid": mid,
            "upper": _map2(lambda m, s: m + k * s, mid, std),
            "lower": _map2(lambda m, s: m - k * s, mid, std)}


def calc_rsi(close: Series, periods: list) -> dict:
    """Wilder 平滑：ewm(alpha=1/p, adjust=False, min_periods=p)；avg_loss=0 按 1e-10 兜底。"""
    gain = [0.0] + [max(b - a, 0.0) for a, b in zip(close, close[1:])]  # type: ignore[operator]
    loss = [0.0] + [max(a - b, 0.0) for a, b in zip(close, close[1:])]  # type: ignore[operator]
    out = {}
    for p in periods:
        avg_gain = _wilder(gain, p)
        avg_loss = _wilder(loss, p)
        rsi: Series = []
        for g, lo in zip(avg_gain, avg_loss):
            if g is None or lo is None:
                rsi.append(None)
            else:
                rs = g / (lo if lo != 0 else 1e-10)
                rsi.append(100 - 100 / (1 + rs))
        out[f"rsi_{p}"] = rsi
    return out


def _wilder(xs: list, period: int) -> Series:
    """ewm(alpha=1/period, adjust=False, min_periods=period)。"""
    alpha = 1.0 / period
    out: Series = [None] * len(xs)
    prev = xs[0]
    for i in range(1, len(xs)):
        prev = alpha * xs[i] + (1 - alpha) * prev
        if i >= period - 1:
            out[i] = prev
    if period == 1 and xs:
        out[0] = xs[0]
    return out


# ---------------------------------------------------------------- 序列组装

BASE_COLUMNS = ("open", "high", "low", "close", "volume", "amount")


def resolve_base_series(cols: dict, on: str) -> Series:
    """--on 基础序列解析：日线列（open/high/low/close/volume/amount），或 maN。"""
    if on in cols:
        return cols[on]
    if on.startswith("ma") and on[2:].isdigit():
        return sma(cols["close"], int(on[2:]))
    raise ValueError(f"不支持的 --on 序列: {on}（可用 {'/'.join(BASE_COLUMNS)}/maN）")


def build_series(cols: dict, indicator: str, params: dict) -> dict:
    """返回 {列名: 序列}。cols 为 {列名: [数值]}（升序）。"""
    close = cols["close"]
    if indicator == "ma":
        base = resolve_base_series(cols, params["on"])
        series = calc_ma(base, params["periods"])
        if params["on"] != "close":
            series = {f"{k}_{params['on']}": v for k, v in series.items()}
        return series
    if indicator == "macd":
        return calc_macd(close, params["fast"], params["slow"], params["signal"])
    if indicator == "boll":
        return calc_boll(close, params["period"], params["k"])
    if indicator == "rsi":
        return calc_rsi(close, params["periods"])
    if indicator in ("diff1", "diff2"):
        base = resolve_base_series(cols, params["on"])
        return {f"{indicator}_{params['on']}": derivative(base, int(indicator[-1]))}
    if indicator in ("rolling_max", "rolling_min"):
        base = resolve_base_series(cols, params["on"])
        w = params["window"]
        fn = max if indicator == "rolling_max" else min
        return {f"{indicator}{w}_{params['on']}": rolling_extreme(base, w, fn)}
    if indicator == "maxdd":
        # 窗口内最大回撤：x / 窗口最高 - 1 的最小值（百分比，负值）
        base = resolve_base_series(cols, params["on"])
        w = params["window"]
        peak = rolling_extreme(base, w, max)
        dd = _map2(lambda x, p: (x / p - 1) * 100, base, peak)
        return {f"maxdd{w}_{params['on']}": _rolling(dd, w, min, 1)}
    if indicator == "chg":
        # N 日前到最新一根的涨跌幅（百分比），仅在序列末尾有判断意义
        base = resolve_base_series(cols, params["on"])
        w = params["window"]
        return {f"chg{w}_{params['on']}": [None] * w + [
            None if b is None or a in (None, 0) else (b / a - 1) * 100
            for a, b in zip(base, base[w:])
        ]}
    raise ValueError(f"未知指标: {indicator}")


# ---------------------------------------------------------------- CLI

def boundary_rule(indicator: str, params: dict) -> str:
    """输出回显用的边界规则说明（null 头部与旧契约一致）。"""
    if indicator.startswith("diff"):
        return "diff1 首元素 null；diff2 首两元素 null"
    if indicator == "macd":
        return (f"DIF 前 {params['slow'] - 1} 点 null；"
                f"DEA/BAR 前 {params['slow'] + params['signal'] - 2} 点 null")
    if indicator == "rsi":
        return "min_periods=N（周期），各 RSI 序列前 N-1 点为 null"
    if indicator == "chg":
        return f"前 {params['window']} 点为 null"
    return "min_periods=窗口，序列前 窗口-1 点为 null"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def load_stdin() -> dict:
    """读取 stdin 的 query_daily 输出 JSON；失败抛 ValueError。"""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"stdin 不是合法 JSON: {e}")
    if not isinstance(payload, dict):
        raise ValueError("stdin 须为 query_daily 输出的 JSON 对象")
    if payload.get("status") != "ok":
        raise ValueError(
            f"上游 status={payload.get('status')!r}（{payload.get('error', '无 error 字段')}），"
            "请传入 status=ok 的 query_daily 输出")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("输入缺少 rows 数组")
    return payload


def parse_cols(rows: list) -> tuple[list, dict]:
    """rows → (trade_dates, {列: [float]})；数值缺失/非法抛 ValueError。"""
    dates = []
    cols: dict[str, list] = {c: [] for c in BASE_COLUMNS}
    for i, row in enumerate(rows):
        dates.append(row.get("trade_date"))
        for c in BASE_COLUMNS:
            v = row.get(c)
            if v is None or isinstance(v, bool):
                raise ValueError(f"第 {i} 行 {c} 缺失或非数值: {v!r}")
            cols[c].append(float(v))
    return dates, cols


def main() -> int:
    ap = argparse.ArgumentParser(
        description="指标计算（stdin 吃 query_daily JSON，stdout 吐指标 JSON）")
    ap.add_argument("--indicator", required=True,
                    choices=["ma", "macd", "boll", "rsi", "diff1", "diff2",
                             "rolling_max", "rolling_min", "maxdd", "chg"])
    ap.add_argument("--periods", default=None, help="ma/rsi 周期列表，逗号分隔")
    ap.add_argument("--period", type=int, default=20, help="boll 周期")
    ap.add_argument("--k", type=float, default=2.0, help="boll 标准差倍数")
    ap.add_argument("--fast", type=int, default=12)
    ap.add_argument("--slow", type=int, default=26)
    ap.add_argument("--signal", type=int, default=9)
    ap.add_argument("--window", type=int, default=250,
                    help="rolling_max/rolling_min/maxdd/chg 窗口（默认 250≈52 周）")
    ap.add_argument("--on", default="close",
                    help="基础序列：open/high/low/close/volume/amount/maN"
                         "（ma/diff*/rolling_*/maxdd/chg 可用）")
    ap.add_argument("--tail", type=int, default=60, help="输出末尾 N 个点；0=全部")
    args = ap.parse_args()  # 参数错误由 argparse 以退出码 2 终止

    default_periods = {"ma": [5, 10, 20, 60], "rsi": [6, 12, 24]}
    try:
        params = {
            "periods": ([int(p) for p in args.periods.split(",")] if args.periods
                        else default_periods.get(args.indicator, [])),
            "period": args.period, "k": args.k,
            "fast": args.fast, "slow": args.slow, "signal": args.signal,
            "on": args.on, "window": args.window,
        }
    except ValueError:
        emit({"status": "error", "error": f"--periods 格式非法: {args.periods!r}"})
        return 2

    # 参数校验先于数据检查：参数错误不被 insufficient_data 遮蔽
    if args.on not in BASE_COLUMNS and not (
            args.on.startswith("ma") and args.on[2:].isdigit()):
        emit({"status": "error",
              "error": f"不支持的 --on 序列: {args.on}"
                       f"（可用 {'/'.join(BASE_COLUMNS)}/maN）"})
        return 2

    try:
        payload = load_stdin()
        dates, cols = parse_cols(payload["rows"])
    except (ValueError, TypeError, AttributeError) as e:
        emit({"status": "error", "error": str(e)})
        return 1

    stock_code = (payload.get("params") or {}).get("stock_code")
    have = len(dates)
    need = required_length(args.indicator, params)
    if have < need:
        emit({"status": "insufficient_data", "stock_code": stock_code,
              "indicator": args.indicator, "params": params,
              "required_length": need, "available_length": have,
              "message": f"计算 {args.indicator} 至少需要 {need} 根日线，输入仅 {have} 根"})
        return 0

    try:
        series = build_series(cols, args.indicator, params)
    except ValueError as e:
        emit({"status": "error", "error": str(e)})
        return 2

    tail = args.tail if args.tail > 0 else have
    points = []
    for i in range(max(0, have - tail), have):
        point = {"trade_date": dates[i]}
        for name, s in series.items():
            v = s[i]
            point[name] = None if v is None else round(v, 4)
        points.append(point)

    emit({
        "status": "ok",
        "stock_code": stock_code,
        "indicator": args.indicator,
        "params": params,
        "data_range": {"start": dates[0], "end": dates[-1]},
        "required_length": need,
        "available_length": have,
        "boundary_rule": boundary_rule(args.indicator, params),
        "series": points,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
