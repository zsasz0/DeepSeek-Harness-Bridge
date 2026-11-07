"""
DeepSeek web chat client.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Generator, List, Optional

from curl_cffi import requests, CurlMime

from .auth import AuthManager
from .pow_solver import PoWSolver

DEEPSEEK_BASE = "https://chat.deepseek.com"
SESSION_CREATE_URL = f"{DEEPSEEK_BASE}/api/v0/chat_session/create"
COMPLETION_URL = f"{DEEPSEEK_BASE}/api/v0/chat/completion"
UPLOAD_FILE_URL = f"{DEEPSEEK_BASE}/api/v0/file/upload_file"


class DeepSeekWebClient:
    """Minimal client for DeepSeek's web chat endpoints."""

    def __init__(self, auth_manager: AuthManager) -> None:
        self.auth = auth_manager
        self.pow_solver = PoWSolver()

    def _new_session(self) -> requests.Session:
        return requests.Session(impersonate="chrome124")

    def create_chat_session(self, session=None) -> str:
        sess = session or self._new_session()
        headers = self.auth.get_headers()

        resp = sess.post(
            SESSION_CREATE_URL,
            headers=headers,
            json={"character_id": None},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to create chat session: HTTP {resp.status_code} - {resp.text[:150]}"
            )

        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(
                f"DeepSeek chat_session/create error: {data.get('msg', 'Unknown error')} "
                f"(code: {data.get('code')})"
            )

        biz_data = data.get("data", {}).get("biz_data", {})
        session_id = biz_data.get("id") or biz_data.get("chat_session", {}).get("id")
        if not session_id:
            raise RuntimeError("Failed to obtain chat_session_id from DeepSeek response")
        return session_id

    def upload_file(self, data, filename, mime_type, session=None):
        sess = session or self._new_session()
        headers = self.auth.get_headers()
        headers.pop("Content-Type", None)

        pow_header = self.pow_solver.fetch_and_solve(
            sess, headers, target_path="/api/v0/file/upload_file"
        )
        headers["x-ds-pow-response"] = pow_header

        mp = CurlMime()
        mp.addpart(
            name="file",
            content_type=mime_type,
            filename=filename,
            data=data,
        )
        resp = sess.post(
            UPLOAD_FILE_URL,
            headers=headers,
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

        print(f"[upload] {file_id} status={status}")

        for attempt in range(15):
            if status == "SUCCESS":
                break
            time.sleep(1)
            try:
                poll_resp = sess.get(
                    f"{DEEPSEEK_BASE}/api/v0/file/fetch_files",
                    headers=self.auth.get_headers(),
                    params={"file_ids": file_id},
                    timeout=10,
                )
                print(f"[upload] poll #{attempt+1} HTTP {poll_resp.status_code}")
                if poll_resp.status_code == 200:
                    data = poll_resp.json()
                    biz = (data.get("data") or {}).get("biz_data") or {}
                    files = biz.get("files") if isinstance(biz, dict) else biz
                    if isinstance(files, list):
                        for f in files:
                            if isinstance(f, dict) and f.get("id") == file_id:
                                status = f.get("status")
                                print(f"[upload] poll #{attempt+1} status={status}")
                                break
                else:
                    print(f"[upload] poll body: {poll_resp.text[:200]}")
            except Exception as e:
                print(f"[upload] poll error: {e}")

        if status != "SUCCESS":
            print(f"[upload] WARN {file_id} still {status}")
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
    ):
        sess = session or self._new_session()
        headers = self.auth.get_headers()

        if not session_id:
            session_id = self.create_chat_session(sess)

        pow_header = self.pow_solver.fetch_and_solve(
            sess, headers, target_path="/api/v0/chat/completion"
        )
        headers["x-ds-pow-response"] = pow_header

        payload = {
            "chat_session_id": session_id,
            "parent_message_id": parent_message_id,
            "prompt": prompt,
            "ref_file_ids": ref_file_ids or [],
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
        }
        print("[chat] payload ref_file_ids=", payload["ref_file_ids"])

        resp = sess.post(
            COMPLETION_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=180,
        )
        if resp.status_code != 200:
            body = resp.text[:300] if hasattr(resp, "text") else ""
            yield {"type": "error", "message": f"HTTP {resp.status_code} - {body}"}
            return

        current_frag_type = "RESPONSE"

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            print("RAW:", line)

            if line.startswith("event:"):
                if line[6:].strip() == "close":
                    yield {"type": "done", "finish_reason": "stop"}
                    return
                continue

            if not line.startswith("data:"):
                continue

            line_content = line[5:].strip()
            if not line_content or line_content == "[DONE]":
                yield {"type": "done", "finish_reason": "stop"}
                return

            try:
                data = json.loads(line_content)
            except json.JSONDecodeError:
                continue

            if isinstance(data, dict):
                if data.get("code", 0) != 0:
                    yield {
                        "type": "error",
                        "message": f"DeepSeek stream error: {data.get('msg', 'Error')} "
                        f"(code: {data.get('code')})",
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

        yield {"type": "done", "finish_reason": "stop"}