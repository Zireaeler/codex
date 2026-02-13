# Codex Prompt 处理判定手册（工具调用与任务分解）

> 这份文档专门回答 3 个问题：
> 1. 一个 prompt 进来后会走哪条处理路径？
> 2. 什么情况下会调用工具？
> 3. 什么情况下会被分解成多轮或多任务？

## 1. 先给结论（避免先读晕）

1. `core` 并不会先做“语义分类器”把自然语言 prompt 静态分成 N 类。
2. 第一层分流主要由 `Op（操作信封）` 决定，而不是由 prompt 文本本身决定。
3. 常规聊天 prompt 进入后，一般走 `RegularTask（常规回合任务）`，但如果当前已有活跃 turn，会先尝试 `steer_input（把输入注入当前任务）`，不一定新建任务。
4. 工具调用是否发生，取决于两件事：
   - 该轮 prompt 是否把该工具暴露给模型；
   - 模型输出流里是否真的发出了工具调用 item（如 `FunctionCall`）。
5. “分解成若干任务”有 3 层含义：
   - `Op` 级别分流成不同任务类型（如 Review/Compact/Undo）。
   - 同一个 `RegularTask` 内被分解成多轮 sampling（follow-up 循环）。
   - 多代理分解（`spawn_agent` / `resume_agent`）创建子线程。

---

## 2. 第 0 层：先被包装成 `Op（操作信封）`

`Op` 定义在 `codex-rs/protocol/src/protocol.rs:90`。

常见映射：

1. 普通聊天输入：`Op::UserInput` 或 `Op::UserTurn`。
2. `/compact`：`Op::Compact`。
3. `/review`：`Op::Review`。
4. `undo`：`Op::Undo`。
5. `!cmd`：`Op::RunUserShellCommand`。

`submission_loop（总分发循环）` 在 `codex-rs/core/src/codex.rs:2924`，按 `Op` 分发：

- `UserInput/UserTurn` -> `handlers::user_input_or_turn`（`codex-rs/core/src/codex.rs:3152`）
- `Compact` -> `handlers::compact`（`codex-rs/core/src/codex.rs:3033`）
- `Review` -> `handlers::review`（`codex-rs/core/src/codex.rs:3063`）
- `Undo` -> `handlers::undo`（`codex-rs/core/src/codex.rs:3030`）
- `RunUserShellCommand` -> `handlers::run_user_shell_command`（`codex-rs/core/src/codex.rs:3042`）

这就是第一道“分流闸门”。

---

## 3. 常规 prompt 的主路径：`UserInput/UserTurn`

### 3.1 `user_input_or_turn（用户输入统一入口）`

代码：`codex-rs/core/src/codex.rs:3152`

关键动作：

1. 根据 `Op::UserTurn` 或 `Op::UserInput` 组装 `updates（本轮配置更新）`。
2. 调 `new_turn_with_sub_id（创建本轮 TurnContext 快照）`。
3. 先尝试 `steer_input（注入当前活跃任务）`。

### 3.2 何时“不开新任务”，只注入当前任务？

看 `steer_input（注入器）`：`codex-rs/core/src/codex.rs:2661`

满足这些条件就直接注入：

1. `input` 非空；
2. 当前有 `active_turn`；
3. `active_turn` 里有至少一个活跃 task。

成功后输入会进入 `TurnState.pending_input（待注入输入缓冲区）`：
`codex-rs/core/src/state/turn.rs:71` 与 `codex-rs/core/src/codex.rs:2688`。

### 3.3 何时会新建任务？

当 `steer_input` 返回 `NoActiveTurn` 时，才会 `spawn_task`：
`codex-rs/core/src/codex.rs:3215-3232`。

此时会：

1. `seed_initial_context_if_needed（必要时播种初始上下文）`
2. `build_settings_update_items（把设置变化写入历史）`
3. `refresh_mcp_servers_if_requested（按需刷新 MCP）`
4. `spawn_task(..., RegularTask)`

`spawn_task（任务启动器）` 在 `codex-rs/core/src/tasks/mod.rs:116`。

注意：`spawn_task` 会先 `abort_all_tasks(TurnAbortReason::Replaced)`（`codex-rs/core/src/tasks/mod.rs:122`），即默认“一次只保留一个主 turn 任务”。

---

## 4. 工具调用：到底什么时候会发生？

## 4.1 先决条件 A：该轮 prompt 把工具“暴露给模型”

每次 sampling 前会构建 `Prompt（发给模型的请求包）`：
`codex-rs/core/src/codex.rs:4388`。

`Prompt` 里有 `tools` 和 `parallel_tool_calls`：
`codex-rs/core/src/client_common.rs:27`。

工具集合来自 `built_tools（工具构建）`：`codex-rs/core/src/codex.rs:4512`。

工具是否可用由 `ToolsConfig::new（工具总开关与能力映射）` 决定：
`codex-rs/core/src/tools/spec.rs:50`。

再由 `build_specs（工具清单注册）` 落地：
`codex-rs/core/src/tools/spec.rs:1379`。

比如：

1. `request_user_input` 只有 `collaboration_modes_tools` 开启才注册（`spec.rs:1475`）。
2. `spawn_agent/send_input/resume_agent/wait/close_agent` 只有 `collab_tools` 开启才注册（`spec.rs:1551`）。
3. `apply_patch` 取决于 `apply_patch_tool_type`（`spec.rs:1485`）。
4. `shell` 类型取决于 `shell_type`（`spec.rs:1420`）。

## 4.2 先决条件 B：模型输出流里真的发出工具调用 item

流事件处理在 `try_run_sampling_request（流式处理主循环）`：
`codex-rs/core/src/codex.rs:4983`。

当收到 `ResponseEvent::OutputItemDone(item)`，会调用：

- `handle_output_item_done（完成 item 处理器）`：`codex-rs/core/src/stream_events_utils.rs:46`
- 内部 `ToolRouter::build_tool_call（判断是否是工具调用）`：`codex-rs/core/src/tools/router.rs:63`

`build_tool_call` 只把这三类识别为“要本地执行的工具调用”：

1. `ResponseItem::FunctionCall`
2. `ResponseItem::CustomToolCall`
3. `ResponseItem::LocalShellCall`

见 `codex-rs/core/src/tools/router.rs:67-130`。

也就是说：如果只是普通 `Message/Reasoning/WebSearchCall`，不会进入本地工具执行链（见 `handle_non_tool_response_item`，`stream_events_utils.rs:167`）。

## 4.3 真执行时的路由与并发

识别成工具调用后：

1. `ToolCallRuntime::handle_tool_call（工具运行时执行器）` 启动执行 future（`codex-rs/core/src/tools/parallel.rs:50`）。
2. 是否可并行看 `tool_supports_parallel`（`tools/router.rs:55` + `tools/registry.rs:196`）。
3. 最终 `ToolRegistry::dispatch（按工具名派发 handler）`（`codex-rs/core/src/tools/registry.rs:76`）。

补充：

- 若模型叫了未注册工具，会返回 `unsupported call` 给模型，而不是静默执行（`tools/registry.rs:99-114`）。
- 若 payload 类型和 handler 不匹配，会报错（`tools/registry.rs:117-129`）。

## 4.4 为什么工具调用会导致“再来一轮”？

`handle_output_item_done` 在识别到工具调用时会设置：

- `output.needs_follow_up = true`

见 `codex-rs/core/src/stream_events_utils.rs:76`。

在 sampling 完成时还会加一个条件：

- `needs_follow_up |= sess.has_pending_input().await`

见 `codex-rs/core/src/codex.rs:5173`。

所以只要有工具调用结果要回灌，或有新输入待处理，就会继续下一轮 sampling。

---

## 5. “分解成若干任务”到底是哪几种分解？

## 5.1 分解类型 A：按 `Op` 分流成不同 `SessionTask（任务执行器）`

入口仍是 `submission_loop`（`codex-rs/core/src/codex.rs:2924`）。

会 `spawn_task` 的典型路径：

1. 普通聊天：`RegularTask`（`codex-rs/core/src/tasks/regular.rs:25`）
2. Review：`ReviewTask`（`codex-rs/core/src/codex.rs:3877`）
3. Compact：`CompactTask`（`codex-rs/core/src/codex.rs:3573`）
4. Undo：`UndoTask`（`codex-rs/core/src/codex.rs:3566`，其 `kind` 归类为 `TaskKind::Regular`，见 `tasks/undo.rs:30`）
5. 用户 shell（无活跃 turn 时）：`UserShellCommandTask`（`codex-rs/core/src/codex.rs:3261`）

特殊分支：`RunUserShellCommand` 在“已有活跃 turn”时不会新建任务，而是作为 `ActiveTurnAuxiliary（活跃任务附属执行）` 启动（`codex-rs/core/src/codex.rs:3243`）。

## 5.2 分解类型 B：同一个 `RegularTask` 内分解成多轮 sampling

`run_turn（本轮总循环）` 在 `codex-rs/core/src/codex.rs:3956`。

它会一直循环调用 `run_sampling_request`，直到 `needs_follow_up == false`：

- 读 `SamplingRequestResult { needs_follow_up }`（`codex.rs:4137`）
- `if !needs_follow_up { break; }`（`codex.rs:4165`）

所以这不是“多个 task”，而是“同一 task 的多轮迭代”。

## 5.3 分解类型 C：多代理子任务（子线程）

这是通过协作工具触发，不是 core 自动硬编码拆分。

入口 handler：`CollabHandler`（`codex-rs/core/src/tools/handlers/collab.rs:50`）

支持工具名：

1. `spawn_agent`
2. `send_input`
3. `resume_agent`
4. `wait`
5. `close_agent`

见 `collab.rs:78-83`。

触发“真正子代理分解”的核心是 `spawn_agent` 或 `resume_agent`：

- `spawn_agent` 在 `collab.rs:111`，调用 `agent_control.spawn_agent`（`collab.rs:148-156`）
- `resume_agent` 在 `collab.rs:295`，必要时可从 rollout 恢复线程（`collab.rs:382`）

限制条件：

1. 深度限制：`MAX_THREAD_SPAWN_DEPTH = 1`（`codex-rs/core/src/agent/guards.rs:25`）
2. 超过限制会直接返回：`Agent depth limit reached...`（`collab.rs:123-126` 与 `304-307`）
3. 当子代理再往下一层会超限时，会在子代理配置里禁用 `Feature::Collab`，防止无限继续分裂（`collab.rs:832-835`）

---

## 6. `request_user_input` 的特殊判定（常被问）

虽然工具表里可能有 `request_user_input`，但 handler 里还有二次判定：

1. 当前协作模式若不允许，直接拒绝。
2. 只有 `Plan` 模式允许。

证据：

- `ModeKind::allows_request_user_input` 仅匹配 `Plan`：`codex-rs/protocol/src/config_types.rs:212`
- handler 实际检查模式并拒绝：`codex-rs/core/src/tools/handlers/request_user_input.rs:74-77`

所以“工具被注册” != “任何时候都能成功调用”。

---

## 7. 一棵简化判定树（源码语义版）

```text
收到 prompt
  -> 先变成 Op
    -> submission_loop 按 Op 分流
      -> 若 Op=UserInput/UserTurn:
           new_turn_with_sub_id
           尝试 steer_input
             -> 有 active turn: 注入 pending_input，不新建任务
             -> 无 active turn: spawn_task(RegularTask)

RegularTask.run -> run_turn 循环
  -> run_sampling_request -> try_run_sampling_request(流)
    -> OutputItemDone(item)
      -> build_tool_call(item)
         -> 是 FunctionCall/CustomToolCall/LocalShellCall:
              执行工具，needs_follow_up = true
         -> 否:
              当普通消息处理
    -> Completed
      -> needs_follow_up |= has_pending_input

  -> needs_follow_up ?
       true  -> 下一轮 sampling（同一 task）
       false -> TurnComplete

若模型调用 collab 工具 spawn_agent/resume_agent 且未超深度限制
  -> 创建/恢复子代理线程（真正多任务分解）
```

---

## 8. 最容易误解的 3 点

1. “一个 prompt 会不会自动拆任务？”
   - 不会先按文本静态拆。
   - 主要是 `Op` 分流 + 模型在流里是否发出工具调用来驱动。

2. “只要开了工具就一定会调用吗？”
   - 不一定。
   - 还要模型在该轮实际输出工具调用 item。

3. “多轮 follow-up 是不是多任务？”
   - 不是。
   - 大多数时候是同一个 `RegularTask` 的多轮 sampling 循环。

