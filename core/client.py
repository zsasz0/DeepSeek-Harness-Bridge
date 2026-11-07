"""
DeepSeek web chat client.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from curl_cffi import requests, CurlMime

from .auth import AuthManager, IMPERSONATE
from .pow_solver import PoWSolver

DEEPSEEK_BASE = "https://chat.deepseek.com"
SESSION_CREATE_URL = f"{DEEPSEEK_BASE}/api/v0/chat_session/create"
COMPLETION_URL = f"{DEEPSEEK_BASE}/api/v0/chat/completion"
UPLOAD_FILE_URL = f"{DEEPSEEK_BASE}/api/v0/file/upload_file"

# Stream watchdogs. ``IDLE`` fires when no upstream line arrives for that long;
# ``MAX`` caps one completion regardless of activity.
STREAM_IDLE_TIMEOUT_S = float(
    os.environ.get("DEEPSEEK_STREAM_IDLE_TIMEOUT_S", "90")
)
STREAM_MAX_SECONDS = float(os.environ.get("DEEPSEEK_STREAM_MAX_S", "300"))


class HTTPStatusError(RuntimeError):
    """Upstream returned a non-2xx status, carrying what the pool needs."""

    def __init__(self, message: str, status_code: int, retry_after=None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class StreamStalled(RuntimeError):
    """Upstream stopped producing data past the watchdog limits."""

    def __init__(self, message: str, timeout_kind: str) -> None:
        super().__init__(message)
        self.timeout_kind = timeout_kind


def _is_auth_error(code, message) -> bool:
    """DeepSeek reports auth failures with HTTP 200 + a business code."""
    text = str(message or "").lower()
    if code in (40001, 40002, 40003):
        return True
    return "invalid token" in text or "authorization failed" in text


def _retry_after_seconds(resp) -> Optional[float]:
    """Parse a Retry-After header (seconds or HTTP date) if present."""
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        return max(0.0, when.timestamp() - time.time())
    except Exception:
        return None


def _watchdog_lines(
    resp,
    idle_timeout: Optional[float] = None,
    max_seconds: Optional[float] = None,
):
    """Yield upstream lines with idle + total deadlines.

    ``resp.iter_lines()`` blocks forever when the server stops sending, so a
    plain timeout on the POST does not protect us. Read it in a daemon thread
    and hand lines over a queue; the consumer enforces both deadlines and can
    stop waiting even while the socket is still open.
    """
    idle = STREAM_IDLE_TIMEOUT_S if idle_timeout is None else idle_timeout
    total = STREAM_MAX_SECONDS if max_seconds is None else max_seconds
    lines: "queue.Queue[object]" = queue.Queue(maxsize=256)
    stop = threading.Event()
    sentinel = object()

    def pump() -> None:
        try:
            for item in resp.iter_lines():
                if stop.is_set():
                    break
                lines.put(item)
        except Exception as exc:  # noqa: BLE001 - surfaced to consumer
            try:
                lines.put(exc)
            except Exception:
                pass
        finally:
            try:
                lines.put(sentinel)
            except Exception:
                pass

    threading.Thread(target=pump, name="ds-stream", daemon=True).start()

    started = time.time()
    while True:
        remaining_total = total - (time.time() - started)
        if remaining_total <= 0:
            stop.set()
            raise StreamStalled(
                f"upstream stream exceeded {int(total)}s", "total"
            )
        wait = min(idle, remaining_total)
        try:
            item = lines.get(timeout=wait)
        except queue.Empty:
            stop.set()
            if wait >= remaining_total:
                raise StreamStalled(
                    f"upstream stream exceeded {int(total)}s", "total"
                ) from None
            raise StreamStalled(
                f"upstream stalled for {int(idle)}s", "idle"
            ) from None
        if item is sentinel:
            return
        if isinstance(item, Exception):
            raise item
        yield item


class DeepSeekWebClient:
    """Minimal client for DeepSeek's web chat endpoints."""

    def __init__(self, auth_manager: AuthManager) -> None:
        self.auth = auth_manager
        self.pow_solver = PoWSolver()

    def _new_session(self) -> requests.Session:
        return requests.Session(impersonate=IMPERSONATE)

    def _resolve_headers(self, headers: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Use per-account headers when provided, else the manager's."""
        return dict(headers) if headers else self.auth.get_headers()

    def create_chat_session(
        self, session=None, headers: Optional[Dict[str, str]] = None
    ) -> str:
        sess = session or self._new_session()
        req_headers = self._resolve_headers(headers)

        resp = sess.post(
            SESSION_CREATE_URL,
            headers=req_headers,
            json={"character_id": None},
            timeout=15,
        )
        if resp.status_code != 200:
            raise HTTPStatusError(
                f"Failed to create chat session: HTTP {resp.status_code} - {resp.text[:150]}",
                status_code=resp.status_code,
                retry_after=_retry_after_seconds(resp),
            )
        data = resp.json()
        if data.get("code") != 0:
            code = data.get("code")
            message = data.get("msg", "Unknown error")
            if _is_auth_error(code, message):
                raise HTTPStatusError(
                    f"DeepSeek auth error: {message} (code: {code})", 401
                )
            raise RuntimeError(
                f"DeepSeek chat_session/create error: {message} "
                f"(code: {code})"
            )

        biz_data = data.get("data", {}).get("biz_data", {})
        session_id = biz_data.get("id") or biz_data.get("chat_session", {}).get("id")
        if not session_id:
            raise RuntimeError("Failed to obtain chat_session_id from DeepSeek response")
        return session_id

    def upload_file(
        self,
        data,
        filename,
        mime_type,
        session=None,
        headers: Optional[Dict[str, str]] = None,
    ):
        sess = session or self._new_session()
        req_headers = self._resolve_headers(headers)
        req_headers.pop("Content-Type", None)

        pow_header = self.pow_solver.fetch_and_solve(
            sess, req_headers, target_path="/api/v0/file/upload_file"
        )
        req_headers["x-ds-pow-response"] = pow_header

        mp = CurlMime()
        mp.addpart(
            name="file",
            content_type=mime_type,
            filename=filename,
            data=data,
        )
        resp = sess.post(
            UPLOAD_FILE_URL,
            headers=req_headers,
            multipart=mp,
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to upload file: HTTP {resp.status_code} - {resp.text[:200]}"
            )

        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"DeepSeek file/upload_file error: {payload.get('msg', 'Unknown')} "
                f"(code: {payload.get('code')})"
            )

        biz_data = payload.get("data", {}).get("biz_data", {}) or {}
        file_id = biz_data.get("id")
        status = biz_data.get("status")
        if not file_id:
            raise RuntimeError("Upload response did not contain a file id")

        for attempt in range(15):
            if status == "SUCCESS":
                break
            time.sleep(1)
            try:
                poll_resp = sess.get(
                    f"{DEEPSEEK_BASE}/api/v0/file/fetch_files",
                    headers=self._resolve_headers(headers),
                    params={"file_ids": file_id},
                    timeout=10,
                )
                if poll_resp.status_code == 200:
                    poll_data = poll_resp.json()
                    biz = (poll_data.get("data") or {}).get("biz_data") or {}
                    files = biz.get("files") if isinstance(biz, dict) else biz
                    if isinstance(files, list):
                        for f in files:
                            if isinstance(f, dict) and f.get("id") == file_id:
                                status = f.get("status")
                                break
            except Exception:
                # Polling is best-effort; the post-upload wait covers slow jobs.
                pass

        if status != "SUCCESS":
            time.sleep(3)
        else:
            time.sleep(1.5)  # wait for vision pipeline to index the file
        return file_id

    def stream_chat(
        self,
        prompt: str,
        session_id=None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        parent_message_id=None,
        ref_file_ids=None,
        session=None,
        headers: Optional[Dict[str, str]] = None,
    ):
        sess = session or self._new_session()
        req_headers = self._resolve_headers(headers)

        if not session_id:
            session_id = self.create_chat_session(sess, headers=req_headers)

        pow_header = self.pow_solver.fetch_and_solve(
            sess, req_headers, target_path="/api/v0/chat/completion"
        )
        req_headers["x-ds-pow-response"] = pow_header

        payload = {
            "chat_session_id": session_id,
            "parent_message_id": parent_message_id,
            "model_type": "default",
            "prompt": prompt,
            "ref_file_ids": ref_file_ids or [],
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
            "action": None,
            "preempt": False,
        }
        resp = sess.post(
            COMPLETION_URL,
            headers=req_headers,
            json=payload,
            stream=True,
            timeout=180,
        )
        if resp.status_code != 200:
            body = resp.text[:300] if hasattr(resp, "text") else ""
            yield {
                "type": "http_error",
                "status": resp.status_code,
                "retry_after": _retry_after_seconds(resp),
            }
            yield {"type": "error", "message": f"HTTP {resp.status_code} - {body}"}
            return

        current_frag_type = "RESPONSE"
        message_id: Optional[str] = None

        try:
            for raw_line in _watchdog_lines(resp):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if line.startswith("event:"):
                    if line[6:].strip() == "close":
                        if message_id:
                            yield {"type": "message_id", "id": message_id}
                        yield {"type": "done", "finish_reason": "stop"}
                        return
                    continue

                if not line.startswith("data:"):
                    continue

                line_content = line[5:].strip()
                if not line_content or line_content == "[DONE]":
                    if message_id:
                        yield {"type": "message_id", "id": message_id}
                    yield {"type": "done", "finish_reason": "stop"}
                    return

                try:
                    data = json.loads(line_content)
                except json.JSONDecodeError:
                    continue

                # Capture the assistant message id so the next turn can pass it
                # back as parent_message_id and continue the same chat chain.
                if isinstance(data, dict):
                    rid = data.get("response_message_id")
                    if rid and not message_id:
                        message_id = str(rid)
                    inner_v = data.get("v")
                    if isinstance(inner_v, dict):
                        resp_obj = inner_v.get("response")
                        if isinstance(resp_obj, dict):
                            inner_mid = resp_obj.get("message_id")
                            if inner_mid:
                                message_id = str(inner_mid)

                if isinstance(data, dict):
                    if data.get("code", 0) != 0:
                        code = data.get("code")
                        message = data.get("msg", "Error")
                        if _is_auth_error(code, message):
                            yield {"type": "http_error", "status": 401, "retry_after": None}
                        yield {
                            "type": "error",
                            "message": f"DeepSeek stream error: {message} "
                            f"(code: {code})",
                        }
                        return
                    inner_data = data.get("data")
                    if isinstance(inner_data, dict) and inner_data.get("biz_code", 0) != 0:
                        yield {
                            "type": "error",
                            "message": f"DeepSeek stream error: {inner_data.get('biz_msg', 'Error')} "
                            f"(biz_code: {inner_data.get('biz_code')})",
                        }
                        return

                if (
                    isinstance(data, dict)
                    and data.get("p") == "response/status"
                    and data.get("v") == "FINISHED"
                ):
                    if message_id:
                        yield {"type": "message_id", "id": message_id}
                    yield {"type": "done", "finish_reason": "stop"}
                    return

                if (
                    isinstance(data, dict)
                    and data.get("o") == "BATCH"
                    and isinstance(data.get("v"), list)
                ):
                    for sub in data["v"]:
                        if (
                            isinstance(sub, dict)
                            and sub.get("p") == "fragments"
                            and sub.get("o") == "APPEND"
                            and isinstance(sub.get("v"), list)
                        ):
                            for frag in sub["v"]:
                                current_frag_type = frag.get("type", "RESPONSE")
                                content = frag.get("content", "")
                                if content:
                                    yield {
                                        "type": "thinking"
                                        if current_frag_type == "THINK"
                                        else "text",
                                        "content": content,
                                    }
                    continue

                if (
                    isinstance(data, dict)
                    and isinstance(data.get("v"), dict)
                    and "response" in data["v"]
                ):
                    for frag in data["v"]["response"].get("fragments", []):
                        current_frag_type = frag.get("type", "RESPONSE")
                        content = frag.get("content", "")
                        if content:
                            yield {
                                "type": "thinking"
                                if current_frag_type == "THINK"
                                else "text",
                                "content": content,
                            }
                    continue

                if (
                    isinstance(data, dict)
                    and data.get("p") == "response/fragments"
                    and data.get("o") == "APPEND"
                    and isinstance(data.get("v"), list)
                ):
                    for frag in data["v"]:
                        current_frag_type = frag.get("type", "RESPONSE")
                        content = frag.get("content", "")
                        if content:
                            yield {
                                "type": "thinking"
                                if current_frag_type == "THINK"
                                else "text",
                                "content": content,
                            }
                    continue

                if (
                    isinstance(data, dict)
                    and data.get("p") == "response/fragments/-1/content"
                    and data.get("o") == "APPEND"
                    and isinstance(data.get("v"), str)
                    and data["v"]
                ):
                    yield {
                        "type": "thinking" if current_frag_type == "THINK" else "text",
                        "content": data["v"],
                    }
                    continue

                if (
                    isinstance(data, dict)
                    and isinstance(data.get("v"), str)
                    and "p" not in data
                    and data["v"]
                ):
                    yield {
                        "type": "thinking" if current_frag_type == "THINK" else "text",
                        "content": data["v"],
                    }
                    continue
        except StreamStalled as exc:
            # Upstream went silent. Surface it as an upstream failure so the
            # caller can cool the account and reset the session.
            yield {
                "type": "http_error",
                "status": 504,
                "retry_after": None,
                "stalled": exc.timeout_kind,
            }
            yield {"type": "error", "message": str(exc)}
            return

        if message_id:
            yield {"type": "message_id", "id": message_id}
        yield {"type": "done", "finish_reason": "stop"}
