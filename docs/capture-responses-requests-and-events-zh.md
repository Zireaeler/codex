# 捕获 Codex 的 Responses 请求与 SSE 输出事件（JSONL）并格式化为可读时间线

本说明覆盖一件事：**当你用 Codex CLI 跑一次 prompt 时，如何在本地同时捕获**

- 发给模型的 `/v1/responses` 请求体（request body）
- 模型返回的 SSE 流事件（`ResponseEvent`，包含文本增量、function/tool call 等）

并把两者格式化成“按自然顺序（request -> response）”的可读 Markdown。

## 1. 你能捕获到什么

### 1.1 Outgoing：Responses 请求体（request body）

抓取点在构造请求体时落盘，内容包含：

- `model`
- `instructions`
- `input[]`（历史消息、function_call_output 等）
- `tools[]`（本轮暴露给模型的工具清单）
- `reasoning` / `include` 等控制项

注意：**只记录请求体，不记录任何鉴权 header**（避免泄露凭证）。

### 1.2 Incoming：SSE 输出事件（ResponseEvent）

抓取点在消费 SSE stream 时落盘，内容包含：

- `OutputTextDelta`（流式文本增量）
- `OutputItemAdded` / `OutputItemDone`
  - `function_call`（如 `shell_command`）
  - `custom_tool_call`（如 `apply_patch`）
  - `web_search_call`
- `Completed`（包含 token usage 等）

同样：**只记录事件内容，不记录任何鉴权 header**。

## 2. 源码改动点（你要知道去哪看）

- `codex-rs/core/src/client.rs`
  - `CODEX_CAPTURE_RESPONSES_REQUESTS_PATH`：追加写出 `ResponsesApiRequest`（JSONL）
  - `CODEX_CAPTURE_RESPONSES_EVENTS_PATH`：追加写出每条 `ResponseEvent`（JSONL）
- `codex-rs/codex-api/src/common.rs`
  - `ResponseEvent` 增加 `Serialize`，便于 JSONL 序列化落盘

## 3. 实际使用步骤（Windows PowerShell）

### 3.1 构建本地 codex.exe（使用源码版本）

在仓库根目录：

```powershell
cd C:\Work\Project\rust\codex\codex\codex-rs
$env:Path = "$env:USERPROFILE\.cargo\bin;" + $env:Path
cargo build -p codex-cli
```

产物通常是：

`C:\Work\Project\rust\codex\codex\codex-rs\target\debug\codex.exe`

### 3.2 跑一次 prompt，并落盘 request/events 两份 JSONL

在仓库根目录：

```powershell
cd C:\Work\Project\rust\codex\codex

$env:CODEX_CAPTURE_RESPONSES_REQUESTS_PATH = "$PWD\capture_requests.jsonl"
$env:CODEX_CAPTURE_RESPONSES_EVENTS_PATH   = "$PWD\capture_events.jsonl"

.\codex-rs\target\debug\codex.exe exec "你的 prompt"
```

说明：

- `capture_requests.jsonl`：每次 GPT call 一行（一次 `/v1/responses` request body）
- `capture_events.jsonl`：同一次 call 的 SSE event 会有很多行（直到 `Completed`）

### 3.3 把 JSONL 格式化成可读 md（交错时间线）

```powershell
cd C:\Work\Project\rust\codex\codex
python .\tools\format_codex_capture.py `
  --requests .\capture_requests.jsonl `
  --events .\capture_events.jsonl `
  --out-readable .\capture_readable.md `
  --out-simplified .\capture_simplified.md
```

输出：

- `capture_readable.md`：按“GPT Call #n -> Request -> Response(SSE)”交错展示
- `capture_simplified.md`：更精简的每轮输入/输出摘要

## 4. 如何理解“多轮 GPT 调用”是怎么发生的

核心规律：

1. 模型在 SSE 输出里发出 `function_call` / `custom_tool_call`（表示“要调用工具”）
2. Codex 在本地执行对应工具
3. Codex 把工具结果包装成 `function_call_output`（或 `custom_tool_call_output`）塞进下一次请求的 `input[]`
4. Codex 发起下一次 `/v1/responses`（因此出现 GPT#2、GPT#3……）

在抓包里你能看到：

- `capture_events.jsonl`：出现 `function_call`/`custom_tool_call` 的事件
- `capture_requests.jsonl`：下一次请求的 `input[]` 里出现对应 `*_call_output`

## 5. 安全与隐私提示

虽然该抓取不记录鉴权 header，但仍可能包含敏感信息：

- prompt 原文
- 仓库路径、文件名
- 工具参数、命令行参数

因此不建议把 `capture_*.jsonl` 直接提交到公共仓库。

