# OpenSandbox Agent 架构与逻辑说明

这是一个利用大语言模型（LLM）的**工具调用 (Tool Calling)** 能力与 [OpenSandbox](https://github.com/kubernetes-sigs/agent-sandbox) 结合，在安全的隔离环境中执行任意代码或指令的智能体 (Agent)。

## 核心流程图
![alt text](<pics/Screenshot 2026-03-04 at 1.17.50 AM.png>)

## 核心组件解析

### 1. 内部记忆 (Internal Memory)
脚本通过一个名为 `messages` 的 List 数组在内存中维护多轮对话的上下文。
记忆严格遵循 Anthropic API 的消息格式规范：
- `role: "user"`：包含用户的自然语言指令，或是上一步工具调用返回的结果（`type: "tool_result"`）。
- `role: "assistant"`：包含大模型的正常文本回复，或是大模型发出的工具调用请求（`type: "tool_use"`）。

这保证了模型在决定下一步行动时，知道之前用户说过了什么，自己执行过了什么代码，以及那段代码返回了什么报错或结果。

### 2. 工具注册与调用 (Tool Calling)
在发起 LLM 请求时，Agent 放弃了传统的“黑盒执行”风格，取而代之地提供了细粒度**按副作用 (Effect) 类型**划分的工具集。通过在 JSON Schema 层面上的强制切分，来逼迫 LLM 做出语义明确的调用动作：

- **`sandbox_no_side_effects`**: 只读操作，无副作用。在断点恢复(Replay)时将被跳过执行，直接使用历史缓存。
- **`sandbox_action_replayable`**: 可在沙盒内安全重复执行的操作（如修改文件、安装软件）。在断点恢复(Replay)时会被重新执行以恢复环境。
- **`sandbox_action_irreversible`**: 绝对不可重放的操作（如执行危险 Bash）。在断点恢复(Replay)时将被强行跳过，直接返回伪造的历史响应。
- **`gateway_fetch_url`**: 特殊宿主工具，也是能力控制网关（Capability Gateway）的核心。Agent 的模型被禁止在沙盒内部直接执行下载和爬虫命令（防止越权发包），所有的 HTTP 请求必须通过这个位于 `agent.py` 的专门工具代发。不管触发的是 GET 还是 POST 请求，网关都会视作产生不可控外部交互的危险副作用，并将其底层标记为 `action_irreversible`。在断点恢复 (Time Travel) 时，网关**严厉阻截二次网络发包**，强制喂回旧的历史记录。

当模型决定行动时，代码里的 `handle_tool_call` 方法会拦截这些请求，按需在运行中的 OpenSandbox 容器执行并采集日志。不仅如此，这些请求的“副作用类别 `effect_type`” 将被显式标注。

### 3. OpenSandbox 运行时封装
Agent 在启动初期，会调用官方封装的 Python SDK (`Sandbox.create`) 异步连接外部的 OpenSandbox 服务端，获取一个完全隔离的容器环境（默认 `ubuntu:22.04`）。
所有的代码和交互都在这个云端或本地隔离的命名空间中发生，彻底避免智能体生成的破环性指令或者死循环代码直接影响宿主机或宿主环境。
在退出时（触发 `quit` 或收到 SIGINT），脚本会自动回收（`sandbox.kill()`）清理容器资源。

### 4. 调试模式与强化学习 (RL) 数据收集
为了支持后续的强化学习、微调或复杂的错误排查，Agent 内置了 `TrajectoryLogger` 机制（调试模式）。
通过在启动时传递 `--debug` 参数或设置 `DEBUG_MODE=true` 环境变量，脚本会自动在 `logs/` 目录下创建一个 `trajectory_<timestamp>.jsonl` 文件。
该模式会记录详细的时间序列执行情况：
- **`user_input`**: 用户的原始 Prompt 提问。
- **`llm_generation`**: 大模型每次生成的思维链（Text Content Block）以及工具调用的参数请求（Tool Use Block），包含耗时统计和 Token 消耗。
- **`tool_execution`**: 执行各项沙盒命令的具体结构，包括细粒度的 `effect_type`、生成的原始命令及其实际执行耗时。

### 5. 分叉与恢复 (Forked Recovery)
在长程测试或 Debug 中，大模型或沙盒都有可能崩溃或者陷入死胡同。
Agent 现在允许通过参数重修旧好：`python agent.py --resume logs/trajectory_xxx.jsonl --fork-at <step>`。
它遵循了基于 `effect_type` 差异化的故障重演逻辑：
- **记忆重建**: 从 JSONL 读取 `messages`，重建断点发生前那一微秒的 LLM 历史。
- **环境快进**: 初始化一个空白沙箱，接着按顺序“快进重播”旧轨迹。
  - 对于历史记录里的 `sandbox_no_side_effects` 事件，Agent 绝对禁止重新向环境发出请求（防止由于网络变更或环境时效导致逻辑分岔），直接返回封存的旧 JSONL 输出。
  - 对于历史记录里的 `sandbox_action_irreversible` 以及所有触发 `gateway_fetch_url` 代理网络的事件，因存在不可控外部副作用和网络抖动带来的污染可能，Agent 同样拦截其实际执行，转而投喂旧 JSONL 输出制造“执行成功”幻象。
  - 对于包含环境破坏但安全的 `sandbox_action_replayable` 事件，Agent 采取强力的 `bash` 快进重放，迫使新的容器强行演化成断点前的物理环境状态。

### 6. 灾难级自动愈合 (Auto-Recovery & Healing)
随着系统的推进，在长时间无人值守执行中遭遇大模型网络超时、API 闪断、或是沙盒物理崩溃是非常正常的。系统内置了被称为“不死鸟循环”的外层 `run_agent_session` 护城河。
- 当系统探测到运行时异常时，会自动销毁并回收旧容器。
- 随后，程序自动接管并在后台开启重演逻辑：**提取导致崩溃那一刻之前的绝大部分完好记忆**（即精准丢弃最后那一条执行失败或酝酿灾难的 `llm_generation` 日志）。系统接着便会将这段裁剪好的健康历史物理移植录入到全新的时间戳 `trajectory_<new>.jsonl` 日志体系中。
- 然后它在新开的云物理沙箱内经历飞速的快进重播（Fast-forward Time Travel）。待时间点刚好吻合之后，系统**强制触发（Auto Trigger）**大模型的进一步重思考流程，实现真正的全天候 Agent 续航闭环。

---

**总结：** 这个 Agent 的本质是一个无尽的 REPL (Read-Eval-Print Loop) 循环，其中“代码逻辑的大脑”是大模型，“执行结果的双手”是 OpenSandbox。它可以自行调试错误、查看环境、重写代码，并且内置了不可思议的故障重修系统返回人类可读的最终验证结果。
