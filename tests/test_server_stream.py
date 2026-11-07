"""End-to-end handler tests: SSE delta shape, 502 on garbage, self-correction.

Run with: ./venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import server
from server import ChatCompletionRequest, create_chat_completion

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
TALK = '{"content": "4", "tool_calls": []}'


def _sse_json(chunks):
    """Parse SSE strings into (delta, finish_reason) pairs; drop [DONE]."""
    out = []
    for chunk in chunks:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                out.append((None, "DONE"))
                continue
            obj = json.loads(payload)
            choices = obj.get("choices") or []
            if not choices:
                # usage-only chunk (stream_options.include_usage)
                out.append(({"__usage__": obj.get("usage")}, "usage"))
                continue
            choice = choices[0]
            out.append((choice["delta"], choice["finish_reason"]))
    return out


def _collect(response):
    async def run():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    return asyncio.run(run())


class FakeClient:
    """Replaces DeepSeekWebClient with scripted replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def _new_session(self):
        return None

    def create_chat_session(self, session=None):
        return "fake-session"

    def stream_chat(self, prompt, **kwargs):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, list):
            for event in reply:
                yield event
            return
        yield {"type": "text", "content": reply}


def _patch_client(fake):
    return [
        patch.object(server.client, "_new_session", fake._new_session),
        patch.object(server.client, "create_chat_session", fake.create_chat_session),
        patch.object(server.client, "stream_chat", fake.stream_chat),
        patch.object(server.auth_manager, "token", "test-token"),
    ]


class StreamTests(unittest.TestCase):
    def _run(self, replies, **request_overrides):
        fake = FakeClient(replies)
        patches = _patch_client(fake)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        req = ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "list files"}],
            tools=TOOLS,
            stream=True,
        )
        for key, value in request_overrides.items():
            setattr(req, key, value)
        # The handler runs generation + parsing before the response starts, so a
        # hard failure surfaces as a synchronous HTTPException.
        response = create_chat_completion(req)
        return _sse_json(_collect(response)), fake

    def test_tool_call_deltas(self):
        events, fake = self._run([GOOD])
        deltas = [d for d, _ in events if d is not None]

        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual(events[-1], (None, "DONE"))
        self.assertEqual(events[-2], ({}, "tool_calls"))

        first = deltas[0]
        self.assertEqual(first["role"], "assistant")
        call = first["tool_calls"][0]
        self.assertEqual(call["index"], 0)
        self.assertTrue(call["id"].startswith("call_"))
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "bash")
        self.assertEqual(call["function"]["arguments"], "")

        second = deltas[1]
        self.assertNotIn("role", second)
        self.assertEqual(
            second["tool_calls"][0]["function"]["arguments"],
            '{"command": "ls -la"}',
        )
        self.assertNotIn("id", second["tool_calls"][0])

        for delta, _ in events:
            if delta:
                self.assertNotIn(None, delta.values(), "never emit role: None")

    def test_stream_errors_before_headers_on_garbage(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run([GARBAGE, GARBAGE])
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("could not be parsed", ctx.exception.detail)

    def test_retry_recovers_once(self):
        events, fake = self._run([GARBAGE, GOOD])
        self.assertEqual(len(fake.prompts), 2)
        self.assertIn("[Correction]", fake.prompts[1])
        self.assertIn("[Assistant previous answer]", fake.prompts[1])
        self.assertIn("Valid tool names: bash", fake.prompts[1])

        deltas = [d for d, _ in events if d is not None]
        self.assertEqual(deltas[0]["tool_calls"][0]["function"]["name"], "bash")

    def test_talk_only_stream(self):
        events, fake = self._run([TALK])
        self.assertEqual(len(fake.prompts), 1, "valid talk-only reply must not retry")
        deltas = [d for d, _ in events if d is not None]
        self.assertEqual(deltas[0]["role"], "assistant")
        self.assertEqual(deltas[0]["content"], "4")
        self.assertEqual(events[-2], ({}, "stop"))
        self.assertEqual(events[-1], (None, "DONE"))

    def test_empty_reply_fails_loudly(self):
        # An empty assistant message is useless; 502 lets the client back off.
        with self.assertRaises(HTTPException) as ctx:
            self._run([""])
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("empty response", ctx.exception.detail)

    def test_usage_chunk_when_requested(self):
        events, _ = self._run([TALK], stream_options={"include_usage": True})
        usage = [d for d, kind in events if kind == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertGreater(usage[0]["__usage__"]["total_tokens"], 0)
        self.assertEqual(events[-1], (None, "DONE"))


class NonStreamTests(unittest.TestCase):
    def _run(self, replies):
        fake = FakeClient(replies)
        patches = _patch_client(fake)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        req = ChatCompletionRequest(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "list files"}],
            tools=TOOLS,
            stream=False,
        )
        return create_chat_completion(req), fake

    def test_tool_call_response(self):
        body, _ = self._run([GOOD])
        choice = body["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        message = choice["message"]
        self.assertIsNone(message["content"])
        self.assertEqual(message["role"], "assistant")
        call = message["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "bash")
        self.assertIsInstance(call["function"]["arguments"], str)
        self.assertEqual(
            json.loads(call["function"]["arguments"]), {"command": "ls -la"}
        )
        self.assertEqual(
            body["usage"]["total_tokens"],
            body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"],
        )

    def test_talk_response(self):
        body, fake = self._run([TALK])
        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["choices"][0]["message"]["content"], "4")

    def test_garbage_raises_502(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run([GARBAGE, GARBAGE])
        self.assertEqual(ctx.exception.status_code, 502)

    def test_content_only_envelope_is_unwrapped(self):
        # The model omitted the empty "tool_calls" array.
        body, _ = self._run(['{"content": "Yes, the function allocates it."}'])
        self.assertEqual(
            body["choices"][0]["message"]["content"],
            "Yes, the function allocates it.",
        )

    def test_truncated_envelope_never_reaches_client(self):
        body, _ = self._run(
            ['{"content": "Long answer about malloc.\\n\\n```c\\nchar *out = malloc(1);']
        )
        content = body["choices"][0]["message"]["content"]
        self.assertNotIn('{"content"', content)
        self.assertIn("Long answer about malloc.", content)


if __name__ == "__main__":
    unittest.main()
