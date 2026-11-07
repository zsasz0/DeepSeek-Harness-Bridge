"""Dashboard HTML for DeepSeek Web2API v2."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeepSeek Web2API v2</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--green:#238636;--blue:#58a6ff;--red:#f85149;--code:#090d13}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.5;padding:24px;max-width:1000px;margin:0 auto}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--border)}
  h1{font-size:20px}
  .badge{padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600}
  .ok{background:rgba(35,134,54,.2);color:#3fb950;border:1px solid var(--green)}
  .bad{background:rgba(248,81,73,.2);color:var(--red);border:1px solid #da3633}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:820px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:18px;margin-bottom:18px}
  .card h2{font-size:15px;margin-bottom:10px}
  textarea,input,select{width:100%;background:var(--code);border:1px solid var(--border);border-radius:6px;color:#fff;padding:9px;font-family:monospace;font-size:13px}
  textarea{height:110px;resize:vertical}
  input{font-family:inherit}
  button{background:var(--green);color:#fff;border:none;padding:8px 14px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;margin-right:6px}
  button:hover{background:#2ea043}
  button.alt{background:#21262d;border:1px solid var(--border);color:var(--text)}
  pre{background:var(--code);border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;font-size:12px}
  .chat{background:var(--code);border:1px solid var(--border);border-radius:6px;min-height:180px;max-height:340px;overflow-y:auto;padding:10px;font-size:14px;margin-bottom:10px}
  .u{color:var(--blue);font-weight:600;margin-bottom:4px}
  .a{margin-bottom:10px;white-space:pre-wrap;word-break:break-word}
  .row{display:flex;gap:8px}
  small{color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>&#9889; DeepSeek Web2API <small>v2</small></h1>
  <div><span id="badge" class="badge bad">&#9679; Checking...</span></div>
</header>

<div class="grid">
  <div>
    <div class="card">
      <h2>&#128273; Credentials</h2>
      <small>Paste your <code>userToken</code> (eyJ...) or exported cookies.</small>
      <textarea id="creds" placeholder="Paste userToken or cookies here..."></textarea>
      <div style="margin-top:8px">
        <button onclick="saveCreds()">Save &amp; Connect</button>
        <button class="alt" onclick="clearCreds()">Clear</button>
      </div>
    </div>
    <div class="card">
      <h2>&#128187; OpenCode Config</h2>
      <pre id="snippet"></pre>
      <button onclick="copySnippet()">Copy</button>
    </div>
  </div>

  <div>
    <div class="card">
      <h2>&#129514; Live Test</h2>
      <select id="model" style="margin-bottom:8px">
        <option value="deepseek-ai/DeepSeek-V4.1-Flash-1017">V4.1 Flash (Fast)</option>
        <option value="deepseek-ai/DeepSeek-V4.1-Flash-1017-search">V4.1 Flash + Search</option>
        <option value="deepseek-reasoner">R1 (Thinking)</option>
        <option value="deepseek-chat">V3 (Chat)</option>
      </select>
      <div id="chat" class="chat"><small>Send a prompt to test your connection.</small></div>
      <div class="row">
        <input id="prompt" placeholder="Ask DeepSeek..." onkeydown="if(event.key==='Enter')send()">
        <button id="sendBtn" onclick="send()">Send</button>
      </div>
    </div>
    <div class="card">
      <h2>&#8505;&#65039; Endpoints</h2>
      <small>
        Base URL: <code>http://localhost:8084/v1</code><br>
        Chat: <code>POST /v1/chat/completions</code><br>
        Models: <code>GET /v1/models</code><br>
        API key: any dummy string
      </small>
    </div>
  </div>
</div>

<script>
const CONFIG = {
  provider: {
    "deepseek-local": {
      npm: "@ai-sdk/openai-compatible",
      name: "DeepSeek Local",
      options: { baseURL: "http://localhost:8084/v1", apiKey: "sk-deepseek-local", timeout: 180000 },
      models: {
        "deepseek-ai/DeepSeek-V4.1-Flash-1017": { name: "DeepSeek V4.1 Flash" },
        "deepseek-ai/DeepSeek-V4.1-Flash-1017-search": { name: "DeepSeek V4.1 Flash + Search" },
        "deepseek-reasoner": { name: "DeepSeek R1" },
        "deepseek-chat": { name: "DeepSeek V3" }
      }
    }
  }
};
document.getElementById("snippet").textContent = JSON.stringify(CONFIG, null, 2);

function copySnippet(){ navigator.clipboard.writeText(JSON.stringify(CONFIG,null,2)); }

function proxyKey(){
  const fromUrl = new URLSearchParams(location.search).get("key");
  if (fromUrl) localStorage.setItem("proxyKey", fromUrl);
  return localStorage.getItem("proxyKey") || "";
}
async function apiFetch(url, options){
  options = options || {};
  const headers = Object.assign({}, options.headers || {});
  const key = proxyKey();
  if (key) headers["Authorization"] = "Bearer " + key;
  const r = await fetch(url, Object.assign({}, options, {headers}));
  if (r.status === 401 && !key){
    const entered = prompt("This proxy requires PROXY_API_KEY. Enter it:");
    if (entered){
      localStorage.setItem("proxyKey", entered.trim());
      return apiFetch(url, options);
    }
  }
  return r;
}

async function checkStatus(){
  const b = document.getElementById("badge");
  try {
    const r = await apiFetch("/api/status");
    const d = await r.json();
    if (d.authenticated) {
      b.className = "badge ok";
      b.textContent = "\\u25CF Connected";
    } else {
      b.className = "badge bad";
      b.textContent = "\\u25CF " + (d.message || "Disconnected");
    }
  } catch(e) {
    b.className = "badge bad";
    b.textContent = "\\u25CF Server unreachable";
  }
}

async function saveCreds(){
  const raw = document.getElementById("creds").value.trim();
  if (!raw) return alert("Paste credentials first.");
  const r = await apiFetch("/api/cookies", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({raw_text: raw})
  });
  const d = await r.json();
  alert(d.message);
  if (d.success) document.getElementById("creds").value = "";
  checkStatus();
}

async function clearCreds(){
  if (!confirm("Clear stored credentials?")) return;
  await apiFetch("/api/cookies", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({raw_text: ""})
  });
  checkStatus();
}

async function send(){
  const input = document.getElementById("prompt");
  const text = input.value.trim();
  if (!text) return;
  const model = document.getElementById("model").value;
  const chat = document.getElementById("chat");
  if (chat.querySelector("small")) chat.innerHTML = "";

  const u = document.createElement("div");
  u.className = "u";
  u.textContent = "You: " + text;
  chat.appendChild(u);

  const a = document.createElement("div");
  a.className = "a";
  a.textContent = "Thinking...";
  chat.appendChild(a);
  input.value = "";
  document.getElementById("sendBtn").disabled = true;

  try {
    const r = await apiFetch("/v1/chat/completions", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({model, messages:[{role:"user",content:text}], stream:true})
    });
    if (!r.ok) { a.textContent = "Error: " + await r.text(); return; }

    a.textContent = "";
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split("\\n");
      buf = lines.pop();
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        const data = t.slice(5).trim();
        if (data === "[DONE]") continue;
        try {
          const j = JSON.parse(data);
          const d = j.choices?.[0]?.delta || {};
          if (d.content) a.textContent += d.content;
          if (d.reasoning_content) a.textContent += d.reasoning_content;
        } catch(e){}
      }
      chat.scrollTop = chat.scrollHeight;
    }
  } catch(e) {
    a.textContent = "Connection error: " + e.message;
  } finally {
    document.getElementById("sendBtn").disabled = false;
  }
}

checkStatus();
</script>
</body>
</html>
"""
