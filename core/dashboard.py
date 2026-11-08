"""Dashboard HTML for DeepSeek Web2API v2."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeepSeek + Gemini Web2API</title>
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
  .acct{display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;background:var(--code)}
  .dot{width:10px;height:10px;border-radius:50%;flex:0 0 auto}
  .dot.good{background:#3fb950;box-shadow:0 0 6px #3fb950}
  .dot.bad{background:var(--red);box-shadow:0 0 6px var(--red)}
  .dot.unknown{background:#d29922}
  .acct .meta{flex:1;min-width:0}
  .acct .name{font-size:13px;font-family:monospace}
  .acct .msg{font-size:11px;color:var(--muted);word-break:break-word}
  .pill{font-size:11px;padding:2px 8px;border-radius:10px;white-space:nowrap}
  .pill.good{background:rgba(35,134,54,.2);color:#3fb950;border:1px solid var(--green)}
  .pill.bad{background:rgba(248,81,73,.2);color:var(--red);border:1px solid #da3633}
  .pill.warn{background:rgba(210,153,34,.2);color:#d29922;border:1px solid #d29922}
  .del{background:transparent;border:1px solid var(--border);color:var(--muted);padding:1px 8px;border-radius:6px;font-size:15px;line-height:1.3;margin:0}
  .del:hover{color:var(--red);border-color:var(--red);background:rgba(248,81,73,.12)}
</style>
</head>
<body>
<header>
  <h1>&#9889; DeepSeek + Gemini <small>Web2API v2</small></h1>
  <div><span id="badge" class="badge bad">&#9679; Checking...</span></div>
</header>

<div class="grid">
  <div>
    <div class="card">
      <h2>&#128273; Add Account</h2>
      <small>Paste your <code>userToken</code> (eyJ...) or exported cookies. Each token is validated and saved automatically as <code>accounts/acctN.txt</code>.</small>
      <textarea id="creds" placeholder="Paste userToken or cookies here..."></textarea>
      <div style="margin-top:8px">
        <button onclick="addAccount()">Add Account</button>
        <button class="alt" onclick="document.getElementById('creds').value=''">Cancel</button>
      </div>
    </div>
    <div class="card">
      <h2>&#128101; Accounts</h2>
      <div id="accounts"><small>Loading...</small></div>
      <div style="margin-top:10px">
        <button onclick="refreshAccounts(true)">Re-check all</button>
        <button class="alt" onclick="reloadAccounts()">Reload files</button>
      </div>
    </div>
    <div class="card">
      <h2>&#10024; Gemini Web</h2>
      <div id="geminiStatus"><small>Checking...</small></div>
      <small>Paste <code>__Secure-1PSID</code> + <code>__Secure-1PSIDTS</code> from <code>gemini.google.com</code> (JSON export, Cookie header, or cookies.txt).</small>
      <textarea id="geminiCookies" placeholder="Paste Gemini cookies here..."></textarea>
      <div style="margin-top:8px">
        <button onclick="connectGemini()">Connect Gemini</button>
        <button class="alt" onclick="verifyGemini()">Verify</button>
        <button class="alt" onclick="disconnectGemini()">Disconnect</button>
      </div>
      <small>Use model names starting with <code>gemini-</code>, e.g. <code>gemini-flash</code> or <code>gemini-pro</code>.</small>
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
        <option value="gemini-flash">Gemini Flash (web)</option>
        <option value="gemini-pro">Gemini Pro (web)</option>
        <option value="gemini-flash-lite">Gemini Flash Lite (web)</option>
      </select>
      <div id="chat" class="chat"><small>Send a prompt to test your connection.</small></div>
      <div class="row">
        <input id="prompt" placeholder="Ask DeepSeek or Gemini..." onkeydown="if(event.key==='Enter')send()">
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
        "deepseek-chat": { name: "DeepSeek V3" },
        "gemini-flash": { name: "Gemini Flash (web)" },
        "gemini-pro": { name: "Gemini Pro (web)" },
        "gemini-flash-lite": { name: "Gemini Flash Lite (web)" }
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

let dsValid = 0, dsTotal = 0;
let geminiState = {configured: false, connected: false, message: "", models: []};

function updateBadge(){
  const b = document.getElementById("badge");
  const parts = [];
  if (dsValid > 0) parts.push(dsValid + "/" + dsTotal + " DeepSeek");
  else if (dsTotal > 0) parts.push("DeepSeek invalid");
  if (geminiState.connected) parts.push("Gemini");
  if (parts.length){
    b.className = "badge ok";
    b.textContent = "\u25CF " + parts.join(" + ");
  } else {
    b.className = "badge bad";
    b.textContent = "\u25CF " + (geminiState.configured ? "Gemini saved (unverified)" : "Disconnected");
  }
}

async function checkStatus(){
  const b = document.getElementById("badge");
  try {
    const r = await apiFetch("/api/status");
    const d = await r.json();
    if (d.gemini) geminiState = d.gemini;
    if (d.authenticated && dsTotal === 0){ dsValid = 1; dsTotal = 1; }
    updateBadge();
  } catch(e) {
    b.className = "badge bad";
    b.textContent = "\u25CF Server unreachable";
  }
}

function fmtAge(ts){
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now()/1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s/60) + "m ago";
  return Math.round(s/3600) + "h ago";
}

function renderAccounts(list, checked){
  const box = document.getElementById("accounts");
  box.innerHTML = "";
  if (!list.length){
    box.innerHTML = '<small>No accounts yet. Paste a token above and press Add Account.</small>';
    return;
  }
  for (const a of list){
    const row = document.createElement("div");
    row.className = "acct";

    const dot = document.createElement("span");
    const state = a.valid === true ? "good" : a.valid === false ? "bad" : "unknown";
    dot.className = "dot " + state;
    row.appendChild(dot);

    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = a.id + "  \u00b7  " + (a.source || "?");
    const msg = document.createElement("div");
    msg.className = "msg";
    let detail = a.valid_message || (a.valid === null ? "not checked yet" : "");
    if (a.last_status) detail += (detail ? " \u00b7 " : "") + "last HTTP " + a.last_status;
    if (a.cooling_for > 0) detail += (detail ? " \u00b7 " : "") + "cooling " + Math.ceil(a.cooling_for) + "s";
    detail += (detail ? " \u00b7 " : "") + "checked " + fmtAge(a.checked_at);
    msg.textContent = detail;
    meta.appendChild(name);
    meta.appendChild(msg);
    row.appendChild(meta);

    const pill = document.createElement("span");
    if (a.valid === true) { pill.className = "pill good"; pill.textContent = "valid"; }
    else if (a.valid === false) { pill.className = "pill bad"; pill.textContent = "invalid"; }
    else { pill.className = "pill warn"; pill.textContent = "unknown"; }
    row.appendChild(pill);

    if ((a.source || "").toLowerCase().endsWith(".txt")) {
      const del = document.createElement("button");
      del.className = "del";
      del.title = "Delete " + a.source;
      del.textContent = "\u00d7";
      del.onclick = () => deleteAccount(a.source);
      row.appendChild(del);
    }

    box.appendChild(row);
  }
  if (checked) {
    const t = document.createElement("small");
    t.textContent = "Checked " + new Date().toLocaleTimeString();
    box.appendChild(t);
  }
}

async function refreshAccounts(force){
  const box = document.getElementById("accounts");
  if (!box.querySelector(".acct")) box.innerHTML = '<small>Checking accounts...</small>';
  try {
    const r = await apiFetch("/v1/accounts" + (force ? "?validate=1" : ""), {method: force ? "POST" : "GET"});
    const d = await r.json();
    renderAccounts(d.data || [], force);
    dsValid = (d.data || []).filter(a => a.valid === true).length;
    dsTotal = (d.data || []).length;
    updateBadge();
  } catch(e) {
    box.innerHTML = '<small>Could not load accounts: ' + e.message + '</small>';
  }
}

async function reloadAccounts(){
  try {
    await apiFetch("/reload-accounts", {method: "POST"});
  } catch(e) {}
  refreshAccounts(true);
}

async function addAccount(){
  const box = document.getElementById("creds");
  const raw = box.value.trim();
  if (!raw) return alert("Paste a token or cookies first.");
  try {
    const r = await apiFetch("/api/accounts", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({raw_text: raw})
    });
    const d = await r.json();
    alert(d.message || (d.success ? "Account added." : "Could not add account."));
    if (d.success) {
      box.value = "";
      refreshAccounts(true);
    }
  } catch(e) {
    alert("Could not add account: " + e.message);
  }
}

async function deleteAccount(name){
  if (!confirm("Delete " + name + "? This removes the credential file.")) return;
  try {
    const r = await apiFetch("/api/accounts/" + encodeURIComponent(name), {method: "DELETE"});
    const d = await r.json();
    if (!d.success) alert(d.message || "Could not delete account.");
  } catch(e) {
    alert("Could not delete account: " + e.message);
  }
  refreshAccounts(true);
}

function renderGemini(s){
  const box = document.getElementById("geminiStatus");
  box.innerHTML = "";
  const row = document.createElement("div");
  row.className = "acct";

  const dot = document.createElement("span");
  dot.className = "dot " + (s.connected ? "good" : s.configured ? "unknown" : "bad");
  row.appendChild(dot);

  const meta = document.createElement("div");
  meta.className = "meta";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = s.connected ? "Gemini connected" : s.configured ? "Gemini cookies saved" : "Gemini not configured";
  const msg = document.createElement("div");
  msg.className = "msg";
  const models = (s.models || []).slice(0, 4).join(", ");
  msg.textContent = (s.message || "") + (models ? " \u00b7 " + models : "");
  meta.appendChild(name);
  meta.appendChild(msg);
  row.appendChild(meta);

  const pill = document.createElement("span");
  pill.className = "pill " + (s.connected ? "good" : s.configured ? "warn" : "bad");
  pill.textContent = s.connected ? "live" : s.configured ? "saved" : "off";
  row.appendChild(pill);

  box.appendChild(row);
}

async function refreshGemini(){
  try {
    const r = await apiFetch("/api/gemini");
    const s = await r.json();
    geminiState = s;
    renderGemini(s);
    updateBadge();
  } catch(e) {
    document.getElementById("geminiStatus").innerHTML = '<small>Could not load Gemini status: ' + e.message + '</small>';
  }
}

async function connectGemini(){
  const box = document.getElementById("geminiCookies");
  const raw = box.value.trim();
  if (!raw) return alert("Paste your Gemini cookies first.");
  try {
    const r = await apiFetch("/api/gemini", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({raw_text: raw})
    });
    const d = await r.json();
    alert(d.message || (d.success ? "Gemini connected." : "Could not connect Gemini."));
    if (d.success) {
      box.value = "";
      refreshGemini();
    }
  } catch(e) {
    alert("Could not connect Gemini: " + e.message);
  }
}

async function verifyGemini(){
  try {
    const r = await apiFetch("/api/gemini", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({raw_text: ""})
    });
    const d = await r.json();
    alert(d.message || (d.success ? "Gemini verified." : "Gemini verification failed."));
    refreshGemini();
  } catch(e) {
    alert("Could not verify Gemini: " + e.message);
  }
}

async function disconnectGemini(){
  if (!confirm("Remove saved Gemini cookies?")) return;
  try { await apiFetch("/api/gemini", {method: "DELETE"}); } catch(e) {}
  refreshGemini();
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
    const controller = new AbortController();
    const r = await apiFetch("/v1/chat/completions", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({model, messages:[{role:"user",content:text}], stream:true}),
      signal: controller.signal
    });
    if (!r.ok) { a.textContent = "Error: " + await r.text(); return; }

    a.textContent = "";
    a.style.color = "";
    let idleTimer = setTimeout(() => controller.abort("idle"), 45000);
    const bumpIdle = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => controller.abort("idle"), 45000);
    };
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        bumpIdle();
        buf += dec.decode(value, {stream:true});
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          const t = line.trim();
          if (!t.startsWith("data:")) continue;
          const data = t.slice(5).trim();
          if (data === "[DONE]") continue;
          try {
            const j = JSON.parse(data);
            if (j.error) {
              a.textContent = "Error: " + (j.error.message || JSON.stringify(j.error));
              a.style.color = "#ff7b72";
              a.style.whiteSpace = "pre-wrap";
              continue;
            }
            const d = j.choices?.[0]?.delta || {};
            if (d.content) a.textContent += d.content;
            if (d.reasoning_content) a.textContent += d.reasoning_content;
          } catch(e){}
        }
        chat.scrollTop = chat.scrollHeight;
      }
    } finally {
      clearTimeout(idleTimer);
    }
    if (!a.textContent.trim()) {
      a.textContent = "No response (empty stream)";
      a.style.color = "#ffb86b";
    }
  } catch(e) {
    if (e && (e.name === "AbortError" || String(e).includes("idle"))) {
      a.textContent = (a.textContent || "") + "\n[stalled - no data for 45s]";
      a.style.color = "#ffb86b";
      a.style.whiteSpace = "pre-wrap";
    } else {
      a.textContent = "Connection error: " + e.message;
    }
  } finally {
    document.getElementById("sendBtn").disabled = false;
  }
}

checkStatus();
refreshAccounts(true);
refreshGemini();
setInterval(() => refreshAccounts(false), 60000);
setInterval(() => refreshGemini(), 120000);
</script>
</body>
</html>
"""
