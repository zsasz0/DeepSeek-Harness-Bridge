"""Gemini cookie parsing, model routing and OpenAI-compatible handler tests."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import server
from core.gemini import (
    GeminiModelError,
    is_gemini_model,
    parse_gemini_cookies,
)
from server import ChatCompletionRequest

PSID = "g.a000test" + "x" * 80
PSIDTS = "sidts-CjEBtest" + "y" * 40

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run shell",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]
GOOD = (
    '{"content": null, "tool_calls": '
    '[{"name": "bash", "arguments": {"command": "ls -la"}}]}'
)
GARBAGE = (
    "<\uff5c\uff5cDSML\uff5c\uff5c calls>\n"
    "dmsl {broken amth"
)


def _sse(chunks):
    events = []
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                events.append((None, "DONE"))
                continue
            obj = json.loads(payload)
            if obj.get("error"):
                events.append(({"__error__": obj["error"]}, "error"))
                continue
            choices = obj.get("choices") or []
            if not choices:
                events.append(({"__usage__": obj.get("usage")}, "usage"))
                continue
            events.append((choices[0]["delta"], choices[0]["finish_reason"]))
    return events


def _collect(response):
    async def run():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    return asyncio.run(run())


class CookieParsingTests(unittest.TestCase):
    def test_json_object(self):
        raw = json.dumps({"__Secure-1PSID": PSID, "__Secure-1PSIDTS": PSIDTS})
        self.assertEqual(parse_gemini_cookies(raw), (PSID, PSIDTS))

    def test_json_array_from_extension(self):
        raw = json.dumps(
            [
                {"name": "__Secure-1PSID", "value": PSID},
                {"name": "__Secure-1PSIDTS", "value": PSIDTS},
            ]
        )
        self.assertEqual(parse_gemini_cookies(raw), (PSID, PSIDTS))

    def test_cookie_header(self):
        raw = f"Cookie: __Secure-1PSID={PSID}; __Secure-1PSIDTS={PSIDTS}; SID=x"
        self.assertEqual(parse_gemini_cookies(raw), (PSID, PSIDTS))

    def test_netscape_file(self):
        raw = (
            f".google.com\tTRUE\t/\tTRUE\t0\t__Secure-1PSID\t{PSID}\n"
            f".google.com\tTRUE\t/\tTRUE\t0\t__Secure-1PSIDTS\t{PSIDTS}\n"
        )
        self.assertEqual(parse_gemini_cookies(raw), (PSID, PSIDTS))

    def test_two_bare_values(self):
        self.assertEqual(parse_gemini_cookies(f"{PSID}\n{PSIDTS}"), (PSID, PSIDTS))

    def test_missing_psid_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_gemini_cookies("")
        with self.assertRaises(ValueError):
            parse_gemini_cookies("__Secure-1PSIDTS=sidts-abc1234567890")

    def test_model_prefix_routing(self):
        self.assertTrue(is_gemini_model("gemini-flash"))
        self.assertTrue(is_gemini_model("Gemini-Pro"))
        self.assertFalse(is_gemini_model("deepseek-chat"))
        self.assertFalse(is_gemini_model(None))


class GeminiHandlerTests(unittest.TestCase):
    def _status(self, configured=True, connected=True):
        return {
            "configured": configured,
            "connected": connected,
            "message": "ok",
            "models": ["gemini-flash"],
        }

    def _call(self, complete=None, stream_fn=None, **overrides):
        patches = [
            patch.object(server.gemini, "status", return_value=self._status()),
            patch.object(
                server.gemini,
                "complete",
                side_effect=complete if complete else lambda *a, **k: ("hello", "thinking"),
            ),
        ]
        if stream_fn is not None:
            patches.append(patch.object(server.gemini, "stream", side_effect=stream_fn))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        payload = {
            "model": "gemini-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        payload.update(overrides)
        return server._gemini_chat_completion(ChatCompletionRequest(**payload))

    def test_not_configured_is_401(self):
        with patch.object(
            server.gemini,
            "status",
            return_value={
                "configured": False,
                "connected": False,
                "message": "off",
                "models": [],
            },
        ):
            with self.assertRaises(HTTPException) as ctx:
                server._gemini_chat_completion(
                    ChatCompletionRequest(
                        model="gemini-flash",
                        messages=[{"role": "user", "content": "hi"}],
                    )
                )
        self.assertEqual(ctx.exception.status_code, 401)

    def test_non_stream_text(self):
        body = self._call()
        choice = body["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "hello")
        self.assertEqual(choice["message"]["reasoning_content"], "thinking")

    def test_stream_text(self):
        def fake_stream(*args, **kwargs):
            yield ("thinking", "hmm")
            yield ("text", "hel")
            yield ("text", "lo")

        response = self._call(stream_fn=fake_stream, stream=True)
        events = _sse(_collect(response))
        text = "".join(
            delta.get("content", "") for delta, _ in events if delta and "content" in delta
        )
        thinking = "".join(
            delta.get("reasoning_content", "")
            for delta, _ in events
            if delta and "reasoning_content" in delta
        )
        self.assertEqual(text, "hello")
        self.assertEqual(thinking, "hmm")
        self.assertEqual(events[-2], ({}, "stop"))
        self.assertEqual(events[-1], (None, "DONE"))

    def test_tool_call_non_stream(self):
        body = self._call(complete=lambda *a, **k: (GOOD, ""), tools=TOOLS)
        choice = body["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "bash")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"command": "ls -la"})

    def test_tool_call_self_correction(self):
        calls = []

        def complete(*args, **kwargs):
            calls.append(args[0])
            return (GARBAGE, "") if len(calls) == 1 else (GOOD, "")

        body = self._call(complete=complete, tools=TOOLS)
        self.assertEqual(len(calls), 2)
        self.assertIn("[Correction]", calls[1])
        self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")

    def test_unparsable_tool_call_is_502(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(complete=lambda *a, **k: (GARBAGE, ""), tools=TOOLS)
        self.assertEqual(ctx.exception.status_code, 502)

    def test_unknown_model_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(
                complete=lambda *a, **k: (_ for _ in ()).throw(
                    GeminiModelError("bad model")
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_chat_completion_routes_gemini(self):
        with patch.object(
            server, "_gemini_chat_completion", return_value={"routed": True}
        ) as handler:
            result = server.create_chat_completion(
                ChatCompletionRequest(
                    model="gemini-pro",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )
        self.assertEqual(result, {"routed": True})
        handler.assert_called_once()

    def test_models_list_includes_gemini(self):
        data = asyncio.run(server.list_models())["data"]
        ids = {model["id"] for model in data}
        self.assertIn("gemini-flash", ids)
        self.assertIn("deepseek-chat", ids)


class GeminiEndpointTests(unittest.TestCase):
    def test_connect_delegates_empty_text_to_saved_cookies(self):
        with patch.object(
            server.gemini, "connect", return_value={"success": True}
        ) as connect:
            result = server.gemini_connect(server.AccountCreateRequest(raw_text="  "))
        self.assertTrue(result["success"])
        connect.assert_called_once_with(raw_text=None)

    def test_status_and_disconnect_delegate(self):
        with patch.object(
            server.gemini, "status", return_value={"connected": True}
        ) as status:
            self.assertEqual(server.gemini_status(), {"connected": True})
            status.assert_called_once()
        with patch.object(
            server.gemini, "disconnect", return_value={"success": True}
        ) as disconnect:
            self.assertTrue(server.gemini_disconnect()["success"])
            disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
