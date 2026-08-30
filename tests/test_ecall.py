"""ecall 离线单元测试——不联网、不调 API，stdlib unittest 零依赖。

    python3 -m unittest discover tests -v

测试哲学：每个测试对应 DESIGN.md 里的一条设计决策。
模型不可离线测，但 harness 的每一条规则都可以、也必须可以。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent
import context
import timetravel
import tools


class WorkspaceCase(unittest.TestCase):
    """每个用例一个独立临时工作区，并把 tools.WORKSPACE 指过去。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ecall-test-")
        self.ws = Path(self._tmp.name).resolve()
        self._old_ws = tools.WORKSPACE
        tools.WORKSPACE = self.ws

    def tearDown(self):
        tools.WORKSPACE = self._old_ws
        self._tmp.cleanup()


# ---------- 工作区 jail（§安全边界） ----------

class TestJail(WorkspaceCase):
    def test_normal_path_ok(self):
        self.assertEqual(tools._jail("a/b.txt"), self.ws / "a/b.txt")

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(PermissionError):
            tools._jail("../outside.txt")

    def test_absolute_escape_rejected(self):
        with self.assertRaises(PermissionError):
            tools._jail("/etc/passwd")

    def test_sneaky_dotdot_rejected(self):
        with self.assertRaises(PermissionError):
            tools._jail("sub/../../outside.txt")


# ---------- 文件读写（§工具层） ----------

class TestFileIO(WorkspaceCase):
    def test_write_read_roundtrip(self):
        tools.write_file("hello.txt", "你好 ecall")
        self.assertIn("你好 ecall", tools.read_file("hello.txt"))

    def test_write_nested_workspace_name_guard(self):
        """模型最常犯的错——把写进 ./xxx/ 写成 xxx/xxx/。
        write_file 必须拦下并给出纠正提示，而不是真的造出嵌套目录。"""
        msg = tools.write_file(f"{self.ws.name}/a.txt", "x")
        self.assertIn("error", msg)
        self.assertIn(self.ws.name, msg)  # 提示里要指名道姓
        self.assertFalse((self.ws / self.ws.name).exists())

    def test_list_dir_root_is_dot(self):
        """根目录必须显示 ./ 而不是工作区同名——
        这个 bug 是评测钓出来的（full 组 22/24 → 24/24），回归测试锁死它。"""
        tools.write_file("a.txt", "x")
        out = tools.list_dir(".")
        first_line = out.splitlines()[0]
        self.assertNotIn(self.ws.name, first_line)

    def test_read_caps_long_file(self):
        tools.write_file("big.txt", "\n".join(f"line{i}" for i in range(5000)))
        out = tools.read_file("big.txt")
        self.assertLess(len(out.splitlines()), 5000 + 5)  # 截断 + 提示行


# ---------- edit_file 两级匹配（§工具层） ----------

class TestEdit(WorkspaceCase):
    def setUp(self):
        super().setUp()
        tools.write_file("m.py", "def add(a, b):\n    return a - b\n")

    def test_exact_replace(self):
        r = tools.edit_file("m.py", "return a - b", "return a + b")
        self.assertIn("exact", r)
        self.assertIn("return a + b", tools.read_file("m.py"))

    def test_fuzzy_rstrip(self):
        """模型最常见的错误：它给出的 old_string 行尾多了空格。
        fuzzy 层忽略行尾空白后应匹配成功。
        （反过来——文件行尾有空格、old_string 没有——exact 子串匹配本就能命中。
        注意 fuzzy 保守地只容忍行尾空白：缩进必须一致，
        容忍缩进差异会把错误缩进静默写进文件。）"""
        r = tools.edit_file("m.py", "    return a - b   ", "    return a + b")
        self.assertIn("fuzzy", r)
        self.assertIn("return a + b", tools.read_file("m.py"))

    def test_ambiguous_match_rejected(self):
        """多处匹配必须拒绝并逼模型提供更多上下文——
        宁可报错也不能猜（猜错就是静默写坏代码）。"""
        tools.write_file("d.py", "x = 1\ny = 1\n")
        r = tools.edit_file("d.py", " = 1", " = 2")
        self.assertIn("多处", r)

    def test_miss_returns_diagnostic(self):
        r = tools.edit_file("m.py", "不存在的内容", "x")
        self.assertIn("error", r)

    def test_edit_nonexistent_file(self):
        r = tools.edit_file("ghost.py", "a", "b")
        self.assertIn("write_file", r)  # 提示应指向正确的工具

    def test_edit_is_atomic(self):
        """写入走 tmp+rename：工作区里不该残留 .ecalltmp 文件。"""
        tools.edit_file("m.py", "return a - b", "return a + b")
        self.assertFalse(list(self.ws.glob("*.ecalltmp")))


# ---------- 命令分级（§威胁模型外圈） ----------

class TestClassify(unittest.TestCase):
    def test_readonly(self):
        for cmd in ["ls -la", "cat a.py", "grep -r foo .", "find . -name x"]:
            self.assertEqual(tools._classify(cmd), "readonly", cmd)

    def test_mutating(self):
        for cmd in ["touch a", "mkdir d", "python3 m.py", "pip install x"]:
            self.assertEqual(tools._classify(cmd), "mutating", cmd)

    def test_dangerous(self):
        for cmd in ["rm -rf /", "rm -fr *", "shutdown now"]:
            self.assertEqual(tools._classify(cmd), "dangerous", cmd)

    def test_pipeline_worst_segment_wins(self):
        """管道里有一段是 mutating，整条就是 mutating。"""
        self.assertEqual(tools._classify("cat log | tee out.txt"), "mutating")

    def test_known_limitation_redirect_is_readonly(self):
        """特征化测试：锁定已知局限（分类器看不懂重定向）。
        这不是在背书这个行为，而是在行为变更时强迫测试先失败——
        安全边界由 bwrap 强制，分类只是审计信息（DESIGN §3）。"""
        self.assertEqual(tools._classify("echo hi > a.txt"), "readonly")


# ---------- bwrap 沙盒（§威胁模型内圈） ----------

class TestBwrap(WorkspaceCase):
    def test_argv_mount_order(self):
        """挂载顺序即安全策略：后挂载覆盖先挂载。
        ro-bind / 必须先于 tmpfs $HOME，tmpfs 必须先于工作区 bind。"""
        argv = tools._bwrap_argv("true")
        s = " ".join(argv)
        self.assertLess(s.index("--ro-bind / /"), s.index("--tmpfs"))
        self.assertLess(s.index("--tmpfs"), s.index("--bind"))
        self.assertIn("--unshare-net", argv)
        self.assertIn("--die-with-parent", argv)

    def test_argv_strips_api_keys(self):
        """密钥只属于宿主进程（真实事故催生的设计）。
        --unsetenv 只针对环境里实际存在的 key 生成，测试时先种一个。"""
        import os
        os.environ["ECALL_API_KEY"] = "sk-test-dummy"
        try:
            s = " ".join(tools._bwrap_argv("true"))
        finally:
            del os.environ["ECALL_API_KEY"]
        i = s.index("--unsetenv")
        self.assertIn("ECALL_API_KEY", s[i:])
        self.assertNotIn("sk-test-dummy", s)  # 值绝不许出现在命令行里

    def test_argv_workspace_writable_bind(self):
        argv = tools._bwrap_argv("true")
        i = argv.index("--bind")
        self.assertEqual(argv[i + 1], str(self.ws))  # 工作区是唯一可写


# ---------- execute 白名单（§子代理权限收缩） ----------

class TestWhitelist(WorkspaceCase):
    def test_subagent_cannot_write(self):
        """白名单必须在 execute 层强制——不靠 Schema 里没给。"""
        allowed = ("read_file", "list_dir", "grep", "glob")
        r = tools.execute("write_file", json.dumps({"path": "x", "content": "y"}),
                          allowed=allowed)
        self.assertIn("error", r)
        self.assertFalse((self.ws / "x").exists())

    def test_subagent_cannot_recurse(self):
        """套娃封堵第二层：白名单里不含 explore，execute 层再拦一次。"""
        allowed = ("read_file", "list_dir", "grep", "glob")
        r = tools.execute("explore", json.dumps({"task": "t"}), allowed=allowed)
        self.assertIn("error", r)


# ---------- 上下文压缩与 swap（§上下文管理） ----------

class TestContext(WorkspaceCase):
    def test_swap_content_addressed_dedup(self):
        """内容寻址：同一内容换出两次只落一个文件（git 对象模型）。"""
        p1 = context._swap_out("x" * 1000)
        p2 = context._swap_out("x" * 1000)
        self.assertEqual(p1, p2)
        self.assertEqual(len(list((self.ws / ".ecall-swap").glob("*.txt"))), 1)

    def test_compress_placeholder_points_to_swap(self):
        """占位符必须给出拉回路径——无损压缩的承诺（DESIGN §2）。"""
        big = "数据" * 500  # 超过 MIN_COMPRESS_CHARS
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
        for i in range(6):
            msgs += [{"role": "assistant", "content": "",
                      "tool_calls": [{"id": f"c{i}", "type": "function",
                                      "function": {"name": "read_file",
                                                   "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
        _msgs, events = context.compress(msgs)
        self.assertGreaterEqual(len(events), 1)
        compressed = [m for m in msgs if m.get("role") == "tool"
                      and "已换出到" in str(m.get("content"))]
        self.assertTrue(compressed)
        self.assertIn(".ecall-swap/", compressed[0]["content"])
        # 最近 KEEP_RECENT_TOOL_RESULTS 条保持原文（模型近因效应）
        self.assertIn("数据", msgs[-1]["content"])

    def test_estimate_tokens_monotonic(self):
        a = [{"role": "user", "content": "短"}]
        b = a + [{"role": "assistant", "content": "长" * 100}]
        self.assertGreater(context.estimate_tokens(b), context.estimate_tokens(a))


# ---------- steer 转向门（§主循环安全点） ----------

class TestSteer(WorkspaceCase):
    def test_consume_and_burn(self):
        (self.ws / ".ecall-steer").write_text("改用迭代写法")
        self.assertEqual(agent._poll_steer(), "改用迭代写法")
        self.assertFalse((self.ws / ".ecall-steer").exists())  # 一次性：消费即焚

    def test_absent_returns_none(self):
        self.assertIsNone(agent._poll_steer())

    def test_steer_file_hidden_from_tools(self):
        """自污染防护：agent 不该在 list_dir 里看到自己的机制文件。"""
        (self.ws / ".ecall-steer").write_text("x")
        (self.ws / "normal.txt").write_text("y")
        out = tools.list_dir(".")
        self.assertIn("normal.txt", out)
        self.assertNotIn(".ecall-steer", out)


# ---------- 轨迹重建（§时间旅行） ----------

class TestRebuild(WorkspaceCase):
    def _write_log(self, events):
        log = self.ws / "log.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in events))
        return str(log)

    def test_tool_call_pairing(self):
        """tool 事件没存 tool_call_id，必须按顺序配对——配错一个全链路错位。"""
        log = self._write_log([
            {"type": "task", "content": "T"},
            {"type": "llm", "step": 1, "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "c2", "type": "function",
                 "function": {"name": "grep", "arguments": "{}"}}]},
            {"type": "tool", "step": 1, "result": "R1"},
            {"type": "tool", "step": 1, "result": "R2"},
        ])
        msgs = timetravel.rebuild_messages(log, to_step=1)
        tools_msgs = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools_msgs], ["c1", "c2"])
        self.assertEqual([m["content"] for m in tools_msgs], ["R1", "R2"])

    def test_rebuild_does_not_mutate_logged_calls(self):
        """别名陷阱回归：配对时 pop(0) 不允许掏空轨迹里的原始列表。"""
        calls = [{"id": "c1", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}}]
        log = self._write_log([
            {"type": "task", "content": "T"},
            {"type": "llm", "step": 1, "content": "", "tool_calls": calls},
            {"type": "tool", "step": 1, "result": "R"},
        ])
        timetravel.rebuild_messages(log, to_step=1)
        self.assertEqual(len(calls), 1)  # 原列表完好

    def test_sub_events_excluded(self):
        """子代理事件不属于父历史：父上下文里只有 explore 的结论。"""
        log = self._write_log([
            {"type": "task", "content": "T"},
            {"type": "llm", "step": 1, "content": "父", "sub": True},
            {"type": "llm", "step": 1, "content": "父本体"},
        ])
        msgs = timetravel.rebuild_messages(log, to_step=1)
        contents = [m.get("content") for m in msgs]
        self.assertIn("父本体", contents)
        self.assertNotIn("父", contents)

    def test_steer_reinserted_at_position(self):
        """转向指令当时注入过历史，重建时必须回到原来的位置。"""
        log = self._write_log([
            {"type": "task", "content": "T"},
            {"type": "llm", "step": 1, "content": "A1"},
            {"type": "steer", "step": 1, "content": "换方向"},
            {"type": "llm", "step": 2, "content": "A2"},
        ])
        msgs = timetravel.rebuild_messages(log, to_step=2)
        roles_contents = [(m["role"], m.get("content") or "") for m in msgs]
        i_steer = next(i for i, (r, c) in enumerate(roles_contents)
                       if "换方向" in c)
        i_a2 = next(i for i, (r, c) in enumerate(roles_contents) if c == "A2")
        self.assertLess(i_steer, i_a2)
        self.assertEqual(msgs[i_steer]["role"], "user")


if __name__ == "__main__":
    unittest.main()
