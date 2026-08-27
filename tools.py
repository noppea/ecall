"""tools.py — 工具的定义（Schema）与本地执行。

模型只能「下单」（输出一段结构化的 token），真正动手的只有这里。
模型 = ring3 不可信进程，本文件 = 内核的陷入处理 + 设备驱动。
"""
import json
import subprocess
from pathlib import Path

# 工作区即监狱：agent 启动时所在的目录，任何文件操作不许逃出去
WORKSPACE = Path.cwd()


def _jail(path: str) -> Path:
    """能力边界检查：把相对路径解析成绝对路径，拒绝越界访问。"""
    p = (WORKSPACE / path).resolve()
    if not p.is_relative_to(WORKSPACE.resolve()):
        raise PermissionError(f"越界访问被拒绝: {path}")
    return p


# ---------- 三个最小工具 ----------

def read_file(path: str) -> str:
    lines = _jail(path).read_text(encoding="utf-8", errors="replace").splitlines()
    out = [f"{i + 1}| {line}" for i, line in enumerate(lines[:200])]
    if len(lines) > 200:
        out.append(f"... 共 {len(lines)} 行，已截断（输出瘦身雏形）")
    return "\n".join(out)


def write_file(path: str, content: str) -> str:
    p = _jail(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"ok: 已写入 {path}（{len(content)} 字符）"


DANGEROUS = ("rm -rf", "mkfs", "shutdown", "reboot", ":(){")


def run_shell(command: str, timeout: int = 60) -> str:
    if any(bad in command for bad in DANGEROUS):
        return "error: 命中危险命令拦截（风险分级雏形）"
    try:
        proc = subprocess.run(
            command, shell=True, cwd=WORKSPACE,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"error: 执行超时（{timeout}s）"
    out = f"exit={proc.returncode}\n{proc.stdout}{proc.stderr}"
    return out if len(out) <= 4000 else out[:4000] + "\n... 输出已截断"


# ---------- Schema：随请求发给模型，告诉它「有哪些系统调用」 ----------

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
        "description": "将内容整体写入工作区内文件（不存在则创建，会覆盖同名文件）",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"},
                           "content": {"type": "string"}},
            "required": ["path", "content"],
        }}},
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "在工作区内执行 shell 命令，返回退出码和输出（有超时与截断）",
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
