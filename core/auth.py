"""
DeepSeek authentication and session management.

Handles parsing credentials from the many formats users paste (JWT, cookie
string, Netscape cookie file, localStorage JSON, cURL), validating them
against the live DeepSeek API, and persisting them to config.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, Optional, Tuple

from curl_cffi import requests

DEEPSEEK_BASE = "https://chat.deepseek.com"
CURRENT_USER_URL = f"{DEEPSEEK_BASE}/api/v0/users/current"

# JWT = header.payload.signature, all base64url segments.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+")
_COOKIE_PAIR_RE = re.compile(r"([A-Za-z0-9_\-.]+)=([^;,\s\r\n]+)")
_COOKIE_PREFIX_RE = re.compile(r"^(?:cookie:\s*)", re.IGNORECASE)
_NON_COOKIE_NAMES = {"http", "https", "bearer", "authorization"}


def _dedupe_cookies(pairs: list[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for pair in pairs:
        key = pair.split("=", 1)[0]
        if key not in seen:
            seen.add(key)
            out.append(pair)
    return "; ".join(out)


class AuthManager:
    """Loads, validates and persists DeepSeek web credentials."""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self.base_dir = base_dir or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        self.config_path = os.path.join(self.base_dir, "config.json")
        self.cookie_file_path = os.path.join(self.base_dir, "cookie_deepseek.txt")
        self.token: Optional[str] = None
        self.cookie_string: str = ""
        self.user_info: Optional[Dict[str, Any]] = None
        self.load()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def parse_credentials(self, raw_text: str) -> Tuple[Optional[str], str]:
        """Extract (userToken, cookie_header) from arbitrary pasted text."""
        raw = (raw_text or "").strip()
        if not raw:
            return None, ""

        token: Optional[str] = None
        cookie_parts: list[str] = []

        # 1. JSON blob (e.g. localStorage copy)
        if raw.startswith("{") and raw.endswith("}"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    token = (
                        data.get("value")
                        or data.get("token")
                        or data.get("userToken")
                        or data.get("data", {}).get("token")
                    )
            except Exception:
                pass

        # 2. Bare JWT anywhere in the input
        if not token:
            match = _JWT_RE.search(raw)
            if match:
                token = match.group(0)

        lines = raw.splitlines()
        is_netscape = any(
            "\t" in line for line in lines if line and not line.startswith("#")
        )

        if is_netscape:
            # 3. Netscape cookies.txt format: domain, flag, path, secure,
            #    expiry, name, value (tab separated).
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split("\t")
                if len(parts) >= 7:
                    name, value = parts[5].strip(), parts[6].strip()
                    cookie_parts.append(f"{name}={value}")
                    if name == "userToken" and not token:
                        token = value
        else:
            # 4. Raw Cookie header / cURL dump
            cookie_text = _COOKIE_PREFIX_RE.sub("", raw)
            for name, value in _COOKIE_PAIR_RE.findall(cookie_text):
                if name.lower() in _NON_COOKIE_NAMES:
                    continue
                cookie_parts.append(f"{name}={value}")
                if name == "userToken" and not token:
                    token = value

        return token, _dedupe_cookies(cookie_parts)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> bool:
        """Load credentials from config.json, then cookie_deepseek.txt."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.token = data.get("token") or self.token
                self.cookie_string = data.get("cookie") or self.cookie_string
                if self.token:
                    return True
            except Exception as exc:
                print(f"[auth] Error reading {self.config_path}: {exc}", file=sys.stderr)

        if os.path.exists(self.cookie_file_path):
            try:
                with open(self.cookie_file_path, "r", encoding="utf-8") as fh:
                    token, cookies = self.parse_credentials(fh.read())
                self.token = token or self.token
                self.cookie_string = cookies or self.cookie_string
                if self.token:
                    return True
            except Exception as exc:
                print(
                    f"[auth] Error reading {self.cookie_file_path}: {exc}",
                    file=sys.stderr,
                )

        return bool(self.token)

    def save(
        self, token: Optional[str] = None, cookie_string: Optional[str] = None
    ) -> None:
        if token is not None:
            self.token = token
        if cookie_string is not None:
            self.cookie_string = cookie_string

        payload = {
            "token": self.token,
            "cookie": self.cookie_string,
            "updated_at": time.time(),
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            print(f"[auth] Credentials saved to {self.config_path}")
        except Exception as exc:
            print(f"[auth] Error saving {self.config_path}: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Validation & headers
    # ------------------------------------------------------------------
    def validate_session(
        self,
        token: Optional[str] = None,
        cookie_string: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Check credentials against /api/v0/users/current."""
        target_token = token or self.token
        target_cookie = cookie_string if cookie_string is not None else self.cookie_string

        if not target_token:
            return False, None, "Missing token. Please provide your userToken or cookies."

        headers = self.get_headers(token=target_token, cookie_string=target_cookie)
        try:
            session = requests.Session(impersonate="chrome124")
            resp = session.get(CURRENT_USER_URL, headers=headers, timeout=10)
        except Exception as exc:
            return False, None, f"Connection error validating session: {exc}"

        if resp.status_code != 200:
            return False, None, f"HTTP Error {resp.status_code}: {resp.text[:150]}"

        try:
            data = resp.json()
        except Exception:
            return False, None, "DeepSeek returned a non-JSON response."

        if data.get("code") != 0:
            msg = data.get("msg", "Unknown error")
            return False, None, f"DeepSeek rejected token: {msg} (code: {data.get('code')})"

        self.user_info = data.get("data")
        return True, self.user_info, "Session valid and active!"

    def get_headers(
        self, token: Optional[str] = None, cookie_string: Optional[str] = None
    ) -> Dict[str, str]:
        """Browser-like headers required by DeepSeek's web API."""
        tok = token or self.token
        ck = cookie_string if cookie_string is not None else self.cookie_string

        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "DNT": "1",
            "Origin": DEEPSEEK_BASE,
            "Referer": f"{DEEPSEEK_BASE}/",
            "Sec-Ch-Ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
            ),
            "x-app-version": "20241129.1",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-version": "1.0.0-always",
        }
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        if ck:
            headers["Cookie"] = ck
        return headers