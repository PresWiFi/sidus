/* Sidus Relay: one thread, Claude Code and Codex taking turns. Plain JS, no build step.
   Loaded by Sidus when SIDUS_PLUGINS includes "relay". */
(() => {
  "use strict";
  const H = window.sidus;
  const { api, esc, toast, fmt } = H;
  const BASE = "/api/relay";
  const $ = (sel, root = document) => root.querySelector(sel);
  const LABEL = { claude: "Claude", codex: "Codex" };

  // Agent output is untrusted: sanitize it, and allow no embeds (they'd load without a click).
  const PURIFY = {
    FORBID_TAGS: ["img", "picture", "source", "video", "audio", "iframe", "object", "embed", "svg", "math", "form",
      "input", "button", "textarea", "select", "style", "link", "meta"],
    FORBID_ATTR: ["style", "srcset"],
  };
  const md = (text) => DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true }), PURIFY);
  const agentBadge = (a) => (a ? `<span class="r-agent r-${a}">${LABEL[a]}</span>` : "");
  const isEditing = () => {
    if ($("#dialog").open) return true;
    const el = document.activeElement;
    return !!el && $("#main").contains(el) && el.matches("input, textarea, select") && !!el.value;
  };

  function usageCard(st) {
    const row = (a) => {
      const u = st.agents[a];
      if (!u.linked) return `<div class="r-use"><b>${LABEL[a]}</b><span class="muted">Not signed in</span></div>`;
      const bar = (label, v, limit) => `<span class="r-meter" title="${label}: ${fmt.pct(v)} (switches at ${limit}%)">
        <span class="bar"><i style="width:${Math.min(100, v || 0)}%" class="${(v || 0) >= limit ? "over" : ""}"></i></span>${label} ${fmt.pct(v)}</span>`;
      const blocked = u.blocked_until ? `<span class="badge warn">Out of usage until ${new Date(u.blocked_until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>` : "";
      return `<div class="r-use"><b>${LABEL[a]}</b>${bar("5h", u.five_hour, st.settings.five_hour)}${bar("Week", u.weekly, st.settings.weekly)}${blocked}</div>`;
    };
    return `<div class="card r-usage"><h2>Usage</h2>${row("claude")}${row("codex")}
      <form class="r-settings" id="r-settings">
        <label>Switch at <input type="number" min="1" max="100" name="five_hour" value="${st.settings.five_hour}">% of 5h</label>
        <label>or <input type="number" min="1" max="100" name="weekly" value="${st.settings.weekly}">% of the week</label>
        <label>Start with <select name="default">${["claude", "codex"].map((a) => `<option value="${a}" ${st.settings.default === a ? "selected" : ""}>${LABEL[a]}</option>`).join("")}</select></label>
        <button class="btn small">Save</button>
      </form></div>`;
  }

  function bindSettings(onSaved) {
    const form = $("#r-settings");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      try {
        await api(`${BASE}/settings`, { method: "PUT", body: { five_hour: +f.get("five_hour"), weekly: +f.get("weekly"), default: f.get("default") } });
        toast("Saved");
        onSaved();
      } catch (err) { toast(err.message); }
    });
  }

  /* ---------------- list ---------------- */

  function listView() {
    let disposed = false;
    H.setMain(`${H.topbar("Relay")}<div class="scroll"><div class="page relay" id="relay"></div></div>`);

    async function load() {
      let threads, st, error = "";
      try {
        [threads, st] = await Promise.all([api(`${BASE}/threads`), api(`${BASE}/status`)]);
      } catch (e) { error = e.message; }
      if (disposed || isEditing()) return;
      const root = $("#relay");
      if (error) { root.innerHTML = `<h1>Relay</h1><p class="error">${esc(error)}</p>`; return; }
      root.innerHTML = `<div class="r-top"><h1>Relay</h1><button class="btn primary" id="r-new">New thread</button></div>
        <p class="muted r-intro">One conversation, two agents. Each turn goes to Claude Code or Codex, whichever plan has room, and they can ask each other questions or hand work over. Turns run unattended with permissions bypassed, in the thread's folder.</p>
        ${usageCard(st)}
        <div class="r-list">${threads.threads.map((t) => `<a class="r-row" href="#/relay/t/${t.id}">
          <span class="r-title">${esc(t.name)}</span>
          <span class="r-meta">${t.running ? `<span class="badge accent">Working</span>` : ""}${t.waiting ? `<span class="badge warn">Waiting</span>` : ""}${t.pin ? `<span class="badge">Pinned: ${LABEL[t.pin]}</span>` : ""}${agentBadge(t.last_agent)}<span class="muted">${fmt.ago(t.updated)}</span></span>
          <span class="r-sub">${esc(t.cwd)}</span></a>`).join("") || `<p class="muted">No threads yet.</p>`}</div>`;
      $("#r-new").onclick = newThread;
      bindSettings(load);
    }
    load();
    const timer = setInterval(load, 8000);
    return { dispose() { disposed = true; clearInterval(timer); } };
  }

  function newThread() {
    const dlg = $("#dialog");
    let cwd = "";
    dlg.innerHTML = `<form method="dialog" id="r-form" class="r-form">
      <h2>New Relay thread</h2>
      <label>Name <input name="name" required maxlength="80" placeholder="What you're working on"></label>
      <label>Folder</label>
      <div class="picker"><div class="picker-path"><button type="button" class="btn small ghost" id="rp-up" aria-label="Up one folder">↑</button><span id="rp-path"></span></div>
        <div class="picker-list" id="rp-list"></div></div>
      <label>Agent <select name="pin"><option value="">Automatic (by usage)</option><option value="claude">Always Claude</option><option value="codex">Always Codex</option></select></label>
      <label>First message <textarea name="message" rows="4" placeholder="Optional"></textarea></label>
      <p class="error" id="r-err"></p>
      <div class="dialog-actions"><button type="button" class="btn" id="r-cancel">Cancel</button><button class="btn primary" id="r-go">Start</button></div>
    </form>`;
    dlg.showModal();

    async function browse(path) {
      try {
        const d = await api("/api/dirs?path=" + encodeURIComponent(path || ""));
        cwd = d.path;
        $("#rp-path").textContent = d.path ? "‎" + d.path : "Workspaces";
        $("#rp-up").disabled = !d.parent && !d.path;
        $("#rp-up").onclick = () => browse(d.parent || "");
        $("#rp-list").innerHTML = d.dirs.map((x) => `<button type="button" data-path="${esc(x.path)}">${esc(x.name)}</button>`).join("") || `<div class="empty">No subfolders</div>`;
      } catch (e) { $("#r-err").textContent = e.message; }
    }
    $("#rp-list").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) browse(b.dataset.path); });
    $("#r-cancel").onclick = () => dlg.close();
    $("#r-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      if (!cwd) { $("#r-err").textContent = "Pick a folder."; return; }
      $("#r-go").disabled = true;
      try {
        const t = await api(`${BASE}/threads`, { method: "POST", body: { name: f.get("name"), cwd, pin: f.get("pin") || null, message: f.get("message") || null } });
        dlg.close();
        location.hash = `#/relay/t/${t.id}`;
      } catch (err) { $("#r-err").textContent = err.message; $("#r-go").disabled = false; }
    });
    browse("");
  }

  /* ---------------- thread ---------------- */

  const STATUS = {
    queued: ["Queued", ""], running: ["Working…", "accent"], done: ["", ""], failed: ["Failed", "bad"],
    stopped: ["Stopped", "warn"], limit: ["Hit its usage limit", "warn"],
  };

  function turnHtml(t) {
    const [label, cls] = STATUS[t.status] || [t.status, ""];
    let head = "";
    if (t.kind === "message") head = `<div class="r-msg r-user">${md(t.input)}</div>`;
    else if (t.kind === "handoff") head = `<div class="r-note">Handed over: ${esc(t.input)}${t.notes ? `<details><summary>Notes</summary>${md(t.notes)}</details>` : ""}</div>`;
    else head = `<div class="r-note">Picked up after the other agent hit its limit.</div>`;
    const steps = t.steps.length ? `<details class="r-steps" ${t.status === "running" ? "open" : ""}><summary>${t.steps.length} step${t.steps.length === 1 ? "" : "s"}${t.files.length ? ` · ${t.files.length} file${t.files.length === 1 ? "" : "s"} changed` : ""}</summary>
      <ul>${t.steps.slice(-60).map((s) => `<li>${esc(s)}</li>`).join("")}</ul></details>` : "";
    const consults = (t.consults || []).map((c) => `<details class="r-consult"><summary>Asked ${LABEL[c.to]}: ${esc(c.question.slice(0, 120))}</summary>
      ${c.answer ? md(c.answer) : `<p class="error">${esc(c.error)}</p>`}${c.session_id ? `<a href="#/s/${esc(c.session_id)}">Open ${LABEL[c.to]}'s session</a>` : ""}</details>`).join("");
    const actions = [
      t.status === "queued" ? `<button class="btn small ghost" data-cancel="${t.id}">Cancel</button>` : "",
      ["failed", "stopped"].includes(t.status) ? `<button class="btn small ghost" data-retry="${t.id}">Retry</button>` : "",
      t.session_id ? `<a class="btn small ghost" href="#/s/${esc(t.session_id)}">Open session</a>` : "",
    ].join("");
    const body = t.agent || t.status !== "queued" ? `<div class="r-msg r-reply">
        <div class="r-reply-head">${agentBadge(t.agent)}${label ? `<span class="badge ${cls}">${label}</span>` : ""}${t.reason ? `<span class="muted r-reason">${esc(t.reason)}</span>` : ""}<span class="r-actions">${actions}</span></div>
        ${steps}${consults}
        ${t.result ? `<div class="r-md">${md(t.result)}</div>` : ""}
        ${t.error && t.status !== "done" ? `<p class="error">${esc(t.error)}</p>` : ""}
        ${t.handoff ? `<p class="muted">Handed the rest to ${LABEL[t.agent === "claude" ? "codex" : "claude"]}.</p>` : ""}
      </div>` : `<div class="r-msg r-reply"><span class="badge">Queued</span><span class="r-actions">${actions}</span></div>`;
    return `<div class="r-turn" id="turn-${t.id}">${head}${body}</div>`;
  }

  function threadView(id) {
    let disposed = false;
    let timer = null;
    let lastKey = "";
    H.setMain(`${H.topbar("Relay")}<div class="scroll" id="r-scroll"><div class="page relay" id="relay"><p class="muted">Loading…</p></div></div>
      <form class="r-compose" id="r-compose"><textarea id="r-text" rows="2" placeholder="Message the thread…"></textarea>
      <button class="btn primary" id="r-send">Send</button></form>`);

    async function load() {
      let t;
      try { t = await api(`${BASE}/threads/${id}`); } catch (e) {
        if (!disposed) $("#relay").innerHTML = `<p class="error">${esc(e.message)}. <a href="#/relay">Back to Relay</a></p>`;
        return;
      }
      if (disposed) return;
      schedule(t.running || t.queued ? 2500 : 10000);
      const key = JSON.stringify([t.updated, t.waiting, t.turn_list.map((x) => [x.status, x.steps.length, x.result.length, (x.consults || []).length])]);
      if (key === lastKey) return;
      lastKey = key;
      const sc = $("#r-scroll");
      const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 80;
      $("#relay").innerHTML = `<div class="r-head">
          <div><a class="r-back" href="#/relay">← Relay</a><h1>${esc(t.name)}</h1><div class="muted r-sub">${esc(t.cwd)}</div></div>
          <div class="r-head-actions">
            <select id="r-pin" aria-label="Which agent"><option value="">Automatic</option><option value="claude" ${t.pin === "claude" ? "selected" : ""}>Always Claude</option><option value="codex" ${t.pin === "codex" ? "selected" : ""}>Always Codex</option></select>
            ${t.running ? `<button class="btn" id="r-stop">Stop</button>` : ""}
            <button class="btn ghost" id="r-archive">${t.archived ? "Unarchive" : "Archive"}</button>
            ${t.running ? "" : `<button class="btn ghost danger" id="r-delete">Delete</button>`}
          </div></div>
        ${t.waiting ? `<p class="badge warn r-waiting">Waiting: ${esc(t.waiting)}</p>` : ""}
        <div class="r-turns">${t.turn_list.map(turnHtml).join("") || `<p class="muted">Send a message to start.</p>`}</div>`;
      if (atBottom) sc.scrollTop = sc.scrollHeight;
      $("#r-pin").onchange = (e) => act(() => api(`${BASE}/threads/${id}`, { method: "PATCH", body: { pin: e.target.value || null } }));
      if ($("#r-stop")) $("#r-stop").onclick = () => act(() => api(`${BASE}/threads/${id}/stop`, { method: "POST" }));
      $("#r-archive").onclick = () => act(() => api(`${BASE}/threads/${id}`, { method: "PATCH", body: { archived: !t.archived } }));
      if ($("#r-delete")) $("#r-delete").onclick = async () => {
        if (!confirm("Delete this thread? Claude's and Codex's own sessions stay in the sidebar.")) return;
        try { await api(`${BASE}/threads/${id}`, { method: "DELETE" }); location.hash = "#/relay"; } catch (e) { toast(e.message); }
      };
    }

    async function act(fn) {
      try { await fn(); lastKey = ""; load(); } catch (e) { toast(e.message); }
    }
    function schedule(ms) { clearTimeout(timer); if (!disposed) timer = setTimeout(load, ms); }

    $("#relay").addEventListener("click", (e) => {
      const retry = e.target.closest("[data-retry]");
      const cancel = e.target.closest("[data-cancel]");
      if (retry) act(() => api(`${BASE}/turns/${retry.dataset.retry}/retry`, { method: "POST" }));
      if (cancel) act(() => api(`${BASE}/turns/${cancel.dataset.cancel}/cancel`, { method: "POST" }));
    });
    const text = $("#r-text");
    text.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#r-compose").requestSubmit(); });
    $("#r-compose").addEventListener("submit", async (e) => {
      e.preventDefault();
      const value = text.value.trim();
      if (!value) return;
      $("#r-send").disabled = true;
      try {
        await api(`${BASE}/threads/${id}/messages`, { method: "POST", body: { text: value } });
        text.value = "";
        lastKey = "";
        load();
      } catch (err) { toast(err.message); }
      $("#r-send").disabled = false;
    });
    load();
    return { dispose() { disposed = true; clearTimeout(timer); } };
  }

  H.registerRoute(/^#\/relay\/?$/, () => listView());
  H.registerRoute(/^#\/relay\/t\/([\w-]+)$/, (m) => threadView(m[1]));
})();
