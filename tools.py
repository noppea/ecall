"""tools.py — 工具的定义（Schema）与本地执行。

模型只能「下单」（输出一段结构化的 token），真正动手的只有这里。
模型 = ring3 不可信进程，本文件 = 内核的陷入处理 + 设备驱动。
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 工作区即监狱：agent 启动时所在的目录，任何文件操作不许逃出去
WORKSPACE = Path.cwd().resolve()

# 部署校验用：python3 -c "import tools; print(tools.TOOLS_VERSION)"
TOOLS_VERSION = "v6.5-streaming"

MAX_FILE_LINES = 200     # read_file 截断
MAX_OUTPUT_CHARS = 4000  # shell 输出截断
MAX_DIFF_LINES = 60      # edit_file 返回 diff 的截断
MAX_LIST_ENTRIES = 200   # list_dir 截断
MAX_GREP_MATCHES = 50    # grep 截断
MAX_GLOB_RESULTS = 100   # glob 截断
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".ecall-swap"}  # 压缩换出目录：观察时隐身，但 read_file 可按路径拉回
# 自我污染防线：agent 的轨迹日志、编辑临时文件、转向门文件不该出现在它自己的视野里
SKIP_FILES = {"trajectory.jsonl", ".ecall-steer"}
SKIP_SUFFIXES = (".ecalltmp",)


def _jail(path: str) -> Path:
    """能力边界检查：把相对路径解析成绝对路径，拒绝越界访问。"""
    p = (WORKSPACE / path).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise PermissionError(f"越界访问被拒绝: {path}")
    return p


# ---------- 文件读写 ----------

def read_file(path: str) -> str:
    lines = _jail(path).read_text(encoding="utf-8", errors="replace").splitlines()
    out = [f"{i + 1}| {line}" for i, line in enumerate(lines[:MAX_FILE_LINES])]
    if len(lines) > MAX_FILE_LINES:
        out.append(f"... 共 {len(lines)} 行，已截断")
    return "\n".join(out)


def write_file(path: str, content: str) -> str:
    p = _jail(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"ok: 已写入 {path}（{len(content)} 字符）"


def peek_file(path: str) -> str | None:
    """时间旅行的快照探针：返回文件当前内容；不存在则返回 None。"""
    p = _jail(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


# ---------- edit_file：外科手术式编辑 ----------

def edit_file(path: str, old_string: str, new_string: str) -> str:
    """把 old_string 替换为 new_string。

    三级策略：
      1. 精确匹配（要求唯一，多处匹配则报错，逼模型提供更多上下文）
      2. 模糊匹配：逐行忽略行尾空白后再比（模型最常见的错误就是行尾多空格）
      3. 都失败 → 返回诊断信息（列出含相似片段的行号），帮模型自我纠正
    写入是原子的：先写临时文件再 rename，失败不会留下半截文件。
    """
    p = _jail(path)
    if not p.exists():
        return f"error: 文件不存在 {path}（要新建文件请用 write_file）"
    content = p.read_text(encoding="utf-8", errors="replace")

    if old_string in content:
        if content.count(old_string) > 1:
            return "error: old_string 匹配到多处，请提供包含更多上下文的唯一片段"
        new_content = content.replace(old_string, new_string, 1)
        strategy = "exact"
    else:
        new_content = _fuzzy_replace(content, old_string, new_string)
        if new_content is None:
            return _edit_failure_hint(content, old_string)
        strategy = "fuzzy(rstrip)"

    tmp = p.with_name(p.name + ".ecalltmp")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, p)  # rename 是原子操作：要么全换，要么不换

    diff = "\n".join(difflib.unified_diff(
        content.splitlines(), new_content.splitlines(),
        fromfile=f"{path}(改前)", tofile=f"{path}(改后)", lineterm="", n=2,
    ))
    diff_lines = diff.splitlines()
    if len(diff_lines) > MAX_DIFF_LINES:
        diff = "\n".join(diff_lines[:MAX_DIFF_LINES]) + "\n... diff 已截断"
    return f"ok: 已编辑 {path}（{strategy} 匹配）\n{diff}"


def _fuzzy_replace(content: str, old: str, new: str):
    """行级模糊匹配：双方逐行 rstrip 后比较，命中且唯一则替换原始行区间。

    保守地只容忍行尾空白差异——容忍缩进差异会把错误缩进写进文件。
    """
    old_lines = old.splitlines()
    if not old_lines:
        return None
    norm = [line.rstrip() for line in old_lines]
    lines = content.splitlines(keepends=True)
    hits = [
        i for i in range(len(lines) - len(old_lines) + 1)
        if [l.rstrip("\n").rstrip() for l in lines[i:i + len(old_lines)]] == norm
    ]
    if len(hits) != 1:
        return None
    i = hits[0]
    new_lines = [l + "\n" for l in new.splitlines()]
    return "".join(lines[:i]) + "".join(new_lines) + "".join(lines[i + len(old_lines):])


def _edit_failure_hint(content: str, old: str) -> str:
    """编辑失败的诊断：列出包含 old 首行片段的行，让模型看着真实内容自我纠正。"""
    first = old.splitlines()[0].strip()[:40] if old.splitlines() else ""
    hits = [f"{i + 1}| {l}" for i, l in enumerate(content.splitlines())
            if first and first in l]
    hint = "\n".join(hits[:10]) if hits else "（文件中未找到相似内容）"
    return (f"error: old_string 未匹配（精确与模糊均失败）。\n"
            f"包含首行片段 {first!r} 的行：\n{hint}")


# ---------- 观察三件套：list_dir / grep / glob ----------

def list_dir(path: str = ".") -> str:
    root = _jail(path)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= 2:  # 只展开两层，避免刷屏
            dirnames[:] = []
            continue
        indent = "  " * depth
        out.append(f"{indent}{Path(dirpath).name}/")
        out.extend(f"{indent}  {f}" for f in sorted(filenames)
                   if f not in SKIP_FILES and not f.endswith(SKIP_SUFFIXES))
        if len(out) > MAX_LIST_ENTRIES:
            out.append("... 条目过多，已截断")
            break
    return "\n".join(out) or "（空目录）"


def grep(pattern: str, path: str = ".") -> str:
    root = _jail(path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"error: 正则非法: {e}"
    matches = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if not p.is_file() or p.name in SKIP_FILES or p.name.endswith(SKIP_SUFFIXES):
            continue
        for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                matches.append(f"{p.relative_to(root)}:{i}: {line[:200]}")
                if len(matches) >= MAX_GREP_MATCHES:
                    return "\n".join(matches) + "\n... 匹配过多，已截断"
    return "\n".join(matches) or "（无匹配）"


def glob_files(pattern: str) -> str:
    hits = sorted(
        p for p in WORKSPACE.glob(pattern)
        if not any(part in SKIP_DIRS for part in p.relative_to(WORKSPACE).parts)
        and p.name not in SKIP_FILES and not p.name.endswith(SKIP_SUFFIXES)
    )
    out = [str(p.relative_to(WORKSPACE)) for p in hits[:MAX_GLOB_RESULTS]]
    if len(hits) > MAX_GLOB_RESULTS:
        out.append("... 结果过多，已截断")
    return "\n".join(out) or "（无匹配）"


# ---------- 子代理：只读探索员 ----------

def explore(task: str) -> str:
    """派一个只读子代理去探索工作区，只把结论带回父上下文。

    实现在 agent.explore（需要复用主循环）；这里惰性导入绕开循环 import：
    agent 依赖 tools 的工具集，tools 的这个工具又依赖 agent 的主循环。
    """
    import agent
    return agent.explore(task)


# ---------- shell：风险分级（外层闸门）+ bwrap 沙箱（内核级监狱） ----------
#
# 故事弧闭环：实验发现 _jail 关不住 shell 通道（模型一句 cat ~/... 就越狱了），
# v2 只能先用三级风险分级缓解：readonly 放行 / mutating 放行但标注 / dangerous 拒绝。
# v6 用内核机制补上这个缺口：bubblewrap 起一个用户命名空间，把根文件系统
# 只读挂载、只把工作区重新挂成可写、断网——这次不是「劝模型别越界」，
# 是内核让它越不出去。风险分级保留为外层闸门（沙箱前就把 dangerous 拒掉）。
# 沙箱是可选能力：ECALL_BWRAP=1 且 bwrap 可用才启用，否则降级为裸执行；
# 运行模式（bwrap/host）标注进每条返回，轨迹可审计。

READONLY_CMDS = {"ls", "cat", "pwd", "head", "tail", "wc", "grep", "rg", "find",
                 "echo", "which", "file", "stat", "tree", "diff", "env", "date"}
DANGEROUS_PATTERNS = ("rm -rf", "rm -fr", "mkfs", "shutdown", "reboot", ":(){",
                      "dd if=", "> /dev/sd", "chmod -R /", "chown -R /")

_BWRAP_OK = None  # 惰性探测缓存：None=未探测，True/False=结论


def _classify(command: str) -> str:
    """粗粒度分级：按 && ; | 切分命令，检查每段的首个单词。"""
    if any(bad in command for bad in DANGEROUS_PATTERNS):
        return "dangerous"
    segments = re.split(r"&&|\|\||[;|]", command)
    firsts = {seg.strip().split()[0] for seg in segments if seg.strip()}
    return "readonly" if firsts and firsts <= READONLY_CMDS else "mutating"


def _bwrap_available() -> bool:
    """惰性探测沙箱可用性：环境变量开启 + 有二进制 + 金丝雀命令真的跑得起来。

    有些发行版默认禁用非特权用户命名空间，bwrap 装了也白装，
    所以不能只查 which，要真跑一次（结果缓存，只探测一次）。
    """
    global _BWRAP_OK
    if _BWRAP_OK is not None:
        return _BWRAP_OK
    _BWRAP_OK = False
    if os.environ.get("ECALL_BWRAP") == "1" and shutil.which("bwrap"):
        try:
            canary = subprocess.run(
                ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true"],
                capture_output=True, timeout=10)
            _BWRAP_OK = canary.returncode == 0
        except Exception:
            pass  # 探测本身失败也按不可用处理，降级为裸执行
    return _BWRAP_OK


def _bwrap_argv(command: str) -> list[str]:
    """组装沙箱命令行。

    挂载顺序有讲究，bwrap 的挂载按顺序叠加、后挂的盖住先挂的：
      1. --ro-bind / /  根文件系统整体只读（/etc、/usr 摸不得，但可读——
         工具链和头文件本来就要读系统目录）
      2. --tmpfs /tmp   盖住真 /tmp，给编译器等一个可写的临时区
      3. --tmpfs $HOME  盖住家目录——这一步才补上当年「cat ~/... 越狱」的洞：
         只读挂载挡不住读，tmpfs 让 ~/.ssh、~/.env、别的项目直接消失
      4. --bind ws ws   唯一的可写缺口：工作区。--bind 的源路径在宿主侧解析，
         所以工作区即使在 $HOME 或 /tmp 下，也能从被盖住的目录上重新挂出来
    另外把 API key 类环境变量摘掉：沙箱里 env 也看不到密钥。
    """
    ws = str(WORKSPACE)
    argv = ["bwrap",
            "--ro-bind", "/", "/",
            "--tmpfs", "/tmp",
            "--tmpfs", str(Path.home()),
            "--bind", ws, ws,
            "--proc", "/proc",         # 全新的 proc，看不到宿主机进程
            "--unshare-net",           # 断网：curl/pip install 直接失败
            "--die-with-parent",       # 父进程被杀时沙箱内进程陪葬，不留孤儿
            "--chdir", ws]
    for var in ("ECALL_API_KEY", "OPENAI_API_KEY", "KIMI_API_KEY", "DEEPSEEK_API_KEY"):
        if var in os.environ:
            argv += ["--unsetenv", var]
    argv += ["--", "bash", "-c", command]
    return argv


_APPROVE_ALL = False  # 会话级「都允许」（审批门里按 a）


def _approve(command: str) -> bool:
    """审批门：mutating 命令在没有沙箱兜底时需要人批准。

    安全策略是互补的两层：
      - 有 bwrap：内核隔离兜底，mutating 命令放心自动执行；
      - 没 bwrap + 交互式终端：问人（y=允许一次 / a=本会话都允许）；
      - 没 bwrap + 非交互（管道/评测器）：无处问人，自动放行——
        headless 场景的诚实取舍，风险靠 _jail 和轨迹审计兜底。
    门只装在 shell 上的原因：文件写入有 WAL 可以 rewind（有 undo），
    shell 命令没有 undo——审批的成本要花在没有后悔药的地方。
    """
    global _APPROVE_ALL
    if _APPROVE_ALL or _bwrap_available() or not sys.stdin.isatty():
        return True
    try:
        ans = input(f"\n[审批] mutating 命令且沙箱未启用：\n  {command}\n"
                    "允许执行？[y=允许一次 / a=本会话都允许 / 其他=拒绝] ")
    except EOFError:
        return True
    if ans.strip().lower() == "a":
        _APPROVE_ALL = True
        return True
    return ans.strip().lower() == "y"


def run_shell(command: str, timeout: int = 60) -> str:
    level = _classify(command)
    if level == "dangerous":
        return f"error: 命中危险命令拦截（{command[:60]}）"
    if level == "mutating" and not _approve(command):
        return "error: 用户拒绝了这条命令的执行（审批门）"
    sandboxed = _bwrap_available()
    try:
        proc = subprocess.run(
            _bwrap_argv(command) if sandboxed else command,
            shell=not sandboxed, cwd=WORKSPACE,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"error: 执行超时（{timeout}s）"
    # 风险等级 + 运行模式都标注进返回文本：轨迹可审计，模型也能从
    # [..|bwrap] 和断网报错里学会「这台机器没网」，而不是盲目重试
    mode = "bwrap" if sandboxed else "host"
    out = f"[{level}|{mode}] exit={proc.returncode}\n{proc.stdout}{proc.stderr}"
    return out if len(out) <= MAX_OUTPUT_CHARS else out[:MAX_OUTPUT_CHARS] + "\n... 输出已截断"


# ---------- Schema：随请求发给模型，告诉它「有哪些系统调用」 ----------
# 注意：description 就是模型的使用说明书，写得越具体，模型用得越准。

SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取工作区内文件内容（带行号，超长截断）",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "description": "相对工作区的路径"}},
            "required": ["path"],
        }}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "将内容整体写入工作区内文件（新建或整体覆盖）。修改已有文件请优先用 edit_file",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path", "content"],
        }}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": ("外科手术式编辑：把 old_string 替换为 new_string。"
                        "old_string 必须是文件中唯一的连续片段，"
                        "请包含足够上下文（如函数签名行）以保证唯一"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "要被替换的原文（需唯一匹配）"},
                "new_string": {"type": "string", "description": "替换后的新内容"},
            },
            "required": ["path", "old_string", "new_string"],
        }}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "查看目录结构（两层深度，自动跳过 .git 等噪音目录）",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "default": "."}},
        }}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "在工作区内按正则搜索文件内容，返回 文件:行号: 内容",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"pattern": {"type": "string"},
                           "path": {"type": "string", "default": "."}},
            "required": ["pattern"],
        }}},
    {"type": "function", "function": {
        "name": "glob",
        "description": "按通配符查找文件，如 **/*.py",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        }}},
    {"type": "function", "function": {
        "name": "explore",
        "description": ("把「摸清代码结构」外包给一个只读子代理：它在独立上下文里自由检索，"
                        "只把结论（文件路径、行号、关键片段）返回给你，"
                        "探索过程不占用你的上下文。适合动手前先了解项目结构；"
                        "它不能修改文件，别派它去干活"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"task": {"type": "string",
                                    "description": "要查清的问题，描述越具体结论越准"}},
            "required": ["task"],
        }}},
    {"type": "function", "function": {
        "name": "run_shell",
        "description": ("在工作区内执行 shell 命令（有超时、截断与风险分级；"
                        "沙箱模式下文件系统只读且断网）"),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"command": {"type": "string"},
                           "timeout": {"type": "integer", "description": "秒", "default": 60}},
            "required": ["command"],
        }}},
]

# mini 模式（消融对照组）：只留 run_shell 一个工具，复刻 mini-SWE-agent 的极简设定，
# 用来实测「工具面」这个变量到底贡献多少 pass 率、多花多少 token。
if os.environ.get("ECALL_MINI") == "1":
    SCHEMAS = [s for s in SCHEMAS if s["function"]["name"] == "run_shell"]

HANDLERS = {
    "read_file": lambda a: read_file(a["path"]),
    "write_file": lambda a: write_file(a["path"], a["content"]),
    "edit_file": lambda a: edit_file(a["path"], a["old_string"], a["new_string"]),
    "list_dir": lambda a: list_dir(a.get("path", ".")),
    "grep": lambda a: grep(a["pattern"], a.get("path", ".")),
    "glob": lambda a: glob_files(a["pattern"]),
    "run_shell": lambda a: run_shell(a["command"], a.get("timeout", 60)),
    "explore": lambda a: explore(a["task"]),
}


def execute(name: str, arguments_json: str, allowed: tuple | None = None) -> str:
    """陷入处理：权限检查 → 校验 → 执行 → 异常兜底。任何失败都变成文本喂回模型。

    allowed 是子代理的权限收缩：即使模型幻觉出白名单外的工具调用，
    也在这一层被拦住——Schema 只是建议，这里才是强制。
    """
    if allowed is not None and name not in allowed:
        return f"error: 当前上下文不允许使用工具 {name}"
    handler = HANDLERS.get(name)
    if handler is None:
        return f"error: 未知工具 {name}"
    try:
        args = json.loads(arguments_json)
    except json.JSONDecodeError as e:
        return f"error: 参数不是合法 JSON：{e}"
    try:
        return handler(args)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
