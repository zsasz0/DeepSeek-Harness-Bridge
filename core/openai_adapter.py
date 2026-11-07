"""
OpenAI protocol adapter.

Converts OpenAI chat-completion requests into DeepSeek web prompts, and
formats DeepSeek responses back into OpenAI-compatible payloads. Tool-call
parsing is delegated to :mod:`core.envelope`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .envelope import parse_tool_calls

AVAILABLE_MODELS: List[Dict[str, Any]] = [
    {
        "id": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": "DeepSeek V4.1 Flash (Fast)",
    },
    {
        "id": "deepseek-ai/DeepSeek-V4.1-Flash-1017-search",
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": "DeepSeek V4.1 Flash + Web Search",
    },
    {
        "id": "deepseek-chat",
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": "DeepSeek V3 (Chat - Fast)",
    },
    {
        "id": "deepseek-reasoner",
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": "DeepSeek R1 (Thinking / Reasoner)",
    },
    {
        "id": "deepseek-reasoner-search",
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": "DeepSeek R1 (Thinking + Web Search)",
    },
    {
        "id": "deepseek-search",
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": "DeepSeek V3 + Web Search",
    },
]

# The wire format the model is told to produce. One JSON object, nothing else.
_ENVELOPE_PROMPT = """Output EXACTLY ONE JSON object and nothing else — no prose, no fences:

  {"content": "<message or null>", "tool_calls": [{"name": "<tool>", "arguments": {...}}]}

Rules:
- Talk only:   {"content": "hi", "tool_calls": []}
- Call only:   {"content": null, "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}
- "arguments" is a JSON OBJECT. It may contain any characters (quotes, braces,
  newlines, unicode). Keep it valid inside the JSON.
- "arguments" must satisfy the tool's parameter schema, including every
  required key.
- Never write the JSON inside a code fence and never add commentary around it.

Examples:
User: list files
You: {"content": null, "tool_calls": [{"name": "bash", "arguments": {"command": "ls -la"}}]}

User: echo a brace
You: {"content": null, "tool_calls": [{"name": "bash", "arguments": {"command": "echo '}'"}}]}

User: what is 2+2
You: {"content": "4", "tool_calls": []}"""

_ENVELOPE_REMINDER = (
    "Reminder: answer with exactly one JSON envelope and nothing else — "
    '{"content": "<message or null>", "tool_calls": [{"name": "<tool>", "arguments": {...}}]}. '
    "Use an empty tool_calls list when you are only talking."
)


def _tool_system_prompt(tools):
    lines = ["You have access to the following tools."]
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name", "unknown")
        desc = fn.get("description", "")
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        lines.append("")
        lines.append("Tool: " + name)
        if desc:
            lines.append("Description: " + desc)
        lines.append(
            "Parameters: " + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )

    lines.append("")
    lines.append(_ENVELOPE_PROMPT)
    lines.append("")
    lines.append(_ENVELOPE_REMINDER)
    return "\n".join(lines)


def _content_to_str(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t == "text":
                    pieces.append(item.get("text", ""))
                elif t in ("image", "image_url", "file"):
                    pieces.append("[Image attached]")  # <--- keep this
            elif isinstance(item, str):
                pieces.append(item)
        return "\n".join(pieces)
    return str(content or "")


def _render_assistant_tool_calls(tool_calls, content=None):
    """Render a previous assistant turn in the same envelope format."""
    calls = []
    for call in tool_calls:
        fn = call.get("function", {})
        name = fn.get("name", "tool")
        raw_args = fn.get("arguments", {})
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {"raw": raw_args}
        else:
            args = raw_args if isinstance(raw_args, dict) else {}
        calls.append({"name": name, "arguments": args})

    envelope = {"content": content or None, "tool_calls": calls}
    return json.dumps(envelope, ensure_ascii=False, indent=2)


def format_messages_to_prompt(messages, tools=None):
    if not messages and not tools:
        return ""

    parts = []
    if tools:
        parts.append(_tool_system_prompt(tools))

    for message in messages or []:
        role = message.get("role", "user").lower()
        content = _content_to_str(message.get("content", ""))

        if role == "system":
            parts.append("[System Instruction]\n" + content)
        elif role == "user":
            parts.append("[User]\n" + content)
        elif role == "assistant":
            calls = message.get("tool_calls")
            if calls:
                rendered = _render_assistant_tool_calls(calls, content=content)
                parts.append("[Assistant]\n" + rendered)
            else:
                parts.append("[Assistant]\n" + content)
        elif role == "tool":
            name = message.get("name", "tool")
            parts.append("[Tool Result (" + name + ")]\n" + content)
        else:
            parts.append("[" + role.capitalize() + "]\n" + content)

    return "\n\n".join(parts)


def parse_envelope_tool_calls(text, tools=None):
    """Return ``(content, tool_calls, attempted)`` for an assistant reply."""
    return parse_tool_calls(text, tools)


def resolve_model_thinking(model_name):
    name = (model_name or "").lower()
    return "reasoner" in name or "r1" in name or "think" in name


def resolve_model_search(model_name):
    name = (model_name or "").lower()
    return "search" in name or "online" in name or "web" in name


def make_chunk_sse(completion_id, model, delta, finish_reason=None):
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return "data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"


def make_done_sse():
    return "data: [DONE]\n\n"


def make_full_response(completion_id, model, content, reasoning_content=None, prompt_tokens=0, completion_tokens=0):
    message = {"role": "assistant", "content": content}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }