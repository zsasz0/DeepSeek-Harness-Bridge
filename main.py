"""DeepSeek Web2API v2 - entry point."""

from __future__ import annotations

import os
import threading
import time
import webbrowser

import uvicorn

from core.auth import AuthManager


def open_browser(url: str, delay: float = 1.5) -> None:
    def _open() -> None:
        time.sleep(delay)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    auth = AuthManager()
    if auth.token:
        print(f"[ok] Loaded credentials from {auth.config_path}")
    elif os.environ.get("DEEPSEEK_AUTH_DIR") or os.environ.get("DEEPSEEK_AUTH_PATH"):
        print("[ok] Using account pool from environment")
    else:
        print("[!] No credentials found. Open the dashboard to paste your userToken.")

    host = os.environ.get("PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("PROXY_PORT", "8084"))
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not os.environ.get("PROXY_API_KEY", "").strip():
        print(
            "[!] WARNING: binding to a non-loopback address without PROXY_API_KEY. "
            "Anyone on the network can use your DeepSeek account."
        )

    url = f"http://{'localhost' if loopback else host}:{port}"
    print(f"\nStarting DeepSeek Web2API on http://{host}:{port}")
    print(f"OpenAI endpoint: {url}/v1/chat/completions")
    print(f"Dashboard:       {url}\n")

    open_browser(url)
    uvicorn.run("server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
