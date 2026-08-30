"""llm.py — 模型无关的 OpenAI 兼容客户端。

换模型 = 换三个环境变量，代码一行不动（模型是可替换部件）。
错误分三类处理：fatal（别重试）/ overflow（上抛给上层压缩）/ transient（退避重试）。

"""
import os
import time

from openai import OpenAI
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.completion_usage import CompletionUsage

_client = None


class ContextOverflow(Exception):
    """上下文超出模型窗口：本地重试无意义，上抛给主循环压缩后重试。"""


class FatalConfigError(Exception):
    """认证/配置错误：重试无意义，立即失败并给出明确提示。"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        missing = [k for k in ("ECALL_BASE_URL", "ECALL_API_KEY", "ECALL_MODEL")
                   if not os.environ.get(k)]
        if missing:
            raise FatalConfigError(
                f"缺少环境变量：{', '.join(missing)}。"
                f"export 只在当前终端生效；参考 .env.example 配好再 source")
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


def _rough_usage(messages, content: str) -> CompletionUsage:
    """网关门没回传 usage 时的兜底估算（预算检查只需要数量级正确）。"""
    prompt = sum(len(str(m.get("content") or "")) for m in messages) // 3
    completion = len(content) // 3
    return CompletionUsage(prompt_tokens=prompt, completion_tokens=completion,
                           total_tokens=prompt + completion)


def _aggregate_stream(stream, messages, on_token):
    """把 SSE 增量聚合成完整的 message 对象（与非流式返回的形状一致）。

    tool_calls 的增量是按 index 分槽的碎片：id 只在首个碎片出现一次，
    name 和 arguments 逐片追加——合并规则就这三条，但拼错就是乱码。
    只带 usage 的收尾块没有 choices，要跳过。
    """
    content_parts: list[str] = []
    slots: dict[int, dict] = {}
    usage = None
    for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            content_parts.append(delta.content)
            if on_token:
                on_token(delta.content)
        for tc in delta.tool_calls or []:
            slot = slots.setdefault(tc.index, {"id": "", "name": "", "args": []})
            if tc.id:
                slot["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function.arguments:
                    slot["args"].append(tc.function.arguments)
    content = "".join(content_parts)
    tool_calls = [
        ChatCompletionMessageToolCall(
            id=slots[i]["id"] or f"call_{i}", type="function",
            function=Function(name=slots[i]["name"],
                              arguments="".join(slots[i]["args"])),
        )
        for i in sorted(slots)
    ]
    message = ChatCompletionMessage(role="assistant", content=content,
                                    tool_calls=tool_calls or None)
    return message, usage or _rough_usage(messages, content)


def chat(messages, tools, max_retries: int = 3, on_token=None):
    """一次「思考」。返回 (message, usage)。

    on_token：流式增量回调（每个 content 片段调一次），仅用于终端实时显示；
    传 None 则静默聚合（评测、子代理走这里，输出不受任何影响）。
    """
    use_stream = os.environ.get("ECALL_NO_STREAM") != "1"
    for attempt in range(max_retries):
        try:
            if use_stream:
                raw = _get_client().chat.completions.create(
                    model=os.environ["ECALL_MODEL"],
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    stream=True,
                    stream_options={"include_usage": True},  # 收尾块带真实 token 统计
                )
                return _aggregate_stream(raw, messages, on_token)
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
            # 流式传输中断也算 transient：已打印的半截内容会重打，可接受
            if on_token:
                on_token(f"\n[流式中断，{2 ** attempt}s 后重试]\n")
            time.sleep(2 ** attempt)  # transient：1s → 2s → 4s 退避重试
