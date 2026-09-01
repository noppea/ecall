"""main.py — CLI 入口。

用法:
  python main.py "任务"                              执行任务（单轮）
  python main.py chat                                多轮会话模式（历史贯穿整个会话）
  python main.py chat --resume [轨迹文件]            恢复会话：给路径从该轨迹恢复，
                                                     不给则自动接该工作区最新的轨迹
  python main.py replay <轨迹文件>                   回放时间线
  python main.py rewind <轨迹文件> <步数>            把文件回滚到该步之前（在原工作区里运行）
  python main.py fork <轨迹文件> <步数> ["提示"]     从该步分叉出新时间线
"""
import glob
import os
import sys
from pathlib import Path

import agent
import timetravel


def _find_resume_log() -> str | None:
    """恢复目标的选取顺序：ECALL_LOG 显式指定 > 该工作区最新的轨迹文件。

    去判断存在性——新时间戳永远不存在，交互式 --resume 永远静默开新会话。
    机制（rebuild）有测试罩着，入口（找日志）没有——测试覆盖盲区的实证。
    """
    env_log = os.environ.get("ECALL_LOG")
    if env_log:
        return env_log if os.path.exists(env_log) else None
    pattern = str(Path.home() / ".ecall" / "logs"
                  / f"{agent.tools.WORKSPACE.name}-*.jsonl")
    existing = sorted(glob.glob(pattern))  # 时间戳格式 %Y%m%d-%H%M%S，字典序即时间序
    return existing[-1] if existing else None


def chat_loop(resume: str | None = None) -> None:
    """多轮会话：同一个 messages 列表反复喂回主循环，历史跨轮次延续。

    resume：None 开新会话；"latest" 自动接该工作区最新轨迹；其余视为
    轨迹文件路径。
    """
    log_path = agent._default_log_path()
    if resume is not None:
        target = _find_resume_log() if resume == "latest" else resume
        if target and os.path.exists(target):
            log_path = target
            messages = timetravel.rebuild_messages(log_path, to_step=10**9)
            n_user = sum(1 for m in messages if m["role"] == "user")
            print(f"已从轨迹恢复会话：{log_path}")
            print(f"重建了 {len(messages)} 条消息（含 {n_user} 轮任务）")
        else:
            print("没有找到可恢复的历史轨迹，按新会话开始。")
            messages = [{"role": "system", "content": agent.build_system_prompt()}]
    else:
        messages = [{"role": "system", "content": agent.build_system_prompt()}]
    total = 0
    print(f"ecall 会话模式（轨迹记录到 {log_path}）")
    print("输入任务开始，exit 或 Ctrl-D 退出。")
    while True:
        try:
            task = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task or task in ("exit", "quit"):
            break
        messages.append({"role": "user", "content": task})
        header = {"type": "task", "content": task,
                  "workspace": str(agent.tools.WORKSPACE)}
        answer, tokens, log_path = agent.run_messages(messages, log_path, header=header)
        total += tokens
        print(f"\n=== 回复（本轮 {tokens} tokens，会话累计 "
              f"{total + agent._SUBAGENT_TOKENS}）===")
        print(answer)
    print(f"\n会话结束，完整轨迹见 {log_path}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "chat":
        rest = sys.argv[2:]
        if "--resume" in rest:
            i = rest.index("--resume")  # --resume 后可跟轨迹路径，不跟则自动找最新
            target = rest[i + 1] if i + 1 < len(rest) else "latest"
            chat_loop(resume=target)
        else:
            chat_loop()
    elif cmd == "replay":
        timetravel.print_replay(sys.argv[2])
    elif cmd == "rewind":
        timetravel.rewind(sys.argv[2], int(sys.argv[3]))
    elif cmd == "fork":
        hint = sys.argv[4] if len(sys.argv) > 4 else None
        timetravel.fork(sys.argv[2], int(sys.argv[3]), hint)
    else:
        answer, tokens, log_path = agent.run(cmd)
        print("\n=== 最终回复 ===")
        print(answer)
        print(f"\n=== 消耗 token：{tokens}（完整轨迹见 {log_path}）===")


if __name__ == "__main__":
    main()
