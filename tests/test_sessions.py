"""Session reuse tests: delta planning, reset triggers, manager endpoints."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core import sessions as sessions_module
from core.sessions import AgentSession, SessionManager


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


class PlanTests(unittest.TestCase):
    def test_first_turn_sends_full_history(self):
        s = AgentSession("a")
        msgs = [user("hi")]
        planned, reason = s.plan(msgs, None)
        self.assertEqual(planned, msgs)
        self.assertIsNone(reason)
        self.assertIsNone(s.chat_session_id)

    def test_second_turn_sends_only_new_user_message(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")

        msgs = [user("hi"), assistant("hello"), user("second")]
        planned, reason = s.plan(msgs, None)
        self.assertIsNone(reason)
        self.assertEqual(planned, [user("second")])
        self.assertEqual(s.parent_message_id, "msg-1")

    def test_rewritten_history_resets(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")

        msgs = [user("completely different"), assistant("x"), user("y")]
        planned, reason = s.plan(msgs, None)
        self.assertEqual(reason, "history_mismatch")
        self.assertEqual(planned, msgs)

    def test_tools_change_resets(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")

        tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
        planned, reason = s.plan([user("hi"), assistant("ok"), user("again")], tools)
        self.assertEqual(reason, "tools_changed")
        self.assertEqual(planned, [user("hi"), assistant("ok"), user("again")])

    def test_ttl_resets(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")
        with patch.object(sessions_module, "SESSION_TTL_MS", -1):
            _, reason = s.plan([user("hi"), assistant("ok"), user("again")], None)
        self.assertEqual(reason, "session_ttl")

    def test_max_message_depth_resets(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")
        with patch.object(sessions_module, "MAX_MESSAGE_DEPTH", 1):
            _, reason = s.plan([user("hi"), assistant("ok"), user("again")], None)
        self.assertEqual(reason, "max_message_depth")

    def test_max_context_chars_resets(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")
        # The char ceiling is skipped for the first turns after a reset.
        s.turns_since_reset = 5
        with patch.object(sessions_module, "MAX_SESSION_CHARS", 1):
            _, reason = s.plan([user("hi"), assistant("ok"), user("again")], None)
        self.assertEqual(reason, "max_context_chars")

    def test_no_reset_loop_when_prompt_resent(self):
        """Full history resent every turn must not trigger a reset each turn."""
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        big = "x" * 20000
        messages = [user(big)]
        s.mark_sent(messages, None, 20000, 100, "msg-1", fresh_chat=True)
        for turn in range(1, 6):
            messages = messages + [assistant("ok"), user(big)]
            planned, reason = s.plan(messages, None)
            self.assertIsNone(reason, f"turn {turn} reset: {reason}")
            s.mark_sent(planned, None, 20000, 100, f"msg-{turn + 1}")

    def test_fresh_chat_resets_char_counter(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.remote_chars = 999999
        s.mark_sent([user("hi")], None, 10, 5, "m", fresh_chat=True)
        self.assertEqual(s.remote_chars, 15)

    def test_reset_cooldown_skips_char_ceiling(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.remote_chars = 10**9
        s.turns_since_reset = 0
        with patch.object(sessions_module, "MAX_SESSION_CHARS", 1):
            self.assertIsNone(s.needs_reset())
            s.turns_since_reset = sessions_module.RESET_COOLDOWN_TURNS
            self.assertEqual(s.needs_reset(), "max_context_chars")

    def test_reset_remote_keeps_history(self):
        s = AgentSession("a")
        s.chat_session_id = "chat-1"
        s.mark_sent([user("hi")], None, 10, 5, "msg-1")
        s.reset_remote()
        self.assertIsNone(s.chat_session_id)
        self.assertIsNone(s.parent_message_id)
        self.assertEqual(s.sent_count, 0)
        self.assertEqual(s.history, [user("hi")])


class ManagerTests(unittest.TestCase):
    def test_get_is_stable_and_reset_all_clears(self):
        m = SessionManager()
        first = m.get("agent")
        self.assertIs(first, m.get("agent"))
        first.chat_session_id = "chat"
        self.assertEqual(len(m.list_public()), 1)
        self.assertTrue(m.reset("all"))
        self.assertEqual(m.list_public(), [])

    def test_reset_unknown_agent_returns_false(self):
        m = SessionManager()
        self.assertFalse(m.reset("nope"))

    def test_sweep_drops_idle(self):
        m = SessionManager()
        m.get("agent")
        self.assertEqual(m.sweep(0), 1)


class HandlerReuseTests(unittest.TestCase):
    """End-to-end: the second turn must not resend the whole conversation."""

    class FakeClient:
        def __init__(self):
            self.prompts = []

        def _new_session(self):
            return None

        def create_chat_session(self, session=None, headers=None):
            return "chat-1"

        def stream_chat(self, prompt, **kwargs):
            self.prompts.append(prompt)
            yield {"type": "text", "content": "hello"}
            yield {"type": "message_id", "id": "msg-1"}
            yield {"type": "done", "finish_reason": "stop"}

    def test_second_turn_sends_delta_only(self):
        import server

        fake = self.FakeClient()
        with patch.object(server.client, "_new_session", fake._new_session), patch.object(
            server.client, "create_chat_session", fake.create_chat_session
        ), patch.object(server.client, "stream_chat", fake.stream_chat), patch.object(
            server.auth_manager, "token", "t"
        ):
            first = server.ChatCompletionRequest(
                model="deepseek-chat", messages=[user("hi")], stream=False
            )
            server.create_chat_completion(first, x_agent_session="reuse-test")

            second = server.ChatCompletionRequest(
                model="deepseek-chat",
                messages=[user("hi"), assistant("hello"), user("second")],
                stream=False,
            )
            server.create_chat_completion(second, x_agent_session="reuse-test")

        self.assertEqual(len(fake.prompts), 2)
        self.assertIn("hi", fake.prompts[0])
        self.assertIn("second", fake.prompts[1])
        self.assertNotIn("[User]\nhi", fake.prompts[1])

        server.agent_sessions.reset("reuse-test")


if __name__ == "__main__":
    unittest.main()
