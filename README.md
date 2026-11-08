# DeepSeek + Gemini Web2API v2

**Use your DeepSeek and Gemini web accounts as a local OpenAI-compatible API.**

Runs a small FastAPI server on `http://localhost:8084` that speaks the OpenAI
`/v1/chat/completions` protocol and forwards requests to `chat.deepseek.com`
or `gemini.google.com` using your own logged-in sessions. Pick the provider
with the model name: `deepseek-*` goes to DeepSeek, `gemini-*` goes to Google.
Works with any OpenAI-compatible client (OpenCode, Cursor, Continue, LiteLLM,
the OpenAI Python/JS SDK, etc.).

![OpenCode using DeepSeek Web2API](docs/opencode-preview.png)

![DeepSeek Web2API dashboard](docs/dashboard.png)

> ⚠️ **Disclaimer** — This project is unofficial and not affiliated with
> DeepSeek or Google. It relies on private web endpoints and may break at any
> time. Use it for personal experimentation only and respect the providers'
> Terms of Service. Never commit your credentials.

---

## Features

- 🚀 **No API key needed** — uses your free `chat.deepseek.com` session.
- 🧠 **Thinking + Search** — exposes the web model in normal, reasoning and
  web-search variants.
- 🔐 **Built-in PoW solver** — solves DeepSeek's `DeepSeekHashV1` WebAssembly
  proof-of-work challenge automatically.
- 🔄 **True SSE streaming** — without `tools` the upstream reply is forwarded
  live, including `reasoning_content`. With `tools` the reply is buffered so
  tool-call deltas can be parsed before emission.
- 🧵 **Session reuse** — one live DeepSeek chat per agent, keyed by the
  `x-agent-session` (or `session` / `user`) header. Follow-up turns send only
  the new messages and reuse the upstream `parent_message_id`.
- 👥 **Multi-account pool** — drop several credential files in a folder and the
  server rotates between them, keeps each agent sticky to one account, honours
  `Retry-After` and cools down rate-limited or expired accounts.
- 🛡️ **Safe by default** — loopback-only bind, restricted CORS, no remote image
  fetching (SSRF-safe), optional `PROXY_API_KEY` bearer gate.
- 🛠️ **Tool calling** — the model is asked for a single JSON envelope; a
  waterfall parser (`raw_decode` → `json_repair` → `json5` → fences) recovers
  it, then retries once with self-correction and fails **loudly** with HTTP 502
  instead of leaking broken markup into `content`.
- 🖼️ **Image input** — uploads images from `image_url` parts to DeepSeek's
  vision pipeline.
- 🎛️ **Web dashboard** — add tokens with one click (auto-saved as
  `accounts/acctN.txt`), check status and test chat at `http://localhost:8084`.
- ✨ **Gemini web support** — paste `__Secure-1PSID` / `__Secure-1PSIDTS`
  cookies once; `gemini-flash`, `gemini-pro` and `gemini-flash-lite` become
  available through the same OpenAI endpoint. Cookies rotate automatically.
- 🐧 **Windows + Linux/macOS** — ships with both `.bat` and `.sh` launchers.

---

## Requirements

- **Python 3.11+**
- A DeepSeek account (https://chat.deepseek.com) and/or a Google account with
  Gemini access (https://gemini.google.com)
- Internet access

---

## Installation

### Windows

```bat
install.bat
start.bat
```

### Linux / macOS

```bash
chmod +x install.sh start.sh
./install.sh
./start.sh
```

### Manual (any OS)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

The server starts on **http://localhost:8084** and your browser is opened
automatically (harmless on headless servers).

---

## Getting your DeepSeek credentials

You only need the **`userToken`** (a JWT starting with `eyJ...`), or the full
cookie string. The easiest path is to paste it into the dashboard's **Add
Account** box: it is validated, then saved automatically as
`accounts/acctN.txt` (git-ignored). The legacy `config.json` single-account
mode still works if no account files are present.

### Method 1 — Browser DevTools (fastest)

1. Open [chat.deepseek.com](https://chat.deepseek.com) and log in.
2. Press `F12` (or right-click → **Inspect**) to open Developer Tools.
3. Go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
4. On the left, expand **Local Storage** → click `https://chat.deepseek.com`.
5. Find the key **`userToken`**, double-click its value and copy it
   (it starts with `eyJ...`).
6. Launch the app (`start.bat` on Windows or `./start.sh` on Linux/macOS),
   open `http://localhost:8084`, paste the value into **Add Account** and press
   **Add Account**. The proxy validates it and writes `accounts/acct3.txt`,
   `acct4.txt`, … as you add more tokens.

### Method 2 — Export cookies

1. Install a cookie export extension such as
   [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
   (Chrome & Firefox).
2. Visit [chat.deepseek.com](https://chat.deepseek.com) while logged in.
3. Export the cookies for the site.
4. Either save them as `cookie_deepseek.txt` in the project folder, or paste
   the content into the dashboard's **Add Account** box (saved as
   `accounts/acctN.txt`). Both raw cookie strings and Netscape `cookies.txt`
   format are accepted.

### Method 3 — Paste anything that contains the token

The dashboard parser also accepts a bare JWT, a `Cookie:` header, a cURL dump,
or a localStorage JSON blob. It extracts `userToken` automatically.

> 🔒 **Keep it secret.** Anyone with your `userToken` can use your DeepSeek
> account. If it leaks, log out of chat.deepseek.com to invalidate it.

---

## Getting your Gemini cookies

Gemini's web app has no official API. The proxy drives the same private RPC the
browser uses, authenticated by two Google cookies:

- `__Secure-1PSID` (required)
- `__Secure-1PSIDTS` (strongly recommended; rotated automatically afterwards)

### Fastest path

1. Open [gemini.google.com](https://gemini.google.com) and make sure you are
   signed in.
2. Press `F12` → **Application** (Chrome/Edge) or **Storage** (Firefox) →
   **Cookies** → `https://gemini.google.com`.
3. Copy `__Secure-1PSID` and `__Secure-1PSIDTS`.
4. Open `http://localhost:8084`, paste them into the **Gemini Web** box (any
   format works: JSON, `Cookie:` header, Netscape `cookies.txt`, or two bare
   values) and press **Connect Gemini**.
5. The server validates them against Google before saving. The pair is stored
   in `gemini_cookies.json` (git-ignored); rotated values are written back
   automatically.

> 🔒 **Keep it secret.** These cookies grant full access to your Google
> account's Gemini session. Do not commit `gemini_cookies.json` or paste it in
> an issue. To revoke access, sign out of Gemini (or change your Google
> password), then press **Disconnect** in the dashboard.

Only one Gemini account is supported. If you need several Google accounts,
run a second instance of the proxy with a different `PROXY_PORT`.

---

## Models

| Model ID | Provider | Description |
|---|---|---|
| `deepseek-ai/DeepSeek-V4.1-Flash-1017` | DeepSeek | Fast default model |
| `deepseek-ai/DeepSeek-V4.1-Flash-1017-search` | DeepSeek | Fast model + web search |
| `deepseek-chat` | DeepSeek | DeepSeek V3 (fast) |
| `deepseek-search` | DeepSeek | DeepSeek V3 + web search |
| `deepseek-reasoner` | DeepSeek | Reasoning / thinking model |
| `deepseek-reasoner-search` | DeepSeek | Reasoning + web search |
| `gemini-flash` | Gemini | Gemini Flash via the web app |
| `gemini-pro` | Gemini | Gemini Pro via the web app |
| `gemini-flash-lite` | Gemini | Cheapest/fastest Gemini tier |

Routing is by model name: anything starting with `gemini` goes to Google,
everything else goes to DeepSeek. After connecting a Gemini account,
`GET /v1/models` also lists the exact model names Google reports for that
account. Gemini replies are stateless: the full message history is sent on
every request, so no `x-agent-session` state is kept for Gemini.

You can also force DeepSeek behaviour per request with `search_enabled` and
`thinking_enabled` boolean fields in the request body.

---

## Tool calling

When a request includes `tools`, the prompt asks the web model for exactly one
JSON object and nothing else:

```json
{"content": "<message or null>", "tool_calls": [{"name": "<tool>", "arguments": {...}}]}
```

Talk only: `{"content": "4", "tool_calls": []}` ·
Call only: `{"content": null, "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}`

The reply is parsed by `core/envelope.py` with a waterfall:

| Step | Handles |
|---|---|
| `json.JSONDecoder.raw_decode` | boundary-safe scan — `}`, `"`, newlines, unicode and `</tool_call>` *inside* argument values are never mistaken for structure |
| `json_repair` | trailing commas, missing quotes, truncated output |
| `json5` | single quotes, unquoted keys, comments |
| fenced blocks | ```` ```json ... ``` ```` with prose around it |
| schema validation | unknown tool name, non-object `arguments`, missing required keys → rejected |

If the model clearly attempted a tool call (markers such as `tool_calls`,
`arguments`, `dsml`, `invoke`, ` ```json `) but nothing validates, the request is
retried **once** with a correction prompt that includes the bad reply. If that
also fails, the request returns **HTTP 502** (`model attempted a tool call that
could not be parsed`) rather than putting garbage in the assistant message.

Streaming uses proper incremental deltas: one chunk with `id` + `name` +
empty `arguments` per call, then one chunk with the full `arguments` string,
then `finish_reason: "tool_calls"`. `role` is emitted exactly once and never
as `null`.

Strings are parsed with `json_repair`/`json5`, so install the two extra
dependencies (`pip install json-repair json5`) — they are already in
`requirements.txt`.

---

## Usage

### List models

```bash
curl http://localhost:8084/v1/models
```

### Chat completion

```bash
curl http://localhost:8084/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Streaming with reasoning

```bash
curl http://localhost:8084/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-reasoner",
    "messages": [{"role": "user", "content": "How many r are in strawberry?"}],
    "stream": true
  }'
```

### Gemini chat completion

```bash
curl http://localhost:8084/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-flash",
    "messages": [{"role": "user", "content": "Hello from Gemini!"}]
  }'
```

`stream: true`, `tools`, and inline `data:` images work the same way as for
DeepSeek. If Gemini has not been connected yet, the request returns
**HTTP 401** with a message pointing at the dashboard.

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8084/v1",
    api_key="sk-deepseek-local",  # any non-empty string
)

stream = client.chat.completions.create(
    model="deepseek-reasoner",
    messages=[{"role": "user", "content": "Solve: 2x + 5 = 15"}],
    stream=True,
)

for chunk in stream:
    delta = chunk.choices[0].delta
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        print(reasoning, end="", flush=True)
    if delta.content:
        print(delta.content, end="", flush=True)
```

---

## Integration and usage in OpenCode

### OpenCode

Add this provider to your OpenCode config
(`~/.config/opencode/opencode.json` on Linux/macOS,
`%APPDATA%\opencode\opencode.json` on Windows):

```json
{
  "provider": {
    "deepseek-local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepSeek Local",
      "options": {
        "baseURL": "http://localhost:8084/v1",
        "apiKey": "sk-deepseek-local",
        "timeout": 180000
      },
      "models": {
        "deepseek-ai/DeepSeek-V4.1-Flash-1017": {
          "name": "DeepSeek V4.1 Flash (Fast)",
          "limit": { "context": 128000, "output": 8192 }
        },
        "deepseek-ai/DeepSeek-V4.1-Flash-1017-search": {
          "name": "DeepSeek V4.1 Flash + Web Search",
          "limit": { "context": 128000, "output": 8192 }
        },
        "deepseek-reasoner": {
          "name": "DeepSeek R1 (Thinking)",
          "limit": { "context": 128000, "output": 64000 }
        },
        "deepseek-chat": {
          "name": "DeepSeek V3 (Fast)",
          "limit": { "context": 128000, "output": 8192 }
        },
        "gemini-flash": {
          "name": "Gemini Flash (web)",
          "limit": { "context": 1000000, "output": 8192 }
        },
        "gemini-pro": {
          "name": "Gemini Pro (web)",
          "limit": { "context": 1000000, "output": 65536 }
        },
        "gemini-flash-lite": {
          "name": "Gemini Flash Lite (web)",
          "limit": { "context": 1000000, "output": 8192 }
        }
      }
    }
  }
}
```

Set it as the default:

```json
{
  "model": "deepseek-local/deepseek-ai/DeepSeek-V4.1-Flash-1017",
  "small_model": "deepseek-local/deepseek-ai/DeepSeek-V4.1-Flash-1017"
}
```

### Any other client

Point it at:

- **Base URL:** `http://localhost:8084/v1`
- **API key:** any non-empty string (e.g. `sk-deepseek-local`)

### Optional: LiteLLM in front (one endpoint for every harness)

`litellm_config.yaml` is included. It does **not** parse anything — it just
forwards to this server, so `core/envelope.py` keeps doing the work:

```bash
pip install 'litellm[proxy]'
litellm --config litellm_config.yaml --port 4000
```

- opencode / Cline / Continue / Aider → `baseURL=http://localhost:4000`, model `deepseek-web`
- Claude Code → `ANTHROPIC_BASE_URL=http://localhost:4000`, `ANTHROPIC_MODEL=deepseek-web`
  (LiteLLM translates OpenAI ↔ Anthropic).

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `GET` | `/health` | Liveness + account summary |
| `GET` | `/readyz` | Readiness (at least one account ready) |
| `GET` | `/api/status` | Auth status and user info |
| `POST` | `/api/accounts` | Validate a token and save it as `accounts/acctN.txt` |
| `DELETE` | `/api/accounts/{file}` | Delete one `acctN.txt` credential file |
| `GET` | `/api/gemini` | Gemini cookie/connection status |
| `POST` | `/api/gemini` | Validate + save Gemini cookies (empty body re-checks saved ones) |
| `DELETE` | `/api/gemini` | Forget saved Gemini cookies and close the client |
| `POST` | `/api/cookies` | Legacy single-account save (writes `config.json`) |
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | Chat completion (stream or not) |
| `GET` | `/v1/sessions` | Per-agent session state |
| `POST` | `/reset-session?agent=<id>\|all` | Drop an agent's upstream chat |
| `GET` | `/v1/accounts` | Account pool state |
| `POST` | `/reload-accounts` | Re-read credential files |

---

## Sessions

Send any of these headers to pin a conversation to one agent:

```
x-agent-session: my-agent
session: my-agent
user: my-agent
```

Without a header the id is `default`.

The first request creates a DeepSeek chat and records which client messages
the remote chat has seen. Later requests send only `messages[N:]` and pass the
previous assistant `message_id` as `parent_message_id`, so the web model keeps
its own context instead of receiving the whole transcript again.

A session resets automatically when:

| Reason | Trigger |
|---|---|
| `session_ttl` | older than `DEEPSEEK_SESSION_TTL_MS` (default 2h) |
| `max_message_depth` | more than `DEEPSEEK_MAX_MESSAGE_DEPTH` turns (100) |
| `max_context_chars` | accumulated prompt+output chars over the limit |
| `tools_changed` | the request's `tools` array changed |
| `history_mismatch` | the client rewrote/compacted earlier messages |

When a reset happens, the **full conversation is resent** to the new chat, so
context is never lost — only reused when safe.

---

## Configuration

The file-based account pool (`accounts/`, described below) is the default.
When it has no usable credential files, the proxy falls back to a single
account read from `config.json` in the project root:

```json
{
  "token": "your-userToken",
  "cookie": "optional-cookie-string",
  "updated_at": 0
}
```

`config.json` and `cookie_deepseek.txt` are **git-ignored**. A safe template
lives in `config.example.json` — copy it to `config.json` and fill in your own
values, or just use the dashboard.

### Gemini

Gemini settings are kept separately from the DeepSeek pool:

```json
{
  "secure_1psid": "...",
  "secure_1psidts": "...",
  "updated_at": 0
}
```

The file is `gemini_cookies.json` in the project root and is **git-ignored**.
Refreshed `__Secure-1PSIDTS` values are copied back automatically after each
request, and `gemini_webapi` keeps its own rotation cache in `gemini_cache/`
(also git-ignored). Delete both, or press **Disconnect** in the dashboard, to
start over with another Google account.

Gemini requests are stateless (full history each turn), so the DeepSeek
session headers do not affect them. Tool calls reuse the same JSON-envelope
parser and the same one-shot self-correction retry.

### Multi-account

#### In-project `accounts/` folder (default)

The repo ships with an `accounts/` folder next to `main.py`. `main.py` points
the pool at this folder automatically when it exists, so no environment variable
is needed:

```text
deepseek-webApi2-v1-json/
├── accounts/
│   ├── acct1.txt      ← tracked placeholder (do not commit a real token)
│   ├── acct2.txt      ← created by the dashboard
│   ├── acct3.txt      ← next token you add
│   └── ...
├── core/
├── main.py
└── ...
```

**How to add your account (recommended):**

1. Start the server and open `http://localhost:8084`.
2. Paste your `userToken` (a JWT starting with `eyJ...`) — or a `Cookie:`
   header, Netscape `cookies.txt`, cURL dump or JSON blob — into **Add Account**
   and press **Add Account**. See
   [Getting your DeepSeek credentials](#getting-your-deepseek-credentials).
3. The proxy checks the token against DeepSeek first. Only valid credentials are
   written, as the next free `acctN.txt` (so `acct1.txt` + `acct2.txt` → you get
   `acct3.txt`). Invalid tokens are rejected and nothing is saved.
4. Add more tokens the same way; the pool rotates between them. Use the `×`
   button on an account row to delete its file.

**Manual alternative:**

1. Open `accounts/acct1.txt` and replace the
   `PUT_YOUR_DEEPSEEK_USERTOKEN_HERE` placeholder with a real token, or copy it
   to `acct2.txt`, `acct3.txt`, … with one token per file.
2. Restart the server, or call `POST /reload-accounts` to pick it up without a
   restart. `parse_credentials` extracts the token from any supported format.

**What is tracked vs. secret:**

- `accounts/` and the placeholder `acct1.txt` **are tracked** in git, so a
  fresh clone already has the folder and the file to paste into.
- Every other file in `accounts/` (e.g. a filled `acct2.txt`) is **git-ignored**
  by the rules at the bottom of `.gitignore`:

  ```gitignore
  accounts/*
  !accounts/.gitkeep
  !accounts/acct1.txt
  ```

- ⚠️ Because `acct1.txt` itself is tracked, **never commit a real token in
  `acct1.txt`**. Either keep it as the placeholder and use `acct2.txt`+ for
  real tokens, or run `git update-index --skip-worktree accounts/acct1.txt` so
  your local edit is never staged.
- A filled credential file is a secret: anyone with it can use that DeepSeek
  account. Never paste one into an issue, commit, or gist.

#### Explicit paths

Point `DEEPSEEK_AUTH_DIR` at a folder of credential files, or list explicit
paths with `DEEPSEEK_AUTH_PATH` (this overrides the in-project folder):

```bash
DEEPSEEK_AUTH_DIR=C:\deepseek-accounts
# or
DEEPSEEK_AUTH_PATH=C:\a.json,C:\b.txt
```

#### Rotation behaviour

An agent stays on its account while it is healthy; when an account returns
401/403 it cools down for 30 min, and 429 / `Too many messages` uses
`Retry-After` (default 60 s). If every account is cooling, the request gets
HTTP 429 with a `Retry-After` header. Inspect the pool at `GET /v1/accounts`
or in the dashboard.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PROXY_HOST` / `PROXY_PORT` | `127.0.0.1` / `8084` | Bind address |
| `PROXY_API_KEY` | unset | Require `Authorization: Bearer <key>` on all routes except `/health`/`/readyz` |
| `PROXY_CORS_ORIGINS` | unset | Extra comma-separated allowed origins |
| `DEEPSEEK_AUTH_DIR` / `DEEPSEEK_AUTH_PATH` | unset | Account pool sources |
| `DEEPSEEK_SESSION_TTL_MS` | `7200000` | Session lifetime |
| `DEEPSEEK_MAX_MESSAGE_DEPTH` | `100` | Turns before reset |
| `DEEPSEEK_MAX_PROMPT_CHARS` | `80000` | Prompt size limit |
| `DEEPSEEK_MAX_SESSION_CHARS` | `240000` | Accumulated session chars |
| `GEMINI_CONNECT_TIMEOUT_S` | `45` | Gemini cookie validation timeout |
| `GEMINI_REQUEST_TIMEOUT_S` | `300` | Non-stream Gemini reply timeout |
| `GEMINI_STREAM_IDLE_TIMEOUT_S` | `90` | Abort a stalled Gemini stream |
| `GEMINI_STREAM_MAX_S` | `600` | Hard cap for one Gemini stream |

> The dashboard and the `/api/*` account endpoints are protected by
> `PROXY_API_KEY` when it is set. Use `curl -H "Authorization: Bearer $PROXY_API_KEY" ...`
> or an API client that sends the header.

---

## Security

- Binds to `127.0.0.1` by default; set `PROXY_HOST=0.0.0.0` only behind a
  reverse proxy, and set `PROXY_API_KEY` if you do.
- CORS allows only loopback origins plus `PROXY_CORS_ORIGINS`; credentials are
  never sent cross-origin by the proxy.
- Image input accepts **inline `data:` URIs only**. Remote URLs are never
  fetched, so this proxy cannot be used as an SSRF gadget against internal
  networks or cloud metadata endpoints.
- Gemini cookies grant access to your Google account's Gemini session. They are
  stored in the git-ignored `gemini_cookies.json`, never logged, and never sent
  anywhere except `gemini.google.com`. Treat them like a password.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 DeepSeek session not configured` | Paste your `userToken` in the dashboard. |
| `502 model attempted a tool call that could not be parsed` | The model ignored the JSON envelope twice. Retry; this is a deliberate loud failure instead of leaking broken markup. |
| `502 DeepSeek returned an empty response` | Upstream sent an empty stream (usually rate limiting). Back off and retry. |
| Process aborts with `panicked at crates/unwinder/src/stackwalk.rs` | Fixed: the WASM PoW store is now serialized. If you see it, you are running an old copy. |
| `Validation failed` | Token expired — log in again and copy a fresh one. |
| `401 Gemini is not configured` | Paste cookies in the dashboard's **Gemini Web** card. |
| `Gemini account unavailable (...)` | Cookies expired or Gemini is blocked in your region — sign in again and copy fresh cookies; a VPN may be required. |
| Gemini replies are very slow | `gemini-pro` thinks before answering. Use `gemini-flash` or raise `GEMINI_REQUEST_TIMEOUT_S`. |
| Gemini tool calls loop or fail | Retry; Gemini occasionally ignores the JSON envelope. The proxy retries once with self-correction, then returns HTTP 502. |
| `pip install` fails on Linux | Install `python3-venv` (`sudo apt install python3-venv`). |
| `wasmtime` install error | Use Python 3.10–3.12; ensure wheels are available for your platform. |
| Browser doesn't open | Headless server — just visit the URL manually. |

---

## Concurrency notes

Parallel use works, with one caveat: the same `x-agent-session` must not be
used by two requests at the same time. Distinct agent ids are fully parallel;
the same agent id in parallel can interleave session state.

| Scenario | State |
|---|---|
| N agents, distinct ids, parallel | Works |
| N agents, same account, parallel | Works, higher upstream rate-limit risk |
| Same agent id, parallel | Not safe — session state can interleave |
| Gemini, parallel requests | Serialized behind one lock (single Google account) |
| PoW under parallelism | Safe (serialized by `_SOLVER_LOCK`) |

Known unguarded spots (documented, not yet fixed):

- `server.py` `create_chat_completion()` runs an unguarded
  `plan()` → `create_chat_session()` → `mark_sent()` sequence on one
  `AgentSession`.
- `core/sessions.py` `AgentSession.mark_sent()` mutates counters and history
  without a lock.
- `core/accounts.py` `note_used()` / `mark_failure()` mutate the shared
  `Account` outside `AccountPool._lock`.

The proposed fix shape is a per-`AgentSession` lock held from `plan()` through
`mark_sent()`, and moving account mutations under the pool lock. Until then,
give each concurrent request its own `x-agent-session` value. Full review notes
live in `docs/concurrency-notes.md`.

---

## License

Provided as-is for educational and personal use. Not affiliated with DeepSeek
or Google.

Gemini support depends on [gemini-webapi](https://github.com/HanaokaYuzu/Gemini-API),
which is licensed **AGPL-3.0**. If you redistribute this project with Gemini
enabled, that dependency's license applies to the combined work.