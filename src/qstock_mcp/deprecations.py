"""基本面弃用清单（issue #115，spec #111）：第四态 planned_absent 的最小落地。

弃用面从采集/透传链移除后，消费方需要一个机器可读的事实来源来说明
"这个 section 是决策性缺席，不是故障"——不再被误报进 errors，也不静默
消失。#113 将为全部采集面落地第四态编排契约（planned_absent 的编排侧
语义）；本清单是基本面透传面的先行承载，#113 落地后可直接消费本常量
（或其迁移后的等价物）生成第四态占位，接口保持不变。
"""

PLANNED_ABSENT_SECTIONS: dict[str, str] = {
    "base_info": (
        "efinance get_base_info 随东财链路退役（spec #111 剔东财），估值快照"
        "（PE/PB/市值）改由 akshare 百度股市通估值序列承载（valuation section）"
    ),
}
