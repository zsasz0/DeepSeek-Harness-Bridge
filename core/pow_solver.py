"""
DeepSeek Proof-of-Work solver.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from typing import Dict, Optional

from curl_cffi import requests
from deepseek_pow import Challenge, registry

DEEPSEEK_BASE = "https://chat.deepseek.com"
POW_CHALLENGE_URL = f"{DEEPSEEK_BASE}/api/v0/chat/create_pow_challenge"

# deepseek-pow keeps ONE solver with ONE wasmtime Store. A Store is not
# thread-safe, and FastAPI runs sync endpoints in a thread pool, so two
# concurrent requests used to call solve() on the same Store and abort the
# process inside wasmtime's unwinder. Serialize the WASM call.
_SOLVER_LOCK = threading.Lock()


class PoWSolver:
    """Stateless wrapper around the deepseek-pow WASM solver."""

    def _get_solver(self, algorithm: str):
        try:
            return registry.get(algorithm)
        except Exception as exc:
            print(f"[pow] Failed to get solver for {algorithm}: {exc}", file=sys.stderr)
            raise

    def _solve(self, algorithm: str, challenge: Challenge):
        # Serialized on purpose: see _SOLVER_LOCK above.
        with _SOLVER_LOCK:
            return self._get_solver(algorithm).solve(challenge)

    def fetch_and_solve(
        self,
        session: requests.Session,
        headers: Dict[str, str],
        target_path: str = "/api/v0/chat/completion",
    ) -> str:
        started = time.time()

        resp = session.post(
            POW_CHALLENGE_URL,
            headers=headers,
            json={"target_path": target_path},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch PoW challenge: HTTP {resp.status_code} - {resp.text[:150]}"
            )

        res_json = resp.json()
        if res_json.get("code") != 0:
            msg = res_json.get("msg", "Unknown error")
            raise RuntimeError(
                f"DeepSeek returned error for PoW challenge: {msg} "
                f"(code {res_json.get('code')})"
            )

        challenge_data = (
            res_json.get("data", {}).get("biz_data", {}).get("challenge")
        )
        if not challenge_data:
            raise RuntimeError("PoW challenge structure missing in response")

        algorithm = challenge_data.get("algorithm", "DeepSeekHashV1")
        challenge_hex = challenge_data.get("challenge")
        salt = challenge_data.get("salt")
        difficulty = int(challenge_data.get("difficulty", 144000))
        expire_at = int(challenge_data.get("expire_at", 0))
        signature = challenge_data.get("signature", "")
        resolved_target_path = challenge_data.get("target_path", target_path)

        challenge = Challenge(
            algorithm=algorithm,
            challenge=challenge_hex,
            salt=salt,
            difficulty=difficulty,
            expire_at=expire_at,
            signature=signature,
        )
        solution = self._solve(algorithm, challenge)

        payload = {
            "algorithm": algorithm,
            "challenge": challenge_hex,
            "salt": salt,
            "answer": int(solution.answer),
            "signature": signature,
            "target_path": resolved_target_path,
        }
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("utf-8")

        print(
            f"[pow] Solved {algorithm} challenge in {time.time() - started:.3f}s "
            f"(nonce: {solution.answer})",
            file=sys.stderr,
        )
        return encoded