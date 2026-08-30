"""context.py — 对话历史管理：token 估算、预算、压缩。

模型无记忆，每轮都要把全部历史重发一遍。历史 = 状态，窗口 = 内存，
这个文件就是 ecall 的「内存管理器」。

"""
import hashlib
import os

import tools


MAX_CONTEXT_TOKENS = int(os.environ.get("ECALL_MAX_CONTEXT", 60000))  # 上下文预算
KEEP_RECENT_TOOL_RESULTS = 4  # 压缩时保留最近 N 条工具结果原文
MIN_COMPRESS_CHARS = 300     # 短于这个长度的工具结果不值得压缩
SWAP_DIR_NAME = ".ecall-swap"  # 换出文件的落点（tools 的观察三件套会跳过它）


def _swap_out(content: str) -> str:
    """把被压缩的工具输出换出到磁盘。内容寻址（git 同款）：
    同内容同文件名，天然去重；不同内容必然不同名，不会互相覆盖。"""
    digest = hashlib.md5(content.encode("utf-8")).hexdigest()[:10]
    d = tools.WORKSPACE / SWAP_DIR_NAME
    d.mkdir(exist_ok=True)
    (d / f"{digest}.txt").write_text(content, encoding="utf-8")
    return f"{SWAP_DIR_NAME}/{digest}.txt"


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
    """规则压缩：把较早的工具结果换出到磁盘，原地留下带路径的占位符。

    保留：system、任务消息、最近 KEEP_RECENT_TOOL_RESULTS 条工具结果原文。
    返回 (历史, 压缩事件列表)。

    两笔刻意的设计账：
    - 零 API 成本：摘要是规则生成的，不消耗模型调用（对比：LLM 摘要管线
      要多花一次请求，还引入摘要幻觉的风险）；
    - 改写历史会让修改点之后的前缀缓存失效——逼近窗口上限时，
      正确性 > 缓存命中率，这是刻意的权衡。
    """
    events = []
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-KEEP_RECENT_TOOL_RESULTS]:
        content = messages[i].get("content", "")
        if len(content) > MIN_COMPRESS_CHARS:
            first_line = content.splitlines()[0] if content else ""
            swap_path = _swap_out(content)
            # 占位符二选一：模型当场写的笔记（聪明，知道重点）优先；
            # 没有笔记退回首行规则（笨但零成本）。原文在 swap 里，两路都可拉回。
            note = messages[i].get("_digest")
            placeholder = (f"[已压缩] 原工具输出 {len(content)} 字符，已换出到 {swap_path}"
                           f"（需要细节可 read_file 拉回）")
            placeholder += f"；模型笔记：{note}" if note else f"；首行：{first_line[:80]}"
            messages[i]["content"] = placeholder
            events.append({"index": i, "original_chars": len(content),
                           "swap": swap_path})
    return messages, events


def maybe_compress(messages, budget=MAX_CONTEXT_TOKENS):
    """逼近预算才压缩。返回 (历史, 压缩事件或 None)。"""
    if estimate_tokens(messages) < budget:
        return messages, None
    return compress(messages)
