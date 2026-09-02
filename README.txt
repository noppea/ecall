ecall —— 编程智能体（coding agent）

仓库：https://github.com/noppea/ecall

运行方法：
pip install -r requirements.txt
export ECALL_BASE_URL=<OpenAI 兼容网关地址>
export ECALL_API_KEY=<你的 key>
export ECALL_MODEL=deepseek-v4-flash
python3 main.py run "任务描述"     # 单发任务
python3 main.py chat               # 交互模式（--resume 恢复会话）
可选：ECALL_BWRAP=1 启用沙箱；python3 -m unittest discover -s tests 运行 55 个测试。

特色功能：
1. 上下文即内存：逼近预算时把较早的工具输出换出到 .ecall-swap/（可 read_file 拉回），原地留下带路径的占位符——压缩是"换页"而非"删除"。
2. digest 即时自我压缩：模型读完大输出当场写下笔记，换出时笔记取代原文成为记忆；强制由 runtime 状态机执行（欠账标记+批界冻结），笔记可与后续操作同批发出。实测 16k 预算下溢价仅 1.09 倍（优化前近 4 倍）。
3. 安全纵深：文件操作过路径监狱；shell 经风险分级+bubblewrap 沙箱（根只读、遮蔽家目录、断网、密钥隔离），无沙箱时审批门接管。
4. 只读子代理：探索外包给独立上下文，只带回结论；权限收缩双层强制；预算全局实时熔断防"fork 炸弹"；可并行扇出。
5. 可观测可恢复：JSONL 轨迹逐行刷盘（WAL），支持崩溃恢复、时间旅行回放与编辑回滚；四类终止条件。
6. 模型无关：换模型只改三个环境变量，已实测 DeepSeek 与 Qwen。

实验：evals/ 内有水位线扫描、消融对照、崩溃恢复与多模型对比，数据与复现命令见仓库 README.md。