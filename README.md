# ecall

一个简化的 Claude Code 式编程智能体 harness（~1500 行 Python，零 agent 框架）。

名字来自 RISC-V 的 `ecall` 指令：模型是运行在 ring3 的用户进程，只能发起"系统调用"（tool call）；
runtime 是内核，负责校验、执行、观察，并可以随时中止这次调用。
整个项目的核心命题是：**智能体的能力边界不取决于模型，而取决于内核给它什么样的 ABI。**

- 不依赖任何 agent 框架 / SDK（无 LangChain / AutoGen / Claude SDK 等），仅使用 OpenAI 兼容 API
- 对话历史管理、工具执行、输出解析、循环终止、错误处理全部为手写实现
- 模型无关：换三个环境变量即可切换任意 OpenAI 兼容模型（已在 DeepSeek / Qwen 上验证）

## 两条设计主线

功能表谁都会堆，这个项目真正想验证的是两个设计观点：

**① 轨迹是一等公民（agent flight recorder）。**
所有事件（任务、模型调用、工具执行、检查点、转向指令）落在同一条 JSONL 时间线上。
这条时间线不是调试副产品，而是五个功能的共享底座：

```
                ┌─ replay    回放时间线
                ├─ rewind    逆放 WAL，回滚工作区
  JSONL 轨迹 ───┼─ fork      从任意步分叉出新时间线
                ├─ resume    会话崩溃后重建模型看到的世界
                └─ eval      评测统计的唯一事实来源
```

屏幕输出可以是花哨的、给人看的；但一切机器消费都走轨迹。黑匣子理念：
出事先看飞行记录仪，不猜。

**② 记忆是一个层级体系（和 OS 内存层级同构）。**

| 层级 | 实现 | OS 类比 | 谁写 |
|---|---|---|---|
| 工作记忆 | todo 工具：每步回显在上下文尾部 | 寄存器：最近、最热、容量最小 | 模型写给自己 |
| 主存 | 上下文窗口；超出 60k 触发压缩 | 内存：预算制、页面会老化 | 双方对话产生 |
| 磁盘 | swap 换出：内容寻址落盘，可按需拉回 | swap 分区：换出不丢弃 | runtime 管理 |
| 只读固件 | AGENTS.md：会话开始时冻结注入 | ROM：开机加载、运行期不变 | 人写给模型 |

四种记忆四种写入者、四种生命周期，用一套隐喻统一——这不是事后贴标签，
每个机制的格式与注入时机都是按"它在层级里的位置"设计的。

## 快速开始

```bash
git clone <repo> && cd ecall
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 配置模型（API key 只走环境变量，仓库里不出现任何密钥）
cp .env.example .env   # 填入你的 key，然后：
set -a && source .env && set +a

# 一次性任务
python3 main.py "写一个命令行 todo 程序并自测"

# 交互式多轮（带会话记忆、token 累计显示、流式输出）
python3 main.py chat

# 崩溃/退出后从轨迹恢复会话，接着聊
python3 main.py chat --resume

# 时间旅行
python3 main.py replay .ecall-log.jsonl        # 回放轨迹
python3 main.py rewind .ecall-log.jsonl -s 12  # 把工作区回滚到第 12 步
python3 main.py fork .ecall-log.jsonl -s 12    # 从第 12 步分叉，换个方向重跑
```

## 功能总览

| 子系统 | 实现 | 文件 |
|---|---|---|
| 主循环 | think → tool_call → observe 循环；4 个终止条件（任务完成 / 步数 / 连续错误 / token 预算）；振荡检测（同一调用重复 3 次拦截） | `agent.py` |
| 工具层 | 11 个工具：read/write/edit_file、list_dir、grep、glob、run_shell、explore、explore_batch、todo、digest；工作区 jail；edit 两级匹配 + 诊断回执 | `tools.py` |
| digest | 模型读完大输出后当场写笔记；压缩换出时笔记取代首行成为占位符，原文留指针可拉回——生成时同步自我压缩 | `tools.py` + `context.py` |
| todo | 模型自维护的任务清单（全量替换语义），每步回显在最新工具结果尾部——对抗上下文漂移的外部记忆 | `tools.py` |
| 沙盒 | bubblewrap：只读根挂载 + 临时 /tmp 与 $HOME + 断网 + 环境变量洗白（API key 不进沙盒）；不可用时自动降级 host 并标注 | `tools.py` |
| 审批门 | 变更型 shell 命令在无沙盒且交互式时需人工确认（y/a）；非交互自动放行不阻塞评测 | `tools.py` |
| 上下文管理 | 60k token 预算；旧工具输出压缩并**内容寻址换出**到 `.ecall-swap/`（git 式去重），模型可按需 read_file 拉回 | `context.py` |
| 子代理 | explore 只读探索员 + explore_batch 并行扇出（≤4）：独立上下文、只读白名单（execute 层强制）、递归套娃双层封堵、token 计入父账单 | `agent.py` |
| 持久化 | 全量事件轨迹（JSONL）+ 文件级 WAL；轨迹是 replay/rewind/fork/评测的唯一事实来源 | `agent.py` |
| 时间旅行 | replay 回放、rewind 逆放 WAL 回滚工作区、fork 从任意历史点分叉续跑、chat --resume 会话恢复 | `timetravel.py` |
| 流式传输 | SSE 默认开启，增量合并 tool_call 分片；`ECALL_NO_STREAM=1` 一键回退 | `llm.py` |
| 项目记忆 | AGENTS.md 声明式项目指令（会话开始时冻结注入，保护前缀缓存） | `agent.py` |
| 中途干预 | 运行中写 `.ecall-steer` 文件，在步边界注入一条用户消息（消费即焚） | `agent.py` |
| 评测 | 8 个种子任务 + 4 个进阶任务 + 7 个内核级大任务（真实 Rust 内核 fixture），临时目录隔离，check 命令退出码作判决，FAIL 自动打印检查器断言，结果落 CSV | `evals/` |
| 测试 | 52 个离线单元测试，零网络零 API，stdlib unittest | `tests/` |

## 评测结果

两个任务集：**种子集**（8 个编程小题：修 bug / 跨文件重命名 / 写 CLI 等）与**进阶集**
（4 个多文件任务：跨 5 文件重构、带测试套件的调试、框架特性扩展、日志考古），
每配置重复 3 次，check 命令退出码判决，报告 pass 率与 token/步数中位数。

种子集（24 次运行/组）：

| 配置 | DeepSeek | Qwen3-Max |
|---|---|---|
| full（完整工具面） | 24/24，6988 tok / 4 步 | 24/24，7324 tok / 5 步 |
| mini（仅 bash） | 24/24，3873 tok / 4 步 | 23/24，4570 tok / 4 步 |

进阶集（12 次运行/组）：

| 配置 | DeepSeek |
|---|---|
| full | 12/12，12016 tok / 6 步 |
| mini | 12/12，8696 tok / 6 步 |

digest 消融（进阶集，8k 压缩水位线，**n=40/组**）：

| 配置 | pass | token 中位数 |
|---|---|---|
| full（强制 digest） | 40/40 | 15810 |
| nodigest | 40/40 | 16008 |

内核级任务（真实 Rust 内核 fixture，8k 压缩水位线）：

统计类（census / unwrap / deps / bigfile，DeepSeek，r=5/组，150 步 / 3M 预算）：

| 任务 | full | nodigest |
|---|---|---|
| census（unsafe/MODS/ARCHS 统计） | 5/5，中位 48657 tok | 5/5，中位 58929 tok |
| unwrap（unwrap 计数 + 最密集文件） | 5/5，中位 31726 tok | 5/5，中位 28444 tok |
| deps（依赖普查 + 最多依赖 crate） | 5/5，中位 27607 tok | 5/5，中位 20386 tok |
| bigfile（文件数/行数/最大文件） | 5/5，中位 23140 tok | 5/5，中位 18293 tok |

写作类：kernel-doc（通读全仓库写 ARCH.md，150 步 / 3M 预算，r=3/组）：full 2/3，nodigest 1/3。

语义饱和任务 kernel-unsafe-audit（为全仓库 228 个 unsafe 块逐个写用途说明，
检查器做全量位置比对 + 说明唯一性防套话；case study，n=1/组）：

| 配置 | 结果 | 步数 | token | 备注 |
|---|---|---|---|---|
| full | PASS | 84 | 3.01M | digest 被强制触发 124 次；熔断瞬间恰好写完 |
| nodigest | PASS | 82 | 2.90M | 自然 done；自发派子代理分流（36.6 万 tok，full 组为 0） |

问题后置任务 kernel-quiz（先通读 11 个文件的合成内核、再回答 5 问跨文件事实链；
4k 压缩水位线，DeepSeek，r=3/组）：

| 配置 | pass | token 中位数 | 步数中位数 | read_file 次数（逐发） |
|---|---|---|---|---|
| full | 3/3 | 245,165 | 28 | 37 / 37 / 32 |
| nodigest | 2/3 | 1,269,959 | 47 | 265 / 80 / 87 |

nodigest 失败的一发为 token_budget 熔断（3.06M token），至死未产出 ANSWER.md。
digest 在 full 组每发被强制触发 18~20 次。

水位线扫描（kernel-quiz，DeepSeek，r=3/组，token 中位数）：

| 压缩水位线 | full | nodigest |
|---|---|---|
| 16k（几乎无压缩） | 3/3，23.0 万 | 3/3，**6.2 万** |
| 8k | 3/3，23.3 万 | 3/3，**4.9 万** |
| 4k | 3/3，**24.5 万** | 2/3，127 万 |
| 2k | 2/3，**21.0 万** | 3/3，92.8 万 |

扫描暴露出全程强制 digest 的**固定税**：每次大观察强制一次笔记 = 一次全上下文
LLM 调用，宽松水位线下 nodigest 反而便宜 4 倍。结论：**digest 是保险——常缴 4 倍
保费，换高压区不 thrash、不熔断**（2k 组 full 的一发 FAIL 为 0 token 秒退，
疑似 API 侧抖动，未计入成本对比）。由此催生的设计迭代有三刀：
①enforcement 改为自适应，首次压缩事件发生后才上岗（`ECALL_DIGEST_FORCE=1`
恢复全程强制）；②**并行清账**——强制期间不再要求模型单独跑一轮纯 digest，
而是允许 digest 与后续操作在同一批 tool_calls 里发出（runtime 将 digest 提前执行，
批次内顺序无关；工具结果按 call_id 精确配对，时间旅行重建同步适配）；
③**欠账冻结在批次边界**——实弹轨迹抓到并行化的残余税：并行读 [A, B] 时 A 落地
才触发挂起，同批的 B 被误拒（每发 5~10 次），修法是只有「发出本批前已欠的账」
才够格拒绝。
自适应复测（r=3/组）：8k 下 3/3、中位 13.1 万（较全程强制 -44%，未压缩的发次
完全免税，触发压缩的发次自动转入保护）；4k 下 3/3、中位 17.7 万（-28%）。
三刀齐下后的终局复测（16k + 全程强制 + 并行 + 批界冻结，r=3）：
3/3、**中位 6.8 万**——与 nodigest 的 6.2 万仅差 9%，强制笔记的费率
从 ≈4 倍压到 ≈1.1 倍；digest 从「昂贵的保险」变成「接近免费的保险」。

崩溃恢复（crash-resume：quiz 任务跑到第 12 步时 SIGKILL，`chat --resume` 从轨迹重建续跑，r=2/组）：

| 配置 | pass | token（逐发） | 恢复后读文件次数 | 压缩次数 |
|---|---|---|---|---|
| full | 2/2 | 90,266 / 127,929 | 20 / 24 | 14 / 13 |
| nodigest | 2/2 | 453,575 / 366,522 | 89 / 98 | 25 / 24 |

两组都活了下来，但 full 靠 digest 笔记"回忆"进度，nodigest 只能把文件重读八九遍
"重新调查"——4 倍 token 的差距。resume 后 digest 笔记能活下来，靠的是重建时从轨迹的
digest 调用事件里把笔记挂回对应大输出（曾因此踩坑，见 DESIGN.md 事故表）。

digest 三级采用率实验（control policy 消融）：L1 仅提供 schema → 自发采用 0 次；
L2 加入系统提示词原则 → 0 次；L3 runtime 强制（大观察未消化则拒绝后续工具）→ 饱和任务中单次运行触发 124 次。

五个值得注意的发现：

1. **在两个规模级别上，mini（仅 bash）pass 率均与 full 持平，且稳定省约 30~45% token**——
   bash 是万能观察工具，grep/cat/find 一件不缺，工具面差异被它消解。这复现并加强了
   mini-SWE-agent 的核心论点。因此 full 工具面的价值不在"能做更多事"，而在护栏（jail /
   审批 / undo）、审计（结构化轨迹）与工程化（精确编辑、上下文经济性）——能力趋同，
   可靠性分层。
2. **harness 的影响是模型相关的**：Qwen 在 full 下满分、在 mini 下反而挂了 1 次（DeepSeek 各组均满分）。
   同一模型在不同 harness 下表现不同，脱离 harness 谈模型强弱没有意义。
3. **评测能钓出真实 harness bug**：full 组初测为 22/24，回放失败轨迹定位到 list_dir 把根目录
   打印成工作区同名、诱导模型嵌套写入的 bug；修复后复测回到 24/24，token 中位数从 7870 降到 6988。
   同类事件还有：预算护栏对子代理事后结算导致的 fork 炸弹（explore_batch 一次扇出
   8 步烧穿 2M token，修复为全局实时熔断）、fixture 空转时模型拒绝编造数据（"证据优先"
   原则实弹生效）、以及 agent 自主发现"read_file 有 jail 而 run_shell 没有"的红队行为——
   这正是 bwrap 沙盒必须默认开启的论据。
4. **自我压缩的价值域在"信息需求后置"的任务，且代价是固定保费。** digest 采用率三级治理
   （L1/L2/L3）之后，价值消融分两步：write-as-you-go 任务（小任务 n=40×2、228 条目饱和
   任务 n=1×2）上无差异——成果边做边落盘，文件系统是最终的记忆外存；问题后置任务
   kernel-quiz 上分出胜负（full 3/3 vs nodigest 2/3，5 倍 token 差）。但水位线扫描
   （16k/8k/4k/2k）揭示了另一面：强制 digest 的固定税 ≈4 倍 token，宽松水位下纯属浪费——
   **digest 是保险，不是免费午餐**。三刀降税（自适应门 + 并行清账 + 批界冻结）之后，
   16k 全程强制的费率从 ≈4 倍压到 ≈1.1 倍（6.8 万 vs nodigest 6.2 万）——
   保险接近免费后，「始终投保」重新成为合理默认。
   机理：占位符质量决定重读放大系数；首行截断是无选择的
   信息丢失，digest 是有选择的保留。崩溃恢复场景同理：SIGKILL 后续跑，full 组读文件
   20~24 次即可接上，nodigest 组要重读 89~98 次。
5. **检查器也是软件，也有边界用例 bug。** kernel-deps 初版检查器没考虑"零依赖 + 全员并列"
   的退化情形：模型正确回答 DEPS=0 与 MAXCRATE=N/A，却被要求等于 max() 任意挑出的路径，
   10/10 团灭——暴露的是 eval bug 而非模型缺陷。修复经历三轮（接受 N/A → 接受空值 →
   prompt 显式约定退化协议），教训：**精确匹配类评测里，"答案不存在"情形的输出协议必须显式约定**。

生成可视化报告：`python3 evals/report.py`（results.csv → 单文件 HTML，零依赖）。

复现：`python3 evals/run_eval.py -c full -m deepseek -r 3`（`-c mini` 切换消融组，`-m` 只是分组标签）。

## 设计文档

见 [DESIGN.md](DESIGN.md)：威胁模型、每个机制的"为什么"、事故—修复对照表、评测方法论。

## 目录结构

```
main.py        入口：一次性任务 / chat 多轮 / replay / rewind / fork
agent.py       主循环、终止条件、振荡检测、子代理、AGENTS.md、steer
tools.py       工具实现与 Schema、工作区 jail、bubblewrap 沙盒、审批门
llm.py         API 客户端、流式聚合、错误分类（致命/溢出/瞬时）、重试
context.py     token 估算、压缩、swap 换出（内容寻址）
timetravel.py  轨迹加载、回放、WAL 回滚、分叉
evals/         任务集、评测脚本、results.csv
```
