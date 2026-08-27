# agent.py — ecall 主循环：思考 → 调工具 → 观察 → 再思考。

import json

import llm
import tools

# 系统提示词
SYSTEM_PROMPT = (
    "你是 ecall，一个编程智能体。你通过工具读写工作区内的文件、执行命令来完成任务。\n"
    "工作原则：\n"
    "1. 先观察再行动：改代码前先读相关文件。\n"
    "2. 小步快跑：每次修改尽量小，改完立即验证。\n"
    "3. 证据优先：声称完成前必须实际运行测试或程序，并在最终回复中引用运行结果。\n"
    "4. 遇到失败：读错误输出，定位根因，不要盲目重试。\n"
)

MAX_STEPS = 30              # 步数预算（终止条件②）
MAX_CONSECUTIVE_ERRORS = 5  # 连续错误预算（终止条件③）


def run(task: str, log_path: str = "trajectory.jsonl"):
    """执行一个任务，返回 (最终回复, 总 token 数)。轨迹全程落盘 JSONL。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    total_tokens = 0
    consecutive_errors = 0

    with open(log_path, "a", encoding="utf-8") as log:
        def record(event: dict):
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
            log.flush()  # 崩了也不丢最后一条

        record({"type": "task", "content": task})

        for step in range(1, MAX_STEPS + 1):
            # —— 思考：把全部历史发给模型。模型无记忆，历史即状态 ——
            try:
                message, usage = llm.chat(messages, tools.SCHEMAS)
            except Exception as e:
                record({"type": "fatal", "step": step, "error": str(e)})
                return f"模型调用失败：{e}", total_tokens
            total_tokens += usage.total_tokens if usage else 0
            record({
                "type": "llm", "step": step, "content": message.content,
                "tool_calls": [tc.model_dump() for tc in (message.tool_calls or [])],
                "usage": usage.model_dump() if usage else None,
            })

            # 终止条件①：模型不再调用工具 = 它认为做完了
            if not message.tool_calls:
                record({"type": "done", "step": step, "total_tokens": total_tokens})
                return message.content, total_tokens

            # 历史只 append、不改写（前缀缓存铁律）
            messages.append(message)

            # 行动 + 观察：本地执行工具，结果喂回模型 
            for tc in message.tool_calls:
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
                        return "连续错误过多，任务中止。", total_tokens
                else:
                    consecutive_errors = 0

                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "content": result,
                })

        # 终止条件2：步数预算耗尽 
        record({"type": "abort", "reason": "max_steps"})
        return f"超出步数预算（{MAX_STEPS}），任务中止。", total_tokens
