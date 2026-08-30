# ecall

一个简化的 Claude Code 式编程智能体 harness（~1500 行 Python，零 agent 框架）。

名字来自 RISC-V 的 `ecall` 指令：模型是运行在 ring3 的用户进程，只能发起"系统调用"（tool call）；
runtime 是内核，负责校验、执行、观察，并可以随时中止这次调用。
整个项目的核心命题是：**智能体的能力边界不取决于模型，而取决于内核给它什么样的 ABI。**

- 不依赖任何 agent 框架 / SDK（无 LangChain / AutoGen / Claude SDK 等），仅使用 OpenAI 兼容 API
- 对话历史管理、工具执行、输出解析、循环终止、错误处理全部为手写实现
- 模型无关：换三个环境变量即可切换任意 OpenAI 兼容模型（已在 DeepSeek / Qwen 上验证）

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

# 时间旅行
python3 main.py replay .ecall-log.jsonl        # 回放轨迹
python3 main.py rewind .ecall-log.jsonl -s 12  # 把工作区回滚到第 12 步
python3 main.py fork .ecall-log.jsonl -s 12    # 从第 12 步分叉，换个方向重跑
```

## 功能总览

| 子系统 | 实现 | 文件 |
|---|---|---|
| 主循环 | think → tool_call → observe 循环；4 个终止条件（任务完成 / 步数 / 连续错误 / token 预算）；振荡检测（同一调用重复 3 次拦截） | `agent.py` |
| 工具层 | 9 个工具：read/write/edit_file、list_dir、grep、glob、run_shell、explore、todo；工作区 jail；edit 两级匹配（精确→行级模糊）+ 诊断回执 | `tools.py` |
| todo | 模型自维护的任务清单（全量替换语义），每步回显在最新工具结果尾部——对抗上下文漂移的外部记忆 | `tools.py` |
| 沙盒 | bubblewrap：只读根挂载 + 临时 /tmp 与 $HOME + 断网 + 环境变量洗白（API key 不进沙盒）；不可用时自动降级 host 并标注 | `tools.py` |
| 审批门 | 变更型 shell 命令在无沙盒且交互式时需人工确认（y/a）；非交互自动放行不阻塞评测 | `tools.py` |
| 上下文管理 | 60k token 预算；旧工具输出压缩并**内容寻址换出**到 `.ecall-swap/`（git 式去重），模型可按需 read_file 拉回 | `context.py` |
| 子代理 | explore 只读探索员：独立上下文、只读白名单（execute 层强制）、递归套娃双层封堵、token 计入父账单 | `agent.py` |
| 持久化 | 全量事件轨迹（JSONL）+ 文件级 WAL；轨迹是 replay/rewind/fork/评测的唯一事实来源 | `agent.py` |
| 时间旅行 | replay 回放、rewind 逆放 WAL 回滚工作区、fork 从任意历史点分叉续跑 | `timetravel.py` |
| 流式传输 | SSE 默认开启，增量合并 tool_call 分片；`ECALL_NO_STREAM=1` 一键回退 | `llm.py` |
| 项目记忆 | AGENTS.md 声明式项目指令（会话开始时冻结注入，保护前缀缓存） | `agent.py` |
| 中途干预 | 运行中写 `.ecall-steer` 文件，在步边界注入一条用户消息（消费即焚） | `agent.py` |
| 评测 | 8 个种子任务 + 4 个进阶任务 × 3 次重复，临时目录隔离，check 命令退出码作判决，结果落 CSV | `evals/` |
| 测试 | 38 个离线单元测试，零网络零 API，stdlib unittest | `tests/` |

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

三个值得注意的发现：

1. **在两个规模级别上，mini（仅 bash）pass 率均与 full 持平，且稳定省约 30~45% token**——
   bash 是万能观察工具，grep/cat/find 一件不缺，工具面差异被它消解。这复现并加强了
   mini-SWE-agent 的核心论点。因此 full 工具面的价值不在"能做更多事"，而在护栏（jail /
   审批 / undo）、审计（结构化轨迹）与工程化（精确编辑、上下文经济性）——能力趋同，
   可靠性分层。
2. **harness 的影响是模型相关的**：Qwen 在 full 下满分、在 mini 下反而挂了 1 次（DeepSeek 各组均满分）。
   同一模型在不同 harness 下表现不同，脱离 harness 谈模型强弱没有意义。
3. **评测能钓出真实 harness bug**：full 组初测为 22/24，回放失败轨迹定位到 list_dir 把根目录
   打印成工作区同名、诱导模型嵌套写入的 bug；修复后复测回到 24/24，token 中位数从 7870 降到 6988。
   没有评测，这个 bug 会一直在那里。

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
