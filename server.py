"""
DeepSeek Web2API v2 - OpenAI-compatible server.

Exposes /v1/chat/completions and /v1/models with tolerant tool-call parsing,
true token streaming (no-tools requests) and per-agent session reuse. The
dashboard lives in ``dashboard.py``.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from core.accounts import (
    build_pool,
    delete_credential_file,
    probe_credentials,
    save_credential_file,
)
from core.auth import AuthManager, parse_credentials
from core.client import DeepSeekWebClient, HTTPStatusError, StreamStalled
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
from core.sessions import sessions as agent_sessions

auth_manager = AuthManager()
client = DeepSeekWebClient(auth_manager)
accounts = build_pool(auth_manager)

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


def _upload_image(url: str, session=None, headers=None) -> Optional[str]:
    """Upload an inline image to DeepSeek.

    Only ``data:`` URIs are accepted. Remote URLs are never fetched: doing so
    would let any client turn this proxy into an SSRF gadget against internal
    hosts, cloud metadata endpoints or local services.
    """
    if not url.startswith("data:"):
        print("[upload] skipping non-inline image url (SSRF protection)")
        return None
    header, _, b64 = url.partition(",")
    mime = header[5:].split(";")[0] or "image/png"
    try:
        data = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid base64 image payload: {exc}") from exc
    ext = _EXT_BY_MIME.get(mime, "png")
    return client.upload_file(
        data, "image." + ext, mime, session=session, headers=headers
    )


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

# Only loopback origins are allowed by default; add explicit ones via
# PROXY_CORS_ORIGINS=https://ui.example.com,http://192.168.1.20:3000
_DEFAULT_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
_extra_origins = [
    origin.strip()
    for origin in os.environ.get("PROXY_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEFAULT_ORIGINS + _extra_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional client authentication. When PROXY_API_KEY is set, every endpoint
# except health checks requires a matching bearer token.
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "").strip()
_OPEN_PATHS = {"/health", "/readyz"}


@app.middleware("http")
async def require_proxy_key(request, call_next):
    if not PROXY_API_KEY:
        return await call_next(request)
    if request.url.path in _OPEN_PATHS:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not key:
        key = request.headers.get("x-api-key", "").strip()
    if key != PROXY_API_KEY:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"error": {"message": "unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    return await call_next(request)


class CookieUpdateRequest(BaseModel):
    raw_text: str


class AccountCreateRequest(BaseModel):
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
    pool = accounts.status()
    return {
        "status": "ok",
        "time": time.time(),
        "accounts": len(pool),
        "accounts_ready": sum(1 for account in pool if account["ready"]),
        "accounts_valid": sum(1 for account in pool if account["valid"] is True),
    }


@app.get("/readyz")
async def ready_check() -> Dict[str, Any]:
    pool = accounts.status()
    ready = sum(1 for account in pool if account["ready"])
    return {
        "status": "ok" if ready else "degraded",
        "accounts": len(pool),
        "ready": ready,
    }


@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    if auth_manager.token:
        is_valid, user_info, message = auth_manager.validate_session()
        return {"authenticated": is_valid, "user": user_info, "message": message}

    pool = accounts.status()
    if not pool:
        return {"authenticated": False, "message": "No token provided"}

    valid = next((account for account in pool if account["valid"] is True), None)
    if valid:
        return {
            "authenticated": True,
            "user": {"nickname": valid["valid_message"]},
            "message": f"{len(pool)} account(s) loaded",
        }
    return {
        "authenticated": False,
        "message": "No valid accounts; check the Accounts panel.",
    }


@app.post("/api/accounts")
def add_account(req: AccountCreateRequest) -> Dict[str, Any]:
    """Validate pasted credentials and save them as accounts/acctN.txt."""
    raw = req.raw_text.strip()
    if not raw:
        return {"success": False, "message": "Paste a token or cookies first."}

    token, cookie = parse_credentials(raw)
    if not token and not cookie:
        return {"success": False, "message": "Could not extract credentials."}

    is_valid, detail = probe_credentials(token, cookie)
    if not is_valid:
        return {"success": False, "message": f"Validation failed: {detail}"}

    try:
        path = save_credential_file(raw)
    except (OSError, ValueError) as exc:
        return {"success": False, "message": f"Could not save account: {exc}"}

    accounts.reload()
    filename = os.path.basename(path)
    accounts.mark_valid(filename, is_valid, detail)
    entry = next(
        (account for account in accounts.status() if account["source"] == filename),
        None,
    )
    return {
        "success": True,
        "message": f"Added {filename} ({detail}).",
        "file": filename,
        "account": entry,
    }


@app.delete("/api/accounts/{filename}")
def remove_account(filename: str) -> Dict[str, Any]:
    """Delete one account file created through the dashboard."""
    try:
        removed = delete_credential_file(filename)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    except OSError as exc:
        return {"success": False, "message": f"Could not delete {filename}: {exc}"}

    if not removed:
        return {"success": False, "message": f"{filename} was already gone."}

    accounts.reload()
    return {
        "success": True,
        "message": f"Deleted {filename}.",
        "data": accounts.status(),
    }


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


@app.get("/v1/sessions")
async def list_sessions() -> Dict[str, Any]:
    return {"object": "list", "data": agent_sessions.list_public()}


@app.get("/v1/accounts")
async def list_accounts() -> Dict[str, Any]:
    return {"object": "list", "data": accounts.status()}


@app.post("/v1/accounts")
def validate_accounts() -> Dict[str, Any]:
    """Check every account live. Used by the dashboard's Re-check button."""
    return {"object": "list", "data": accounts.validate_all(force=True)}


@app.post("/reload-accounts")
async def reload_accounts() -> Dict[str, Any]:
    count = accounts.reload()
    return {"accounts": count, "data": accounts.status()}


@app.post("/reset-session")
async def reset_session(agent: str = "all") -> Dict[str, Any]:
    return {"agent": agent, "reset": agent_sessions.reset(agent)}


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


def _collect(events) -> tuple[str, str, Optional[str]]:
    """Consume client events and return (text, thinking, message_id).

    Upstream HTTP failures are raised as :class:`HTTPStatusError` so the
    account pool can read the status and ``Retry-After`` value.
    """
    text_parts: List[str] = []
    thinking_parts: List[str] = []
    message_id: Optional[str] = None
    http_status: Optional[int] = None
    retry_after: Optional[float] = None
    error_message: Optional[str] = None

    for event in events:
        kind = event.get("type")
        if kind == "http_error":
            http_status = event.get("status")
            retry_after = event.get("retry_after")
            continue
        if kind == "error":
            error_message = event.get("message", "DeepSeek stream error")
            continue
        if kind == "text":
            text_parts.append(event.get("content", ""))
        elif kind == "thinking":
            thinking_parts.append(event.get("content", ""))
        elif kind == "message_id":
            message_id = event.get("id") or message_id

    if http_status:
        raise HTTPStatusError(
            error_message or f"HTTP {http_status}", http_status, retry_after
        )
    if error_message:
        raise RuntimeError(error_message)

    return "".join(text_parts), "".join(thinking_parts), message_id


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
    parent_message_id: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> tuple[str, List[Dict[str, Any]], str, str, Optional[str]]:
    """Collect one reply, retrying once when a tool-call attempt fails to parse.

    Returns ``(content, tool_calls, thinking, raw_text, message_id)``. Raises
    ``HTTPException(502)`` when the model clearly attempted a tool call and
    neither the first reply nor the self-correction retry could be parsed.
    """
    text, thinking, message_id = _collect(
        client.stream_chat(
            prompt=prompt,
            session_id=session_id,
            session=session,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            ref_file_ids=ref_file_ids,
            parent_message_id=parent_message_id,
            headers=headers,
        )
    )
    content, calls, attempted = parse_envelope_tool_calls(text, tools)

    if not calls and attempted and tools:
        print("[chat] tool-call parse failed; retrying with self-correction")
        text2, thinking2, message_id2 = _collect(
            client.stream_chat(
                prompt=_retry_prompt(prompt, text, tools),
                session_id=session_id,
                session=session,
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
                ref_file_ids=ref_file_ids,
                # Thread the correction after the reply we just received.
                parent_message_id=message_id or parent_message_id,
                headers=headers,
            )
        )
        content, calls, attempted = parse_envelope_tool_calls(text2, tools)
        thinking = thinking or thinking2
        message_id = message_id2 or message_id
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

    return content, calls, thinking, text, message_id


def _assistant_message(
    content: Optional[str],
    thinking: Optional[str],
    tool_calls: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content or None}
    if thinking:
        message["reasoning_content"] = thinking
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _account_failure(agent, account, exc: HTTPStatusError) -> HTTPException:
    """Cool the account down, detach the chat and build a client-facing error."""
    cooldown = accounts.mark_failure(
        account,
        exc.status_code,
        getattr(exc, "retry_after", None),
        str(exc),
    )
    agent.reset_remote()
    lowered = str(exc).lower()
    if exc.status_code in (401, 403):
        status = exc.status_code
    elif exc.status_code == 429 or "too many messages" in lowered:
        # DeepSeek reports rate limits as HTTP 400 + "Too many messages".
        status = 429
    else:
        status = 502
    return HTTPException(
        status_code=status,
        detail=str(exc),
        headers={"Retry-After": str(max(1, int(cooldown)))},
    )


@app.post("/v1/chat/completions")
def create_chat_completion(
    req: ChatCompletionRequest,
    x_agent_session: Optional[str] = Header(default=None, alias="x-agent-session"),
    session_header: Optional[str] = Header(default=None, alias="session"),
    user: Optional[str] = Header(default=None),
):
    if accounts.size() == 0:
        raise HTTPException(
            status_code=401,
            detail="DeepSeek session not configured. Open http://localhost:8084 to set your token.",
        )

    def _hdr(value: Any) -> Optional[str]:
        # When the handler is called directly (tests), FastAPI has not resolved
        # Header defaults yet, so they arrive as Header() objects.
        return value if isinstance(value, str) and value else None

    agent_id = (
        _hdr(x_agent_session) or _hdr(session_header) or _hdr(user) or "default"
    )
    agent = agent_sessions.get(agent_id)

    account = accounts.select(agent_id, agent.account_id)
    if account is None:
        wait = accounts.next_ready_in()
        raise HTTPException(
            status_code=429,
            detail="All DeepSeek accounts are rate limited. Retry later.",
            headers={"Retry-After": str(max(1, int(wait)))},
        )
    if agent.account_id and agent.account_id != account.id:
        # The live chat session belongs to a different account; it cannot be
        # continued with this one.
        agent.reset_remote()
    agent.account_id = account.id
    headers = account.headers()

    delta_messages, reset_reason = agent.plan(req.messages, req.tools)

    prompt = format_messages_to_prompt(delta_messages, tools=req.tools)
    image_urls = _collect_images(req.messages)
    if not prompt and not image_urls:
        raise HTTPException(status_code=400, detail="Empty prompt / messages.")

    session = client._new_session()
    fresh_chat = bool(reset_reason or not agent.chat_session_id)
    if fresh_chat:
        if reset_reason:
            print(f"[session:{agent_id}] reset: {reset_reason}")
            agent.reset_remote()
        try:
            chat_session_id = client.create_chat_session(session, headers=headers)
        except HTTPStatusError as exc:
            raise _account_failure(agent, account, exc) from exc
        agent.chat_session_id = chat_session_id
        parent_message_id = None
    else:
        chat_session_id = agent.chat_session_id
        parent_message_id = agent.parent_message_id

    ref_file_ids: List[str] = []
    for image_url in image_urls:
        try:
            file_id = _upload_image(image_url, session=session, headers=headers)
        except HTTPStatusError as exc:
            raise _account_failure(agent, account, exc) from exc
        except Exception as exc:  # noqa: BLE001
            print("UPLOAD ERROR:", exc)
            raise HTTPException(
                status_code=502, detail=f"Image upload failed: {exc}"
            ) from exc
        if file_id:
            ref_file_ids.append(file_id)

    if ref_file_ids:
        time.sleep(2)

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

    # Hybrid streaming (option A): when the request has no tools, forward the
    # reply live. With tools we must buffer the whole reply to parse the JSON
    # envelope before we can emit proper tool-call deltas.
    if req.stream and not req.tools:
        want_usage_live = bool(
            req.stream_options and req.stream_options.get("include_usage")
        )

        def live_event_stream():
            emitted_role = False
            text_parts: List[str] = []
            thinking_parts: List[str] = []
            message_id: Optional[str] = None
            finished = False
            last_emit = time.time()
            try:
                for event in client.stream_chat(
                    prompt=prompt,
                    session_id=chat_session_id,
                    session=session,
                    thinking_enabled=thinking_enabled,
                    search_enabled=search_enabled,
                    ref_file_ids=ref_file_ids,
                    parent_message_id=parent_message_id,
                    headers=headers,
                ):
                    kind = event.get("type")
                    if kind == "message_id":
                        message_id = event.get("id") or message_id
                        continue
                    if kind == "http_error":
                        status = event.get("status")
                        cooldown = accounts.mark_failure(
                            account,
                            status,
                            event.get("retry_after"),
                            "",
                        )
                        agent.reset_remote()
                        if status == 504:
                            message = (
                                "DeepSeek upstream stalled (no data received); "
                                "session reset, retry the request"
                            )
                            err_type = "timeout_error"
                        else:
                            message = f"DeepSeek upstream HTTP {status}"
                            err_type = (
                                "rate_limit_error"
                                if status == 429
                                else "authentication_error"
                                if status in (401, 403)
                                else "upstream_error"
                            )
                        err = {
                            "error": {
                                "message": message,
                                "type": err_type,
                                "retry_after": max(1, int(cooldown)),
                            }
                        }
                        yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
                        yield make_done_sse()
                        return
                    if kind == "error":
                        # Headers are already sent: report inside the stream.
                        agent.reset_remote()
                        err = {
                            "error": {
                                "message": event.get("message", "DeepSeek stream error"),
                                "type": "upstream_error",
                            }
                        }
                        yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
                        yield make_done_sse()
                        return
                    if kind == "thinking":
                        chunk = event.get("content", "")
                        if chunk:
                            thinking_parts.append(chunk)
                            yield make_chunk_sse(
                                completion_id, req.model, {"reasoning_content": chunk}
                            )
                    elif kind == "text":
                        chunk = event.get("content", "")
                        if chunk:
                            text_parts.append(chunk)
                            delta: Dict[str, Any] = {"content": chunk}
                            if not emitted_role:
                                delta = {"role": "assistant", **delta}
                                emitted_role = True
                            yield make_chunk_sse(completion_id, req.model, delta)
                    last_emit = time.time()
                    if time.time() - last_emit > 15:
                        yield ": keepalive\n\n"
                        last_emit = time.time()

                full_text = "".join(text_parts)
                if not emitted_role and not full_text.strip():
                    err = {
                        "error": {
                            "message": "DeepSeek returned an empty response",
                            "type": "upstream_error",
                        }
                    }
                    yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
                    yield make_done_sse()
                    finished = True
                    return

                if not emitted_role:
                    yield make_chunk_sse(
                        completion_id,
                        req.model,
                        {"role": "assistant", "content": ""},
                    )
                yield make_chunk_sse(
                    completion_id, req.model, {}, finish_reason="stop"
                )
                if want_usage_live:
                    yield make_usage_chunk(
                        completion_id,
                        req.model,
                        prompt_tokens,
                        _estimate_tokens(full_text),
                    )
                yield make_done_sse()
                finished = True
                accounts.note_used(account)
            except Exception as exc:  # noqa: BLE001 - headers already sent
                print("STREAM ERROR:", exc)
                agent.reset_remote()
                err = {
                    "error": {
                        "message": str(exc),
                        "type": "upstream_error",
                    }
                }
                yield f"data: {json.dumps(err, separators=(',', ':'))}\n\n"
                yield make_done_sse()
            finally:
                # Record the turn only when the upstream exchange finished.
                if finished:
                    full_text = "".join(text_parts)
                    if full_text.strip():
                        agent.mark_sent(
                            sent_messages=delta_messages,
                            tools=req.tools,
                            prompt_chars=len(prompt),
                            output_chars=len(full_text) + len("".join(thinking_parts)),
                            message_id=message_id,
                            fresh_chat=fresh_chat,
                        )

        return StreamingResponse(
            live_event_stream(), media_type="text/event-stream"
        )

    try:
        content, tool_calls, thinking, raw_text, message_id = _generate(
            prompt=prompt,
            session=session,
            session_id=chat_session_id,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            ref_file_ids=ref_file_ids,
            tools=req.tools,
            parent_message_id=parent_message_id,
            headers=headers,
        )
    except HTTPStatusError as exc:
        raise _account_failure(agent, account, exc) from exc
    except HTTPException as exc:
        if exc.status_code == 502:
            # Empty stream or unparsable tool envelope: the upstream chat may
            # now contain a stray turn, so start clean next time.
            agent.reset_remote()
        raise
    except StreamStalled as exc:
        cooldown = accounts.mark_failure(account, 504, None, str(exc))
        agent.reset_remote()
        raise HTTPException(
            status_code=504,
            detail=f"DeepSeek upstream stalled: {exc}",
            headers={"Retry-After": str(max(1, int(cooldown)))},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        print("CHAT ERROR:", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    accounts.note_used(account)
    agent.mark_sent(
        sent_messages=delta_messages,
        tools=req.tools,
        prompt_chars=len(prompt),
        output_chars=len(raw_text or ""),
        message_id=message_id,
        fresh_chat=fresh_chat,
    )

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
                    "message": _assistant_message(content, thinking, tool_calls),
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
