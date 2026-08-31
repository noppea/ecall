# agent.py — ecall 主循环：思考 → 调工具 → 观察 → 再思考。

import json
import os
import sys
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
    "6. 读到大量内容（长文件、长日志、大量 grep 结果）后，立即用 digest 记下要点"
    "（结论、关键行号）；上下文压缩时，你的笔记会取代原文成为你的记忆。\n"
)

# 预算三件套全部环境变量化：默认值是分钟级任务的标定，
# 长程实验用 ECALL_MAX_STEPS / ECALL_MAX_TOTAL_TOKENS 放宽——
# 实验配置进环境不进源码（与 ECALL_MAX_CONTEXT 同一原则）
MAX_STEPS = int(os.environ.get("ECALL_MAX_STEPS", 30))  # 终止条件②：步数预算
MAX_CONSECUTIVE_ERRORS = 5  # 终止条件③：连续错误预算
MAX_TOTAL_TOKENS = int(os.environ.get("ECALL_MAX_TOTAL_TOKENS", 200_000))  # 终止条件④：token 总预算
WARN_REMAINING = 3          # 剩余 3 步时提醒模型收尾
REPEAT_CALL_LIMIT = 3       # 同一调用重复 N 次判定为振荡

MUTATING_TOOLS = ("write_file", "edit_file")  # 动手前要打 checkpoint 的工具
OBSERVATION_TOOLS = ("read_file", "list_dir", "grep", "glob", "run_shell")  # 大输出需要 digest 的观察类工具
DIGEST_ENFORCE_MIN_CHARS = 1500  # 超过这个体积的观察触发强制笔记政策

# —— 子代理：只读探索员 ——
# 父上下文是宝贵资产（前缀缓存 + token 预算），探索阶段的几十条 grep 输出
# 不该污染它。子代理像 fork 出的进程：有自己的地址空间（独立 messages）、
# 权限收缩（只读工具白名单，execute 层强制）、只带回一个返回值（结论摘要）。
# 白名单不含 explore 自己，套娃在 Schema 和 execute 两层都被堵死。
EXPLORE_SYSTEM_PROMPT = (
    "你是 ecall 的只读探索子代理。你的任务是在工作区里查清一个问题并汇报结论。\n"
    "你只有只读工具（read_file/list_dir/grep/glob），不能修改文件、不能执行命令。\n"
    "要求：结论要具体——给出相关文件路径、行号、关键代码片段；\n"
    "如果没找到，明确说没找到，并列出你查过哪些地方。\n"
)
EXPLORE_TOOLS = ("read_file", "list_dir", "grep", "glob")  # 只读白名单
# 子代理步数预算比父代理小：探索不该比干活还贵。
# 实测教训：15 步对内核级仓库太浅（子代理集体超预算，父代理被迫亲自下海）
EXPLORE_MAX_STEPS = int(os.environ.get("ECALL_EXPLORE_MAX_STEPS", 15))

_SUBAGENT_TOKENS = 0        # 子代理累计花费（计入父代理的预算检查，不许隐身）

# 全局实时熔断计数器：父代理与子代理的每一次 API 调用后立即累加。
# 教训（真实事故）：旧设计是「子代理回家后一次性把账单计入父预算」——
# 事后结算。explore_batch 扇出 4 个 × 子代理各 40 步，父代理 8 步烧穿
# 2M token（568 条子代理事件），护栏全程没有机会介入。
# 进程树的资源边界必须是全局且实时的，不能等进程退出再算总账。
_TOTAL_SPENT = [0]  # 列表单元格：子代理线程也能就地累加
_CURRENT_LOG: str | None = None  # 当前活跃轨迹，供 explore 把子代理事件记进同一份日志


AGENTS_MD_MAX_CHARS = 2000  # 项目自定义指令的长度上限（保护上下文预算）


def build_system_prompt() -> str:
    """系统提示词 = 内置原则 + 环境信息 + 项目自定义（AGENTS.md）。

    AGENTS.md 是「声明式提示词 + 长期记忆」的合体：
    - 声明式：项目级的约定（代码风格、测试命令、禁区）写在文件里，不进代码；
    - 记忆：模型自己有读写工具，想跨会话记住什么，让它写进 AGENTS.md——
      「记忆就是一个文件，agent 自己维护」（Claude Code 的 CLAUDE.md 同款思路）。
    会话开始时读入并冻结进 messages[0]，运行中改了也下个会话才生效——
    冻结快照保护前缀缓存（改动落在历史中间会让后面的缓存全部失效）。
    """
    prompt = (
        SYSTEM_PROMPT
        + f"\n环境信息：你的工作区（当前目录）绝对路径是 {tools.WORKSPACE}；"
          "所有相对路径都基于它，不要再创建与工作区同名的子目录。\n"
    )
    agents_md = tools.WORKSPACE / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text(encoding="utf-8", errors="replace")
        if len(content) > AGENTS_MD_MAX_CHARS:
            content = content[:AGENTS_MD_MAX_CHARS] + "\n...（AGENTS.md 超长已截断）"
        prompt += f"\n项目自定义指令（AGENTS.md）：\n{content}\n"
    return prompt


def _poll_steer() -> str | None:
    """文件式转向门（steering 的最小实现）：运行中向工作区写入 .ecall-steer，
    主循环在步边界（安全点）消费它——异步投递、同步消费，消息不插进工具执行中途。"""
    f = tools.WORKSPACE / ".ecall-steer"
    try:
        msg = f.read_text(encoding="utf-8").strip()
        f.unlink()  # 一次性消息：消费即焚
        return msg or None
    except FileNotFoundError:
        return None


def _msg_to_dict(message) -> dict:
    """把 SDK 的 message 对象转成普通 dict——历史全用 dict，context.py 才能统一处理。"""
    d = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        d["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]
    return d


def _default_log_path() -> str:
    """轨迹日志放 ~/.ecall/logs/，绝不写进工作区（防自我污染）。
    评测器可用 ECALL_LOG 环境变量指定落点。"""
    override = os.environ.get("ECALL_LOG")
    if override:
        Path(override).parent.mkdir(parents=True, exist_ok=True)
        return override
    log_dir = Path.home() / ".ecall" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return str(log_dir / f"{tools.WORKSPACE.name}-{stamp}.jsonl")


def run(task: str, log_path: str | None = None):
    """执行一个新任务，返回 (最终回复, 总 token 数, 轨迹文件路径)。"""
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": task},
    ]
    header = {"type": "task", "content": task, "workspace": str(tools.WORKSPACE)}
    answer, tokens, path = run_messages(messages, log_path, header=header)
    return answer, tokens + _SUBAGENT_TOKENS, path  # 账单含子代理的花费


def _explore_one(task: str, tag: str = "") -> tuple[str, int]:
    """单个只读子代理的完整生命周期，返回 (结论, tokens)——
    explore 与 explore_batch 共用，token 汇总由调用方在主线程完成（线程安全）。"""
    schemas = [s for s in tools.SCHEMAS if s["function"]["name"] in EXPLORE_TOOLS]
    if not schemas:
        return "error: 当前模式（mini）没有只读工具，无法派出子代理", 0
    print(f"  >>> 子代理{tag}出动：{task[:60]}")
    messages = [
        {"role": "system", "content":
            EXPLORE_SYSTEM_PROMPT + f"\n工作区绝对路径：{tools.WORKSPACE}\n"},
        {"role": "user", "content": task},
    ]
    answer, tokens, _ = run_messages(
        messages, log_path=_CURRENT_LOG,
        header={"type": "subagent", "content": task},
        schemas=schemas, allowed_tools=EXPLORE_TOOLS,
        max_steps=EXPLORE_MAX_STEPS, _sub=True)
    print(f"  <<< 子代理{tag}归来（{tokens} tokens）")
    return answer, tokens


def explore(task: str) -> str:
    """explore 工具的实现（tools.py 惰性导入这里，绕开循环 import）。

    复用 run_messages 主循环，但换成只读系统提示词 + 只读工具白名单 +
    更小的步数预算；事件记进父代理的同一份轨迹（带 sub 标记）。
    """
    global _SUBAGENT_TOKENS
    answer, tokens = _explore_one(task)
    _SUBAGENT_TOKENS += tokens
    return f"[子代理结论，本次探索消耗 {tokens} tokens]\n{answer}"


EXPLORE_BATCH_MAX = 4  # 并行扇出上限：更多并发省不了墙钟时间，只会让日志交织成灾难


def explore_batch(tasks: list) -> str:
    """并行派出多个只读子代理，各查一个问题，结论汇总返回。

    为什么并行只给只读代理：写操作并行 = 文件冲突地狱，
    而只读代理之间没有共享可变状态——这和内核里读者不用加锁、
    写者必须串行是同一个道理（读者-写者问题的对称性）。

    线程安全的三笔账：
    - 子代理之间零共享：各自的 messages、各自的白名单执行
    - token 账单在主线程汇总（future 返回值），不用给全局计数器加锁
    - 轨迹并发写：jsonl 单行 append，CPython 单次 write 有 GIL 兜底，
      行不会撕裂；行序可能交织，但每条事件自带 step/sub 标记，可后验还原
    """
    global _SUBAGENT_TOKENS
    if not isinstance(tasks, list) or not tasks:
        return "error: tasks 必须是非空数组"
    tasks = [str(t) for t in tasks[:EXPLORE_BATCH_MAX]]
    if len(tasks) == 1:
        return explore(tasks[0])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        results = list(pool.map(
            lambda it: _explore_one(it[1], tag=f"#{it[0] + 1}"),
            enumerate(tasks)))
    total = sum(t for _, t in results)
    _SUBAGENT_TOKENS += total
    out = [f"[并行探索：{len(tasks)} 个子代理，共消耗 {total} tokens]"]
    for i, ((answer, t), task) in enumerate(zip(results, tasks)):
        out.append(f"── 子代理#{i + 1}（{t} tokens）{task[:40]} ──\n{answer}")
    return "\n\n".join(out)


def run_messages(messages: list[dict], log_path: str | None = None,
                 header: dict | None = None, schemas: list | None = None,
                 allowed_tools: tuple | None = None,
                 max_steps: int | None = None, _sub: bool = False):
    """从一段已有历史开始/继续跑主循环（fork 时间线、explore 子代理也走这里）。

    schemas/allowed_tools/max_steps 是子代理的权限收缩接口：父代理用默认值，
    子代理换成只读工具面 + 更小的步数预算。_sub=True 时事件带 sub 标记。
    """
    global _CURRENT_LOG
    if log_path is None:
        log_path = _default_log_path()
    _CURRENT_LOG = log_path
    if schemas is None:
        schemas = tools.SCHEMAS
    if max_steps is None:
        max_steps = MAX_STEPS
    if not _sub:
        _TOTAL_SPENT[0] = 0  # 每次顶层任务重置；子代理不重置（共享父任务的预算池）
    total_tokens = 0
    consecutive_errors = 0
    warned = False
    call_counts = Counter()  # 振荡检测：统计每个 (工具, 参数) 组合的出现次数

    # —— digest 强制政策（control policy 最高档）——
    # 实验发现：schema 提供（0 采用）→ 提示词原则（0 采用），模型对可选的
    # 自我压缩工具一概无视。于是升级为 runtime 强制：大观察落地后挂起标记，
    # 标记未清除前拒绝执行其他工具——和内核的强制访问控制一个思路：
    # 不指望进程自觉，把纪律焊死在 ABI 里。
    # 仅在 digest 工具可用时启用（nodigest 消融组/mini/只读子代理自动豁免，
    # 否则没有 digest 工具还被强制 = 死锁）。
    digest_available = any(s["function"]["name"] == "digest" for s in schemas)
    pending_digest = [False]  # 列表当单元格用：闭包里要能改它

    with open(log_path, "a", encoding="utf-8") as log:
        def record(event: dict):
            if _sub:
                event["sub"] = True  # 子代理事件：时间旅行重建与评测统计时跳过
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
            log.flush()  # 崩了也不丢最后一条

        if header:
            record(header)

        for step in range(1, max_steps + 1):
            # —— 内存管理：逼近上下文预算就压缩老的工具输出 ——
            messages, events = context.maybe_compress(messages)
            if events:
                record({"type": "compress", "step": step, "events": events,
                        "est_tokens": context.estimate_tokens(messages)})

            # 转向门：步边界是安全点，检查有没有运行中投递的用户指令
            steer = _poll_steer()
            if steer:
                print(f"[step {step}] 收到转向指令：{steer[:50]}")
                messages.append({"role": "user",
                                 "content": f"[runtime] 用户转向指令：{steer}"})
                record({"type": "steer", "step": step, "content": steer})

            # 预算预警：让模型体面收尾，而不是被硬切断
            if not warned and max_steps - step <= WARN_REMAINING:
                messages.append({"role": "user", "content":
                    "[runtime] 步数预算即将耗尽，请停止探索，立即总结进展并给出最终回复。"})
                warned = True

            # —— 思考：把全部历史发给模型。模型无记忆，历史即状态 ——
            # 流式透传：仅交互终端 + 非子代理时把内容增量实时打印出来
            # （评测走管道自动静默；子代理的思考不刷父代理的屏）
            streamed = [False]
            if sys.stdout.isatty() and not _sub:
                def on_token(s: str, _f=streamed):
                    _f[0] = True
                    print(s, end="", flush=True)
            else:
                on_token = None
            try:
                message, usage = llm.chat(messages, schemas, on_token=on_token)
            except llm.ContextOverflow:
                # 缺页处理：窗口炸了 → 强制压缩 → 重试一次
                messages, events = context.compress(messages)
                record({"type": "overflow_compress", "step": step, "events": events})
                try:
                    message, usage = llm.chat(messages, schemas, on_token=on_token)
                except Exception as e:
                    record({"type": "fatal", "step": step, "error": str(e)})
                    return f"模型调用失败（压缩后仍失败）：{e}", total_tokens, log_path
            except llm.FatalConfigError as e:
                record({"type": "fatal", "step": step, "error": str(e)})
                return f"模型配置错误（请检查 key 与模型名）：{e}", total_tokens, log_path
            except Exception as e:
                record({"type": "fatal", "step": step, "error": str(e)})
                return f"模型调用失败：{e}", total_tokens, log_path
            if streamed[0]:
                print()  # 流式输出收尾换行，再接后面的 [step N] 行

            total_tokens += usage.total_tokens if usage else 0
            _TOTAL_SPENT[0] += usage.total_tokens if usage else 0  # 实时入账
            record({
                "type": "llm", "step": step, "content": message.content,
                "tool_calls": [tc.model_dump() for tc in (message.tool_calls or [])],
                "usage": usage.model_dump() if usage else None,
                "est_context": context.estimate_tokens(messages),
            })

            # —— 终止条件④：token 总预算耗尽——全局实时熔断 ——
            # 父代理、子代理的每个循环、每一步都查这同一个计数器：
            # 子代理跑到一半就能把整棵树拉闸，而不是回家才交账单
            if _TOTAL_SPENT[0] > MAX_TOTAL_TOKENS:
                record({"type": "abort", "reason": "token_budget",
                        "total_tokens": total_tokens,
                        "subagent_tokens": _SUBAGENT_TOKENS})
                return f"超出 token 预算（{total_tokens}），任务中止。", total_tokens, log_path

            # —— 终止条件①：模型不再调用工具 = 它认为做完了 ——
            if not message.tool_calls:
                # 最终回复也要进历史：多轮会话里下一轮需要看到「我上一轮说了什么」
                # （轨迹里的 llm 事件已在上面记录，重建时自然带上这条）
                messages.append(_msg_to_dict(message))
                record({"type": "done", "step": step, "total_tokens": total_tokens,
                        "subagent_tokens": _SUBAGENT_TOKENS})
                return message.content, total_tokens, log_path

            # —— 历史只 append、不改写（前缀缓存铁律；压缩是唯一例外，见 context.py）——
            messages.append(_msg_to_dict(message))

            # —— 行动 + 观察：本地执行工具，结果喂回模型 ——
            for tc in message.tool_calls:
                sig = (tc.function.name, tc.function.arguments)
                call_counts[sig] += 1

                if (pending_digest[0] and digest_available
                        and tc.function.name != "digest"):
                    # 强制笔记政策执行中：大观察未消化，拒绝继续行动
                    result = ("error: [runtime] 上一条工具输出超过阈值且尚未 digest。"
                              "强制笔记政策：先调用 digest 记录要点，再继续其他操作。")
                    record({"type": "tool", "step": step, "name": sig[0],
                            "arguments": sig[1], "result": result})
                elif call_counts[sig] >= REPEAT_CALL_LIMIT:
                    # 振荡检测：同一调用原地打转，不再执行，把警告装进工具结果喂回去
                    # （每个 tool_call 都必须有对应的 tool 响应，否则 API 报错）。
                    result = (f"error: [runtime] 振荡检测：完全相同的调用已重复 "
                              f"{REPEAT_CALL_LIMIT} 次，本次未执行。请分析根因、换个思路。")
                    record({"type": "oscillation", "step": step, "name": sig[0]})
                else:
                    # WAL：变更型工具动手前，先把旧内容记进轨迹（供 rewind 倒放）
                    if tc.function.name in MUTATING_TOOLS:
                        try:
                            args = json.loads(tc.function.arguments)
                            record({"type": "checkpoint", "step": step,
                                    "path": args["path"],
                                    "old_content": tools.peek_file(args["path"])})
                        except Exception:
                            pass  # 快照失败不阻塞主流程
                    print(f"[step {step}] {tc.function.name}({tc.function.arguments[:60]}...)")
                    result = tools.execute(tc.function.name, tc.function.arguments,
                                           allowed_tools)
                    # digest 关联：把笔记贴到最近一条工具观察上——
                    # compress 换出它时，这条笔记将成为占位符（见 context.py）。
                    # 只改消息对象的私有标注，不进轨迹、不改 role 内容，
                    # 对前缀缓存零影响
                    if tc.function.name == "digest" and not result.startswith("error"):
                        try:
                            note = json.loads(tc.function.arguments)["summary"]
                            note = note.strip()[:tools.DIGEST_MAX_CHARS]
                            for m in reversed(messages):
                                if m.get("role") == "tool":
                                    m["_digest"] = note
                                    break
                        except Exception:
                            pass  # 笔记关联失败不阻塞主流程
                    # digest 挂起/解除：观察类工具的大输出挂起标记并现场通知；
                    # digest 成功调用解除标记。一次 digest 清一次账。
                    if tc.function.name == "digest" and not result.startswith("error"):
                        pending_digest[0] = False
                    elif (digest_available
                          and tc.function.name in OBSERVATION_TOOLS
                          and len(result) > DIGEST_ENFORCE_MIN_CHARS):
                        pending_digest[0] = True
                        result += (f"\n\n[runtime] 该输出 {len(result)} 字符，超过阈值"
                                   f"（{DIGEST_ENFORCE_MIN_CHARS}）。强制笔记政策："
                                   f"请立即调用 digest 记录要点，否则后续工具将被拒绝。")
                    # todo 回显：模型写给自己的外部记忆，钉在上下文最新的位置——
                    # 追加在尾部，不改写历史，前缀缓存不受影响
                    if tools.TODO:
                        result += "\n\n[todo]\n" + tools.render_todo()
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
        return f"超出步数预算（{max_steps}），任务中止。", total_tokens, log_path
