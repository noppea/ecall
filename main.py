"""main.py — CLI 入口。

用法:
  python main.py "任务"                              执行任务（单轮）
  python main.py chat                                多轮会话模式（历史贯穿整个会话）
  python main.py replay <轨迹文件>                   回放时间线
  python main.py rewind <轨迹文件> <步数>            把文件回滚到该步之前（在原工作区里运行）
  python main.py fork <轨迹文件> <步数> ["提示"]     从该步分叉出新时间线
"""
import sys

import agent
import timetravel


def chat_loop() -> None:
    """多轮会话：同一个 messages 列表反复喂回主循环，历史跨轮次延续。

    模型无记忆，「记得你上轮回合说过什么」靠的就是这份越来越长的历史——
    所以多轮会话天然更吃上下文，压缩机制在这里真正派上用场。
    轨迹整个会话记同一份：每轮写一条 type=task 的 header，回放时按轮分段。
    """
    messages = [{"role": "system", "content": agent.build_system_prompt()}]
    log_path = agent._default_log_path()
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
