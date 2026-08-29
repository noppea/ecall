"""evals/run_eval.py — 评测器：同一批任务 × 不同配置 × 重复 N 次，出 CSV。

方法论和跑内核 benchmark 一模一样：隔离环境（每个任务一个临时目录）、
固定输入（tasks.json）、重复取统计（-r N）、记录一切（轨迹 + CSV）。

用法:
  python evals/run_eval.py                  # 全装模式，默认重复 3 遍
  python evals/run_eval.py -c mini          # mini 对照组（只留 run_shell）
  ECALL_MODEL=deepseek-chat python evals/run_eval.py   # 换模型对比
"""
import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ECALL_DIR = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(__file__).parent / "tasks.json"
LOG_DIR = Path(__file__).parent / "logs"
RESULTS_CSV = Path(__file__).parent / "results.csv"

COLUMNS = ["ts", "task", "config", "rep", "passed", "steps", "tokens", "wall_s", "log"]


def run_one(task: dict, config: str, rep: int) -> dict:
    """在一次性临时目录里跑一个任务，再用 check 命令判定通过与否（exit 0 = PASS）。

    临时目录同时也是对 _jail 的实战检验：agent 只能在里面折腾。
    """
    log_file = LOG_DIR / f"{task['id']}-{config}-r{rep}.jsonl"
    with tempfile.TemporaryDirectory(prefix=f"ecall-eval-{task['id']}-") as ws:
        # 摆 fixture：把任务自带的初始文件写进临时工作区
        for name, content in (task.get("files") or {}).items():
            p = Path(ws) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        env = dict(os.environ, ECALL_LOG=str(log_file))
        if config == "mini":
            env["ECALL_MINI"] = "1"

        t0 = time.time()
        subprocess.run(
            [sys.executable, str(ECALL_DIR / "main.py"), task["prompt"]],
            cwd=ws, env=env, capture_output=True, text=True, timeout=900,
        )
        wall = time.time() - t0

        # 验收：check 命令的退出码就是判决（和 CI 一个思想）
        check = subprocess.run(task["check"], shell=True, cwd=ws,
                               capture_output=True, text=True, timeout=120)
        passed = check.returncode == 0

        # 从轨迹里挖 steps / tokens（轨迹是唯一事实来源）
        steps, tokens = 0, 0
        if log_file.exists():
            for line in log_file.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                steps = max(steps, e.get("step") or 0)
                if e["type"] in ("done", "abort"):
                    tokens = e.get("total_tokens") or tokens

        return {"ts": int(t0), "task": task["id"], "config": config, "rep": rep,
                "passed": int(passed), "steps": steps, "tokens": tokens,
                "wall_s": round(wall, 1), "log": log_file.name}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="full", choices=["full", "mini"])
    ap.add_argument("-r", "--repeat", type=int, default=3)
    args = ap.parse_args()

    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    new_rows = []
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if f.tell() == 0:
            writer.writeheader()
        for task in tasks:
            for rep in range(1, args.repeat + 1):
                row = run_one(task, args.config, rep)
                writer.writerow(row)
                f.flush()  # 逐行落盘，跑挂了也不丢已完成的结果
                new_rows.append(row)
                print(f"[{args.config}] {task['id']} #{rep}: "
                      f"{'PASS' if row['passed'] else 'FAIL'} "
                      f"({row['steps']} 步, {row['tokens']} token, {row['wall_s']}s)")

    n = len(new_rows)
    passes = sum(r["passed"] for r in new_rows)
    print(f"\n== {args.config} 汇总 ==")
    print(f"pass 率: {passes}/{n} = {passes / n:.0%}")
    print(f"token 中位数: {statistics.median(r['tokens'] for r in new_rows):.0f}")
    print(f"步数中位数: {statistics.median(r['steps'] for r in new_rows):.0f}")
    print(f"明细见 {RESULTS_CSV}；full 和 mini 各跑一遍，CSV 里直接对比")


if __name__ == "__main__":
    main()