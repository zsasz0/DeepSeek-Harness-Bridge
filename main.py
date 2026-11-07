"""DeepSeek Web2API v2 - entry point."""

from __future__ import annotations

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
    else:
        print("[!] No credentials found. Open the dashboard to paste your userToken.")

    host, port = "127.0.0.1", 8084
    url = f"http://{host}:{port}"
    print(f"\nStarting DeepSeek Web2API on {url}")
    print(f"OpenAI endpoint: {url}/v1/chat/completions")
    print(f"Dashboard:       {url}\n")

    open_browser(url)
    uvicorn.run("server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
