/* Sidus Team: hand it a goal or runbook; a manager splits it into phases and Claude Code and Codex do the tasks.
   Plain JS, no build step. Loaded by Sidus when SIDUS_PLUGINS includes "team". */
(() => {
  "use strict";
  const H = window.sidus;
  const { api, esc, toast, fmt } = H;
  const BASE = "/api/team";
  const $ = (sel, root = document) => root.querySelector(sel);
  const LABEL = { claude: "Claude", codex: "Codex", any: "Either" };

  // Agent output is untrusted: sanitize it, and allow no embeds (they'd load without a click).
  const PURIFY = {
    FORBID_TAGS: ["img", "picture", "source", "video", "audio", "iframe", "object", "embed", "svg", "math", "form",
      "input", "button", "textarea", "select", "style", "link", "meta"],
    FORBID_ATTR: ["style", "srcset"],
  };
  // Agents link to files by path; those open in Sidus's file viewer, which can also read the goal's own folder.
  let fileGoal = null, fileBase = "";
  const md = (text) => H.fileLinks(DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true }), PURIFY),
    { base: fileBase, goal: fileGoal });
  const fileLink = (f) => {
    const path = H.localPath(f, fileBase);
    return path ? `<a class="file-link" href="${esc(H.viewHref(path, fileGoal))}">${esc(f)}</a>` : esc(f);
  };
  const size = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  const artifactsHtml = (g) => !g.artifacts?.length ? "" : `<h2 class="group-label">Files</h2><div class="card t-files">${g.artifacts.map((f) =>
    `<a class="t-file" href="${esc(H.viewHref(f.path, g.id))}" title="${esc(f.path)}"><span class="t-file-name">${esc(f.name)}</span>
      <span class="muted t-small">${size(f.size)} · ${fmt.ago(f.mtime)}</span></a>`).join("")}</div>`;
  const agent = (a) => (a && a !== "any" ? `<span class="t-agent t-${a}">${LABEL[a]}</span>` : `<span class="badge">Either</span>`);
  const tokens = (n) => (n ? `<span class="muted t-tok" title="New (uncached) input plus output tokens, as the agents reported them">${fmt.tokens(n)} tokens</span>` : "");
  const isEditing = () => {
    if ($("#dialog").open) return true;
    const el = document.activeElement;
    return !!el && $("#main").contains(el) && el.matches("input, textarea, select");
  };
  const GOAL_STATUS = {
    planning: ["Planning", "accent"], questions: ["Needs your answers", "warn"], proposed: ["Waiting for approval", "warn"],
    running: ["Working", "accent"], paused: ["Paused", "warn"], done: ["Done", "good"], cancelled: ["Cancelled", ""],
  };
  const PHASE_STATUS = { todo: ["Next", ""], active: ["In progress", "accent"], done: ["Done", "good"], skipped: ["Skipped", ""] };
  const TASK_STATUS = {
    todo: ["To do", ""], working: ["Working", "accent"], review: ["Manager deciding", "accent"], done: ["Done", "good"],
    failed: ["Failed", "bad"], skipped: ["Skipped", ""],
  };
  const badge = (map, s) => { const [l, c] = map[s] || [s, ""]; return `<span class="badge ${c}">${esc(l)}</span>`; };
  const ROLE = { outline: "Outline", phase: "Phase plan", wrapup: "Phase check", review: "Decision", work: "Task", ask: "Question" };

  /* ---------------- overview ---------------- */

  function usageCard(u) {
    const row = (a) => {
      const x = u.agents[a];
      if (!x.linked) return `<div class="t-use"><b>${LABEL[a]}</b><span class="muted">Not signed in</span></div>`;
      const bar = (label, v, limit) => `<span class="t-meter" title="${label}: ${fmt.pct(v)} (switches at ${limit}%)"><span class="bar"><i style="width:${Math.min(100, v || 0)}%" class="${(v || 0) >= limit ? "over" : ""}"></i></span>${label} ${fmt.pct(v)}</span>`;
      const blocked = x.blocked_until ? `<span class="badge warn">Out of usage until ${new Date(x.blocked_until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>` : "";
      return `<div class="t-use"><b>${LABEL[a]}</b>${bar("5h", x.five_hour, u.settings.five_hour)}${bar("Week", x.weekly, u.settings.weekly)}${blocked}</div>`;
    };
    const s = u.settings;
    return `<div class="card t-usage"><h2>Usage</h2>${row("claude")}${row("codex")}
      <details class="t-settings"><summary>Settings</summary><form id="t-settings">
        <label>Switch agents at <input type="number" min="1" max="100" name="five_hour" value="${s.five_hour}">% of 5h or <input type="number" min="1" max="100" name="weekly" value="${s.weekly}">% of the week</label>
        <label>Prefer <select name="default">${["claude", "codex"].map((a) => `<option value="${a}" ${s.default === a ? "selected" : ""}>${LABEL[a]}</option>`).join("")}</select> when both have room</label>
        <label>Claude as manager uses model <input name="manager_claude_model" value="${esc(s.manager_claude_model)}" placeholder="default" size="10"> with at most <input type="number" min="3" max="100" name="manager_max_turns" value="${s.manager_max_turns}"> steps</label>
        <label>Codex as manager uses reasoning effort <select name="manager_codex_effort">${["", "minimal", "low", "medium", "high"].map((e) => `<option value="${e}" ${s.manager_codex_effort === e ? "selected" : ""}>${e || "default"}</option>`).join("")}</select></label>
        <label class="check"><input type="checkbox" name="review_each" ${s.review_each ? "checked" : ""}> Manager reviews every finished task (uses more)</label>
        <button class="btn small">Save</button></form></details></div>`;
  }

  function overview() {
    let disposed = false;
    H.setMain(`${H.topbar("Team")}<div class="scroll"><div class="page team" id="team"></div></div>`);
    async function load() {
      let d;
      try { d = await api(`${BASE}/overview`); } catch (e) { if (!disposed) $("#team").innerHTML = `<h1>Team</h1><p class="error">${esc(e.message)}</p>`; return; }
      if (disposed || isEditing()) return;
      $("#team").innerHTML = `<div class="t-top"><h1>Team</h1><button class="btn primary" id="t-new">New goal</button></div>
        <p class="muted t-intro">Give the team a goal or a runbook. A manager (Claude or Codex, whichever has more room) splits it into phases and asks you anything it needs. You approve the phases once; then Claude Code and Codex work through the tasks one at a time in the folder, unattended, with permissions bypassed.</p>
        ${usageCard(d.usage)}
        <div class="t-plans">${d.goals.map((g) => `<a class="t-row" href="#/team/g/${g.id}">
            <span class="t-row-title">${esc(g.title)}<span class="muted t-small">${g.current_phase ? ` · ${esc(g.current_phase)}` : ""}</span></span>
            ${g.phases_total ? `<span class="muted t-small">${g.phases_done}/${g.phases_total} phases</span>` : ""}
            ${badge(GOAL_STATUS, g.status)}${g.manager ? agent(g.manager) : ""}${tokens(g.tokens)}<span class="muted t-small">${fmt.ago(g.updated)}</span></a>`).join("")
          || `<p class="muted">No goals yet.</p>`}</div>`;
      $("#t-new").onclick = newGoal;
      bindSettings(load);
    }
    load();
    const timer = setInterval(load, 10000);
    return { dispose() { disposed = true; clearInterval(timer); } };
  }

  function bindSettings(reload) {
    const form = $("#t-settings");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      try {
        await api(`${BASE}/settings`, { method: "PUT", body: {
          five_hour: +f.get("five_hour"), weekly: +f.get("weekly"), default: f.get("default"),
          manager_claude_model: f.get("manager_claude_model"), manager_max_turns: +f.get("manager_max_turns"),
          manager_codex_effort: f.get("manager_codex_effort"), review_each: f.get("review_each") === "on" } });
        toast("Saved");
        reload();
      } catch (err) { toast(err.message); }
    });
  }

  function newGoal() {
    const dlg = $("#dialog");
    let cwd = "";
    dlg.innerHTML = `<form method="dialog" id="t-start" class="t-form"><h2>New goal</h2>
      <label>Name <input name="title" required maxlength="120" placeholder="What you're after"></label>
      <label>Folder</label>
      <div class="picker"><div class="picker-path"><button type="button" class="btn small ghost" id="tp-up" aria-label="Up one folder">↑</button><span id="tp-path"></span></div>
        <div class="picker-list" id="tp-list"></div></div>
      <label>Goal or runbook <textarea name="goal" rows="8" placeholder="Describe what you want done, or paste a runbook. Long is fine."></textarea></label>
      <label>Upload a runbook <input type="file" name="upload" id="t-upload"></label>
      <label>Or a runbook file already in the folder <input name="runbook" placeholder="e.g. docs/runbook.md (optional)"></label>
      <p class="error" id="t-err"></p>
      <div class="dialog-actions"><button type="button" class="btn" id="t-cancel">Cancel</button><button class="btn primary" id="t-go">Start planning</button></div></form>`;
    dlg.showModal();
    async function browse(path) {
      try {
        const d = await api("/api/dirs?path=" + encodeURIComponent(path || ""));
        cwd = d.path;
        $("#tp-path").textContent = d.path ? "‎" + d.path : "Workspaces";
        $("#tp-up").disabled = !d.parent && !d.path;
        $("#tp-up").onclick = () => browse(d.parent || "");
        $("#tp-list").innerHTML = d.dirs.map((x) => `<button type="button" data-path="${esc(x.path)}">${esc(x.name)}</button>`).join("") || `<div class="empty">No subfolders</div>`;
      } catch (e) { $("#t-err").textContent = e.message; }
    }
    $("#tp-list").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) browse(b.dataset.path); });
    $("#t-cancel").onclick = () => dlg.close();
    $("#t-start").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      if (!cwd) { $("#t-err").textContent = "Pick a folder."; return; }
      const file = $("#t-upload").files[0];
      if (!f.get("goal").trim() && !f.get("runbook").trim() && !file) { $("#t-err").textContent = "Describe the goal or give a runbook."; return; }
      if (file && f.get("runbook").trim()) { $("#t-err").textContent = "Give one runbook: an upload or a file in the folder."; return; }
      $("#t-go").disabled = true;
      try {
        let upload = "";
        if (file) {
          $("#t-go").textContent = "Uploading…";
          const res = await fetch(`${BASE}/uploads?name=${encodeURIComponent(file.name)}`, {
            method: "POST", body: file, credentials: "same-origin", headers: { "X-Sidus": "1" } });
          const data = await res.json().catch(() => null);
          if (!res.ok) throw new Error(data?.detail || `Upload failed (HTTP ${res.status})`);
          upload = data.upload;
        }
        const g = await api(`${BASE}/goals`, { method: "POST", body: { title: f.get("title"), cwd, goal: f.get("goal"), runbook: f.get("runbook"), runbook_upload: upload } });
        dlg.close();
        location.hash = `#/team/g/${g.id}`;
      } catch (err) { $("#t-err").textContent = err.message; $("#t-go").disabled = false; $("#t-go").textContent = "Start planning"; }
    });
    browse("");
  }

  /* ---------------- goal ---------------- */

  function runHtml(r) {
    const status = r.status === "running" ? `<span class="badge accent">Running</span>` : ["done", "retried"].includes(r.status) ? "" : `<span class="badge ${r.status === "failed" ? "bad" : "warn"}">${esc(r.status === "limit" ? "Hit usage limit" : r.status)}</span>`;
    return `<details class="t-run" ${r.status === "running" ? "open" : ""}><summary>${agent(r.agent)} <span>${ROLE[r.role] || r.role}</span>${status}${r.reason ? `<span class="muted t-small">${esc(r.reason)}</span>` : ""}${tokens(r.tokens)}</summary>
      ${r.steps.length ? `<ul class="t-steps">${r.steps.slice(-40).map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
      ${r.result ? `<div class="t-md">${md(r.result)}</div>` : ""}${r.error ? `<p class="error">${esc(r.error)}</p>` : ""}
      ${r.session_id ? `<a class="t-small" href="#/s/${esc(r.session_id)}">Open session</a>` : ""}</details>`;
  }

  const who = (c) => c.author === "you" ? "You" : c.author === "manager" ? `Manager${c.agent ? ` (${LABEL[c.agent]})` : ""}` : c.author === "sidus" ? "Sidus" : LABEL[c.author] || c.author;
  const commentHtml = (c) => `<div class="t-comment t-by-${esc(c.author)}"><div class="t-small muted">${esc(who(c))} · ${fmt.ago(c.created)}</div><div class="t-md">${md(c.text)}</div></div>`;

  function taskEditor(t, i) {
    return `<div class="t-task edit" data-task="${t.id}">
      <div class="t-task-head"><span class="t-num">${i}</span><input class="t-title" data-f="title" value="${esc(t.title)}" maxlength="120" aria-label="Task name">
        <select data-f="assignee" aria-label="Who does it">${["any", "claude", "codex"].map((a) => `<option value="${a}" ${t.assignee === a ? "selected" : ""}>${LABEL[a]}</option>`).join("")}</select>
        <button type="button" class="btn small ghost" data-tmove="${i - 1}" ${i === 1 ? "disabled" : ""} aria-label="Move up">↑</button>
        <button type="button" class="btn small ghost danger" data-tdel="${t.id}" aria-label="Delete task">✕</button></div>
      <textarea data-f="details" rows="2" placeholder="Details" aria-label="Details">${esc(t.details)}</textarea>
      <label class="t-done-when">Done when<input data-f="done_when" value="${esc(t.done_when)}" maxlength="500"></label></div>`;
  }

  function phaseEditor(p, i) {
    return `<div class="card t-phase edit" data-phase="${p.id}">
      <div class="t-task-head"><span class="t-num">${i}</span><input class="t-title" data-pf="title" value="${esc(p.title)}" maxlength="120" aria-label="Phase name">
        <button type="button" class="btn small ghost" data-pmove="${i - 1}" ${i === 1 ? "disabled" : ""} aria-label="Move up">↑</button>
        <button type="button" class="btn small ghost danger" data-pdel="${p.id}" aria-label="Delete phase">✕</button></div>
      <textarea data-pf="details" rows="2" placeholder="What this phase covers" aria-label="What this phase covers">${esc(p.details)}</textarea>
      <label class="t-done-when">Done when<input data-pf="done_when" value="${esc(p.done_when)}" maxlength="500"></label>
      ${p.tasks.length ? `<div class="t-sub">Tasks</div>${p.tasks.map((t, k) => taskEditor(t, k + 1)).join("")}` : `<p class="muted t-small">Tasks are planned when this phase starts.</p>`}
      ${i === 1 ? `<button type="button" class="btn small" data-tadd="${p.id}">Add a task</button>` : ""}</div>`;
  }

  function taskCard(t, i, g) {
    const comments = g.comments.filter((c) => c.task_id === t.id);
    const runs = g.runs.filter((r) => r.task_id === t.id);
    const open = !["done", "cancelled"].includes(g.status);
    const actions = open ? [
      ["failed", "skipped"].includes(t.status) ? `<button class="btn small" data-retry="${t.id}">Retry</button>` : "",
      ["todo", "failed"].includes(t.status) ? `<button class="btn small ghost" data-skip="${t.id}">Skip</button>` : "",
      `<button class="btn small ghost" data-note="${t.id}">Add a note</button>`,
    ].join("") : "";
    return `<details class="t-task ${t.status}" ${["working", "review", "failed"].includes(t.status) ? "open" : ""}>
      <summary><span class="t-num">${i}</span><span class="t-task-title">${esc(t.title)}</span>${badge(TASK_STATUS, t.status)}${agent(t.worker || t.assignee)}</summary>
      <div class="t-task-body">${t.details ? `<div class="t-md">${md(t.details)}</div>` : ""}${t.done_when ? `<p class="t-small"><b>Done when:</b> ${esc(t.done_when)}</p>` : ""}
        ${t.files.length ? `<p class="t-small muted">Files: ${t.files.map(fileLink).join(", ")}</p>` : ""}
        ${runs.map(runHtml).join("")}${comments.map(commentHtml).join("")}
        <div class="t-row-btns">${actions}</div></div></details>`;
  }

  function phaseCard(p, g) {
    const runs = g.runs.filter((r) => r.phase_id === p.id && !r.task_id);
    const canSkip = !["done", "skipped"].includes(p.status) && !["done", "cancelled"].includes(g.status) && !g.running;
    return `<details class="card t-phase ${p.status}" ${p.status === "active" ? "open" : ""}>
      <summary><span class="t-num">${p.position}</span><span class="t-task-title">${esc(p.title)}</span>${badge(PHASE_STATUS, p.status)}
        ${p.tasks.length ? `<span class="muted t-small">${p.tasks.filter((t) => ["done", "skipped"].includes(t.status)).length}/${p.tasks.length} tasks</span>` : ""}</summary>
      ${p.details ? `<div class="t-md t-small">${md(p.details)}</div>` : ""}${p.done_when ? `<p class="t-small"><b>Done when:</b> ${esc(p.done_when)}</p>` : ""}
      ${p.summary ? `<div class="t-comment"><div class="t-md">${md(p.summary)}</div></div>` : ""}
      ${p.tasks.map((t, i) => taskCard(t, i + 1, g)).join("")}${runs.map(runHtml).join("")}
      ${canSkip ? `<div class="t-row-btns">${p.status !== "done" ? `<button class="btn small" data-tadd="${p.id}">Add a task</button>` : ""}<button class="btn small ghost" data-pskip="${p.id}">Skip phase</button></div>` : ""}</details>`;
  }

  function goalView(id) {
    let disposed = false, timer = null, lastKey = "";
    H.setMain(`${H.topbar("Team")}<div class="scroll"><div class="page team" id="team"><p class="muted">Loading…</p></div></div>`);

    async function load(force = false) {
      let g;
      try { g = await api(`${BASE}/goals/${id}`); } catch (e) {
        if (!disposed) $("#team").innerHTML = `<p class="error">${esc(e.message)}. <a href="#/team">Back to Team</a></p>`;
        return;
      }
      if (disposed) return;
      schedule(g.running || g.status === "planning" ? 3000 : 12000);
      const key = JSON.stringify([g.updated, g.runs.map((r) => [r.status, r.steps.length, r.tokens])]);
      if (!force && (key === lastKey || isEditing())) return;
      lastKey = key;
      render(g);
    }

    function render(g) {
      fileGoal = g.id;
      fileBase = g.cwd;
      const actions = {
        planning: `<button class="btn" data-a="stop">Stop</button>`,
        questions: "", proposed: `<button class="btn ghost" data-a="replan">Re-plan</button><button class="btn primary" data-a="approve">Approve and start</button>`,
        running: `<button class="btn" data-a="pause">Pause</button>${g.running ? `<button class="btn" data-a="stop">Stop now</button>` : ""}`,
        paused: `<button class="btn primary" data-a="resume">Resume</button>`,
        done: "", cancelled: "",
      }[g.status];
      const end = !["done", "cancelled"].includes(g.status) ? `<button class="btn ghost danger" data-a="cancel">Cancel</button>` : (g.running ? "" : `<button class="btn ghost danger" data-a="delete">Delete</button>`);
      const notes = g.comments.filter((c) => !c.task_id && c.text !== "Phases approved.");
      const outline = g.runs.filter((r) => r.role === "outline");
      $("#team").innerHTML = `<div class="t-head"><div>
          <a class="t-back" href="#/team">← Team</a>
          <h1>${esc(g.title)}</h1>
          <div class="muted t-small">${esc(g.cwd)}${g.runbook ? ` · <a href="${esc(H.viewHref(H.localPath(g.runbook, g.cwd) || g.runbook, g.id))}">Runbook: ${esc(g.runbook.split("/").pop())}</a>` : ""}</div>
          <div class="t-meta">${badge(GOAL_STATUS, g.status)}${g.manager ? `<span class="t-small">Manager</span>${agent(g.manager)}` : ""}${g.phases_total ? `<span class="muted t-small">${g.phases_done}/${g.phases_total} phases</span>` : ""}${tokens(g.tokens)}</div></div>
        <div class="t-head-actions">${actions}${end}</div></div>
        ${g.waiting ? `<p class="badge warn t-waiting">${esc(g.waiting)}</p>` : ""}
        <details class="t-goal"><summary>The goal</summary><div class="t-md">${md(g.goal)}</div></details>
        ${g.status === "questions" ? `<div class="card t-questions"><h2>The manager asks</h2><div class="t-md">${md(g.questions)}</div>
          <form id="t-answer"><textarea name="text" rows="4" required placeholder="Your answers"></textarea><button class="btn primary">Send answers</button></form></div>` : ""}
        ${g.summary && ["proposed", "done"].includes(g.status) ? `<div class="card"><div class="t-md">${md(g.summary)}</div></div>` : ""}
        ${artifactsHtml(g)}
        <h2 class="group-label">Phases</h2>
        ${g.status === "proposed" ? `<div id="t-edit">${g.phases.map((p, i) => phaseEditor(p, i + 1)).join("")}<button class="btn small" data-a="addphase">Add a phase</button></div>`
          : (g.phases.map((p) => phaseCard(p, g)).join("") || `<p class="muted">${g.status === "planning" ? "The manager is outlining…" : "No phases."}</p>`)}
        ${outline.length ? `<h2 class="group-label">Outline</h2>${outline.map(runHtml).join("")}` : ""}
        <h2 class="group-label">Notes</h2>${notes.map(commentHtml).join("") || `<p class="muted t-small">Nothing yet.</p>`}
        <form id="t-note" class="t-note"><textarea name="text" rows="2" required placeholder="Add a note for the team"></textarea><button class="btn">Add</button></form>`;
      $("#t-answer")?.addEventListener("submit", (e) => { e.preventDefault(); act(() => post(`/goals/${id}/answer`, { text: new FormData(e.target).get("text") })); });
      $("#t-note").addEventListener("submit", (e) => { e.preventDefault(); act(() => post(`/goals/${id}/comments`, { text: new FormData(e.target).get("text") })); });
      $("#t-edit")?.addEventListener("change", (e) => {
        const tf = e.target.dataset.f, pf = e.target.dataset.pf;
        if (tf) act(() => api(`${BASE}/tasks/${e.target.closest("[data-task]").dataset.task}`, { method: "PATCH", body: { [tf]: e.target.value } }), false);
        if (pf) act(() => api(`${BASE}/phases/${e.target.closest("[data-phase]").dataset.phase}`, { method: "PATCH", body: { [pf]: e.target.value } }), false);
      });
    }

    async function act(fn, reload = true) {
      try { await fn(); if (reload) { document.activeElement?.blur?.(); load(true); } } catch (e) { toast(e.message); }
    }
    function schedule(ms) { clearTimeout(timer); if (!disposed) timer = setTimeout(load, ms); }
    const post = (path, body) => api(`${BASE}${path}`, { method: "POST", body });
    const patch = (path, body) => api(`${BASE}${path}`, { method: "PATCH", body });

    $("#team").addEventListener("click", async (e) => {
      const t = e.target.closest("[data-a],[data-retry],[data-skip],[data-tdel],[data-tmove],[data-note],[data-tadd],[data-pdel],[data-pmove],[data-pskip]");
      if (!t) return;
      const a = t.dataset.a, d = t.dataset;
      if (a === "approve") act(() => post(`/goals/${id}/approve`));
      else if (a === "pause") act(() => post(`/goals/${id}/pause`));
      else if (a === "resume") act(() => post(`/goals/${id}/resume`));
      else if (a === "stop") act(() => post(`/goals/${id}/stop`).then(() => post(`/goals/${id}/pause`)));
      else if (a === "cancel") { if (confirm("Cancel this goal? Running work is stopped; changes already made stay.")) act(() => post(`/goals/${id}/cancel`)); }
      else if (a === "delete") { if (confirm("Delete this goal and its history? The agents' sessions stay in the sidebar.")) { try { await api(`${BASE}/goals/${id}`, { method: "DELETE" }); location.hash = "#/team"; } catch (err) { toast(err.message); } } }
      else if (a === "replan") { const note = prompt("What should the manager change? (optional)"); if (note !== null) act(() => post(`/goals/${id}/replan`, { note })); }
      else if (a === "addphase") act(() => post(`/goals/${id}/phases`, { title: "New phase" }));
      else if (d.tadd) act(() => post(`/phases/${d.tadd}/tasks`, { title: "New task" }));
      else if (d.retry) act(() => patch(`/tasks/${d.retry}`, { status: "todo" }));
      else if (d.skip) act(() => patch(`/tasks/${d.skip}`, { status: "skipped" }));
      else if (d.tdel) act(() => api(`${BASE}/tasks/${d.tdel}`, { method: "DELETE" }));
      else if (d.tmove) act(() => patch(`/tasks/${t.closest("[data-task]").dataset.task}`, { position: +d.tmove }));
      else if (d.pdel) { if (confirm("Delete this phase?")) act(() => api(`${BASE}/phases/${d.pdel}`, { method: "DELETE" })); }
      else if (d.pmove) act(() => patch(`/phases/${t.closest("[data-phase]").dataset.phase}`, { position: +d.pmove }));
      else if (d.pskip) { if (confirm("Skip this phase? Its unfinished tasks won't run.")) act(() => patch(`/phases/${d.pskip}`, { status: "skipped" })); }
      else if (d.note) { const text = prompt("Note for this task (the next worker reads it):"); if (text) act(() => post(`/goals/${id}/comments`, { text, task_id: d.note })); }
    });
    load(true);
    return { dispose() { disposed = true; clearTimeout(timer); } };
  }

  H.registerRoute(/^#\/team\/?$/, () => overview());
  H.registerRoute(/^#\/team\/g\/([\w-]+)$/, (m) => goalView(m[1]));
})();
