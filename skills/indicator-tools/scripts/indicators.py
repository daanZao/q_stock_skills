"""indicator-tools（管道版）：stdin 吃 query_daily 输出 JSON，stdout 吐指标 JSON。

口径基线继承旧契约（旧系统 data_provider/base.py calculate_indicators）：
min_periods=窗口、MACD EMA(12/26,9) adjust=False、RSI Wilder 平滑、BOLL(20,2) ddof=0。
纯标准库实现，不直接访问数据库，指标现算现用、不物化（边界见 docs/adr/0001）。

用法：
    <query_daily JSON> | python indicators.py --indicator macd [--tail 60]
    <query_daily JSON> | python indicators.py --indicator ma --periods 5,10,20,60
    <query_daily JSON> | python indicators.py --indicator diff1 --on close
    <corr JSON>      | python indicators.py --indicator corr [--on close]
    <promote JSON>   | python indicators.py --indicator promote

corr 输入为多标的：{"status":"ok","series":{代码: query_daily 输出}}；
promote 输入为昨日/今日连板数据：{"status":"ok","yesterday":{...},"today":{...}}。

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
    if indicator == "vol_price":
        return 2  # 需前一日环比
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


# ---------------------------------------------------------------- 量价判定 / 相关性

def calc_vol_price(close: Series, amount: Series, threshold: float) -> dict:
    """量价形态判定：逐日价格/成交额环比（%），按 threshold（小数，如 0.2=20%）分档。

    量档：amount_chg >= θ*100 放量；<= -θ*100 缩量；否则平量。
    形态：放量上涨/缩量上涨/平量上涨/放量下跌/缩量下跌/平量下跌/平盘。
    divergence=True 仅当缩量上涨（价升量减，经典量价背离）。
    """
    n = len(close)
    t = threshold * 100
    price_chg: list = [None] * n
    amount_chg: list = [None] * n
    pattern: list = [None] * n
    divergence: list = [None] * n
    for i in range(1, n):
        a, b = close[i - 1], close[i]
        va, vb = amount[i - 1], amount[i]
        if a in (None, 0) or b is None or va in (None, 0) or vb is None:
            continue
        pc = (b / a - 1) * 100
        ac = (vb / va - 1) * 100
        price_chg[i] = pc
        amount_chg[i] = ac
        if pc > 0:
            leg = "上涨"
            vol = "放量" if ac >= t else ("缩量" if ac <= -t else "平量")
        elif pc < 0:
            leg = "下跌"
            vol = "放量" if ac >= t else ("缩量" if ac <= -t else "平量")
        else:
            leg = None
            vol = None
        pattern[i] = "平盘" if leg is None else f"{vol}{leg}"
        divergence[i] = pattern[i] == "缩量上涨"
    return {"price_chg": price_chg, "amount_chg": amount_chg,
            "vol_price": pattern, "divergence": divergence}


def pearson(xs: list, ys: list) -> Optional[float]:
    """Pearson 相关系数；任一侧方差为 0 时无定义，返回 None。"""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / (vx * vy) ** 0.5


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
    if indicator == "vol_price":
        return calc_vol_price(cols["close"], cols["amount"], params["threshold"])
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
    if indicator == "vol_price":
        return "首点 null（环比需前一交易日）"
    return "min_periods=窗口，序列前 窗口-1 点为 null"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _read_payload() -> dict:
    """读取 stdin JSON 并做公共校验（合法 JSON、dict、status=ok）；失败抛 ValueError。"""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"stdin 不是合法 JSON: {e}")
    if not isinstance(payload, dict):
        raise ValueError("stdin 须为 JSON 对象")
    if payload.get("status") != "ok":
        raise ValueError(
            f"上游 status={payload.get('status')!r}（{payload.get('error', '无 error 字段')}），"
            "请传入 status=ok 的输入")
    return payload


def load_stdin() -> dict:
    """读取 stdin 的 query_daily 输出 JSON；失败抛 ValueError。"""
    payload = _read_payload()
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


def run_corr(args, params) -> int:
    """corr：多标的相关性矩阵。输入 {"series": {代码: query_daily 输出}}，
    按 trade_date 交集对齐后两两计算 Pearson。"""
    try:
        payload = _read_payload()
        raw = payload.get("series")
        if not isinstance(raw, dict) or len(raw) < 2:
            raise ValueError(
                "corr 输入须含 series 对象（至少 2 个标的："
                '{"status":"ok","series":{代码: query_daily 输出}}）')
        per: dict[str, tuple[list, Series]] = {}
        for code, sub in raw.items():
            rows = sub.get("rows") if isinstance(sub, dict) else None
            if not isinstance(rows, list):
                raise ValueError(f"series[{code!r}] 缺少 rows 数组")
            dates, cols = parse_cols(rows)
            per[code] = (dates, resolve_base_series(cols, args.on))
    except (ValueError, TypeError, AttributeError) as e:
        emit({"status": "error", "error": str(e)})
        return 1

    common: Optional[set] = None
    for dates, _ in per.values():
        common = set(dates) if common is None else common & set(dates)
    first_dates = next(iter(per.values()))[0]
    aligned = [d for d in first_dates if d in (common or set())]
    if len(aligned) < 2:
        emit({"status": "insufficient_data", "indicator": "corr",
              "params": {"on": args.on},
              "required_length": 2, "available_length": len(aligned),
              "message": f"corr 至少需要 2 个对齐交易日，当前交集仅 {len(aligned)} 个"})
        return 0

    symbols = sorted(per)
    aligned_series = {}
    for code, (dates, series) in per.items():
        by_date = dict(zip(dates, series))
        aligned_series[code] = [by_date[d] for d in aligned]
    matrix = {}
    for a in symbols:
        row = {}
        for b in symbols:
            r = pearson(aligned_series[a], aligned_series[b])
            row[b] = None if r is None else round(r, 4)
        matrix[a] = row

    emit({"status": "ok", "indicator": "corr", "params": {"on": args.on},
          "symbols": symbols,
          "aligned_length": len(aligned),
          "required_length": 2,
          "data_range": {"start": aligned[0], "end": aligned[-1]},
          "boundary_rule": "按 trade_date 交集对齐；方差为 0 的标的对应矩阵位置为 null",
          "matrix": matrix})
    return 0


def run_promote(args, params) -> int:
    """promote：晋级率与连板梯队。输入昨日/今日连板数据
    {"yesterday": {"trade_date", "boards": [{stock_code, limit_count}]}, "today": {...}}。"""
    try:
        payload = _read_payload()
        legs = {}
        for key in ("yesterday", "today"):
            leg = payload.get(key)
            boards_raw = leg.get("boards") if isinstance(leg, dict) else None
            if not isinstance(boards_raw, list):
                raise ValueError(f"输入缺少 {key}.boards 数组")
            boards = []
            for i, b in enumerate(boards_raw):
                code = b.get("stock_code") if isinstance(b, dict) else None
                lc = b.get("limit_count") if isinstance(b, dict) else None
                if not isinstance(code, str) or not code:
                    raise ValueError(
                        f"{key}.boards[{i}] stock_code 缺失或非法: {code!r}")
                if not isinstance(lc, int) or isinstance(lc, bool) or lc < 1:
                    raise ValueError(
                        f"{key}.boards[{i}] limit_count 须为正整数: {lc!r}")
                boards.append((code, lc))
            legs[key] = {"trade_date": leg.get("trade_date"), "boards": boards}
    except (ValueError, TypeError, AttributeError) as e:
        emit({"status": "error", "error": str(e)})
        return 1

    y, t = legs["yesterday"], legs["today"]
    if not y["boards"]:
        emit({"status": "insufficient_data", "indicator": "promote",
              "required_length": 1, "available_length": 0,
              "message": "昨日无连板股，晋级率无意义"})
        return 0

    today_codes = {c for c, _ in t["boards"]}
    promoted = [c for c, _ in y["boards"] if c in today_codes]
    failed = [c for c, _ in y["boards"] if c not in today_codes]
    ladder: dict[str, int] = {}
    ladder_detail: dict[str, list] = {}
    for c, lc in sorted(t["boards"], key=lambda x: x[1]):
        k = str(lc)
        ladder[k] = ladder.get(k, 0) + 1
        ladder_detail.setdefault(k, []).append(c)

    emit({"status": "ok", "indicator": "promote", "params": {},
          "yesterday": {"trade_date": y["trade_date"], "count": len(y["boards"])},
          "today": {"trade_date": t["trade_date"], "count": len(t["boards"])},
          "promote_rate": round(len(promoted) / len(y["boards"]) * 100, 4),
          "promoted": promoted, "failed": failed,
          "ladder": ladder, "ladder_detail": ladder_detail})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="指标计算（stdin 吃 query_daily JSON，stdout 吐指标 JSON）")
    ap.add_argument("--indicator", required=True,
                    choices=["ma", "macd", "boll", "rsi", "diff1", "diff2",
                             "rolling_max", "rolling_min", "maxdd", "chg",
                             "corr", "vol_price", "promote"])
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
                         "（ma/diff*/rolling_*/maxdd/chg/corr 可用）")
    ap.add_argument("--threshold", type=float, default=0.2,
                    help="vol_price 放量/缩量阈值（小数，默认 0.2=20%）")
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
            "threshold": args.threshold,
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
    if args.indicator == "vol_price" and not 0 <= args.threshold < 10:
        emit({"status": "error",
              "error": f"--threshold 须在 [0, 10) 区间: {args.threshold!r}"})
        return 2

    if args.indicator == "corr":
        return run_corr(args, params)
    if args.indicator == "promote":
        return run_promote(args, params)

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
            if v is None or isinstance(v, (str, bool)):
                point[name] = v  # vol_price 的判定标签/布尔标志原样输出
            else:
                point[name] = round(v, 4)
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
