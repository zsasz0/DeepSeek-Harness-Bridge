"""Bare opaque tokens (non-JWT) must load from the account folder."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from core.accounts import AccountPool
from core.auth import parse_credentials

T1 = "h5CokkGVv4BuXjqt2KUl5od+MY7Ej49ekRiSTaO8SakI/iAefwIhGkrVmsF9um4C"
T2 = "1YtkZTsqc3jCB+TluArLUIOjtGEgCBPldbGAjsew7NrmQHHywayX86/esWZS5G9G"


def user(text):
    return {"role": "user", "content": text}


class BareTokenTests(unittest.TestCase):
    def test_bare_opaque_token_is_parsed(self):
        token, cookie = parse_credentials(T1)
        self.assertEqual(token, T1)
        self.assertEqual(cookie, "")

    def test_second_bare_token_is_parsed(self):
        self.assertEqual(parse_credentials(T2)[0], T2)

    def test_json_wrapper_still_works(self):
        self.assertEqual(parse_credentials('{"token": "' + T1 + '"}')[0], T1)

    def test_cookie_string_still_parses(self):
        token, cookie = parse_credentials("sessionid=abc; userToken=" + T1)
        self.assertEqual(token, T1)
        self.assertIn("sessionid=abc", cookie)

    def test_netscape_still_parses(self):
        raw = ".deepseek.com\tTRUE\t/\tFALSE\t0\tuserToken\t" + T1
        self.assertEqual(parse_credentials(raw)[0], T1)

    def test_short_garbage_is_not_a_token(self):
        self.assertIsNone(parse_credentials("hello world")[0])

    def test_two_token_folder_loads_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, token in (("acct1.txt", T1), ("acct2.txt", T2)):
                with open(os.path.join(tmp, name), "w", encoding="ascii") as fh:
                    fh.write(token)
            with patch.dict(
                os.environ,
                {"DEEPSEEK_AUTH_DIR": tmp, "DEEPSEEK_AUTH_PATH": ""},
            ):
                pool = AccountPool(auth_manager=None)
        self.assertEqual(pool.size(), 2)
        self.assertEqual({a["id"] for a in pool.status()}, {"acct-1", "acct-2"})
        self.assertTrue(all(a["ready"] for a in pool.status()))

    def test_pool_rotates_between_two_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, token in (("acct1.txt", T1), ("acct2.txt", T2)):
                with open(os.path.join(tmp, name), "w", encoding="ascii") as fh:
                    fh.write(token)
            with patch.dict(
                os.environ,
                {"DEEPSEEK_AUTH_DIR": tmp, "DEEPSEEK_AUTH_PATH": ""},
            ):
                pool = AccountPool(auth_manager=None)
            first = pool.select("agent")
            pool.note_used(first)
            second = pool.select("agent")
            self.assertNotEqual(first.id, second.id)
            self.assertEqual({first.token, second.token}, {T1, T2})
            # Failover: cooling one leaves the other ready.
            pool.mark_failure(first, 429, retry_after=60)
            self.assertIs(pool.select("agent"), second)


if __name__ == "__main__":
    unittest.main()
