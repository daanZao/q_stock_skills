"""个股日线适配器共用：复权参数映射与涨跌/振幅推算（issue #112）。

腾讯与新浪都只给 OHLCV/amount/turnover，change_percent / change_amount /
amplitude 由前收推算。两源的单位换算不同，但推算口径必须一致——集中在这里，
避免两份实现漂移。
"""

from .base import FetchError

# 库内 adj 词汇 → 两源通用的 adjust 参数（none = 不复权）
ADJUST_PARAMS = {"qfq": "qfq", "hfq": "hfq", "none": ""}


def adjust_param(source: str, adj: str) -> str:
    """库内 adj 词汇 → 上游 adjust 参数；不支持的口径抛 FetchError(unsupported)。

    腾讯/新浪 fetch_daily 的公共入口守卫（ADR 0007：by-design 不支持不算抓取
    故障，unsupported 单次即弃不重试）。
    """
    if adj not in ADJUST_PARAMS:
        raise FetchError(f"{source}不支持复权口径: {adj}", kind="unsupported")
    return ADJUST_PARAMS[adj]


def derive_change_fields(close, high, low, prev_close):
    """→ (change_amount, change_percent, amplitude)。

    无前收（序列首行/上市首日）或缺价时对应值为 None（不伪造）；前收为 0
    （异常数据）同样不推算，避免除零。
    """
    change_amount = (
        close - prev_close if close is not None and prev_close else None
    )
    change_percent = (
        change_amount / prev_close * 100
        if change_amount is not None and prev_close
        else None
    )
    amplitude = (
        (high - low) / prev_close * 100
        if high is not None and low is not None and prev_close
        else None
    )
    return change_amount, change_percent, amplitude
