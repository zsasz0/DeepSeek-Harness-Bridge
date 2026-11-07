"""Account pool, header freshness, SSRF guard and proxy-key tests."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import server
from core.accounts import (
    Account,
    AccountPool,
    RATE_LIMIT_COOLDOWN,
)
from core.auth import build_headers

JWT_A = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY2NvdW50LWEifQ.c2lnbmF0dXJlLWE"
JWT_B = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhY2NvdW50LWIifQ.c2lnbmF0dXJlLWI"


def make_pool(*accounts):
    pool = AccountPool(auth_manager=None)
    pool._accounts = list(accounts)
    pool._loaded_from_files = True
    return pool


class PoolTests(unittest.TestCase):
    def test_select_prefers_sticky_account(self):
        first = Account("a", JWT_A)
        second = Account("b", JWT_B)
        pool = make_pool(first, second)
        self.assertIs(pool.select("agent", sticky_id="b"), second)

    def test_select_rotates_when_no_sticky(self):
        first = Account("a", JWT_A)
        second = Account("b", JWT_B)
        pool = make_pool(first, second)
        self.assertIs(pool.select("agent"), first)  # never used -> lowest id
        pool.note_used(first)
        self.assertIs(pool.select("agent"), second)

    def test_rate_limit_cooldown_uses_retry_after(self):
        account = Account("a", JWT_A)
        pool = make_pool(account)
        cooldown = pool.mark_failure(account, 429, retry_after=120)
        self.assertEqual(cooldown, 120)
        self.assertFalse(account.ready())
        self.assertIsNone(pool.select("agent"))

    def test_too_many_messages_is_rate_limit(self):
        account = Account("a", JWT_A)
        pool = make_pool(account)
        cooldown = pool.mark_failure(
            account, 400, None, "HTTP 400 - Too many messages"
        )
        self.assertEqual(cooldown, RATE_LIMIT_COOLDOWN)

    def test_auth_failure_cools_longer(self):
        account = Account("a", JWT_A)
        pool = make_pool(account)
        cooldown = pool.mark_failure(account, 401, None, "unauthorized")
        self.assertGreaterEqual(cooldown, 30 * 60)

    def test_success_clears_cooldown(self):
        account = Account("a", JWT_A, cooldown_until=10**12)
        pool = make_pool(account)
        pool.note_used(account)
        self.assertTrue(account.ready())

    def test_next_ready_in_reports_wait(self):
        account = Account("a", JWT_A)
        pool = make_pool(account)
        pool.mark_failure(account, 429, retry_after=30)
        self.assertGreater(pool.next_ready_in(), 0)

    def test_reload_from_auth_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index, token in enumerate((JWT_A, JWT_B), start=1):
                with open(os.path.join(tmp, f"acct{index}.json"), "w", encoding="utf-8") as fh:
                    json.dump({"token": token}, fh)
            with patch.dict(
                os.environ,
                {"DEEPSEEK_AUTH_DIR": tmp, "DEEPSEEK_AUTH_PATH": ""},
            ):
                pool = AccountPool(auth_manager=None)
            self.assertEqual(pool.size(), 2)
            self.assertEqual([a["id"] for a in pool.status()], ["acct-1", "acct-2"])

    def test_no_accounts_returns_none(self):
        pool = make_pool()
        self.assertIsNone(pool.select("agent"))
        self.assertEqual(pool.size(), 0)


class HeaderTests(unittest.TestCase):
    def test_headers_are_current(self):
        headers = build_headers(token=JWT_A, cookie_string="a=b")
        self.assertEqual(headers["x-client-version"], "2.0.0")
        self.assertEqual(headers["x-app-version"], "2.0.0")
        self.assertIn("Chrome/149", headers["User-Agent"])
        self.assertEqual(headers["Authorization"], f"Bearer {JWT_A}")
        self.assertEqual(headers["Cookie"], "a=b")

    def test_account_headers_use_own_token(self):
        account = Account("a", JWT_B)
        self.assertEqual(account.headers()["Authorization"], f"Bearer {JWT_B}")


class SSRFGuardTests(unittest.TestCase):
    def test_remote_url_is_never_fetched(self):
        with patch.object(server.client, "upload_file") as upload:
            result = server._upload_image("http://169.254.169.254/latest/meta-data/")
        self.assertIsNone(result)
        upload.assert_not_called()

    def test_inline_image_is_uploaded(self):
        with patch.object(server.client, "upload_file", return_value="file-1") as upload:
            result = server._upload_image("data:image/png;base64,aGVsbG8=")
        self.assertEqual(result, "file-1")
        upload.assert_called_once()


class ProxyKeyTests(unittest.TestCase):
    class _Request:
        def __init__(self, path, headers=None):
            self.url = type("URL", (), {"path": path})()
            self.headers = headers or {}

    class _Response:
        status_code = 200

    def _call(self, request, key):
        async def call_next(_request):
            return self._Response()

        with patch.object(server, "PROXY_API_KEY", key):
            return asyncio.run(server.require_proxy_key(request, call_next))

    def test_no_key_configured_allows_everything(self):
        response = self._call(self._Request("/v1/chat/completions"), "")
        self.assertEqual(response.status_code, 200)

    def test_missing_key_is_rejected(self):
        response = self._call(self._Request("/v1/chat/completions"), "secret")
        self.assertEqual(response.status_code, 401)

    def test_bearer_key_is_accepted(self):
        request = self._Request(
            "/v1/chat/completions", {"authorization": "Bearer secret"}
        )
        self.assertEqual(self._call(request, "secret").status_code, 200)

    def test_health_is_open(self):
        self.assertEqual(self._call(self._Request("/health"), "secret").status_code, 200)


class UpstreamErrorTests(unittest.TestCase):
    def test_auth_business_code_is_detected(self):
        from core.client import _is_auth_error

        self.assertTrue(_is_auth_error(40003, "Authorization Failed (invalid token)"))
        self.assertFalse(_is_auth_error(0, "ok"))
        self.assertFalse(_is_auth_error(50001, "quota"))

    def test_collect_raises_status_error(self):
        from core.client import HTTPStatusError

        events = [
            {"type": "http_error", "status": 429, "retry_after": 12.0},
            {"type": "error", "message": "HTTP 429 - slow down"},
        ]
        with self.assertRaises(HTTPStatusError) as ctx:
            server._collect(events)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after, 12.0)

    def test_too_many_messages_maps_to_429(self):
        from core.client import HTTPStatusError

        account = Account("a", JWT_A)
        agent = server.agent_sessions.get("error-map-test")
        exc = HTTPStatusError("HTTP 400 - Too many messages", 400)
        http_exc = server._account_failure(agent, account, exc)
        self.assertEqual(http_exc.status_code, 429)
        self.assertIn("Retry-After", http_exc.headers)
        server.agent_sessions.reset("error-map-test")


if __name__ == "__main__":
    unittest.main()
