"""main.py — CLI 入口。

用法:
  python main.py "任务"                              执行任务
  python main.py replay <轨迹文件>                   回放时间线
  python main.py rewind <轨迹文件> <步数>            把文件回滚到该步之前（在原工作区里运行）
  python main.py fork <轨迹文件> <步数> ["提示"]     从该步分叉出新时间线
"""
import sys

import agent
import timetravel


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "replay":
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
