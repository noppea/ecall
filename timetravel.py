"""timetravel.py — 时间旅行：轨迹回放、文件回滚、会话分叉。

核心思想：trajectory 不只是日志，而是 WAL（写前日志）。
主循环在每次变更型工具动手前，把文件的旧内容记成 checkpoint 事件；
于是「时间旅行」就是三种放法：replay 正着看，rewind 倒着放，fork 从中间开分支。
"""
import json
import sys
from pathlib import Path

import agent
import tools


def load_events(log_path: str) -> list[dict]:
    events = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events


def _check_workspace(events: list[dict]) -> None:
    """rewind 必须在原工作区里执行（文件路径都是相对它记的）。"""
    ws = next((e.get("workspace") for e in events if e["type"] == "task"), None)
    if ws and Path(ws) != tools.WORKSPACE:
        print(f"错误：这条轨迹的工作区是 {ws}\n当前目录是 {tools.WORKSPACE}，请先 cd 过去。")
        sys.exit(1)


def print_replay(log_path: str) -> None:
    """把轨迹打印成可读的时间线（视频素材也靠它）。"""
    for e in load_events(log_path):
        t = e["type"]
        if t == "subagent":
            print(f"=== 子代理出动: {e['content'][:80]}")
            continue
        if e.get("sub"):
            if t == "done":
                print(f"    (子代理收尾：{e.get('total_tokens')} tokens)")
            continue  # 子代理的过程事件不刷屏，结论在父代理的 explore 结果里
        if t == "task":
            print(f"=== 任务: {e['content']}\n    工作区: {e.get('workspace')}")
        elif t == "fork":
            print(f"=== 分叉自 {e['from']} 第 {e['at_step']} 步")
        elif t == "llm":
            step = e.get("step")
            if e.get("content"):
                print(f"[step {step}] 模型说: {e['content'][:200]}")
            for tc in e.get("tool_calls") or []:
                print(f"[step {step}] 调用 {tc['function']['name']}"
                      f"({tc['function']['arguments'][:80]})")
        elif t == "tool":
            first = e["result"].splitlines()[0] if e["result"] else ""
            print(f"         -> {e['name']}: {first[:120]}")
        elif t in ("compress", "overflow_compress"):
            print(f"  [事件] 压缩了 {len(e.get('events') or [])} 条旧工具输出")
        elif t == "oscillation":
            print(f"  [事件] 振荡警告: {e['name']}")
        elif t == "steer":
            print(f"  [转向] 运行中收到用户指令: {e['content'][:80]}")
        elif t == "checkpoint":
            print(f"  [快照] step {e['step']} 备份了 {e['path']} 的旧版本")
        elif t == "done":
            print(f"=== 完成（step {e.get('step')}，总 token {e.get('total_tokens')}）")
        elif t in ("abort", "fatal"):
            print(f"=== 中止/失败: {e}")


def rewind(log_path: str, to_step: int) -> None:
    """把 to_step 之后被改过的文件恢复到「改前」状态。

    倒序回放 WAL：同一文件被改多次时，先恢复晚的快照、再恢复早的，
    最终留下的就是最早那次修改前的内容（顺序反了就错了）。
    """
    events = load_events(log_path)
    _check_workspace(events)
    checkpoints = [e for e in events
                   if e["type"] == "checkpoint" and e["step"] > to_step]
    if not checkpoints:
        print("没有需要回滚的修改。")
        return
    for e in reversed(checkpoints):
        p = tools.WORKSPACE / e["path"]
        if e["old_content"] is None:
            p.unlink(missing_ok=True)
            print(f"删除 {e['path']}（它当时是新建的文件）")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(e["old_content"], encoding="utf-8")
            print(f"恢复 {e['path']}（回到 step {e['step']} 改前的版本）")


def rebuild_messages(log_path: str, to_step: int) -> list[dict]:
    """从轨迹事件重建 to_step 之前（含）的完整对话历史。

    tool 事件没有记录 tool_call_id，按顺序和上面 llm 事件的 tool_calls 配对。
    """
    events = load_events(log_path)
    messages = [{"role": "system", "content": agent.build_system_prompt()}]
    pending_calls: list[dict] = []  # 上一条 assistant 消息发出的、还没配对结果的调用
    for e in events:
        if e.get("sub"):
            continue  # 子代理事件不属于父历史：父上下文里只有 explore 的结论
        if e["type"] == "llm" and (e.get("step") or 0) > to_step:
            break  # 越过分叉点：之后的事件（含后续轮次的 task header）都不属于这条时间线
        if e["type"] == "task":
            # 多轮会话里每轮一条 task header = 一条用户消息；
            # 单轮轨迹只有一条，语义不变
            messages.append({"role": "user", "content": e["content"]})
        elif e["type"] == "llm":
            msg = {"role": "assistant", "content": e.get("content") or ""}
            pending_calls = e.get("tool_calls") or []
            if pending_calls:
                # 必须拷贝：下面配对时 pop(0) 会原地修改这个列表，
                # 不拷贝的话消息里的 tool_calls 会被一起掏空（别名陷阱）
                msg["tool_calls"] = list(pending_calls)
            messages.append(msg)
        elif e["type"] == "tool" and pending_calls:
            tc = pending_calls.pop(0)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": e["result"]})
            if tc["function"]["name"] == "digest":
                # digest 笔记不落轨迹本体，但调用事件里有 summary：
                # 重建时把它挂回前一条大输出，否则 resume/fork 后压缩退回首行规则，
                # 笔记在崩溃点全部蒸发（轨迹一等公民原则的漏洞）
                try:
                    note = json.loads(tc["function"]["arguments"]).get("summary", "")
                except (json.JSONDecodeError, TypeError):
                    note = ""
                for m in reversed(messages[:-1]):
                    if m.get("role") == "tool":
                        if note:
                            m["_digest"] = note
                        break
        elif e["type"] == "steer" and (e.get("step") or 0) <= to_step:
            # 转向指令当时被注入过历史，重建时也要回到原来的位置
            messages.append({"role": "user",
                             "content": f"[runtime] 用户转向指令：{e['content']}"})
    return messages


def fork(log_path: str, to_step: int, hint: str | None = None) -> None:
    """从第 to_step 步分叉出新时间线：重建历史，带着新提示继续跑。

    原轨迹只读不动——就像 git branch，旧分支永远都在。
    彩蛋：重建时用当前的系统提示词，所以改了提示词再 fork，就是 prompt A/B 实验。
    """
    messages = rebuild_messages(log_path, to_step)
    if hint:
        messages.append({"role": "user",
                         "content": f"[runtime] 来自时间旅行者的提示：{hint}"})
    print(f"已从第 {to_step} 步分叉（重建了 {len(messages)} 条历史消息）")
    answer, tokens, new_log = agent.run_messages(
        messages, header={"type": "fork", "from": log_path, "at_step": to_step})
    print("\n=== 分叉线的最终回复 ===")
    print(answer)
    print(f"\n=== 分叉线消耗 token：{tokens}（新轨迹见 {new_log}）===")
