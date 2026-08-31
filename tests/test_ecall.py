"""ecall 离线单元测试——不联网、不调 API，stdlib unittest 零依赖。

    python3 -m unittest discover tests -v

测试哲学：每个测试对应 DESIGN.md 里的一条设计决策。
模型不可离线测，但 harness 的每一条规则都可以、也必须可以。
"""
import json
import os
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
        """v6.6：模型最常犯的错——把写进 ./xxx/ 写成 xxx/xxx/。
        write_file 必须拦下并给出纠正提示，而不是真的造出嵌套目录。"""
        msg = tools.write_file(f"{self.ws.name}/a.txt", "x")
        self.assertIn("error", msg)
        self.assertIn(self.ws.name, msg)  # 提示里要指名道姓
        self.assertFalse((self.ws / self.ws.name).exists())

    def test_list_dir_root_is_dot(self):
        """v6.6：根目录必须显示 ./ 而不是工作区同名——
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

    def test_rebuild_reattaches_digest(self):
        """digest 笔记不落轨迹本体，但重建时必须从调用事件里挂回前一条大输出，
        否则 resume/fork 之后压缩退回首行规则，笔记在崩溃点蒸发。"""
        log = self._write_log([
            {"type": "task", "content": "T"},
            {"type": "llm", "step": 1, "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"type": "tool", "step": 1, "result": "X" * 5000},
            {"type": "llm", "step": 2, "content": "", "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "digest",
                              "arguments": json.dumps({"summary": "页表初始化代码"})}}]},
            {"type": "tool", "step": 2, "result": "已记录"},
        ])
        msgs = timetravel.rebuild_messages(log, to_step=2)
        tools_msgs = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual(tools_msgs[0].get("_digest"), "页表初始化代码")
        self.assertNotIn("_digest", tools_msgs[1])  # digest 自己的回执不被误标

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

    def test_rebuild_pairs_by_call_id_when_reordered(self):
        """并行清账把 digest 提前执行：tool 事件顺序 ≠ tool_calls 发出顺序。
        重建必须按 call_id 精确配对，否则 digest 的回执错配给 write 的 id。"""
        log = self._write_log([
            {"type": "task", "content": "T"},
            {"type": "llm", "step": 1, "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"type": "tool", "step": 1, "call_id": "c1", "result": "X" * 5000},
            # 模型同批发出 [write, digest]，runtime 把 digest 提前执行：
            {"type": "llm", "step": 2, "content": "", "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "write_file", "arguments": "{}"}},
                {"id": "c3", "type": "function",
                 "function": {"name": "digest",
                              "arguments": json.dumps({"summary": "笔记"})}}]},
            {"type": "tool", "step": 2, "call_id": "c3", "result": "ok 已记录"},
            {"type": "tool", "step": 2, "call_id": "c2", "result": "ok 已写入"},
        ])
        msgs = timetravel.rebuild_messages(log, to_step=2)
        tools_msgs = [m for m in msgs if m["role"] == "tool"]
        by_id = {m["tool_call_id"]: m for m in tools_msgs}
        self.assertEqual(by_id["c3"]["content"], "ok 已记录")   # 不错配
        self.assertEqual(by_id["c2"]["content"], "ok 已写入")
        self.assertEqual(by_id["c1"].get("_digest"), "笔记")      # 笔记贴对观察

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


# ---------- todo：模型的外部记忆（§上下文漂移） ----------

class TestTodo(WorkspaceCase):
    def tearDown(self):
        tools.TODO = []  # 会话级状态，用例间必须清零
        super().tearDown()

    def test_update_and_render(self):
        r = tools.execute("todo", json.dumps({"items": [
            {"content": "读代码", "status": "done"},
            {"content": "修 bug", "status": "doing"}]}))
        self.assertIn("[x] 读代码", r)
        self.assertIn("[~] 修 bug", r)

    def test_full_replace_semantics(self):
        """全量替换而非追加：模型对计划有完全控制权。"""
        tools.execute("todo", json.dumps({"items": [{"content": "A", "status": "doing"}]}))
        tools.execute("todo", json.dumps({"items": [{"content": "B", "status": "done"}]}))
        self.assertEqual(len(tools.TODO), 1)
        self.assertEqual(tools.TODO[0]["content"], "B")

    def test_bad_status_normalized(self):
        """模型乱填 status 不该炸——归一化为 pending。"""
        tools.execute("todo", json.dumps({"items": [{"content": "A", "status": "胡说"}]}))
        self.assertEqual(tools.TODO[0]["status"], "pending")

    def test_cap_items(self):
        items = [{"content": f"t{i}", "status": "pending"} for i in range(50)]
        tools.execute("todo", json.dumps({"items": items}))
        self.assertEqual(len(tools.TODO), tools.TODO_MAX_ITEMS)


# ---------- digest：模型笔记占位符（§即时自我压缩） ----------

class TestDigest(WorkspaceCase):
    def test_digest_tool_validation(self):
        self.assertIn("ok", tools.execute("digest", json.dumps({"summary": "重点在 42 行"})))
        self.assertIn("error", tools.execute("digest", json.dumps({"summary": "  "})))

    def test_compress_prefers_model_note(self):
        """有 digest 的观察，压缩占位符用模型笔记而不是首行规则。"""
        big = "噪音行\n" + "x" * 400
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for i in range(6):
            msgs += [{"role": "assistant", "content": "",
                      "tool_calls": [{"id": f"c{i}", "type": "function",
                                      "function": {"name": "read_file",
                                                   "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
        msgs[3]["_digest"] = "关键：bug 在 fetch 的重试逻辑"  # 给最旧那条 tool 观察贴上笔记
        _msgs, events = context.compress(msgs)
        self.assertGreaterEqual(len(events), 1)
        # 被压缩的最旧一条应带着模型笔记
        self.assertIn("模型笔记：关键：bug 在 fetch 的重试逻辑", msgs[3]["content"])
        self.assertIn(".ecall-swap/", msgs[3]["content"])  # 指针还在，原文可拉回

    def test_compress_falls_back_without_digest(self):
        """没贴笔记的观察退回首行规则——机制向下兼容。"""
        big = "首行信息\n" + "x" * 400
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for i in range(6):
            msgs += [{"role": "assistant", "content": "",
                      "tool_calls": [{"id": f"c{i}", "type": "function",
                                      "function": {"name": "read_file",
                                                   "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
        context.compress(msgs)
        self.assertIn("首行：首行信息", msgs[3]["content"])
        self.assertNotIn("模型笔记", msgs[3]["content"])


# ---------- digest 强制政策（§control policy 最高档） ----------

class _FakeMsg:
    """模拟 SDK 返回的消息对象：content + tool_calls。"""
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

class _FakeTC:
    class _Fn:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments
    def __init__(self, name, arguments, _id="c1"):
        self.id = _id
        self.function = self._Fn(name, arguments)
    def model_dump(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name,
                             "arguments": self.function.arguments}}


class TestDigestEnforcement(WorkspaceCase):
    def _run_script(self, script):
        """用剧本假扮模型跑主循环，返回轨迹事件。"""
        import types
        calls = {"i": 0}
        def fake_chat(messages, tools_, max_retries=3, on_token=None):
            msg = script[min(calls["i"], len(script) - 1)]
            calls["i"] += 1
            usage = types.SimpleNamespace(total_tokens=100, model_dump=lambda: {})
            return msg, usage
        import llm as llm_mod
        old = llm_mod.chat
        agent.llm.chat = fake_chat
        os.environ["ECALL_DIGEST_FORCE"] = "1"  # 这两个用例测 enforcement 机制本身，跳过自适应门
        try:
            log = str(self.ws / "t.jsonl")
            agent.run_messages(
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "t"}],
                log_path=log, header={"type": "task", "content": "t"})
            return [json.loads(l) for l in open(log)]
        finally:
            agent.llm.chat = old
            os.environ.pop("ECALL_DIGEST_FORCE", None)

    def test_big_observation_blocks_until_digest(self):
        """剧本：读大文件 → 试图直接写文件（应被强制政策拒绝）→ digest → 完工。"""
        big = "x" * 2000
        (self.ws / "big.txt").write_text(big)
        script = [
            _FakeMsg(tool_calls=[_FakeTC("read_file", '{"path": "big.txt"}')]),
            _FakeMsg(tool_calls=[_FakeTC("write_file", '{"path": "o.txt", "content": "y"}')]),
            _FakeMsg(tool_calls=[_FakeTC("digest", '{"summary": "全是 x"}')]),
            _FakeMsg(content="读完了"),
        ]
        events = self._run_script(script)
        results = [e for e in events if e["type"] == "tool"]
        # 第一发：大观察 + 强制通知
        self.assertIn("强制笔记政策", results[0]["result"])
        # 第二发：write_file 被拒之门外，o.txt 不应存在
        self.assertIn("尚未 digest", results[1]["result"])
        self.assertFalse((self.ws / "o.txt").exists())
        # 第三发：digest 成功，解除挂起
        self.assertIn("ok", results[2]["result"])

    def test_enforcement_off_without_digest_tool(self):
        """digest 不在工具面时（消融组/子代理），强制政策必须整体关闭——
        否则就是死锁：要求用一个不存在的工具。"""
        import types
        big = "x" * 2000
        (self.ws / "big.txt").write_text(big)
        calls = {"i": 0}
        script = [
            _FakeMsg(tool_calls=[_FakeTC("read_file", '{"path": "big.txt"}')]),
            _FakeMsg(tool_calls=[_FakeTC("write_file", '{"path": "o.txt", "content": "y"}')]),
            _FakeMsg(content="done"),
        ]
        def fake_chat(messages, tools_, max_retries=3, on_token=None):
            msg = script[min(calls["i"], len(script) - 1)]
            calls["i"] += 1
            return msg, types.SimpleNamespace(total_tokens=100, model_dump=lambda: {})
        old = agent.llm.chat
        agent.llm.chat = fake_chat
        try:
            schemas = [s for s in tools.SCHEMAS
                       if s["function"]["name"] != "digest"]
            log = str(self.ws / "t2.jsonl")
            agent.run_messages(
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "t"}],
                log_path=log, header={"type": "task", "content": "t"},
                schemas=schemas)
        finally:
            agent.llm.chat = old
        self.assertTrue((self.ws / "o.txt").exists())  # write 未被拦截

    def test_digest_can_batch_with_next_action(self):
        """并行清账（digest 税的第二刀）：digest 与后续操作打在同一批发出时
        整批放行——enforcement 不再白缴一次全上下文往返。
        且批次内顺序无关：digest 被提前执行，write 不再被拒。"""
        big = "x" * 2000
        (self.ws / "big.txt").write_text(big)
        script = [
            _FakeMsg(tool_calls=[_FakeTC("read_file", '{"path": "big.txt"}')]),
            # 同一批、且顺序「错误」（先干活后笔记）——runtime 应重排：
            _FakeMsg(tool_calls=[
                _FakeTC("write_file", '{"path": "o.txt", "content": "y"}', _id="c2"),
                _FakeTC("digest", '{"summary": "全是 x"}', _id="c3"),
            ]),
            _FakeMsg(content="done"),
        ]
        events = self._run_script(script)
        results = [e for e in events if e["type"] == "tool"]
        # 第一发：大观察挂起 + 通知（提示语改为同批发出）
        self.assertIn("强制笔记政策", results[0]["result"])
        self.assertIn("同一批", results[0]["result"])
        # 第二发：digest 被提前执行，成功清账
        self.assertEqual(results[1]["name"], "digest")
        self.assertIn("ok", results[1]["result"])
        # 第三发：write_file 照常执行，不再被拒
        self.assertEqual(results[2]["name"], "write_file")
        self.assertNotIn("尚未 digest", results[2]["result"])
        self.assertTrue((self.ws / "o.txt").exists())

    def test_batch_without_digest_still_rejected(self):
        """整批不含 digest 时维持原判：全部拒绝——并行化不是开闸放水。"""
        big = "x" * 2000
        (self.ws / "big.txt").write_text(big)
        script = [
            _FakeMsg(tool_calls=[_FakeTC("read_file", '{"path": "big.txt"}')]),
            _FakeMsg(tool_calls=[
                _FakeTC("write_file", '{"path": "o.txt", "content": "y"}', _id="c2"),
                _FakeTC("read_file", '{"path": "big.txt"}', _id="c3"),
            ]),
            _FakeMsg(tool_calls=[_FakeTC("digest", '{"summary": "全是 x"}')]),
            _FakeMsg(content="done"),
        ]
        events = self._run_script(script)
        results = [e for e in events if e["type"] == "tool"]
        self.assertIn("尚未 digest", results[1]["result"])  # write 被拒
        self.assertIn("尚未 digest", results[2]["result"])  # read 也被拒
        self.assertFalse((self.ws / "o.txt").exists())
        self.assertIn("ok", results[3]["result"])  # digest 清账

    def test_mid_batch_arming_does_not_reject_siblings(self):
        """批内埋雷不炸同批（实弹抓到的 bug）：模型并行发 [read A, read B]
        时账是清的，A 落地大输出才挂起——同批的 B 不该被拒，
        否则每次误拒 = 模型白烧一轮全上下文往返重发。旧账下批再追。"""
        big = "x" * 2000
        (self.ws / "a.txt").write_text(big)
        (self.ws / "b.txt").write_text(big)
        script = [
            _FakeMsg(tool_calls=[
                _FakeTC("read_file", '{"path": "a.txt"}', _id="c1"),
                _FakeTC("read_file", '{"path": "b.txt"}', _id="c2"),
            ]),
            _FakeMsg(tool_calls=[_FakeTC("digest", '{"summary": "两坨 x"}')]),
            _FakeMsg(content="done"),
        ]
        events = self._run_script(script)
        results = [e for e in events if e["type"] == "tool"]
        self.assertIn("强制笔记政策", results[0]["result"])   # a.txt 挂起+通知
        self.assertNotIn("尚未 digest", results[1]["result"])  # b.txt 不被追溯
        self.assertIn("ok", results[2]["result"])              # 下一批 digest 清账

    def test_enforcement_is_adaptive(self):
        """自适应强制（水位线扫描实验的产物）：没发生压缩时不缴保费
        （大观察不挂起）；首次压缩事件之后 enforcement 才上岗。"""
        import types
        big = "x" * 2000
        (self.ws / "big.txt").write_text(big)
        (self.ws / "big2.txt").write_text(big)
        os.environ.pop("ECALL_DIGEST_FORCE", None)  # 确保走自适应路径
        script = [
            _FakeMsg(tool_calls=[_FakeTC("read_file", '{"path": "big.txt"}')]),
            _FakeMsg(tool_calls=[_FakeTC("read_file", '{"path": "big2.txt"}')]),
            _FakeMsg(tool_calls=[_FakeTC("write_file", '{"path": "o.txt", "content": "y"}')]),
            _FakeMsg(tool_calls=[_FakeTC("digest", '{"summary": "两坨 x"}')]),
            _FakeMsg(content="done"),
        ]
        calls = {"i": 0}
        def fake_chat(messages, tools_, max_retries=3, on_token=None):
            msg = script[min(calls["i"], len(script) - 1)]
            calls["i"] += 1
            return msg, types.SimpleNamespace(total_tokens=100, model_dump=lambda: {})
        mc_calls = {"i": 0}
        def fake_mc(messages):
            mc_calls["i"] += 1
            # 从第 2 步起声称发生了压缩（换出开始，笔记有了保值义务）
            return messages, ([{"e": 1}] if mc_calls["i"] >= 2 else [])
        old_chat, old_mc = agent.llm.chat, agent.context.maybe_compress
        agent.llm.chat = fake_chat
        agent.context.maybe_compress = fake_mc
        try:
            log = str(self.ws / "t3.jsonl")
            agent.run_messages(
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "t"}],
                log_path=log, header={"type": "task", "content": "t"})
            events = [json.loads(l) for l in open(log)]
        finally:
            agent.llm.chat = old_chat
            agent.context.maybe_compress = old_mc
        results = [e for e in events if e["type"] == "tool"]
        # 第 1 次读：压缩未发生 → 不挂起、无强制通知
        self.assertNotIn("强制笔记政策", results[0]["result"])
        # 第 2 次读：压缩已发生 → 挂起 + 通知
        self.assertIn("强制笔记政策", results[1]["result"])
        # write 被拒，直到 digest 清账
        self.assertIn("尚未 digest", results[2]["result"])
        self.assertFalse((self.ws / "o.txt").exists())


# ---------- 预算全局熔断（§fork 炸弹修复） ----------

class TestBudgetCircuitBreaker(WorkspaceCase):
    def _run_with_usage(self, tokens_per_call, sub=False):
        import types
        def fake_chat(messages, tools_, max_retries=3, on_token=None):
            return _FakeMsg(content="想"), types.SimpleNamespace(
                total_tokens=tokens_per_call, model_dump=lambda: {})
        old = agent.llm.chat
        agent.llm.chat = fake_chat
        try:
            log = str(self.ws / "b.jsonl")
            agent.run_messages(
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "t"}],
                log_path=log, header={"type": "task", "content": "t"},
                max_steps=10, _sub=sub)
            return [json.loads(l) for l in open(log)]
        finally:
            agent.llm.chat = old

    def test_breaker_trips_immediately(self):
        """单次调用就烧穿预算 → 第 1 步立即熔断，不会活到第 2 步。"""
        events = self._run_with_usage(agent.MAX_TOTAL_TOKENS + 1)
        aborts = [e for e in events if e["type"] == "abort"]
        self.assertTrue(aborts)
        self.assertEqual(aborts[0]["reason"], "token_budget")
        self.assertEqual(len([e for e in events if e["type"] == "llm"]), 1)

    def test_subagent_shares_global_pool(self):
        """子代理循环查同一个全局计数器：余额不足时第一步就该熔断，
        而不是回家才交账单（fork 炸弹的修复回归测试）。"""
        agent._TOTAL_SPENT[0] = agent.MAX_TOTAL_TOKENS - 50  # 预算只剩 50
        events = self._run_with_usage(1000, sub=True)
        aborts = [e for e in events if e["type"] == "abort"]
        self.assertTrue(aborts)
        self.assertEqual(len([e for e in events if e["type"] == "llm"]), 1)

    def test_top_level_resets_pool(self):
        """顶层任务重置预算池：上一个任务的花费不该拖累下一个。"""
        agent._TOTAL_SPENT[0] = 10**9
        events = self._run_with_usage(100)  # 顶层（sub=False）应先清零
        # 池子被重置，100 tokens 远未超限 → 不该有 budget abort
        self.assertFalse([e for e in events if e.get("reason") == "token_budget"])


if __name__ == "__main__":
    unittest.main()
