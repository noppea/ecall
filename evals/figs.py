#!/usr/bin/env python3
"""figs.py — results.csv / crash_results.csv → 论文风 PNG 图（落盘 evals/figs/）。

    python3 evals/figs.py            # 生成 evals/figs/fig1~4_*.png

report.py 的单文件 HTML 走零依赖路线；
本脚本走 matplotlib 路线，服务视频/汇报场景——图要经得起投影和暂停键。
四张图各对应一条叙事线：
  fig1 水位线扫描（X 形交叉 → 「低压≈免费，高压 3~9 倍」）
  fig2 降税解剖（23.0万 → 14.8万 → 6.8万 的三刀）
  fig3 崩溃恢复（SIGKILL 后续跑的成本差）
  fig4 跨模型（同一 harness，不同模型的成本剖面）
"""
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # 无显示环境（CI/SSH）也能出图
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
OUT = HERE / "figs"

# 与 report.py 同一套配色，叙事一致：full=蓝，nodigest=红橙，mini=灰
C_FULL, C_NODIGEST, C_MINI = "#4c7cf3", "#e05c5c", "#999999"


CJK_FONTS = ("Noto Sans CJK SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
             "PingFang SC", "Microsoft YaHei", "SimHei", "Source Han Sans SC",
             "Noto Sans CJK JP")  # 兜底：JP 也覆盖常用汉字


def ensure_cjk_font():
    """字体栈首位不是中文字体时才设置回退列表；已配置好（如预装镜像）则不插手。
    注意：DejaVu Sans「已安装」不等于「能渲染中文」——按首位字体判断。"""
    from matplotlib import font_manager
    cur = plt.rcParams["font.sans-serif"]
    if cur and cur[0] in CJK_FONTS:
        return
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_FONTS:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name] + list(cur)
            plt.rcParams["axes.unicode_minus"] = False
            return


def load_results() -> list[dict]:
    rows = list(csv.DictReader(open(HERE / "results.csv", encoding="utf-8")))
    if not rows:
        raise SystemExit("results.csv 是空的，先跑评测")
    return rows


def med(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def waterline_of(model: str) -> int | None:
    """模型标签里的水位线编码：ds-16k → 16000；ds-16k-para2 之类 → None。"""
    if model.startswith("ds-") and model.endswith("k") and "-" not in model[3:]:
        return int(model[3:-1]) * 1000
    return None


def fig1_waterline(rows: list[dict]) -> None:
    """X 形交叉：full 成本几乎不随水位动，nodigest 随压力爆炸。
    只统计 DeepSeek 系（deepseek + ds-* 标签）——qwen 行是跨模型实验的，
    混进来会污染水位线这个单变量对照。"""
    def is_ds(model: str) -> bool:
        return model == "deepseek" or waterline_of(model) is not None

    data = defaultdict(lambda: defaultdict(list))  # [config][waterline] -> tokens
    ns = defaultdict(lambda: [0, 0])               # [(config, waterline)] -> [n, ok]
    for r in rows:
        if r["task"] != "kernel-quiz" or not is_ds(r["model"]):
            continue
        wl = waterline_of(r["model"]) or 60_000  # 无编码 = 默认水位
        key = (r["config"], wl)
        ns[key][0] += 1
        ns[key][1] += int(r["passed"])
        if int(r["tokens"]) > 0:
            data[r["config"]][wl].append(int(r["tokens"]))
    wls = sorted({wl for cfg in data.values() for wl in cfg})
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for cfg, color, label, dy in (("full", C_FULL, "full（digest 开启）", 9),
                                  ("nodigest", C_NODIGEST, "nodigest（消融）", -14)):
        ys = [med(data[cfg][w]) for w in wls]
        ax.plot(range(len(wls)), ys, "o-", color=color, label=label, lw=2)
        for i, w in enumerate(wls):
            n, ok = ns[(cfg, w)]
            if n:
                ax.annotate(f"{ok}/{n}", (i, ys[i]),
                            textcoords="offset points", xytext=(0, dy),
                            ha="center", fontsize=8, color=color)
    ax.set_yscale("log")
    ax.set_xticks(range(len(wls)),
                  [f"{w // 1000}k" + ("（默认）" if w == 60_000 else "") for w in wls])
    ax.set_xlabel("压缩水位线（ECALL_MAX_CONTEXT，越小压缩越频繁）")
    ax.set_ylabel("token 中位数（对数轴）")
    ax.set_title("kernel-quiz：低压≈免费，高压 3~9 倍（r=3/组）")
    ax.legend()
    ax.grid(alpha=0.25, which="both")
    fig.tight_layout()
    fig.savefig(OUT / "fig1_waterline.png", dpi=160)
    plt.close(fig)


def fig2_tax_anatomy(rows: list[dict]) -> None:
    """三刀降税。orig 行来自旧代码版本（results.csv.bak-pre-parallel / README 记载），
    para/para2 行从当前 results.csv 实时算——重跑即更新。"""
    def med_of(model_label, config="full"):
        return med([int(r["tokens"]) for r in rows
                    if r["task"] == "kernel-quiz" and r["model"] == model_label
                    and r["config"] == config and int(r["tokens"]) > 0])

    orig = 230_000  # 旧代码全程强制（见 README 降税解剖表；该行在备份 CSV 里）
    para = med_of("ds-16k-para") or 148_000       # + 并行清账（6 发）
    para2 = med_of("ds-16k-para2") or 68_000      # + 批界冻结（终版）
    floor = med_of("ds-16k", "nodigest") or 62_000  # nodigest 地板
    labels = ["原始版\n(拒绝+等下轮)", "+ 并行清账", "+ 批界冻结\n(终版)", "nodigest\n(地板)"]
    vals = [orig, para, para2, floor]
    colors = [C_NODIGEST, "#f0a04c", C_FULL, C_MINI]
    fig, ax = plt.subplots(figsize=(6.8, 4.2))
    bars = ax.bar(labels, vals, color=colors)
    for b, v in zip(bars, vals):
        ax.annotate(f"{v / 10000:.1f}万", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 4), ha="center", fontsize=10)
    ax.axhline(floor, color=C_MINI, ls="--", lw=1)
    ax.set_ylabel("token 中位数")
    ax.set_title("digest 税的解剖：16k 全程强制，23万 → 6.8万（kernel-quiz）")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "fig2_tax_anatomy.png", dpi=160)
    plt.close(fig)


def fig3_crash_resume() -> None:
    path = HERE / "crash_results.csv"
    if not path.exists():
        print("（跳过 fig3：没有 crash_results.csv，先跑 evals/crash_resume.py）")
        return
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    data = defaultdict(lambda: defaultdict(list))
    for r in rows:
        data[r["config"]]["tokens"].append(int(r["tokens"]))
        data[r["config"]]["reads"].append(int(r["reads"]))
        data[r["config"]]["passed"].append(int(r["passed"]))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8, 3.8))
    for ax, key, title in ((a1, "tokens", "token 中位数"), (a2, "reads", "恢复后读文件次数")):
        cfgs = [c for c in ("full", "nodigest") if c in data]
        vals = [med(data[c][key]) for c in cfgs]
        colors = [C_FULL if c == "full" else C_NODIGEST for c in cfgs]
        bars = ax.bar(cfgs, vals, color=colors)
        for b, v in zip(bars, vals):
            ax.annotate(f"{v:,.0f}", (b.get_x() + b.get_width() / 2, v),
                        textcoords="offset points", xytext=(0, 4),
                        ha="center", fontsize=9)
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.25)
    fig.suptitle("崩溃恢复：第 12 步 SIGKILL 后 --resume 续跑（kernel-quiz, 4k 水位）")
    fig.tight_layout()
    fig.savefig(OUT / "fig3_crash_resume.png", dpi=160)
    plt.close(fig)


def fig4_models(rows: list[dict]) -> None:
    """种子集：同一 harness 下不同模型/配置的成本剖面。"""
    seeds = [r for r in rows if r["task"] not in
             ("extract-config", "fix-test-suite", "add-middleware", "log-archaeology")
             and not r["task"].startswith("kernel-") and int(r["tokens"]) > 0]
    groups = defaultdict(list)
    for r in seeds:
        groups[(r["model"], r["config"])].append(int(r["tokens"]))
    order = sorted(groups.items(), key=lambda kv: med(kv[1]))
    labels = [f"{m}\n{c}" for (m, c), _ in order]
    vals = [med(v) for _, v in order]
    colors = [C_FULL if c == "full" else (C_MINI if c == "mini" else C_NODIGEST)
              for _, c in order]
    fig, ax = plt.subplots(figsize=(6.8, 4))
    bars = ax.bar(labels, vals, color=colors)
    for b, v in zip(bars, vals):
        ax.annotate(f"{v:,.0f}", (b.get_x() + b.get_width() / 2, v),
                    textcoords="offset points", xytext=(0, 4), ha="center", fontsize=9)
    ax.set_ylabel("token 中位数")
    ax.set_title("种子集 24 发/组：同一 harness，不同模型的成本剖面")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "fig4_models.png", dpi=160)
    plt.close(fig)


def main() -> None:
    ensure_cjk_font()
    OUT.mkdir(exist_ok=True)
    rows = load_results()
    fig1_waterline(rows)
    fig2_tax_anatomy(rows)
    fig3_crash_resume()
    fig4_models(rows)
    print(f"图已生成：{OUT}")


if __name__ == "__main__":
    main()
