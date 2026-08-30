#!/usr/bin/env python3
"""report.py — results.csv → 单文件 HTML 评测报告（零依赖，内联 SVG，双击即开）。

    python3 evals/report.py            # 生成 evals/report.html

为什么手写 SVG 而不引图表库：报告要能脱离环境打开（发给别人、附在材料里），
任何外部 CDN 依赖都是"打开时机不对就白屏"的隐患。
"""
import csv
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
CSV = HERE / "results.csv"
OUT = HERE / "report.html"

# 任务集 → 规模标签（用于分组展示；未列入的 id 归种子集）
HARD_TASKS = {"extract-config", "fix-test-suite", "add-middleware", "log-archaeology"}

PALETTE = {"full": "#4c7cf3", "mini": "#f0954c"}


def task_set(task_id: str) -> str:
    return "进阶集" if task_id in HARD_TASKS else "种子集"


def bar_chart(title: str, rows: list[tuple[str, float, str]], ymax: float,
              fmt=str) -> str:
    """rows = [(label, value, color)]，画一组带数值标注的柱子。"""
    w, h, pad_b, pad_t = 340, 200, 46, 30
    n = len(rows)
    bw = min(46, (w - 60) // max(n, 1) - 14)
    gap = (w - 60) / max(n, 1)
    parts = [f'<text x="10" y="18" class="ct">{title}</text>']
    for i, (label, v, color) in enumerate(rows):
        x = 30 + i * gap + (gap - bw) / 2
        bh = 0 if ymax == 0 else (h - pad_b - pad_t) * v / ymax
        y = h - pad_b - bh
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw}" height="{bh:.0f}" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{x + bw / 2:.0f}" y="{y - 5:.0f}" class="v">{fmt(v)}</text>')
        parts.append(f'<text x="{x + bw / 2:.0f}" y="{h - pad_b + 15:.0f}" class="l">{label}</text>')
    base = h - pad_b
    parts.append(f'<line x1="20" y1="{base}" x2="{w - 10}" y2="{base}" class="axis"/>')
    return f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}">{"".join(parts)}</svg>'


def main() -> None:
    rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
    if not rows:
        raise SystemExit("results.csv 是空的，先跑评测")

    # 按 (任务集, 模型, 配置) 分桶
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(task_set(r["task"]), r.get("model") or "?", r["config"])].append(r)

    # 汇总表
    lines = ["<table><tr><th>任务集</th><th>模型</th><th>配置</th><th>pass 率</th>"
             "<th>token 中位数</th><th>步数中位数</th><th>样本</th></tr>"]
    charts_pass, charts_tok = [], []
    for (tset, model, config), rs in sorted(groups.items()):
        n = len(rs)
        passed = sum(int(r["passed"]) for r in rs)
        toks = [int(r["tokens"]) for r in rs if int(r["tokens"]) > 0]
        steps = [int(r["steps"]) for r in rs]
        med_tok = statistics.median(toks) if toks else 0
        med_step = statistics.median(steps) if steps else 0
        lines.append(
            f"<tr><td>{tset}</td><td>{model}</td><td>{config}</td>"
            f"<td>{passed}/{n} = {passed / n:.0%}</td>"
            f"<td>{med_tok:.0f}</td><td>{med_step:.0f}</td><td>{n}</td></tr>")
        tag = f"{tset[:2]}·{model[:5]}·{config}"
        charts_pass.append((tag, passed / n, PALETTE.get(config, "#999")))
        charts_tok.append((tag, med_tok, PALETTE.get(config, "#999")))
    lines.append("</table>")

    html = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>ecall 评测报告</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ font-size: 1.5em; }} h2 {{ font-size: 1.1em; margin-top: 2em; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 14px; }}
th {{ background: #f5f5f5; }}
svg {{ margin: 8px; background: #fafafa; border-radius: 8px; }}
.ct {{ font-size: 13px; font-weight: 600; }}
.v {{ font-size: 11px; text-anchor: middle; }}
.l {{ font-size: 10px; text-anchor: middle; }}
.axis {{ stroke: #999; stroke-width: 1; }}
.charts {{ display: flex; flex-wrap: wrap; }}
.meta {{ color: #888; font-size: 12px; }}
</style></head><body>
<h1>ecall 评测报告</h1>
<p class="meta">数据源：results.csv（{len(rows)} 条运行记录）· 判决：check 命令退出码 · 统计：中位数</p>
{''.join(lines)}
<h2>pass 率</h2>
<div class="charts">{bar_chart('各组 pass 率', charts_pass, 1.0, lambda v: f'{v:.0%}')}</div>
<h2>token 中位数</h2>
<div class="charts">{bar_chart('各组 token 中位数', charts_tok, max((v for _, v, _ in charts_tok), default=1) * 1.15, lambda v: f'{v:.0f}')}</div>
</body></html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"报告已生成：{OUT}（浏览器直接打开）")


if __name__ == "__main__":
    main()
