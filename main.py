"""main.py — CLI 入口。

用法: python main.py "任务"
"""
import sys

import agent


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    answer, tokens = agent.run(sys.argv[1])
    print("\n=== 最终回复 ===")
    print(answer)
    print(f"\n=== 消耗 token：{tokens}（完整轨迹见 trajectory.jsonl）===")


if __name__ == "__main__":
    main()
