"""tools.py — 工具的定义（Schema）与本地执行。

模型只能「下单」（输出一段结构化的 token），真正动手的只有这里。
模型 = ring3 不可信进程，本文件 = 内核的陷入处理 + 设备驱动。
"""
import difflib
import json
import os
import re
import subprocess
from pathlib import Path

# 工作区即监狱：agent 启动时所在的目录，任何文件操作不许逃出去
WORKSPACE = Path.cwd().resolve()

MAX_FILE_LINES = 200     # read_file 截断
MAX_OUTPUT_CHARS = 4000  # shell 输出截断
MAX_DIFF_LINES = 60      # edit_file 返回 diff 的截断
MAX_LIST_ENTRIES = 200   # list_dir 截断
MAX_GREP_MATCHES = 50    # grep 截断
MAX_GLOB_RESULTS = 100   # glob 截断
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


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


# ---------- edit_file：外科手术式编辑（今天的硬骨头） ----------

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
        out.extend(f"{indent}  {f}" for f in sorted(filenames))
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
        if not p.is_file():
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
    )
    out = [str(p.relative_to(WORKSPACE)) for p in hits[:MAX_GLOB_RESULTS]]
    if len(hits) > MAX_GLOB_RESULTS:
        out.append("... 结果过多，已截断")
    return "\n".join(out) or "（无匹配）"


#shell：风险分级

READONLY_CMDS = {"ls", "cat", "pwd", "head", "tail", "wc", "grep", "rg", "find",
                 "echo", "which", "file", "stat", "tree", "diff", "env", "date"}
DANGEROUS_PATTERNS = ("rm -rf", "rm -fr", "mkfs", "shutdown", "reboot", ":(){",
                      "dd if=", "> /dev/sd", "chmod -R /", "chown -R /")


def _classify(command: str) -> str:
    """粗粒度分级：按 && ; | 切分命令，检查每段的首个单词。"""
    if any(bad in command for bad in DANGEROUS_PATTERNS):
        return "dangerous"
    segments = re.split(r"&&|\|\||[;|]", command)
    firsts = {seg.strip().split()[0] for seg in segments if seg.strip()}
    return "readonly" if firsts and firsts <= READONLY_CMDS else "mutating"


def run_shell(command: str, timeout: int = 60) -> str:
    level = _classify(command)
    if level == "dangerous":
        return f"error: 命中危险命令拦截（{command[:60]}）"
    try:
        proc = subprocess.run(command, shell=True, cwd=WORKSPACE,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"error: 执行超时（{timeout}s）"
    # 风险等级标注进返回文本：轨迹可审计，也为后续的审批门留好位置
    out = f"[{level}] exit={proc.returncode}\n{proc.stdout}{proc.stderr}"
    return out if len(out) <= MAX_OUTPUT_CHARS else out[:MAX_OUTPUT_CHARS] + "\n... 输出已截断"


# Schema：随请求发给模型，告诉它有哪些系统调用
# description 就是模型的使用说明书，写得越具体，模型用得越准。

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
        "name": "run_shell",
        "description": "在工作区内执行 shell 命令（有超时、截断与风险分级）",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"command": {"type": "string"},
                           "timeout": {"type": "integer", "description": "秒", "default": 60}},
            "required": ["command"],
        }}},
]

HANDLERS = {
    "read_file": lambda a: read_file(a["path"]),
    "write_file": lambda a: write_file(a["path"], a["content"]),
    "edit_file": lambda a: edit_file(a["path"], a["old_string"], a["new_string"]),
    "list_dir": lambda a: list_dir(a.get("path", ".")),
    "grep": lambda a: grep(a["pattern"], a.get("path", ".")),
    "glob": lambda a: glob_files(a["pattern"]),
    "run_shell": lambda a: run_shell(a["command"], a.get("timeout", 60)),
}


def execute(name: str, arguments_json: str) -> str:
    """陷入处理：校验 → 执行 → 异常兜底。任何失败都变成文本喂回模型。"""
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