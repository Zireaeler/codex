# Codex 项目工作原理全景（Rust 主实现，面向 Rust 初学者）

## 1. 这个项目到底做什么
一句话版本：
Codex 是一个“会读代码、会改代码、会跑命令、会把过程流式回传给前端”的本地代码代理系统。

它不是单一 UI，而是一套“引擎 + 多入口”架构：
- 你可以用终端全屏 TUI（`codex-rs/tui`）。
- 你可以用非交互执行（`codex-rs/exec`）。
- 你可以让 IDE 通过 JSON-RPC 接入（`codex-rs/app-server`）。
- 它们最终都走到同一个核心引擎（`codex-rs/core`）。

---

## 2. 仓库级架构（先看全景）

| 目录 | 角色 | 备注 |
| --- | --- | --- |
| `codex-rs/` | Rust 主实现（当前维护主线） | 你这次最该重点读 |
| `codex-cli/` | 旧版 TypeScript CLI | README 明确标注 legacy |
| `sdk/typescript/` | TS SDK（通过启动 CLI + JSONL 交互） | 面向程序化调用 |
| `docs/` | 用户文档 | 配置、技能、安装等 |
| `shell-tool-mcp/` | 独立 MCP server（shell 工具） | 实验性，给 Codex 扩展能力 |

所以“完整理解项目”时，你可以把它看成：
1. `codex-rs/core` 是发动机。
2. `tui/exec/app-server` 是不同驾驶舱。
3. `protocol` 是统一线缆标准（操作和事件格式）。

---

## 3. `codex-rs` 子系统地图（核心）

| crate | 可视化理解 | 主要职责 |
| --- | --- | --- |
| `core` | 大脑和调度中枢 | 会话状态、prompt 构建、流式处理、工具执行、审批流程 |
| `protocol` | 统一消息协议层 | 定义 `Op`（输入操作）和 `EventMsg`（输出事件） |
| `tui` | 终端前端 | 用户输入、显示流式 item/event |
| `exec` | 批处理前端 | 一次性运行任务并输出结果 |
| `app-server` | IDE 网关 | JSON-RPC 双向通信，桥接 core 事件到 IDE 事件 |
| `app-server-protocol` | 网关协议定义 | `turn/start`、`turn/completed` 等 v2 API 类型 |
| `codex-api` | 上游 Responses 适配器 | SSE/WebSocket 事件解析成内部 `ResponseEvent` |

---

## 4. 先认识关键“数据对象”（名字后附形象解释）

| 名字 | 位置 | 形象解释 |
| --- | --- | --- |
| `Op`（操作信封） | `codex-rs/protocol/src/protocol.rs:90` | 前端投给 core 的“我要做什么” |
| `Submission { id, op }`（带单号信封） | `codex-rs/core/src/codex.rs:451` | 每个操作都会有唯一流水号 |
| `EventMsg`（事件广播） | `codex-rs/protocol/src/protocol.rs:862` | core 对外发的“过程和结果” |
| `Session`（线程级控制器） | `codex-rs/core/src/codex.rs:504` | 一个 thread 的总状态和服务容器 |
| `TurnContext`（单轮任务背包） | `codex-rs/core/src/codex.rs:524` | 一次 turn 的配置快照（cwd/model/sandbox 等） |
| `ActiveTurn`（当前工位） | `codex-rs/core/src/state/turn.rs:21` | 当前正在执行的 turn/task |
| `TurnState`（临时收纳盒） | `codex-rs/core/src/state/turn.rs:71` | 等审批、等用户输入、待注入输入等临时状态 |
| `ContextManager`（对话账本） | `codex-rs/core/src/context_manager/history.rs:25` | 内存中的历史记录和 token 估算 |
| `ToolRouter`（工具分诊台） | `codex-rs/core/src/tools/router.rs:31` | 判断是否工具调用，路由到对应 handler |
| `ToolCallRuntime`（工具执行器） | `codex-rs/core/src/tools/parallel.rs:25` | 负责并行/串行、安全取消、返回工具输出 |
| `ModelClientSession`（模型连接会话） | `codex-rs/core/src/client.rs:881` | 一轮中复用传输连接，必要时 WS 回退 HTTP |

---

## 5. 一条 Prompt 的完整生命周期（主线）

下面这条链路是你最关心的“从输入到输出”。

### 5.1 入口：前端把输入包装成 `Op`

典型入口：
- TUI 在 `codex-rs/tui/src/chatwidget.rs:3791` 组装 `Op::UserTurn`。
- exec 在 `codex-rs/exec/src/lib.rs:500` 组装 `Op::UserTurn`。
- app-server v2 的 `turn/start` 在 `codex-rs/app-server/src/codex_message_processor.rs:4899` 最终提交 `Op::UserInput`（必要时先发 `Op::OverrideTurnContext`）。

这一步的核心意义：
统一入口。无论你是 TUI、CLI 还是 IDE，最终都变成协议层的 `Op`。

### 5.2 投递：`submit (投递操作)` 进入核心队列

调用链：
1. `CodexThread::submit (线程门面投递)`  
`codex-rs/core/src/codex_thread.rs:47`
2. `Codex::submit (生成 submission id)`  
`codex-rs/core/src/codex.rs:449`
3. `submit_with_id (发送到 tx_sub)`  
`codex-rs/core/src/codex.rs:458`

这里做了两件事：
- 生成唯一 turn/submission id（后续所有事件都关联它）。
- 把请求异步送入 `tx_sub`，与执行解耦。

### 5.3 收件分发：`submission_loop (收件员循环)`

位置：`codex-rs/core/src/codex.rs:2924`

它持续消费 `rx_sub`，按 `Op` 分发到不同 handler。  
你可以把它理解成“线程内唯一总调度循环”。

`Op::UserInput / Op::UserTurn` 走到：
- `handlers::user_input_or_turn (用户输入统一入口)`  
`codex-rs/core/src/codex.rs:3152`

### 5.4 组装 turn 上下文：`new_turn_with_sub_id (造一份本轮配置快照)`

关键逻辑在 `codex-rs/core/src/codex.rs:1597`：
1. 应用本次配置更新（cwd/sandbox/model/effort/personality...）。
2. 生成 `TurnContext`。
3. 如配置非法，直接发 `EventMsg::Error` 并返回。

这一步为什么重要：
让“每一轮”有稳定配置快照，避免运行中被全局配置漂移污染。

### 5.5 决策：注入到当前 turn 还是启动新 turn

在 `user_input_or_turn` 内：
- 先尝试 `steer_input (向正在运行的 turn 注入输入)`  
`codex-rs/core/src/codex.rs:2661`
- 如果没有活跃 turn，才启动新任务：
  1. `seed_initial_context_if_needed (只播种一次初始上下文)`  
  `codex-rs/core/src/codex.rs:2321`
  2. `build_settings_update_items (把设置变化写进历史)`  
  `codex-rs/core/src/codex.rs:1851`
  3. `spawn_task (启动后台任务执行 turn)`  
  `codex-rs/core/src/tasks/mod.rs:116`

### 5.6 任务层：`spawn_task (工位切换器)` 保证同一时刻只有一个主 turn

`spawn_task` 做的事情非常关键（`codex-rs/core/src/tasks/mod.rs:116`）：
1. `abort_all_tasks(TurnAbortReason::Replaced)`：先停旧任务。
2. 注册 `ActiveTurn`。
3. 在 tokio 后台跑 `SessionTask::run`。
4. 结束后统一调用 `on_task_finished` 发 `TurnComplete`。

对于普通对话任务：
- `RegularTask::run (普通轮次执行器)`  
`codex-rs/core/src/tasks/regular.rs:89`
- 它直接调用 `run_turn`。

### 5.7 真正处理 prompt：`run_turn (本轮主循环)`

位置：`codex-rs/core/src/codex.rs:3956`

主步骤：
1. 发 `TurnStarted` 事件（`3970` 附近）。
2. 做上下文压缩检查（`run_pre_sampling_compact`）。
3. 解析技能/应用 mention，注入技能指令。
4. 记录用户输入到历史和 rollouts（`record_user_prompt_and_emit_turn_item`）。
5. 进入循环：不断发“采样请求”直到本轮结束。

### 5.8 每次采样：`run_sampling_request (一次向模型请求 + 重试策略)`

位置：`codex-rs/core/src/codex.rs:4386`

它会：
1. `built_tools (构建本轮可用工具路由)`。
2. 拼 `Prompt { input, tools, base_instructions, output_schema... }`。
3. 调 `try_run_sampling_request` 真正消费流式输出。
4. 处理重试和回退：
   - 重试预算耗尽后可从 WS 切到 HTTP  
   `try_switch_fallback_transport` in `codex-rs/core/src/client.rs:934`

### 5.9 流式核心：`try_run_sampling_request (边收边处理事件)`

位置：`codex-rs/core/src/codex.rs:4983`

这段是核心中的核心，事件驱动：
- `OutputItemAdded`：发 item started。
- `OutputTextDelta / ReasoningDelta`：推增量给前端。
- `OutputItemDone`：判断是否工具调用并处理结果。
- `Completed`：更新 token 使用，决定是否继续 follow-up。

### 5.10 工具调用链：从模型函数调用到工具输出回灌

路径：
1. `handle_output_item_done (完成项处理器)`  
`codex-rs/core/src/stream_events_utils.rs:46`
2. `ToolRouter::build_tool_call (识别工具调用)`  
`codex-rs/core/src/tools/router.rs:63`
3. `ToolCallRuntime::handle_tool_call (执行控制器)`  
`codex-rs/core/src/tools/parallel.rs:50`
4. `ToolRegistry::dispatch (按工具名找 handler)`  
`codex-rs/core/src/tools/registry.rs:76`
5. 得到 `ResponseInputItem`，写回历史，下一轮继续发给模型。

并行策略：
- 支持并行的工具走读锁（可并发）。
- 不支持并行的工具走写锁（串行）。
- 通过 `parallel_execution: RwLock<()>` 控制。  
`codex-rs/core/src/tools/parallel.rs:30`

### 5.11 审批与用户输入：core 等待、前端回答、再回 core

core 发出请求事件：
- `ExecApprovalRequest`
- `ApplyPatchApprovalRequest`
- `RequestUserInput`

这些 pending 回调先存入 `TurnState`（`pending_approvals/pending_user_input`）：
- `codex-rs/core/src/state/turn.rs:72`

app-server 桥接逻辑：
- `apply_bespoke_event_handling (事件适配层)`  
`codex-rs/app-server/src/bespoke_event_handling.rs:106`

用户在前端点了同意/拒绝后，app-server 会回提 `Op`：
- `Op::ExecApproval`
- `Op::PatchApproval`
- `Op::UserInputAnswer`

对应代码：
- `on_command_execution_request_approval_response`  
`codex-rs/app-server/src/bespoke_event_handling.rs:1687`
- `on_request_user_input_response`  
`codex-rs/app-server/src/bespoke_event_handling.rs:1492`

### 5.12 收尾：完成、失败、打断

正常完成：
1. `run_turn` 返回 `last_agent_message`。
2. `spawn_task` 统一调用 `on_task_finished`。
3. 发送 `EventMsg::TurnComplete`。  
`codex-rs/core/src/tasks/mod.rs:196`

中断流程：
1. `Op::Interrupt` 进入 `submission_loop`。
2. `abort_all_tasks` 取消任务并清理。
3. 发 `EventMsg::TurnAborted`。  
`codex-rs/core/src/tasks/mod.rs:306`

app-server 侧会把 `TurnAborted` 转成：
- pending `turn/interrupt` 请求的响应
- `turn/completed` with `Interrupted` 状态  
`codex-rs/app-server/src/bespoke_event_handling.rs:1044` 和 `1335`

---

## 6. 数据流（类型如何一路变形）

从“用户文字”到“最终 item”的类型轨迹：

1. `Vec<UserInput>`（前端输入形态）  
来源：TUI/exec/app-server 入口。

2. `Op::UserTurn / Op::UserInput`（协议操作形态）  
进入 submission 队列。

3. `ResponseInputItem`（可发给模型的输入形态）  
例如用户消息、函数输出。

4. `ResponseItem`（模型和历史中的统一记录形态）  
被 `ContextManager` 管理。

5. `TurnItem` / `ThreadItem`（UI 展示形态）  
通过 `ItemStarted/ItemCompleted` 和 delta 事件流向前端。

为什么要多层类型：
- `UserInput` 适合入口表达。
- `ResponseItem` 适合模型上下文和持久化。
- `TurnItem/ThreadItem` 适合 UI 呈现和状态机。

这是典型“协议层、模型层、展示层分离”。

---

## 7. app-server 在这条链中的位置（IDE 关心）

### 7.1 方法入口

`turn/start` 在协议层定义：
- `codex-rs/app-server-protocol/src/protocol/common.rs:258`
- `codex-rs/app-server-protocol/src/protocol/v2.rs:2169`

### 7.2 请求处理

`codex_message_processor` 负责把 JSON-RPC 转 core 调用：
- `turn_start`：`codex-rs/app-server/src/codex_message_processor.rs:4899`
- 监听 core 事件：`conversation.next_event()`  
`codex-rs/app-server/src/codex_message_processor.rs:5421`

### 7.3 事件翻译

app-server 会做两层输出：
1. 原始 `codex/event/*` 通知。
2. v2 结构化通知（`turn/started`、`item/completed`、`turn/completed` 等）。

这层翻译就是 `apply_bespoke_event_handling` 在做。

---

## 8. 设计原因（为什么这样做）

1. `Op/Event` 双向协议统一了多入口，避免每个前端自己管理复杂状态机。
2. `submission_loop` 单点分发，线程内行为更可预测。
3. `Session + TurnContext` 分层：会话级长期状态 vs 单轮快照状态，减少串扰。
4. `ActiveTurn/TurnState` 显式建模 pending 审批和注入输入，便于中断恢复。
5. 历史和 rollouts 同步写入，支持 resume、回放、审计。
6. 流式事件即到即处理，用户可实时看到 delta 和工具进展。
7. 工具执行有并行控制与取消语义，防止互相踩状态。
8. 传输层 WS 优先 + HTTP fallback，提高稳定性。
9. app-server 独立事件适配层，前端协议演进时不必侵入 core。

---

## 9. 给 Rust 初学者的源码阅读路线（按收益排序）

1. 看协议：  
`codex-rs/protocol/src/protocol.rs`  
先理解 `Op` 和 `EventMsg`，后面所有代码都围绕它们。

2. 看主循环：  
`codex-rs/core/src/codex.rs` 的 `submission_loop`、`run_turn`、`try_run_sampling_request`。

3. 看任务生命周期：  
`codex-rs/core/src/tasks/mod.rs` 和 `tasks/regular.rs`。

4. 看工具链：  
`core/src/stream_events_utils.rs`、`tools/router.rs`、`tools/parallel.rs`、`tools/registry.rs`。

5. 看 app-server 桥接：  
`app-server/src/codex_message_processor.rs` 和 `app-server/src/bespoke_event_handling.rs`。

6. 最后看前端入口：  
`tui/src/chatwidget.rs` 和 `exec/src/lib.rs`，对照你熟悉的交互方式理解。

---

## 10. 一句话总结

可以把 Codex 想成一条“工业流水线”：
前端把请求封成 `Op (工单)` 投递给 `core (中控)`，  
`core` 在 `run_turn (生产线)` 中一边和模型流式对话、一边调工具、一边处理审批，  
再把全过程和结果通过 `EventMsg (回执流)` 持续回推到 TUI/CLI/IDE。

