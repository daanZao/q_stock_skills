"""妙想（MX）HTTP 客户端：mx-data 查询接口的薄封装（issue #23/T1）。

只用标准库（urllib.request），不加新依赖。api_key 缺省读环境变量 MX_APIKEY，
缺失抛 MXError（懒加载报错先例见 adapters/akshare_adapter.py 的 _ak()）。
上游业务码（body 里 code：0 成功 / 100 参数错误 / 113 配额上限 / 114 密钥无效）
不在本层判定——client 只负责返回解析后的 body dict，判定在 tools 层。
"""

import json
import os
import urllib.error
import urllib.request

ENDPOINT = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
TIMEOUT_SEC = 30


class MXError(RuntimeError):
    """MX 调用失败：key 缺失、HTTP 非 200、传输错误、非法 JSON 等。"""


class MxClient:
    """mx-data 查询客户端：query(tool_query) → 解析后的响应 body dict。"""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key if api_key is not None else os.environ.get("MX_APIKEY")
        if not key:
            raise MXError(
                "MX_APIKEY 未配置：请设置环境变量 MX_APIKEY（妙想开放平台 apikey）"
            )
        self._api_key = key

    def query(self, tool_query: str) -> dict:
        body = json.dumps({"toolQuery": tool_query}).encode("utf-8")
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "apikey": self._api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise MXError(f"MX HTTP 错误：status={e.code}") from e
        except Exception as e:  # noqa: BLE001 - URLError/超时等传输错误
            raise MXError(f"MX 传输错误：{e}") from e
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MXError(f"MX 返回非法 JSON：{e}") from e
        if not isinstance(parsed, dict):
            raise MXError(f"MX 返回非 JSON 对象：{type(parsed).__name__}")
        return parsed
