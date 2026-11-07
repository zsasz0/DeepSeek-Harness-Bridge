"""
DeepSeek Web2API v2 - OpenAI-compatible server.

Exposes /v1/chat/completions and /v1/models with tolerant tool-call parsing
and true token streaming. The dashboard lives in ``dashboard.py``.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from core.auth import AuthManager
from core.client import DeepSeekWebClient
from core.dashboard import DASHBOARD_HTML
from core.openai_adapter import (
    AVAILABLE_MODELS,
    format_messages_to_prompt,
    make_chunk_sse,
    make_done_sse,
    make_full_response,
    parse_envelope_tool_calls,
    resolve_model_search,
    resolve_model_thinking,
)

auth_manager = AuthManager()
client = DeepSeekWebClient(auth_manager)

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


def _upload_image(url: str, session=None) -> str:
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        data = base64.b64decode(b64)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            mime = resp.headers.get_content_type() or "image/png"
    ext = _EXT_BY_MIME.get(mime, "png")
    return client.upload_file(data, "image." + ext, mime, session=session)


def _collect_images(messages: List[Dict[str, Any]]) -> List[str]:
    """Only pull image parts from the most recent user message."""
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            return []
        urls: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_url":
                obj = item.get("image_url") or {}
                url = obj.get("url") if isinstance(obj, dict) else obj
                if url:
                    urls.append(url)
            elif item.get("type") == "image":
                url = item.get("image")
                if url:
                    urls.append(url)
            elif item.get("type") == "file":
                obj = item.get("file") or {}
                url = obj.get("file_data")
                if url:
                    urls.append(url)
        return urls
    return []

app = FastAPI(title="DeepSeek Web2API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CookieUpdateRequest(BaseModel):
    raw_text: str


class ChatCompletionRequest(BaseModel):
    model: str = "deepseek-chat"
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]] = None
    stream: bool = False
    stream_options: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    search_enabled: Optional[bool] = None
    thinking_enabled: Optional[bool] = None


# ----------------------------------------------------------------------
# Basic routes
# ----------------------------------------------------------------------
@app.get("/health")
async def health_check() -> Dict[str, Any]:
    return {"status": "ok", "time": time.time()}


@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    if not auth_manager.token:
        return {"authenticated": False, "message": "No token provided"}
    is_valid, user_info, message = auth_manager.validate_session()
    return {"authenticated": is_valid, "user": user_info, "message": message}


@app.post("/api/cookies")
async def update_cookies(req: CookieUpdateRequest) -> Dict[str, Any]:
    raw = req.raw_text.strip()
    if not raw:
        auth_manager.save(token="", cookie_string="")
        return {"success": True, "message": "Credentials cleared"}

    token, cookie_str = auth_manager.parse_credentials(raw)
    if not token and not cookie_str:
        return {"success": False, "message": "Could not extract credentials."}

    is_valid, user_info, message = auth_manager.validate_session(
        token=token, cookie_string=cookie_str
    )
    if not is_valid:
        return {"success": False, "message": f"Validation failed: {message}"}

    auth_manager.save(token=token, cookie_string=cookie_str)
    display = (
        (user_info or {}).get("nickname")
        or (user_info or {}).get("email")
        or "DeepSeek user"
    )
    return {"success": True, "message": f"Connected as {display}!", "user": user_info}


@app.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    return {"object": "list", "data": AVAILABLE_MODELS}


# ----------------------------------------------------------------------
# Chat completions
# ----------------------------------------------------------------------
def _estimate_tokens(text: str) -> int:
    return len(text.split()) if text else 0


def _count_message_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate across the original OpenAI messages."""
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    total += _estimate_tokens(item["text"])
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            total += _estimate_tokens(fn.get("name", ""))
            total += _estimate_tokens(fn.get("arguments", ""))
        total += 4  # per-message overhead
    return total


def make_usage_chunk(
    completion_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> str:
    """OpenAI-style final usage chunk (choices is empty)."""
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _collect(events) -> tuple[str, str, bool]:
    """Consume client events and return (text, thinking, errored)."""
    text_parts: List[str] = []
    thinking_parts: List[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "error":
            raise RuntimeError(event.get("message", "DeepSeek stream error"))
        if kind == "text":
            text_parts.append(event.get("content", ""))
        elif kind == "thinking":
            thinking_parts.append(event.get("content", ""))
    return "".join(text_parts), "".join(thinking_parts), False


# ----------------------------------------------------------------------
# Generation + self-correction
# ----------------------------------------------------------------------
_RETRY_MAX_PROMPT_CHARS = 32_000
_RETRY_MAX_BAD_CHARS = 4_000


def _tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for tool in tools or []:
        fn = (tool or {}).get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _clip_prompt(prompt: str, limit: int = _RETRY_MAX_PROMPT_CHARS) -> str:
    """Keep the envelope instructions (head) and the latest turns (tail)."""
    if len(prompt) <= limit:
        return prompt
    head = prompt[:6000]
    tail = prompt[-(limit - 6000):]
    return head + "\n\n[...earlier conversation truncated...]\n\n" + tail


def _retry_prompt(prompt: str, bad_text: str, tools) -> str:
    snippet = (bad_text or "").strip()
    if len(snippet) > _RETRY_MAX_BAD_CHARS:
        snippet = snippet[:_RETRY_MAX_BAD_CHARS] + "\n...[truncated]"
    names = ", ".join(_tool_names(tools)) or "(none)"
    return (
        _clip_prompt(prompt)
        + "\n\n[Assistant previous answer]\n"
        + snippet
        + "\n\n[Correction]\n"
        + "Your previous reply was not a valid JSON envelope. Output ONLY one JSON object:\n"
        + '{"content": "<message or null>", "tool_calls": [{"name": "<tool>", "arguments": {...}}]}\n'
        + "No prose, no fences and no other tags. Valid tool names: " + names + "."
    )


def _generate(
    prompt: str,
    session,
    session_id: str,
    thinking_enabled: bool,
    search_enabled: bool,
    ref_file_ids: List[str],
    tools: Optional[List[Dict[str, Any]]],
) -> tuple[str, List[Dict[str, Any]], str, str]:
    """Collect one reply, retrying once when a tool-call attempt fails to parse.

    Returns ``(content, tool_calls, thinking, raw_text)``. Raises
    ``HTTPException(502)`` when the model clearly attempted a tool call and
    neither the first reply nor the self-correction retry could be parsed.
    """
    text, thinking, _ = _collect(
        client.stream_chat(
            prompt=prompt,
            session_id=session_id,
            session=session,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            ref_file_ids=ref_file_ids,
        )
    )
    content, calls, attempted = parse_envelope_tool_calls(text, tools)

    if not calls and attempted and tools:
        print("[chat] tool-call parse failed; retrying with self-correction")
        text2, thinking2, _ = _collect(
            client.stream_chat(
                prompt=_retry_prompt(prompt, text, tools),
                session_id=session_id,
                session=session,
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
                ref_file_ids=ref_file_ids,
            )
        )
        content, calls, attempted = parse_envelope_tool_calls(text2, tools)
        thinking = thinking or thinking2
        if text2:
            text = text2

    if not calls and attempted:
        raise HTTPException(
            status_code=502,
            detail="model attempted a tool call that could not be parsed",
        )

    if not calls and not content:
        # Upstream can return an empty stream (rate limiting, stalled session).
        # An empty assistant message is useless to the client -- fail loudly so
        # the harness can back off and retry.
        raise HTTPException(
            status_code=502, detail="DeepSeek returned an empty response"
        )

    return content, calls, thinking, text


@app.post("/v1/chat/completions")
def create_chat_completion(req: ChatCompletionRequest):
    if not auth_manager.token:
        raise HTTPException(
            status_code=401,
            detail="DeepSeek session not configured. Open http://localhost:8084 to set your token.",
        )

    prompt = format_messages_to_prompt(req.messages, tools=req.tools)
    image_urls = _collect_images(req.messages)
    if not prompt and not image_urls:
        raise HTTPException(status_code=400, detail="Empty prompt / messages.")

    session = client._new_session()
    chat_session_id = client.create_chat_session(session)

    ref_file_ids: List[str] = []
    for image_url in image_urls:
        try:
            ref_file_ids.append(_upload_image(image_url, session=session))
        except Exception as exc:  # noqa: BLE001
            print("UPLOAD ERROR:", exc)
            raise HTTPException(
                status_code=502, detail=f"Image upload failed: {exc}"
            ) from exc

    if ref_file_ids:
        import time as _time
        _time.sleep(2)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    thinking_enabled = (
        req.thinking_enabled
        if req.thinking_enabled is not None
        else resolve_model_thinking(req.model)
    )
    search_enabled = (
        req.search_enabled
        if req.search_enabled is not None
        else resolve_model_search(req.model)
    )

    prompt_tokens = _count_message_tokens(req.messages)

    try:
        content, tool_calls, thinking, raw_text = _generate(
            prompt=prompt,
            session=session,
            session_id=chat_session_id,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            ref_file_ids=ref_file_ids,
            tools=req.tools,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        print("CHAT ERROR:", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    completion_tokens = _estimate_tokens(raw_text)

    if req.stream:
        want_usage = bool(
            req.stream_options and req.stream_options.get("include_usage")
        )

        def event_stream():
            emitted_role = False
            try:
                if thinking:
                    yield make_chunk_sse(
                        completion_id, req.model, {"reasoning_content": thinking}
                    )

                if content:
                    yield make_chunk_sse(
                        completion_id,
                        req.model,
                        {"role": "assistant", "content": content},
                    )
                    emitted_role = True

                if tool_calls:
                    # Chunk 1 per call: id + name + empty arguments.
                    for index, call in enumerate(tool_calls):
                        delta: Dict[str, Any] = {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["function"]["name"],
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                        if not emitted_role:
                            delta = {"role": "assistant", **delta}
                            emitted_role = True
                        yield make_chunk_sse(completion_id, req.model, delta)

                    # Chunk 2 per call: the full arguments string.
                    for index, call in enumerate(tool_calls):
                        yield make_chunk_sse(
                            completion_id,
                            req.model,
                            {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "function": {
                                            "arguments": call["function"]["arguments"]
                                        },
                                    }
                                ]
                            },
                        )

                    yield make_chunk_sse(
                        completion_id, req.model, {}, finish_reason="tool_calls"
                    )
                else:
                    if not emitted_role:
                        yield make_chunk_sse(
                            completion_id,
                            req.model,
                            {"role": "assistant", "content": ""},
                        )
                    yield make_chunk_sse(
                        completion_id, req.model, {}, finish_reason="stop"
                    )

                if want_usage:
                    yield make_usage_chunk(
                        completion_id, req.model, prompt_tokens, completion_tokens
                    )

                yield make_done_sse()
            except Exception as exc:  # noqa: BLE001 - headers already sent
                print("STREAM ERROR:", exc)
                yield make_chunk_sse(
                    completion_id, req.model, {}, finish_reason="stop"
                )
                yield make_done_sse()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    if tool_calls:
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "reasoning_content": thinking or None,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    return make_full_response(
        completion_id=completion_id,
        model=req.model,
        content=content,
        reasoning_content=thinking or None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


@app.get("/", response_class=HTMLResponse)
async def get_dashboard() -> str:
    return DASHBOARD_HTML