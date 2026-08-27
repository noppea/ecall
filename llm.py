"""llm.py — 模型无关的 OpenAI 兼容客户端。

换模型 = 换三个环境变量，代码一行不动（模型是可替换部件）。
"""
import os
import time

from openai import OpenAI

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ["ECALL_BASE_URL"],
            api_key=os.environ["ECALL_API_KEY"],
        )
    return _client


def chat(messages, tools, max_retries: int = 3):
    """一次「思考」。返回 (message, usage)。网络类错误指数退避重试。"""
    for attempt in range(max_retries):
        try:
            resp = _get_client().chat.completions.create(
                model=os.environ["ECALL_MODEL"],
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            return resp.choices[0].message, resp.usage
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)  # 1s → 2s → 4s
