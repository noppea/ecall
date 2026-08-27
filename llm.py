"""llm.py — 模型无关的 OpenAI 兼容客户端。

换模型 = 换三个环境变量，代码一行不动（模型是可替换部件）。
错误分三类处理：fatal（别重试）/ overflow（上抛给上层压缩）/ transient（退避重试）。
"""
import os
import time

from openai import OpenAI

_client = None


class ContextOverflow(Exception):
    """上下文超出模型窗口：本地重试无意义，上抛给主循环压缩后重试。"""


class FatalConfigError(Exception):
    """认证/配置错误：重试无意义，立即失败并给出明确提示。"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ["ECALL_BASE_URL"],
            api_key=os.environ["ECALL_API_KEY"],
        )
    return _client


def _classify_error(e: Exception) -> str:
    """看错误信息分类。等价于内核里的异常向量表：不同的异常走不同的处理程序。"""
    msg = str(e).lower()
    if ("401" in msg or "unauthorized" in msg
            or "invalid api key" in msg or "authentication" in msg):
        return "fatal"
    if (("context" in msg and ("length" in msg or "window" in msg))
            or "too long" in msg):
        return "overflow"
    return "transient"


def chat(messages, tools, max_retries: int = 3):
    """一次「思考」。返回 (message, usage)。"""
    for attempt in range(max_retries):
        try:
            resp = _get_client().chat.completions.create(
                model=os.environ["ECALL_MODEL"],
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            return resp.choices[0].message, resp.usage
        except Exception as e:
            kind = _classify_error(e)
            if kind == "fatal":
                raise FatalConfigError(str(e)) from e
            if kind == "overflow":
                raise ContextOverflow(str(e)) from e
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)  # transient：1s → 2s → 4s 退避重试