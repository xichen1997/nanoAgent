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
在发起 LLM 请求时，Agent 提供了一份 JSON schema 定义了 `run_code_in_sandbox` 这个工具。
当模型遇到它认为需要运行代码或命令行才能解决的问题时，它会输出工具调用请求 `tool_use`，包括待执行的 `command` 参数。
代码里的 `handle_tool_call` 方法此时会拦截，执行真正的业务逻辑，将 `command` 传递给运行中的 OpenSandbox 容器，并把执行后的标准输出日志 (stdout/stderr) 打包成字符串返回。

### 3. OpenSandbox 运行时封装
Agent 在启动初期，会调用官方封装的 Python SDK (`Sandbox.create`) 异步连接外部的 OpenSandbox 服务端，获取一个完全隔离的容器环境（默认 `ubuntu:22.04`）。
所有的代码和交互都在这个云端或本地隔离的命名空间中发生，彻底避免智能体生成的破环性指令或者死循环代码直接影响宿主机或宿主环境。
在退出时（触发 `quit` 或收到 SIGINT），脚本会自动回收（`sandbox.kill()`）清理容器资源。

---

**总结：** 这个 Agent 的本质是一个无尽的 REPL (Read-Eval-Print Loop) 循环，其中“代码逻辑的大脑”是大模型，“执行结果的双手”是 OpenSandbox。它可以自行调试错误、查看环境、重写代码，最终向用户返回人类可读的最终验证结果。
