# agent.py — ecall 主循环：思考 → 调工具 → 观察 → 再思考。

import json
import time
from collections import Counter
from pathlib import Path

import context
import llm
import tools

# 系统提示词 = 模型的「ABI 手册」。
# 注意：内容必须字节级稳定，绝不放时间戳等易变信息——
# 前缀缓存是纯前缀匹配，历史里改一个字节，后面所有 token 的缓存全部失效。
SYSTEM_PROMPT = (
    "你是 ecall，一个编程智能体。你通过工具读写工作区内的文件、执行命令来完成任务。\n"
    "工作原则：\n"
    "1. 先观察再行动：改代码前先用 list_dir/grep/glob 了解结构、读相关文件。\n"
    "2. 小步快跑：每次修改尽量小，改完立即验证。\n"
    "3. 证据优先：声称完成前必须实际运行测试或程序，并在最终回复中引用运行结果。\n"
    "4. 遇到失败：读错误输出，定位根因，不要盲目重试。\n"
    "5. 需求不清时（路径不明、目标含糊），停下来用最终回复向我澄清，不要猜测乱试。\n"
)

MAX_STEPS = 30              # 终止条件②：步数预算
MAX_CONSECUTIVE_ERRORS = 5  # 终止条件③：连续错误预算
MAX_TOTAL_TOKENS = 200_000  # 终止条件④：token 总预算（整个任务的累计花费）
WARN_REMAINING = 3          # 剩余 3 步时提醒模型收尾
REPEAT_CALL_LIMIT = 3       # 同一调用重复 N 次判定为振荡


def build_system_prompt() -> str:
    """把工作区绝对路径注入系统提示词（会话内常量，不破坏前缀缓存）。"""
    return (
        SYSTEM_PROMPT
        + f"\n环境信息：你的工作区（当前目录）绝对路径是 {tools.WORKSPACE}；"
          "所有相对路径都基于它，不要再创建与工作区同名的子目录。\n"
    )


def _msg_to_dict(message) -> dict:
    """把 SDK 的 message 对象转成普通 dict——历史全用 dict，context.py 才能统一处理。"""
    d = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        d["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]
    return d


def _default_log_path() -> str:
    """轨迹日志放 ~/.ecall/logs/，绝不写进工作区——
    否则 agent 会 grep 到自己的日志、读自己的黑历史（实测翻车过，叫自我污染）。"""
    log_dir = Path.home() / ".ecall" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return str(log_dir / f"{tools.WORKSPACE.name}-{stamp}.jsonl")


def run(task: str, log_path: str | None = None):
    """执行一个任务，返回 (最终回复, 总 token 数, 轨迹文件路径)。"""
    if log_path is None:
        log_path = _default_log_path()
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": task},
    ]
    total_tokens = 0
    consecutive_errors = 0
    warned = False
    call_counts = Counter()  # 振荡检测：统计每个 (工具, 参数) 组合的出现次数

    with open(log_path, "a", encoding="utf-8") as log:
        def record(event: dict):
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
            log.flush()  # 崩了也不丢最后一条

        record({"type": "task", "content": task, "workspace": str(tools.WORKSPACE)})

        for step in range(1, MAX_STEPS + 1):
            # —— 内存管理：逼近上下文预算就压缩老的工具输出 ——
            messages, events = context.maybe_compress(messages)
            if events:
                record({"type": "compress", "step": step, "events": events,
                        "est_tokens": context.estimate_tokens(messages)})

            # 预算预警：让模型体面收尾，而不是被硬切断
            if not warned and MAX_STEPS - step <= WARN_REMAINING:
                messages.append({"role": "user", "content":
                    "[runtime] 步数预算即将耗尽，请停止探索，立即总结进展并给出最终回复。"})
                warned = True

            # —— 思考：把全部历史发给模型。模型无记忆，历史即状态 ——
            try:
                message, usage = llm.chat(messages, tools.SCHEMAS)
            except llm.ContextOverflow:
                # 缺页处理：窗口炸了 → 强制压缩 → 重试一次
                messages, events = context.compress(messages)
                record({"type": "overflow_compress", "step": step, "events": events})
                try:
                    message, usage = llm.chat(messages, tools.SCHEMAS)
                except Exception as e:
                    record({"type": "fatal", "step": step, "error": str(e)})
                    return f"模型调用失败（压缩后仍失败）：{e}", total_tokens, log_path
            except llm.FatalConfigError as e:
                record({"type": "fatal", "step": step, "error": str(e)})
                return f"模型配置错误（请检查 key 与模型名）：{e}", total_tokens, log_path
            except Exception as e:
                record({"type": "fatal", "step": step, "error": str(e)})
                return f"模型调用失败：{e}", total_tokens, log_path

            total_tokens += usage.total_tokens if usage else 0
            record({
                "type": "llm", "step": step, "content": message.content,
                "tool_calls": [tc.model_dump() for tc in (message.tool_calls or [])],
                "usage": usage.model_dump() if usage else None,
                "est_context": context.estimate_tokens(messages),
            })

            # —— 终止条件④：token 总预算耗尽（整个任务的累计花费）——
            if total_tokens > MAX_TOTAL_TOKENS:
                record({"type": "abort", "reason": "token_budget",
                        "total_tokens": total_tokens})
                return f"超出 token 预算（{total_tokens}），任务中止。", total_tokens, log_path

            # —— 终止条件①：模型不再调用工具 = 它认为做完了 ——
            if not message.tool_calls:
                record({"type": "done", "step": step, "total_tokens": total_tokens})
                return message.content, total_tokens, log_path

            # —— 历史只 append、不改写（前缀缓存铁律；压缩是唯一例外，见 context.py）——
            messages.append(_msg_to_dict(message))

            # —— 行动 + 观察：本地执行工具，结果喂回模型 ——
            for tc in message.tool_calls:
                sig = (tc.function.name, tc.function.arguments)
                call_counts[sig] += 1

                if call_counts[sig] >= REPEAT_CALL_LIMIT:
                    # 振荡检测：同一调用原地打转，不再执行，直接警告它换思路。
                    # 注意：每个 tool_call 都必须有对应的 tool 响应，否则 API 报错——
                    # 所以警告是以「工具结果」的形式喂回去的。
                    result = (f"error: [runtime] 振荡检测：完全相同的调用已重复 "
                              f"{REPEAT_CALL_LIMIT} 次，本次未执行。请分析根因、换个思路。")
                    record({"type": "oscillation", "step": step, "name": sig[0]})
                else:
                    print(f"[step {step}] {tc.function.name}({tc.function.arguments[:60]}...)")
                    result = tools.execute(tc.function.name, tc.function.arguments)
                    record({
                        "type": "tool", "step": step, "name": tc.function.name,
                        "arguments": tc.function.arguments, "result": result,
                    })

                if result.startswith("error"):
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        record({"type": "abort", "reason": "consecutive_errors"})
                        return "连续错误过多，任务中止。", total_tokens, log_path
                else:
                    consecutive_errors = 0

                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "content": result,
                })

        # —— 终止条件②：步数预算耗尽 ——
        record({"type": "abort", "reason": "max_steps"})
        return f"超出步数预算（{MAX_STEPS}），任务中止。", total_tokens, log_path
