#!/usr/bin/env python3
"""
Format Codex capture JSONL files into human-readable Markdown.

This is a debugging aid for understanding the request/streaming-event lifecycle:
- CODEX_CAPTURE_RESPONSES_REQUESTS_PATH (outgoing ResponsesApiRequest bodies)
- CODEX_CAPTURE_RESPONSES_EVENTS_PATH   (incoming ResponseEvent stream)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def fmt_ts_ms(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "unknown"
    try:
        t = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc)
        return t.isoformat()
    except Exception:
        return str(ts_ms)


def md_code_block(lang: str, text: str) -> str:
    return f"```{lang}\n{text}\n```"


def summarize_tools(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, dict) and "name" in tool:
            names.append(str(tool["name"]))
        elif isinstance(tool, dict) and tool.get("type") == "web_search":
            names.append("web_search")
        else:
            names.append("<unknown>")
    # de-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            out.append(n)
            seen.add(n)
    return out


def render_response_item(item: dict[str, Any]) -> str:
    t = item.get("type", "<missing-type>")
    if t == "message":
        role = item.get("role", "<missing-role>")
        content = item.get("content", [])
        lines: list[str] = [f"- `message` role=`{role}`"]
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    lines.append(f"  - content: {c!r}")
                    continue
                ct = c.get("type", "<missing-type>")
                if "text" in c:
                    text = str(c["text"])
                    lines.append(f"  - `{ct}`: {text}")
                else:
                    lines.append(f"  - `{ct}`: {json.dumps(c, ensure_ascii=False)}")
        else:
            lines.append(f"  - content: {json.dumps(content, ensure_ascii=False)}")
        return "\n".join(lines)

    # Many other ResponseItem variants exist. Keep a compact JSON for readability.
    return f"- `{t}`: {json.dumps(item, ensure_ascii=False)}"


def parse_event_obj(event_obj: Any) -> tuple[str, Any]:
    """
    ResponseEvent is serde-serialized as an externally-tagged enum by default, e.g.:
      {"OutputTextDelta": "..."}
      {"Completed": {"response_id": "...", ...}}
    """
    if isinstance(event_obj, str):
        return event_obj, None
    if isinstance(event_obj, dict) and len(event_obj) == 1:
        name, payload = next(iter(event_obj.items()))
        return str(name), payload
    return "<unknown>", event_obj


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… <truncated {len(text) - limit} chars>"


def extract_message_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if "text" in c:
            parts.append(str(c["text"]))
    return "".join(parts)


def summarize_input_item(item: dict[str, Any], *, text_limit: int) -> str:
    t = item.get("type", "<missing-type>")
    if t == "message":
        role = item.get("role", "<missing-role>")
        text = extract_message_text(item.get("content"))
        # Heuristic redactions for very noisy boilerplate.
        if role == "developer" and "<permissions instructions>" in text:
            return f"- `developer`: `<permissions instructions omitted ({len(text)} chars)>`"
        if role == "user" and text.startswith("# AGENTS.md instructions"):
            return f"- `user`: `<AGENTS.md instructions omitted ({len(text)} chars)>`"
        if role == "user" and "<environment_context>" in text:
            return f"- `user`: `<environment_context omitted ({len(text)} chars)>`"
        if role == "assistant" and not text:
            return "- `assistant`: <empty>"
        clean_text = text.replace("\r", "")
        return f"- `{role}`: {truncate(clean_text, text_limit)}"

    if t == "function_call":
        name = item.get("name", "<missing-name>")
        call_id = item.get("call_id", "<missing-call-id>")
        args = item.get("arguments", "")
        return f"- `function_call` name=`{name}` call_id=`{call_id}` args={truncate(str(args), text_limit)}"

    if t == "custom_tool_call":
        name = item.get("name", "<missing-name>")
        call_id = item.get("call_id", "<missing-call-id>")
        status = item.get("status", "<missing-status>")
        tool_input = item.get("input", "")
        return (
            f"- `custom_tool_call` name=`{name}` call_id=`{call_id}` status=`{status}` "
            f"input={truncate(str(tool_input), text_limit)}"
        )

    if t in ("function_call_output", "custom_tool_call_output", "call_output"):
        call_id = item.get("call_id", "<missing-call-id>")
        output = item.get("output", "")
        return f"- `function_call_output` call_id=`{call_id}` output={truncate(str(output), text_limit)}"

    if t == "reasoning":
        return "- `reasoning`: `<encrypted_content omitted>`"

    return f"- `{t}`: `<omitted>`"


def extract_tools_from_events(seg: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for r in seg:
        ev = r.get("event")
        if ev is None:
            continue
        ts = fmt_ts_ms(r.get("ts_ms"))
        name, payload = parse_event_obj(ev)
        if name not in ("OutputItemAdded", "OutputItemDone"):
            continue
        if not isinstance(payload, dict):
            continue
        item_type = payload.get("type")
        if item_type == "function_call":
            tool_name = payload.get("name", "<missing-name>")
            call_id = payload.get("call_id", "<missing-call-id>")
            args = payload.get("arguments", "")
            out.append(
                f"- ts=`{ts}` `{name}` `function_call`: `{tool_name}` call_id=`{call_id}` args={truncate(str(args), 200)}"
            )
        elif item_type == "custom_tool_call":
            tool_name = payload.get("name", "<missing-name>")
            call_id = payload.get("call_id", "<missing-call-id>")
            status = payload.get("status", "<missing-status>")
            tool_input = payload.get("input", "")
            out.append(
                f"- ts=`{ts}` `{name}` `custom_tool_call`: `{tool_name}` call_id=`{call_id}` status=`{status}` input={truncate(str(tool_input), 200)}"
            )
        elif item_type == "web_search_call":
            status = payload.get("status", "<missing-status>")
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            query = action.get("query") or action.get("queries")
            out.append(
                f"- ts=`{ts}` `{name}` `web_search_call`: status=`{status}` query={truncate(json.dumps(query, ensure_ascii=False), 200)}"
            )
    # de-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def extract_assistant_final_text(seg: list[dict[str, Any]]) -> str:
    text, _ts = extract_assistant_final_text_with_ts(seg)
    return text


def extract_assistant_final_text_with_ts(seg: list[dict[str, Any]]) -> tuple[str, str | None]:
    # Prefer finalized assistant message (OutputItemDone message -> output_text).
    for r in reversed(seg):
        ev = r.get("event")
        if ev is None:
            continue
        name, payload = parse_event_obj(ev)
        if name != "OutputItemDone" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            continue
        content = payload.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "output_text" and "text" in c:
                    return str(c["text"]), fmt_ts_ms(r.get("ts_ms"))
    # Fall back to streaming deltas.
    parts: list[str] = []
    last_ts: str | None = None
    for r in seg:
        ev = r.get("event")
        if ev is None:
            continue
        name, payload = parse_event_obj(ev)
        if name == "OutputTextDelta" and isinstance(payload, str):
            parts.append(payload)
            last_ts = fmt_ts_ms(r.get("ts_ms"))
    return "".join(parts), last_ts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=Path, required=True)
    ap.add_argument("--events", type=Path, required=False)
    ap.add_argument("--out-readable", type=Path, required=True)
    ap.add_argument("--out-simplified", type=Path, required=True)
    args = ap.parse_args()

    req_rows = load_jsonl(args.requests)
    ev_rows = load_jsonl(args.events) if args.events else []

    # Split the event stream into per-response segments (boundary = Completed).
    event_segments: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for row in ev_rows:
        cur.append(row)
        event_obj = row.get("event")
        if event_obj is None:
            continue
        name, _payload = parse_event_obj(event_obj)
        if name == "Completed":
            event_segments.append(cur)
            cur = []
    if cur:
        event_segments.append(cur)

    def summarize_event_segment(seg: list[dict[str, Any]]) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        completed: dict[str, Any] | None = None
        output_items_done = 0
        first_ts_ms: int | None = None
        last_ts_ms: int | None = None
        for r in seg:
            ts_ms = r.get("ts_ms")
            if isinstance(ts_ms, int):
                if first_ts_ms is None or ts_ms < first_ts_ms:
                    first_ts_ms = ts_ms
                if last_ts_ms is None or ts_ms > last_ts_ms:
                    last_ts_ms = ts_ms
            ev = r.get("event")
            if ev is None:
                continue
            name, payload = parse_event_obj(ev)
            counts[name] += 1
            if name == "Completed" and isinstance(payload, dict):
                completed = payload
            if name == "OutputItemDone":
                output_items_done += 1
        assistant_text, assistant_ts = extract_assistant_final_text_with_ts(seg)
        return {
            "counts": counts,
            "output_text": assistant_text,
            "output_ts": assistant_ts,
            "segment_start_ts": fmt_ts_ms(first_ts_ms),
            "segment_end_ts": fmt_ts_ms(last_ts_ms),
            "completed": completed,
            "output_items_done": output_items_done,
            "tools": extract_tools_from_events(seg),
        }

    seg_summaries = [summarize_event_segment(seg) for seg in event_segments]

    # Readable MD
    readable: list[str] = []
    readable.append("# Codex Capture (Readable)")
    readable.append("")
    readable.append("## Files")
    readable.append(f"- requests: `{args.requests}` ({len(req_rows)} lines, {args.requests.stat().st_size if args.requests.exists() else 0} bytes)")
    if args.events:
        readable.append(f"- events: `{args.events}` ({len(ev_rows)} lines, {args.events.stat().st_size if args.events.exists() else 0} bytes)")
    readable.append("")

    readable.append("## Timeline (Interleaved)")
    readable.append("Requests and streaming responses are separate capture sources, but rendered below in chronological order per call.")
    readable.append("")

    total_calls = max(len(req_rows), len(seg_summaries))
    if total_calls == 0:
        readable.append("_No requests or events captured._")
    for i in range(total_calls):
        req_row = req_rows[i] if i < len(req_rows) else None
        seg_summary = seg_summaries[i] if i < len(seg_summaries) else None
        seg = event_segments[i] if i < len(event_segments) else None

        readable.append(f"### GPT Call #{i + 1}")

        if req_row is None:
            readable.append("#### Request")
            readable.append("_No request captured for this call index._")
        else:
            ts_ms = req_row.get("ts_ms")
            conv = req_row.get("conversation_id")
            req = req_row.get("request", {})
            readable.append("#### Request")
            readable.append(f"- ts: `{fmt_ts_ms(ts_ms)}`")
            readable.append(f"- conversation_id: `{conv}`")
            if isinstance(req, dict):
                readable.append(f"- model: `{req.get('model')}`")
                readable.append(f"- tool_choice: `{req.get('tool_choice')}`")
                readable.append(f"- parallel_tool_calls: `{req.get('parallel_tool_calls')}`")
                tools = req.get("tools", [])
                if isinstance(tools, list):
                    tool_names = summarize_tools(tools)
                    if len(tool_names) > 6:
                        shown = ", ".join(f"`{n}`" for n in tool_names[:6])
                        readable.append(f"- tools: {shown}, `<+{len(tool_names) - 6} more>`")
                    else:
                        readable.append(
                            f"- tools: {', '.join(f'`{n}`' for n in tool_names) if tool_names else '`(none)`'}"
                        )
                instructions = req.get("instructions")
                if isinstance(instructions, str):
                    readable.append(f"- instructions: `{len(instructions)}` chars (see JSONL for full text)")
                include = req.get("include", [])
                if isinstance(include, list) and include:
                    readable.append(f"- include: {', '.join(f'`{x}`' for x in include)}")
                readable.append("")
                readable.append("input[] (condensed):")
                input_items = req.get("input", [])
                if isinstance(input_items, list) and input_items:
                    req_ts = fmt_ts_ms(ts_ms)
                    for it in input_items:
                        if isinstance(it, dict):
                            readable.append(f"[ts=`{req_ts}`] {summarize_input_item(it, text_limit=260)}")
                        else:
                            readable.append(f"[ts=`{req_ts}`] - <non-object>: <omitted>")
                else:
                    readable.append("_No input items._")

        readable.append("")

        readable.append("#### Response (SSE)")
        if seg_summary is None or seg is None:
            readable.append("_No streaming response segment captured for this call index._")
            readable.append("")
            continue

        counts: Counter[str] = seg_summary["counts"]
        output_text: str = seg_summary["output_text"]
        completed_payload: dict[str, Any] | None = seg_summary["completed"]
        output_items_done: int = seg_summary["output_items_done"]

        readable.append(f"- segment_start_ts: `{seg_summary.get('segment_start_ts')}`")
        readable.append(f"- segment_end_ts: `{seg_summary.get('segment_end_ts')}`")
        readable.append(f"- assistant_output_ts: `{seg_summary.get('output_ts')}`")
        readable.append(f"- events: `{len(seg)}`")
        if counts:
            readable.append(f"- event type counts: {', '.join(f'`{k}`={v}' for k, v in counts.most_common())}")
        readable.append(f"- OutputItemDone events: `{output_items_done}`")
        readable.append("")

        tools_used: list[str] = seg_summary.get("tools") or []
        if tools_used:
            readable.append("Tool calls emitted by the model:")
            readable.extend(tools_used)
            readable.append("")

        if output_text.strip():
            readable.append("Assistant final text:")
            readable.append(md_code_block("text", output_text.strip()))
            readable.append("")

        if completed_payload is not None:
            readable.append("Completed:")
            readable.append(md_code_block("json", json.dumps(completed_payload, ensure_ascii=False, indent=2)))
            readable.append("")

    args.out_readable.parent.mkdir(parents=True, exist_ok=True)
    args.out_readable.write_text("\n".join(readable) + "\n", encoding="utf-8-sig")

    # Simplified MD (timeline-like)
    simplified: list[str] = []
    simplified.append("# Codex Capture (Simplified)")
    simplified.append("")
    simplified.append("This view keeps the essential per-call input/output and collapses boilerplate as `<...>`.")
    simplified.append("")

    if not req_rows:
        simplified.append("_No requests captured._")
    for i, row in enumerate(req_rows, start=1):
        ts = fmt_ts_ms(row.get("ts_ms"))
        req = row.get("request", {})
        conv = row.get("conversation_id")
        simplified.append(f"## GPT Call #{i}")
        simplified.append(f"- ts: `{ts}`")
        simplified.append(f"- conversation_id: `{conv}`")
        if isinstance(req, dict):
            simplified.append(f"- model: `{req.get('model')}`")
            simplified.append(f"- tool_choice: `{req.get('tool_choice')}`")
            simplified.append(f"- parallel_tool_calls: `{req.get('parallel_tool_calls')}`")
            tools = req.get("tools", [])
            if isinstance(tools, list):
                tool_names = summarize_tools(tools)
                if len(tool_names) > 6:
                    shown = ", ".join(f"`{n}`" for n in tool_names[:6])
                    simplified.append(f"- tools: {shown}, `<+{len(tool_names) - 6} more>`")
                else:
                    simplified.append(f"- tools: {', '.join(f'`{n}`' for n in tool_names) if tool_names else '`(none)`'}")
        simplified.append("")
        simplified.append(f"### Input (condensed) @ `{ts}`")
        if isinstance(req, dict) and isinstance(req.get("input"), list):
            for it in req["input"]:
                if isinstance(it, dict):
                    simplified.append(f"[ts=`{ts}`] {summarize_input_item(it, text_limit=220)}")
                else:
                    simplified.append(f"[ts=`{ts}`] - `<unknown input item omitted>`")
        else:
            simplified.append("_No input items._")
        simplified.append("")

        if args.events and i <= len(seg_summaries):
            summary = seg_summaries[i - 1]
            tools_used: list[str] = summary.get("tools") or []
            out_text: str = summary.get("output_text", "")
            simplified.append("### Output (condensed)")
            simplified.append(f"- segment_start_ts: `{summary.get('segment_start_ts')}`")
            simplified.append(f"- segment_end_ts: `{summary.get('segment_end_ts')}`")
            simplified.append(f"- assistant_output_ts: `{summary.get('output_ts')}`")
            if tools_used:
                simplified.append("Tool calls emitted by the model:")
                simplified.extend(tools_used)
            else:
                simplified.append("Tool calls emitted by the model: `<none>`")
            simplified.append("")
            simplified.append("Assistant final text:")
            simplified.append(md_code_block("text", out_text.strip() or "<empty>"))
            simplified.append("")

    simplified.append("## Notes")
    simplified.append("- New GPT calls typically happen when Codex needs to send tool results back as `function_call_output` and ask the model how to proceed.")
    simplified.append("- Web search may appear as `web_search_call` items in the output stream; local tools appear as `function_call` items.")
    simplified.append("")

    args.out_simplified.parent.mkdir(parents=True, exist_ok=True)
    args.out_simplified.write_text("\n".join(simplified) + "\n", encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
