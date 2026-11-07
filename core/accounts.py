"""Multi-account pool for DeepSeek Web API.

Several ``userToken`` accounts can be supplied so a single rate-limited or
expired account does not take the whole proxy down. Accounts are sticky per
agent session, so an ongoing chat keeps using the same identity, and rotate
only when an account is cooling down after a failure.

Sources, in priority order:

* ``DEEPSEEK_AUTH_DIR``  - directory of ``*.json`` / ``*.txt`` credential files
* ``DEEPSEEK_AUTH_PATH`` - comma separated list of credential files
* ``config.json``        - single account managed by ``AuthManager``

A credential file can be any format ``parse_credentials`` understands: a bare
``userToken`` JWT, a Cookie header, a Netscape ``cookies.txt`` or a JSON blob.
"""

from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .auth import AuthManager, build_headers, parse_credentials
from .auth import requests as _requests
from .auth import CURRENT_USER_URL

# Cooldowns (seconds).
AUTH_FAILURE_COOLDOWN = 30 * 60  # 401/403: the token itself is bad
RATE_LIMIT_COOLDOWN = 60  # 429 or DeepSeek's "Too many messages"
SERVER_ERROR_COOLDOWN = 30
MAX_BACKOFF = 300


@dataclass
class Account:
    id: str
    token: Optional[str] = None
    cookie: str = ""
    cooldown_until: float = 0.0
    failures: int = 0
    last_used: float = 0.0
    last_status: Optional[int] = None
    source: str = ""
    valid: Optional[bool] = None
    valid_message: str = ""
    checked_at: float = 0.0

    def headers(self) -> Dict[str, str]:
        return build_headers(token=self.token, cookie_string=self.cookie)

    def ready(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) >= self.cooldown_until

    @property
    def cooling_for(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def to_public(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "ready": self.ready(),
            "cooling_for": round(self.cooling_for, 1),
            "failures": self.failures,
            "last_status": self.last_status,
            "last_used": self.last_used or None,
            "valid": self.valid,
            "valid_message": self.valid_message,
            "checked_at": self.checked_at or None,
        }


class AccountPool:
    def __init__(self, auth_manager: Optional[AuthManager] = None) -> None:
        self.auth = auth_manager
        self._accounts: List[Account] = []
        self._lock = threading.RLock()
        self._rr = 0
        self._loaded_from_files = False
        self.reload()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _project_accounts_dir(self) -> Optional[str]:
        """``<project>/accounts`` next to ``core/``, when it exists."""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(base, "accounts")
        return candidate if os.path.isdir(candidate) else None

    def _credential_paths(self) -> List[str]:
        paths: List[str] = []
        directory = os.environ.get("DEEPSEEK_AUTH_DIR", "").strip()
        if not directory or not os.path.isdir(directory):
            # No env var: fall back to the in-project accounts folder.
            directory = self._project_accounts_dir() or ""
        if directory and os.path.isdir(directory):
            for pattern in ("*.json", "*.txt"):
                paths.extend(sorted(glob.glob(os.path.join(directory, pattern))))
        raw_list = os.environ.get("DEEPSEEK_AUTH_PATH", "").strip()
        if raw_list:
            for item in raw_list.split(","):
                item = item.strip()
                if item and os.path.isfile(item):
                    paths.append(item)
        # De-duplicate while preserving order.
        seen = set()
        unique = []
        for path in paths:
            resolved = os.path.abspath(path)
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    def reload(self) -> int:
        """(Re)load accounts from credential files. Returns account count."""
        accounts: List[Account] = []
        for index, path in enumerate(self._credential_paths()):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = handle.read()
            except OSError:
                continue
            token, cookie = parse_credentials(raw)
            if not token and not cookie:
                continue
            accounts.append(
                Account(
                    id=f"acct-{index + 1}",
                    token=token,
                    cookie=cookie,
                    source=os.path.basename(path),
                )
            )

        with self._lock:
            self._loaded_from_files = bool(accounts)
            self._accounts = accounts
            self._rr = 0
        return len(accounts)

    def _synthesized(self) -> List[Account]:
        """Single-account mode backed by ``AuthManager`` (config.json)."""
        if not self.auth or not self.auth.token:
            return []
        cookie = self.auth.cookie_string or ""
        current = self._accounts[0] if self._accounts else None
        if current and current.token == self.auth.token and current.cookie == cookie:
            return self._accounts
        account = Account(
            id="config",
            token=self.auth.token,
            cookie=cookie,
            source="config.json",
        )
        if current:
            # Preserve cooldown state across token refreshes of the same slot.
            account.cooldown_until = current.cooldown_until
            account.failures = current.failures
            account.last_status = current.last_status
            account.last_used = current.last_used
        with self._lock:
            self._accounts = [account]
        return self._accounts

    def _all(self) -> List[Account]:
        if self._loaded_from_files:
            return list(self._accounts)
        return list(self._synthesized())

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def select(
        self, agent_id: Optional[str] = None, sticky_id: Optional[str] = None
    ) -> Optional[Account]:
        """Return a ready account, preferring the agent's current one."""
        now = time.time()
        with self._lock:
            accounts = self._all()
            ready = [a for a in accounts if a.ready(now)]
            if not ready:
                return None
            if sticky_id:
                for account in ready:
                    if account.id == sticky_id:
                        return account
            # Least-recently-used keeps load balanced without a shared counter.
            ready.sort(key=lambda a: (a.last_used, a.id))
            return ready[0]

    def note_used(self, account: Account) -> None:
        account.last_used = time.time()
        account.failures = 0
        account.last_status = None
        account.cooldown_until = 0.0

    def mark_failure(
        self,
        account: Account,
        status_code: int,
        retry_after: Optional[float] = None,
        message: str = "",
    ) -> float:
        """Put an account on cooldown. Returns the cooldown in seconds."""
        account.failures += 1
        account.last_status = status_code
        lowered = (message or "").lower()

        if status_code in (401, 403):
            cooldown = AUTH_FAILURE_COOLDOWN
        elif status_code == 429 or "too many messages" in lowered or "rate limit" in lowered:
            cooldown = retry_after or RATE_LIMIT_COOLDOWN
        elif status_code >= 500:
            cooldown = min(SERVER_ERROR_COOLDOWN * account.failures, MAX_BACKOFF)
        else:
            cooldown = RATE_LIMIT_COOLDOWN
        if retry_after and status_code != 401 and status_code != 403:
            cooldown = max(cooldown, retry_after)

        account.cooldown_until = time.time() + cooldown
        return cooldown

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def status(self) -> List[Dict[str, object]]:
        with self._lock:
            return [account.to_public() for account in self._all()]

    def size(self) -> int:
        with self._lock:
            return len(self._all())

    def next_ready_in(self) -> float:
        with self._lock:
            accounts = self._all()
            if not accounts:
                return 0.0
            waits = [a.cooling_for for a in accounts]
            return round(min(waits), 1) if waits else 0.0

    # ------------------------------------------------------------------
    # Validity probes
    # ------------------------------------------------------------------
    def validate_all(self, force: bool = False, max_age: float = 300.0) -> List[Dict[str, object]]:
        """Check every account against ``/api/v0/users/current``.

        Results are cached for ``max_age`` seconds unless ``force`` is set, so
        the dashboard can poll without hammering DeepSeek.
        """
        with self._lock:
            accounts = self._all()

        now = time.time()
        for account in accounts:
            if (
                not force
                and account.checked_at
                and now - account.checked_at < max_age
            ):
                continue
            try:
                session = _requests.Session(impersonate="chrome124")
                resp = session.get(
                    CURRENT_USER_URL,
                    headers=account.headers(),
                    timeout=15,
                )
                if resp.status_code != 200:
                    account.valid = False
                    account.valid_message = f"HTTP {resp.status_code}"
                else:
                    body = resp.json()
                    code = body.get("code")
                    if code == 0:
                        account.valid = True
                        info = body.get("data") or {}
                        account.valid_message = (
                            info.get("nickname")
                            or info.get("email")
                            or "valid"
                        )
                    else:
                        account.valid = False
                        account.valid_message = (
                            f"{body.get('msg', 'invalid')} (code: {code})"
                        )
            except Exception as exc:  # noqa: BLE001
                account.valid = False
                account.valid_message = f"check failed: {exc}"
            account.checked_at = time.time()

        return self.status()


def build_pool(auth_manager: Optional[AuthManager] = None) -> AccountPool:
    return AccountPool(auth_manager)
