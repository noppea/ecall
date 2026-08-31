"""crash_resume.py — 崩溃-恢复对照实验。

协议：任务在临时工作区开跑，轨迹 step 达到 --kill-at 时 SIGKILL 强杀；
随后 chat --resume 从轨迹重建现场，注入"继续"指令接着跑，最后跑 check 判决。

对照逻辑：
- full 组：digest 笔记随重建挂回对应的大输出（timetravel reattach），
  压缩后的世界里占位符是语义笔记；
- nodigest 组：占位符只有首行 80 字。
指标：check 过不过、恢复后重读了几个文件（笔记不够用的直接证据）、总 token。

用法：python3 evals/crash_resume.py -t evals/tasks_quiz.json -c full -m deepseek -r 1
结果落 evals/crash_results.csv（与主榜分开，避免污染）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ECALL_DIR = Path(__file__).resolve().parent.parent
RESULTS_CSV = Path(__file__).parent / "crash_results.csv"

# 实验协议锁死的水位线：4k，两组相同，保证 fixture（约 6k token）必触发多次压缩
PROTOCOL_CONTEXT = "4000"

RESUME_HINT = ("刚才你被系统强行 kill 了。请继续完成最开始交给你的原任务。"
               "先回想自己已经做到哪一步，不要从头重来，然后接着做完。")


def _max_step(log_file: Path) -> int:
    """轨迹里主代理（非 sub）到达的最大 llm 步数。"""
    if not log_file.exists():
        return 0
    step = 0
    for line in log_file.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # 强杀瞬间可能写坏最后一行，跳过
        if e.get("sub"):
            continue
        if e.get("type") == "llm":
            step = max(step, e.get("step") or 0)
    return step


def _log_stats(log_file: Path) -> dict:
    """从轨迹挖指标：重读次数、压缩次数、digest 次数、终止方式、token。"""
    reads, digests, compressions = 0, 0, 0
    fate, tokens = "killed-midway", 0
    for line in log_file.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("sub"):
            continue
        if e.get("type") == "tool" and e.get("name") == "read_file":
            reads += 1
        elif e.get("type") == "tool" and e.get("name") == "digest":
            digests += 1
        elif e.get("type") in ("compress", "overflow_compress"):
            compressions += 1
        elif e.get("type") == "done":
            fate, tokens = "done", (e.get("total_tokens") or 0) + (e.get("subagent_tokens") or 0)
        elif e.get("type") in ("abort", "fatal"):
            fate = e.get("type")
            tokens = (e.get("total_tokens") or tokens or 0) + (e.get("subagent_tokens") or 0)
    return {"reads": reads, "digests": digests, "compressions": compressions,
            "fate": fate, "tokens": tokens}


def run_crash(task: dict, config: str, model: str, rep: int, kill_at: int) -> dict:
    log_file = Path(tempfile.mkdtemp(prefix="ecall-crash-log-")) / "traj.jsonl"
    with tempfile.TemporaryDirectory(prefix=f"ecall-crash-{task['id']}-") as ws:
        for name, content in (task.get("files") or {}).items():
            p = Path(ws) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if task.get("copy_dir"):
            src = (Path(__file__).parent / task["copy_dir"]).resolve()
            shutil.copytree(src, Path(ws) / src.name)

        env = dict(os.environ,
                   ECALL_LOG=str(log_file),
                   ECALL_MAX_CONTEXT=PROTOCOL_CONTEXT)
        if config == "nodigest":
            env["ECALL_NO_DIGEST"] = "1"

        # ---- 阶段一：跑到 kill_at 步，然后 SIGKILL ----
        t0 = time.time()
        proc = subprocess.Popen(
            [sys.executable, str(ECALL_DIR / "main.py"), task["prompt"]],
            cwd=ws, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = t0 + 600
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # 提前跑完或自杀了（任务太小撑不到 kill_at）
            if _max_step(log_file) >= kill_at:
                time.sleep(0.3)  # 让当前步的工具结果落盘，保证轨迹完整可重建
                proc.kill()
                break
            time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
        proc.wait()

        # ---- 阶段二：chat --resume 重建现场，注入"继续" ----
        resume = subprocess.run(
            [sys.executable, str(ECALL_DIR / "main.py"), "chat", "--resume"],
            cwd=ws, env=env, input=RESUME_HINT + "\n",
            capture_output=True, text=True, timeout=900)
        wall = time.time() - t0

        check = subprocess.run(task["check"], shell=True, cwd=ws,
                               capture_output=True, text=True, timeout=120)
        passed = check.returncode == 0
        stats = _log_stats(log_file)
        stats["reads_after_kill"] = stats["reads"]  # reads 含两阶段，重读数看绝对值即可
        return {"ts": int(t0), "task": task["id"], "config": config, "model": model,
                "rep": rep, "passed": int(passed), "wall_s": round(wall, 1),
                **{k: stats[k] for k in ("fate", "tokens", "reads", "digests", "compressions")},
                "log": log_file.name,
                "check_tail": "" if passed else (check.stderr or "").strip().splitlines()[-1][:200]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="full", choices=["full", "nodigest"])
    ap.add_argument("-t", "--tasks", default=str(Path(__file__).parent / "tasks_quiz.json"))
    ap.add_argument("-r", "--repeat", type=int, default=1)
    ap.add_argument("-m", "--model", default="deepseek")
    ap.add_argument("--kill-at", type=int, default=12, help="到达该步数时 SIGKILL")
    args = ap.parse_args()

    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    new = not RESULTS_CSV.exists()
    with RESULTS_CSV.open("a", encoding="utf-8") as f:
        if new:
            f.write("ts,task,config,model,rep,passed,fate,tokens,reads,digests,"
                    "compressions,wall_s,log,check_tail\n")
        for task in tasks:
            for rep in range(1, args.repeat + 1):
                try:
                    row = run_crash(task, args.config, args.model, rep, args.kill_at)
                except Exception as exc:  # 单发失败不拖垮整批
                    print(f"[{args.config}|{args.model}] {task['id']} #{rep}: ERROR {exc}")
                    continue
                verdict = "PASS" if row["passed"] else f"FAIL ({row['check_tail']})"
                print(f"[{args.config}|{args.model}] {task['id']} #{rep}: {verdict} "
                      f"({row['fate']}, {row['tokens']} tok, 读文件 {row['reads']} 次, "
                      f"digest {row['digests']} 次, 压缩 {row['compressions']} 次, {row['wall_s']}s)")
                f.write(",".join(str(row[k]) for k in
                                 ("ts", "task", "config", "model", "rep", "passed", "fate",
                                  "tokens", "reads", "digests", "compressions", "wall_s",
                                  "log", "check_tail")) + "\n")
                f.flush()


if __name__ == "__main__":
    main()
