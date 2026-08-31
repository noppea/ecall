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

COLUMNS = ["ts", "task", "config", "model", "rep", "passed", "steps", "tokens", "wall_s", "log"]


def run_one(task: dict, config: str, model: str, rep: int) -> dict:
    """在一次性临时目录里跑一个任务，再用 check 命令判定通过与否（exit 0 = PASS）。

    临时目录同时也是对 _jail 的实战检验：agent 只能在里面折腾。
    """
    log_file = LOG_DIR / f"{task['id']}-{config}-{model}-r{rep}.jsonl"
    # 轨迹是 append 模式：重跑同 rep 前必须删掉旧日志，
    # 否则新一轮的事件叠在旧尸体后面，步数/token 解析全是脏数据
    log_file.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"ecall-eval-{task['id']}-") as ws:
        # 摆 fixture：把任务自带的初始文件写进临时工作区
        for name, content in (task.get("files") or {}).items():
            p = Path(ws) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        # 大任务 fixture：整个目录树拷进来（比如一个真实内核仓库）
        if task.get("copy_dir"):
            import shutil
            src = (Path(__file__).parent / task["copy_dir"]).resolve()
            shutil.copytree(src, Path(ws) / src.name)

        env = dict(os.environ, ECALL_LOG=str(log_file))
        if config == "mini":
            env["ECALL_MINI"] = "1"
        if config == "nodigest":
            env["ECALL_NO_DIGEST"] = "1"  # digest 消融组：压缩退回纯首行规则

        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(ECALL_DIR / "main.py"), task["prompt"]],
            cwd=ws, env=env, capture_output=True, text=True, timeout=900,
            stdin=subprocess.DEVNULL,  # 非交互：审批门走自动放行路径，绝不阻塞评测
        )
        wall = time.time() - t0

        # 验收：check 命令的退出码就是判决（和 CI 一个思想）
        check = subprocess.run(task["check"], shell=True, cwd=ws,
                               capture_output=True, text=True, timeout=120)
        passed = check.returncode == 0
        if not passed:
            # FAIL 不许静默：把检查器的断言尾巴印出来，省得事后 grep 考古
            err_lines = (check.stderr or "").strip().splitlines() \
                        or (check.stdout or "").strip().splitlines()
            tail = err_lines[-1] if err_lines else "(check 无任何输出)"
            print(f"  [check] {tail[:200]}")

        # 从轨迹里挖 steps / tokens（轨迹是唯一事实来源）
        steps, tokens = 0, 0
        if log_file.exists():
            for line in log_file.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if e.get("sub"):
                    continue  # 子代理事件：步数编号独立，不参与父代理统计
                steps = max(steps, e.get("step") or 0)
                if e["type"] in ("done", "abort"):
                    # 账单 = 父代理花费 + 子代理花费（不许隐身）
                    tokens = ((e.get("total_tokens") or 0)
                              + (e.get("subagent_tokens") or 0)) or tokens

        # 秒退（0 步）说明进程根本没跑起来：把 stderr 尾巴吐出来，别让 FAIL 静默
        if steps == 0:
            tail = "\n".join((proc.stderr or "").splitlines()[-5:]) \
                   or "\n".join((proc.stdout or "").splitlines()[-5:])
            print(f"  [诊断] agent 秒退，输出末尾：\n  {tail}")

        return {"ts": int(t0), "task": task["id"], "config": config, "model": model, "rep": rep,
                "passed": int(passed), "steps": steps, "tokens": tokens,
                "wall_s": round(wall, 1), "log": log_file.name}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="full",
                    choices=["full", "mini", "nodigest"])
    ap.add_argument("-r", "--repeat", type=int, default=3)
    ap.add_argument("-m", "--model", default="deepseek",
                    help="模型标签，只写进 CSV 用于分组统计；实际连哪家由 ECALL_BASE_URL/ECALL_API_KEY 决定")
    ap.add_argument("-t", "--tasks", default=str(TASKS_FILE),
                    help="任务集 JSON，默认 tasks.json；传 tasks_hard.json 跑大任务集")
    args = ap.parse_args()

    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    new_rows = []
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if f.tell() == 0:
            writer.writeheader()
        for task in tasks:
            for rep in range(1, args.repeat + 1):
                row = run_one(task, args.config, args.model, rep)
                writer.writerow(row)
                f.flush()  # 逐行落盘，跑挂了也不丢已完成的结果
                new_rows.append(row)
                print(f"[{args.config}|{args.model}] {task['id']} #{rep}: "
                      f"{'PASS' if row['passed'] else 'FAIL'} "
                      f"({row['steps']} 步, {row['tokens']} token, {row['wall_s']}s)")

    n = len(new_rows)
    passes = sum(r["passed"] for r in new_rows)
    print(f"\n== {args.config}|{args.model} 汇总 ==")
    print(f"pass 率: {passes}/{n} = {passes / n:.0%}")
    print(f"token 中位数: {statistics.median(r['tokens'] for r in new_rows):.0f}")
    print(f"步数中位数: {statistics.median(r['steps'] for r in new_rows):.0f}")
    print(f"明细见 {RESULTS_CSV}；full 和 mini 各跑一遍，CSV 里直接对比")


if __name__ == "__main__":
    main()
