# DeepSeek Web2API v2

**Use your DeepSeek web account as a local OpenAI-compatible API.**

Runs a small FastAPI server on `http://localhost:8084` that speaks the OpenAI
`/v1/chat/completions` protocol and forwards requests to `chat.deepseek.com`
using your own logged-in session. Works with any OpenAI-compatible client
(OpenCode, Cursor, Continue, LiteLLM, the OpenAI Python/JS SDK, etc.).

> ⚠️ **Disclaimer** — This project is unofficial and not affiliated with
> DeepSeek. It relies on private web endpoints and may break at any time.
> Use it for personal experimentation only and respect DeepSeek's Terms of
> Service. Never commit your credentials.

---

## Features

- 🚀 **No API key needed** — uses your free `chat.deepseek.com` session.
- 🧠 **Thinking + Search** — exposes the web model in normal, reasoning and
  web-search variants.
- 🔐 **Built-in PoW solver** — solves DeepSeek's `DeepSeekHashV1` WebAssembly
  proof-of-work challenge automatically.
- 🔄 **True SSE streaming** — streams content and `reasoning_content` as it is
  generated.
- 🛠️ **Tool calling** — the model is asked for a single JSON envelope; a
  waterfall parser (`raw_decode` → `json_repair` → `json5` → fences) recovers
  it, then retries once with self-correction and fails **loudly** with HTTP 502
  instead of leaking broken markup into `content`.
- 🖼️ **Image input** — uploads images from `image_url` parts to DeepSeek's
  vision pipeline.
- 🎛️ **Web dashboard** — paste credentials, check status and test chat at
  `http://localhost:8084`.
- 🐧 **Windows + Linux/macOS** — ships with both `.bat` and `.sh` launchers.

---

## Requirements

- **Python 3.10+**
- A DeepSeek account (https://chat.deepseek.com)
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
cookie string. Both are stored locally in `config.json`, which is git-ignored
and never committed.

### Method 1 — Browser DevTools (fastest)

1. Open [chat.deepseek.com](https://chat.deepseek.com) and log in.
2. Press `F12` (or right-click → **Inspect**) to open Developer Tools.
3. Go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
4. On the left, expand **Local Storage** → click `https://chat.deepseek.com`.
5. Find the key **`userToken`**, double-click its value and copy it
   (it starts with `eyJ...`).
6. Paste it into the dashboard at `http://localhost:8084` and click
   **Save & Connect**.

### Method 2 — Export cookies

1. Install a cookie export extension such as
   [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
   (Chrome & Firefox).
2. Visit [chat.deepseek.com](https://chat.deepseek.com) while logged in.
3. Export the cookies for the site.
4. Either save them as `cookie_deepseek.txt` in the project folder, or paste
   the content into the dashboard. Both raw cookie strings and Netscape
   `cookies.txt` format are accepted.

### Method 3 — Paste anything that contains the token

The dashboard parser also accepts a bare JWT, a `Cookie:` header, a cURL dump,
or a localStorage JSON blob. It extracts `userToken` automatically.

> 🔒 **Keep it secret.** Anyone with your `userToken` can use your DeepSeek
> account. If it leaks, log out of chat.deepseek.com to invalidate it.

---

## Models

| Model ID | Description |
|---|---|
| `deepseek-ai/DeepSeek-V4.1-Flash-1017` | Fast default model |
| `deepseek-ai/DeepSeek-V4.1-Flash-1017-search` | Fast model + web search |
| `deepseek-chat` | DeepSeek V3 (fast) |
| `deepseek-search` | DeepSeek V3 + web search |
| `deepseek-reasoner` | Reasoning / thinking model |
| `deepseek-reasoner-search` | Reasoning + web search |

You can also force behaviour per request with `search_enabled` and
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

## Client integration

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
| `GET` | `/health` | Liveness check |
| `GET` | `/api/status` | Auth status and user info |
| `POST` | `/api/cookies` | Save/validate credentials |
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | Chat completion (stream or not) |

---

## Configuration

Credentials are read from `config.json` in the project root:

```json
{
  "token": "your-userToken",
  "cookie": "optional-cookie-string",
  "updated_at": 0
}
```

`config.json` and `cookie_deepseek.txt` are **git-ignored**. A safe template
lives in `config.example.json`.

To change the host/port, edit `main.py` (defaults: `127.0.0.1:8084`).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 DeepSeek session not configured` | Paste your `userToken` in the dashboard. |
| `502 model attempted a tool call that could not be parsed` | The model ignored the JSON envelope twice. Retry; this is a deliberate loud failure instead of leaking broken markup. |
| `502 DeepSeek returned an empty response` | Upstream sent an empty stream (usually rate limiting). Back off and retry. |
| Process aborts with `panicked at crates/unwinder/src/stackwalk.rs` | Fixed: the WASM PoW store is now serialized. If you see it, you are running an old copy. |
| `Validation failed` | Token expired — log in again and copy a fresh one. |
| `pip install` fails on Linux | Install `python3-venv` (`sudo apt install python3-venv`). |
| `wasmtime` install error | Use Python 3.10–3.12; ensure wheels are available for your platform. |
| Browser doesn't open | Headless server — just visit the URL manually. |

---

## License

Provided as-is for educational and personal use. Not affiliated with DeepSeek.