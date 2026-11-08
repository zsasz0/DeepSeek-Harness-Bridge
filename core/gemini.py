"""
Gemini web (gemini.google.com) integration.

Wraps the async :mod:`gemini_webapi` client in a dedicated event-loop thread so
the synchronous FastAPI handlers can call it without blocking the server loop.

Configuration lives in ``gemini_cookies.json`` (git-ignored):

    {"secure_1psid": "...", "secure_1psidts": "...", "updated_at": 0}

Only one Google account is supported. Cookies are refreshed in the background
by ``gemini_webapi`` and synchronised back to the JSON file after each use.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# gemini_webapi caches rotated cookies here. Keep it inside the project so a
# restart (or a different working directory) does not lose the refreshed
# __Secure-1PSIDTS value.
_CACHE_DIR = os.path.join(_PROJECT_ROOT, "gemini_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)
os.environ.setdefault("GEMINI_COOKIE_PATH", _CACHE_DIR)

try:
    from gemini_webapi import GeminiClient, set_log_level  # type: ignore[no-redef]
    from gemini_webapi.constants import AccountStatus  # type: ignore[no-redef]

    GEMINI_AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - optional dependency
    GeminiClient = None  # type: ignore[assignment]
    AccountStatus = None  # type: ignore[assignment]
    set_log_level = None  # type: ignore[assignment]
    GEMINI_AVAILABLE = False
    _IMPORT_ERROR = str(_exc)

INSTALL_HINT = (
    "gemini-webapi is not installed. Run "
    "`pip install -r requirements.txt` with Python 3.11+ and restart the server."
)

COOKIE_FILE = os.path.join(_PROJECT_ROOT, "gemini_cookies.json")

# Timeouts (seconds).
CONNECT_TIMEOUT = float(os.environ.get("GEMINI_CONNECT_TIMEOUT_S", "45"))
REQUEST_TIMEOUT = float(os.environ.get("GEMINI_REQUEST_TIMEOUT_S", "300"))
STREAM_IDLE_TIMEOUT = float(os.environ.get("GEMINI_STREAM_IDLE_TIMEOUT_S", "90"))
STREAM_MAX_SECONDS = float(os.environ.get("GEMINI_STREAM_MAX_S", "600"))

# Gemini's web model names are discovered per-account. These aliases are always
# advertised and resolved by gemini_webapi's registry when the account supports
# them.
STATIC_MODEL_NAMES = [
    "gemini-auto",
    "gemini-flash",
    "gemini-pro",
    "gemini-flash-lite",
]

_COOKIE_PAIR_RE = re.compile(
    r"(__Secure-1PSIDTS|__Secure-1PSID)\s*=\s*([^;\s,\r\n\"'}]+)",
    re.IGNORECASE,
)
_SIDTS_RE = re.compile(r"sidts-[A-Za-z0-9._\-]{10,}")
_JSESSION_RE = re.compile(r"\b\d{6,}\b")


class GeminiError(RuntimeError):
    """Base class for Gemini integration failures."""


class GeminiNotConfigured(GeminiError):
    """No Gemini cookies have been saved yet."""


class GeminiAuthError(GeminiError):
    """Google rejected the cookies or the account is restricted."""


class GeminiModelError(GeminiError):
    """The requested Gemini model is not available on this account."""


# ----------------------------------------------------------------------
# Cookie handling
# ----------------------------------------------------------------------
def _looks_like_psid(value: str) -> bool:
    return len(value) >= 10 and "=" not in value and not value.startswith("sidts-")


def parse_gemini_cookies(raw_text: str) -> Tuple[str, str]:
    """Extract ``(__Secure-1PSID, __Secure-1PSIDTS)`` from pasted text.

    Accepts the JSON blob exported by cookie extensions, a ``Cookie:`` header,
    a Netscape ``cookies.txt`` file, or two bare values (PSID first).
    Raises :class:`ValueError` when no usable PSID is found.
    """
    raw = (raw_text or "").strip()
    if not raw:
        raise ValueError("Paste your Gemini cookies first.")

    psid = ""
    psidts = ""

    # 1. JSON: object, array of {name, value}, or DevTools-style nested values.
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                name = str(key).lower()
                if name in ("__secure-1psid", "secure_1psid"):
                    psid = value.get("value", "") if isinstance(value, dict) else str(value)
                elif name in ("__secure-1psidts", "secure_1psidts"):
                    psidts = value.get("value", "") if isinstance(value, dict) else str(value)
            if not psid and isinstance(data.get("cookies"), list):
                data = data["cookies"]
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).lower()
                value = item.get("value", "")
                if name == "__secure-1psid":
                    psid = str(value)
                elif name == "__secure-1psidts":
                    psidts = str(value)

    # 2. Netscape cookies.txt (tab separated).
    if not psid or not psidts:
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split("\t")
            if len(parts) >= 7:
                name, value = parts[5].strip().lower(), parts[6].strip()
                if name == "__secure-1psid" and not psid:
                    psid = value
                elif name == "__secure-1psidts" and not psidts:
                    psidts = value

    # 3. Cookie header / DevTools pairs.
    for name, value in _COOKIE_PAIR_RE.findall(raw):
        if name.lower() == "__secure-1psid" and not psid:
            psid = value
        elif name.lower() == "__secure-1psidts" and not psidts:
            psidts = value

    # 4. Bare values: a sidts-... token is the TS cookie, the other is the PSID.
    if not psid or not psidts:
        bare = [piece.strip("\"'") for piece in re.split(r"[\s,]+", raw) if piece.strip("\"'")]
        for piece in bare:
            if not psidts and piece.startswith("sidts-"):
                psidts = piece
            elif not psid and _looks_like_psid(piece) and not _JSESSION_RE.fullmatch(piece):
                psid = piece

    if not psid:
        raise ValueError(
            "Could not find __Secure-1PSID. Copy the cookie from "
            "gemini.google.com (DevTools > Application > Cookies)."
        )
    return psid, psidts


def load_saved_cookies() -> Tuple[str, str]:
    """Read the saved cookie pair, or ``("", "")`` when absent/corrupt."""
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return (
        str(data.get("secure_1psid") or data.get("__Secure-1PSID") or ""),
        str(data.get("secure_1psidts") or data.get("__Secure-1PSIDTS") or ""),
    )


def save_cookies(psid: str, psidts: str) -> None:
    """Persist the cookie pair atomically."""
    payload = {
        "secure_1psid": psid,
        "secure_1psidts": psidts,
        "updated_at": time.time(),
    }
    tmp = COOKIE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, COOKIE_FILE)


def _clear_cache(psid: str) -> None:
    """Remove gemini_webapi's rotated-cookie cache for one PSID."""
    if not psid:
        return
    try:
        for name in os.listdir(_CACHE_DIR):
            if psid in name and name.startswith(".cached_cookies_"):
                os.remove(os.path.join(_CACHE_DIR, name))
    except OSError:
        pass


def is_gemini_model(model: Optional[str]) -> bool:
    """True when a request should be routed to Google Gemini."""
    return (model or "").strip().lower().startswith("gemini")


def _cookie_value(cookies: Any, name: str) -> str:
    try:
        value = cookies.get(name)
        if value:
            return str(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        for cookie in cookies.jar:
            if getattr(cookie, "name", "") == name:
                return str(getattr(cookie, "value", ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------
class GeminiProvider:
    """Thread-safe, lazily connected Gemini web client."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._client: Optional[GeminiClient] = None
        self._connected = False
        self._message = "Not connected"
        self._models: List[str] = []
        self._psid = ""
        self._psidts = ""
        self._checked_saved = False

    # -- event loop plumbing -------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run_loop, args=(loop,), name="gemini-loop", daemon=True
            )
            thread.start()
            self._loop = loop
            self._loop_thread = thread
            return loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _submit(self, coro, timeout: float):
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    # -- status ---------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        if not GEMINI_AVAILABLE:
            return {
                "configured": False,
                "connected": False,
                "message": INSTALL_HINT,
                "models": list(STATIC_MODEL_NAMES),
                "installed": False,
            }
        with self._state_lock:
            if self._connected and self._client is not None:
                return {
                    "configured": True,
                    "connected": True,
                    "message": self._message,
                    "models": list(self._models) or list(STATIC_MODEL_NAMES),
                }
            psid, _ = load_saved_cookies()
            if psid:
                return {
                    "configured": True,
                    "connected": False,
                    "message": "Cookies saved. They are checked on the next request, or press Verify.",
                    "models": list(STATIC_MODEL_NAMES),
                }
            return {
                "configured": False,
                "connected": False,
                "message": "No Gemini cookies saved.",
                "models": list(STATIC_MODEL_NAMES),
            }

    def model_entries(self) -> List[Dict[str, Any]]:
        names: List[str] = []
        for name in list(self._models) + list(STATIC_MODEL_NAMES):
            if name and name not in names:
                names.append(name)
        return [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "google",
                "name": name.replace("gemini-", "Gemini ").replace("-", " ").title(),
            }
            for name in names
        ]

    # -- connect / disconnect ------------------------------------------
    async def _init_client(
        self, psid: str, psidts: str
    ) -> Tuple[Optional[GeminiClient], str, List[str]]:
        client = GeminiClient(psid, psidts)
        try:
            await client.init(timeout=CONNECT_TIMEOUT, auto_refresh=True)
        except Exception as exc:  # noqa: BLE001
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            name = type(exc).__name__
            if "Auth" in name or "Unauth" in name:
                return None, f"Gemini rejected the cookies: {exc}", []
            return None, f"Could not connect to Gemini: {exc}", []

        status = getattr(client, "account_status", None)
        if status is not None:
            try:
                available = int(status) == int(AccountStatus.AVAILABLE)
            except Exception:  # noqa: BLE001
                available = True
            if not available:
                description = getattr(status, "description", str(status))
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                return (
                    None,
                    f"Gemini account unavailable: {description} "
                    "Sign in to gemini.google.com and copy fresh cookies.",
                    [],
                )

        models: List[str] = []
        try:
            for item in client.list_models() or []:
                if getattr(item, "is_available", True):
                    name = getattr(item, "model_name", "") or getattr(item, "display_name", "")
                    if name:
                        models.append(str(name))
        except Exception:  # noqa: BLE001
            pass
        return client, "Connected to Gemini.", models

    def connect(
        self,
        raw_text: Optional[str] = None,
        psid: Optional[str] = None,
        psidts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate cookies and replace the live client on success."""
        if not GEMINI_AVAILABLE:
            return {"success": False, "message": INSTALL_HINT}
        try:
            if raw_text is not None:
                psid, psidts = parse_gemini_cookies(raw_text)
            elif not psid:
                psid, psidts = load_saved_cookies()
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        if not psid:
            return {"success": False, "message": "No Gemini cookies provided."}
        psidts = psidts or ""

        with self._state_lock:
            old_client = self._client
            self._client = None
            self._connected = False
            if old_client is not None:
                try:
                    self._submit(old_client.close(), 10)
                except Exception:  # noqa: BLE001
                    pass

            try:
                client, message, models = self._submit(
                    self._init_client(psid, psidts), CONNECT_TIMEOUT + 30
                )
            except Exception as exc:  # noqa: BLE001
                self._message = f"Could not connect to Gemini: {exc}"
                return {"success": False, "message": self._message}

            if client is None:
                self._message = message
                return {"success": False, "message": message}

            self._client = client
            self._connected = True
            self._message = message
            self._models = models
            self._psid = psid
            self._psidts = _cookie_value(client.cookies, "__Secure-1PSIDTS") or psidts
            try:
                save_cookies(psid, self._psidts)
            except OSError as exc:
                print(f"[gemini] could not save cookies: {exc}")
            return {
                "success": True,
                "message": message,
                "models": list(models),
            }

    def _ensure_client(self) -> GeminiClient:
        with self._state_lock:
            if self._connected and self._client is not None:
                return self._client
            psid, psidts = load_saved_cookies()
            if not psid:
                raise GeminiNotConfigured(
                    "Gemini is not configured. Paste cookies in the dashboard."
                )
            result = self.connect(psid=psid, psidts=psidts)
            if not result.get("success") or self._client is None:
                raise GeminiAuthError(result.get("message", "Gemini connection failed."))
            return self._client

    def disconnect(self) -> Dict[str, Any]:
        with self._state_lock:
            client = self._client
            psid = self._psid or load_saved_cookies()[0]
            self._client = None
            self._connected = False
            self._message = "Not connected"
            self._models = []
            if client is not None:
                try:
                    self._submit(client.close(), 10)
                except Exception:  # noqa: BLE001
                    pass
            try:
                os.remove(COOKIE_FILE)
            except OSError:
                pass
            _clear_cache(psid)
            return {"success": True, "message": "Gemini cookies removed."}

    # -- generation -----------------------------------------------------
    def _fallback_model(self, client: "GeminiClient"):
        """Return the account's unified model when it only exposes one.

        Google is rolling out a single ``gemini-auto`` entry to many accounts
        instead of separate Flash/Pro entries. Friendly aliases then map onto
        it, with ``gemini-pro`` enabling extended thinking.
        """
        for name in ("gemini-auto",) + tuple(self._models):
            try:
                return client.resolve_model(name)
            except Exception:  # noqa: BLE001
                continue
        return None

    def _resolve_model(
        self, client: "GeminiClient", model: Optional[str]
    ) -> Tuple[Any, bool]:
        """Return ``(model_arg, extended_thinking)`` for a request."""
        name = (model or "").strip()
        if not name or name.lower() in ("gemini", "gemini-web", "gemini-default"):
            return None, False

        candidates = [name]
        if name.lower().startswith("gemini-"):
            candidates.append(name[7:])
        for candidate in candidates:
            try:
                return client.resolve_model(candidate), False
            except Exception:  # noqa: BLE001
                continue

        # The account may only expose "gemini-auto"; keep the friendly names
        # working instead of failing every request.
        lowered = name.lower()
        if lowered in STATIC_MODEL_NAMES:
            fallback = self._fallback_model(client)
            if fallback is not None:
                thinking = lowered.endswith("-pro")
                print(
                    f"[gemini] model alias {name} -> "
                    f"{getattr(fallback, 'model_name', 'default')} "
                    f"(extended_thinking={thinking})"
                )
                return fallback, thinking

        available = ", ".join(
            dict.fromkeys(list(self._models) + list(STATIC_MODEL_NAMES))
        )
        raise GeminiModelError(
            f"Unknown Gemini model '{model}'. Available: {available}"
        )

    def _sync_cookies(self, client: GeminiClient) -> None:
        try:
            psid = _cookie_value(client.cookies, "__Secure-1PSID") or self._psid
            psidts = _cookie_value(client.cookies, "__Secure-1PSIDTS") or self._psidts
        except Exception:  # noqa: BLE001
            return
        if psid and (psid != self._psid or psidts != self._psidts):
            self._psid, self._psidts = psid, psidts
            try:
                save_cookies(psid, psidts)
            except OSError:
                pass

    def complete(
        self, prompt: str, model: Optional[str] = None, files: Optional[List[str]] = None
    ) -> Tuple[str, str]:
        """Return ``(text, thoughts)`` for one stateless completion."""
        client = self._ensure_client()
        model_arg, extended_thinking = self._resolve_model(client, model)

        async def _generate():
            return await client.generate_content(
                prompt,
                files=files or None,
                model=model_arg,
                extended_thinking=extended_thinking,
            )

        with self._request_lock:
            output = self._submit(_generate(), REQUEST_TIMEOUT + 30)
            self._sync_cookies(client)
        return (getattr(output, "text", "") or "", getattr(output, "thoughts", "") or "")

    def stream(
        self,
        prompt: str,
        model: Optional[str] = None,
        files: Optional[List[str]] = None,
    ) -> Iterator[Tuple[str, str]]:
        """Yield ``("text"|"thinking", delta)`` tuples from Gemini's stream."""
        client = self._ensure_client()
        model_arg, extended_thinking = self._resolve_model(client, model)
        # One Google account: serialize generation so cookie rotation and the
        # upstream session cannot race between concurrent requests.
        self._request_lock.acquire()
        items: "queue.Queue[object]" = queue.Queue(maxsize=256)
        stop = threading.Event()

        async def _worker() -> None:
            generator = client.generate_content_stream(
                prompt,
                files=files or None,
                model=model_arg,
                extended_thinking=extended_thinking,
            )
            try:
                async for chunk in generator:
                    if stop.is_set():
                        break
                    thoughts = getattr(chunk, "thoughts_delta", "") or ""
                    if thoughts:
                        items.put(("thinking", thoughts))
                    delta = getattr(chunk, "text_delta", "") or ""
                    if delta:
                        items.put(("text", delta))
            except Exception as exc:  # noqa: BLE001
                items.put(exc)
            finally:
                try:
                    await generator.aclose()
                except Exception:  # noqa: BLE001
                    pass
                items.put(None)

        future = asyncio.run_coroutine_threadsafe(_worker(), self._ensure_loop())
        started = time.time()
        try:
            while True:
                if time.time() - started > STREAM_MAX_SECONDS:
                    raise GeminiError(
                        f"Gemini stream exceeded {int(STREAM_MAX_SECONDS)}s"
                    )
                try:
                    item = items.get(timeout=STREAM_IDLE_TIMEOUT)
                except queue.Empty:
                    raise GeminiError(
                        f"Gemini stalled for {int(STREAM_IDLE_TIMEOUT)}s"
                    ) from None
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item  # type: ignore[misc]
        finally:
            stop.set()
            try:
                future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._sync_cookies(client)
            self._request_lock.release()

    def shutdown(self) -> None:
        with self._state_lock:
            client = self._client
            self._client = None
            self._connected = False
        if client is not None:
            try:
                self._submit(client.close(), 10)
            except Exception:  # noqa: BLE001
                pass
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)


if GEMINI_AVAILABLE and set_log_level is not None:
    set_log_level("WARNING")

gemini = GeminiProvider()
atexit.register(gemini.shutdown)
