"""context.py — 对话历史管理：token 估算、预算、压缩。

模型无记忆，每轮都要把全部历史重发一遍。历史 = 状态，窗口 = 内存，
这个文件就是 ecall 的「内存管理器」。
"""

MAX_CONTEXT_TOKENS = 60000   # 上下文预算（按所用模型窗口调小、留足余量）
KEEP_RECENT_TOOL_RESULTS = 4  # 压缩时保留最近 N 条工具结果原文
MIN_COMPRESS_CHARS = 300     # 短于这个长度的工具结果不值得压缩


def estimate_tokens(messages) -> int:
    """粗估 token 数：中英混排按约 3 字符 ≈ 1 token。

    预算决策只需要数量级正确，不需要精确——
    就像内存管理器也不需要知道每个字节的用途。
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if content:
            total += len(content) // 3
        for tc in m.get("tool_calls") or []:
            total += len(tc["function"]["arguments"]) // 3
    return total


def compress(messages):
    """规则压缩：把较早的工具结果替换成一行摘要（不消耗模型调用，零成本）。

    保留：system、任务消息、最近 KEEP_RECENT_TOOL_RESULTS 条工具结果原文。
    返回 (历史, 压缩事件列表)。

    注意：这会改写历史 → 修改点之后的前缀缓存失效。
    这是刻意的权衡：逼近窗口上限时，正确性 > 缓存命中率。
    """
    events = []
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-KEEP_RECENT_TOOL_RESULTS]:
        content = messages[i].get("content", "")
        if len(content) > MIN_COMPRESS_CHARS:
            first_line = content.splitlines()[0] if content else ""
            messages[i]["content"] = (
                f"[已压缩] 原工具输出 {len(content)} 字符，首行：{first_line[:80]}"
            )
            events.append({"index": i, "original_chars": len(content)})
    return messages, events


def maybe_compress(messages, budget=MAX_CONTEXT_TOKENS):
    """逼近预算才压缩。返回 (历史, 压缩事件或 None)。"""
    if estimate_tokens(messages) < budget:
        return messages, None
    return compress(messages)