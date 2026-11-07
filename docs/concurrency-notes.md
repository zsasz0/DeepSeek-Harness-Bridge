# Concurrency notes (review request — no code changes)

Status: documentation only. Nothing in this branch changes runtime behaviour.
Purpose: let a reviewer agent verify the parallel-execution claims against the
actual source, with exact locations and proposed tests.

Verified against commit `15d700e`.

## Summary

| Scenario | Current state |
|---|---|
| N agents, distinct ids, parallel | Works |
| N agents, same account, parallel | Works, higher upstream rate-limit risk |
| Same agent id, parallel | **Broken** — session state corruption |
| PoW under parallelism | Safe (already serialized) |

---

## Confirmed defects

### 1. No per-agent lock in the request handler

Location: `server.py`, `create_chat_completion()`.

The handler performs an unguarded read-modify-write sequence on the
`AgentSession`:

1. `agent.plan(req.messages, req.tools)` reads `chat_session_id`,
   `sent_signatures`, `tools_key`.
2. `agent.reset_remote()` may zero that state.
3. `client.create_chat_session(...)` is awaited (long).
4. `agent.chat_session_id = chat_session_id`.
5. `agent.mark_sent(...)` appends signatures and bumps counters.

Two requests with the same `x-agent-session` header can interleave at any
point in that sequence. Observable outcomes: a chat id assigned to the wrong
turn, `sent_count` advanced twice for one logical turn, `parent_message_id`
from the wrong reply.

`SessionManager.get()` is protected by a `threading.Lock`, so session *lookup*
is safe. Mutation is not.

### 2. Non-atomic session counters

Location: `core/sessions.py`, `AgentSession.mark_sent()`.

```python
for message in sent_messages:
    self.sent_signatures.append(_turn_signature(message))
self.sent_count = len(self.sent_signatures)
...
self.remote_chars += prompt_chars + output_chars
self.message_count += 1
```

`append` and `+=` are not atomic across threads. Parallel writers on one agent
double-count turns, which then makes `plan()` report `history_mismatch` on the
next request.

### 3. History rebinding loses entries

Location: `core/sessions.py`, `AgentSession.mark_sent()`.

```python
self.history = (self.history + list(sent_messages))[-RECOVERY_HISTORY_TURNS * 2 :]
```

Rebinding is not torn, but two concurrent writers each read the old list and
the last write wins, so one turn is lost from the recovery history.

### 4. Account mutations outside the pool lock

Location: `core/accounts.py`, `AccountPool.note_used()` and
`AccountPool.mark_failure()`.

`select()` and `status()` hold `self._lock`. `note_used()` and
`mark_failure()` mutate the shared `Account` dataclass with no lock:

```python
account.failures += 1
account.last_status = status_code
account.cooldown_until = time.time() + cooldown
```

Parallel failures on one account can lose increments, so backoff grows slower
than intended. `last_used` ordering used by LRU selection is also racy.

### 5. `_all()` read outside the lock

Location: `core/accounts.py`.

`_synthesized()` writes `self._accounts` while holding the lock, but `_all()`
reads `self._accounts` (or `self._loaded_from_files`) without it. Low impact,
still a visibility gap.

---

## Confirmed NOT a problem

Removed from the risk list after reading the source.

### PoW solver thread safety

`core/pow_solver.py:24` defines a module-level lock:

```python
_SOLVER_LOCK = threading.Lock()
```

and `_solve()` wraps the WASM call:

```python
def _solve(self, algorithm: str, challenge: Challenge):
    # Serialized on purpose: see _SOLVER_LOCK above.
    with _SOLVER_LOCK:
        return self._get_solver(algorithm).solve(challenge)
```

The in-file comment documents that concurrent `solve()` calls previously
crashed the process inside wasmtime's unwinder. PoW is safe under parallelism.

### Session lookup

`SessionManager.get()` and `reset()` hold a `threading.Lock` around the
dictionary.

### Account selection

`AccountPool.select()` holds `threading.RLock` and only reads account state
under it.

---

## Untested risk (stated as risk, not fact)

- One `curl_cffi.Session` is created per request (`_new_session()`), so there
  is no connection reuse. Ten parallel agents open ten TLS connections to
  DeepSeek. Performance concern, not correctness.
- Account selection is sticky per agent. Many parallel agents therefore stay
  on one account and are more likely to hit upstream `Too many messages` or a
  stalled stream. The pool cannot help because all of them share the sticky
  account until it cools down.
- No test exercises parallel execution. All 95 tests are serial.

---

## Proposed tests (not written in this branch)

1. **Distinct agents in parallel.** 4 agents x 3 turns concurrently against a
   fake client. Assert each session's `sent_count`, `parent_message_id` and
   `remote_chars` are internally consistent and no turn is lost.
2. **Same agent in parallel.** Two concurrent requests with the same
   `x-agent-session`. Currently expected to corrupt state; the test documents
   the failure until a per-agent lock exists.
3. **PoW concurrency.** N concurrent `fetch_and_solve()` calls. Expected to
   pass because of `_SOLVER_LOCK`.
4. **Account failure counting.** N concurrent `mark_failure()` calls on one
   account. Assert `failures` equals the number of calls.

---

## Suggested fix shape (for the reviewer, not implemented)

- Give `AgentSession` its own `threading.Lock` and hold it in the handler from
  `plan()` through `mark_sent()`. This serializes same-agent turns and leaves
  different agents fully parallel.
- Move `note_used()` / `mark_failure()` mutations under `AccountPool._lock`.
- Optionally keep one `curl_cffi.Session` per account for connection reuse.

---

## Reviewer checklist

- [ ] Confirm handler has no lock spanning steps 1-5 above.
- [ ] Confirm `mark_sent()` mutations are unguarded.
- [ ] Confirm `_SOLVER_LOCK` exists and wraps `.solve()`.
- [ ] Confirm `SessionManager` and `select()` are locked.
- [ ] Confirm `note_used()` / `mark_failure()` run outside the pool lock.
