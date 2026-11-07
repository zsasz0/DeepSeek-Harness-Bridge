"""Agent session reuse for DeepSeek Web Chat.

One agent id maps to one live DeepSeek chat. The chat has already seen the
first N client messages; each turn sends only messages[N:] (plus the echoed
assistant reply is skipped from the prompt since the remote chat generated it).

Count-based tracking avoids the tool-call shape mismatch: the client echoes an
assistant tool call in OpenAI shape, while the remote chat saw envelope text.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

SESSION_TTL_MS = int(os.environ.get("DEEPSEEK_SESSION_TTL_MS", str(2 * 60 * 60 * 1000)))
MAX_MESSAGE_DEPTH = int(os.environ.get("DEEPSEEK_MAX_MESSAGE_DEPTH", "100"))
MAX_PROMPT_CHARS = int(os.environ.get("DEEPSEEK_MAX_PROMPT_CHARS", "80000"))
MAX_SESSION_CHARS = int(
    os.environ.get("DEEPSEEK_MAX_SESSION_CHARS", str(MAX_PROMPT_CHARS * 3))
)
RECOVERY_HISTORY_TURNS = int(os.environ.get("DEEPSEEK_RECOVERY_HISTORY_TURNS", "8"))


def _tools_key(tools: Optional[List[Dict[str, Any]]]) -> str:
    return json.dumps(tools or [], sort_keys=True, ensure_ascii=False)


def _norm(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _turn_signature(message: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(message.get("role", "")),
        _norm(message.get("content", "")),
        str(message.get("name") or message.get("tool_call_id") or ""),
    )


class AgentSession:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.chat_session_id: Optional[str] = None
        self.parent_message_id: Optional[str] = None
        self.account_id: Optional[str] = None
        self.tools_key: Optional[str] = None
        self.sent_count = 0
        self.sent_signatures: List[Tuple[str, str, str]] = []
        self.message_count = 0
        self.remote_chars = 0
        self.created_at = time.time()
        self.last_used = self.created_at
        self.history: List[Dict[str, Any]] = []

    def to_public(self) -> Dict[str, Any]:
        return {
            "agent": self.agent_id,
            "chat_session_id": self.chat_session_id,
            "parent_message_id": self.parent_message_id,
            "account_id": self.account_id,
            "message_count": self.message_count,
            "remote_chars": self.remote_chars,
            "sent_messages": self.sent_count,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    def reset_remote(self) -> None:
        self.chat_session_id = None
        self.parent_message_id = None
        self.tools_key = None
        self.sent_count = 0
        self.sent_signatures = []
        self.message_count = 0
        self.remote_chars = 0
        self.created_at = time.time()

    def needs_reset(self, now: Optional[float] = None) -> Optional[str]:
        now = now or time.time()
        if self.chat_session_id is None:
            return None
        if self.message_count >= MAX_MESSAGE_DEPTH:
            return "max_message_depth"
        if now - self.created_at > SESSION_TTL_MS:
            return "session_ttl"
        if self.remote_chars >= MAX_SESSION_CHARS:
            return "max_context_chars"
        return None

    def plan(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return ``(messages_to_send, reset_reason)``.

        ``reset_reason`` is None when the live chat can be continued and
        ``messages_to_send`` is the new tail; otherwise the full conversation
        is returned and a fresh chat must be created.
        """
        if self.chat_session_id is None:
            return messages, None

        reason = self.needs_reset()
        if reason:
            return messages, reason

        if _tools_key(tools) != self.tools_key:
            return messages, "tools_changed"

        if not self.sent_signatures:
            return messages, "history_mismatch"

        if len(messages) < self.sent_count:
            return messages, "history_mismatch"

        incoming = [_turn_signature(m) for m in messages[: self.sent_count]]
        if incoming != self.sent_signatures:
            return messages, "history_mismatch"

        delta = messages[self.sent_count :]
        # The client echoes our own assistant reply back; the remote chat
        # already contains it, so never send it again.
        while delta and str(delta[0].get("role", "")) == "assistant":
            delta = delta[1:]
        if not delta:
            return messages, "history_mismatch"
        return delta, None

    def mark_sent(
        self,
        sent_messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        prompt_chars: int,
        output_chars: int,
        message_id: Optional[str],
    ) -> None:
        """Record client messages the remote chat now contains."""
        for message in sent_messages:
            self.sent_signatures.append(_turn_signature(message))
        self.sent_count = len(self.sent_signatures)
        self.tools_key = _tools_key(tools)
        self.message_count += 1
        self.remote_chars += prompt_chars + output_chars
        if message_id:
            self.parent_message_id = message_id
        self.history = (self.history + list(sent_messages))[-RECOVERY_HISTORY_TURNS * 2 :]
        self.last_used = time.time()

    def recovery_prefix(self) -> List[Dict[str, Any]]:
        return list(self.history)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, AgentSession] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> AgentSession:
        with self._lock:
            session = self._sessions.get(agent_id)
            if session is None:
                session = AgentSession(agent_id)
                self._sessions[agent_id] = session
            return session

    def reset(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id == "all":
                self._sessions.clear()
                return True
            session = self._sessions.get(agent_id)
            if session is None:
                return False
            session.reset_remote()
            return True

    def sweep(self, max_idle_ms: Optional[int] = None) -> int:
        max_idle = (
            (max_idle_ms if max_idle_ms is not None else SESSION_TTL_MS * 2) / 1000.0
        )
        now = time.time()
        removed = 0
        with self._lock:
            for agent_id in list(self._sessions):
                if now - self._sessions[agent_id].last_used >= max_idle:
                    del self._sessions[agent_id]
                    removed += 1
        return removed

    def list_public(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.to_public() for s in self._sessions.values()]


sessions = SessionManager()
