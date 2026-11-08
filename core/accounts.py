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
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .auth import AuthManager, build_headers, parse_credentials
from .auth import requests as _requests
from .auth import CURRENT_USER_URL

# Cooldowns (seconds).
AUTH_FAILURE_COOLDOWN = 30 * 60  # 401/403: the token itself is bad
RATE_LIMIT_COOLDOWN = 60  # 429 or DeepSeek's "Too many messages"
SERVER_ERROR_COOLDOWN = 30
MAX_BACKOFF = 300

# Files created by the dashboard's "Add Account" button (acct1.txt, acct2.txt, ...).
ACCOUNT_FILE_RE = re.compile(r"^acct(\d+)\.txt$", re.IGNORECASE)


def accounts_dir(create: bool = False) -> str:
    """Directory backing the file account pool.

    Honours ``DEEPSEEK_AUTH_DIR`` and otherwise uses ``<project>/accounts``.
    Creates the folder when ``create`` is set.
    """
    directory = os.environ.get("DEEPSEEK_AUTH_DIR", "").strip()
    if not directory:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        directory = os.path.join(base, "accounts")
    if create:
        os.makedirs(directory, exist_ok=True)
    return directory


def _next_account_path(directory: str) -> str:
    highest = 0
    for name in os.listdir(directory):
        match = ACCOUNT_FILE_RE.match(name)
        if match:
            highest = max(highest, int(match.group(1)))
    return os.path.join(directory, f"acct{highest + 1}.txt")


def save_credential_file(raw_text: str, directory: Optional[str] = None) -> str:
    """Write pasted credentials to the next free ``acctN.txt`` file.

    The text is stored verbatim (``parse_credentials`` handles JWTs, cookie
    headers, Netscape files and JSON blobs). Returns the absolute path that was
    created.
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("no credentials provided")

    target_dir = os.path.abspath(directory or accounts_dir(create=True))

    while True:
        path = _next_account_path(target_dir)
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
            return path
        except FileExistsError:
            # Another writer grabbed the same name; recompute and retry.
            continue


def delete_credential_file(name: str, directory: Optional[str] = None) -> bool:
    """Delete one ``acctN.txt`` credential file. Returns False if missing."""
    filename = (name or "").strip()
    if filename != os.path.basename(filename) or not ACCOUNT_FILE_RE.match(filename):
        raise ValueError("invalid account file name")

    target_dir = os.path.abspath(directory or accounts_dir())
    path = os.path.abspath(os.path.join(target_dir, filename))
    if os.path.dirname(path) != target_dir:
        raise ValueError("invalid account file name")

    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True


def probe_credentials(token: Optional[str], cookie: str = "") -> Tuple[bool, str]:
    """Check a token/cookie pair against ``/api/v0/users/current``.

    Returns ``(valid, message)`` where message is the account nickname, email,
    an HTTP status or a DeepSeek error string. Never raises.
    """
    if not token and not cookie:
        return False, "no credentials provided"

    try:
        session = _requests.Session(impersonate="chrome124")
        resp = session.get(
            CURRENT_USER_URL,
            headers=build_headers(token=token, cookie_string=cookie),
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"check failed: {exc}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False, "non-JSON response"

    code = body.get("code")
    if code != 0:
        return False, f"{body.get('msg', 'invalid')} (code: {code})"

    info = body.get("data") or {}
    return True, (info.get("nickname") or info.get("email") or "valid")


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
        directory = accounts_dir()
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
    def mark_valid(self, source: str, valid: bool, message: str) -> None:
        """Cache a validation result for the account loaded from ``source``."""
        with self._lock:
            for account in self._accounts:
                if account.source == source:
                    account.valid = valid
                    account.valid_message = message
                    account.checked_at = time.time()
                    return

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
            account.valid, account.valid_message = probe_credentials(
                account.token, account.cookie
            )
            account.checked_at = time.time()

        return self.status()


def build_pool(auth_manager: Optional[AuthManager] = None) -> AccountPool:
    return AccountPool(auth_manager)
