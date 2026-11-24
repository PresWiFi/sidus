/* Sidus Team: a manager plans an Ideas milestone, Claude Code and Codex do the tasks. Plain JS, no build step.
   Loaded by Sidus when SIDUS_PLUGINS includes "team". */
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
  const md = (text) => DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true }), PURIFY);
  const agent = (a) => (a && a !== "any" ? `<span class="t-agent t-${a}">${LABEL[a]}</span>` : `<span class="badge">Either</span>`);
  const tokens = (n) => (n ? `<span class="muted t-tok" title="New (uncached) input plus output tokens, as the agents reported them">${fmt.tokens(n)} tokens</span>` : "");
  const isEditing = () => {
    if ($("#dialog").open) return true;
    const el = document.activeElement;
    return !!el && $("#main").contains(el) && el.matches("input, textarea, select");
  };
  const PLAN_STATUS = {
    planning: ["Planning", "accent"], questions: ["Needs your answers", "warn"], proposed: ["Waiting for approval", "warn"],
    running: ["Working", "accent"], paused: ["Paused", "warn"], done: ["Done", "good"], cancelled: ["Cancelled", ""],
  };
  const TASK_STATUS = {
    todo: ["To do", ""], working: ["Working", "accent"], review: ["Manager checking", "accent"], done: ["Done", "good"],
    failed: ["Failed", "bad"], skipped: ["Skipped", ""],
  };
  const badge = (map, s) => { const [l, c] = map[s] || [s, ""]; return `<span class="badge ${c}">${esc(l)}</span>`; };
  const ROLE = { plan: "Planning", work: "Task", review: "Review", wrapup: "Wrap-up", ask: "Question" };

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
      const milestone = (p, m) => `<div class="t-ms"><div><b>${esc(m.title)}</b>${m.done_criteria ? `<div class="muted t-small">Done when: ${esc(m.done_criteria)}</div>` : ""}</div>
        <div class="t-ms-act">${m.plan ? `${badge(PLAN_STATUS, m.plan.status)} <a class="btn small" href="#/team/p/${m.plan.id}">Open plan</a>` : `<button class="btn small primary" data-plan="${p.id}:${m.id}" data-title="${esc(m.title)}">Plan</button>`}</div></div>`;
      $("#team").innerHTML = `<h1>Team</h1>
        <p class="muted t-intro">A manager (Claude or Codex, whichever has more room) plans a milestone into tasks and asks you anything it needs. You approve the tasks; then Claude Code and Codex do them one at a time in the project folder, unattended, with permissions bypassed.</p>
        ${usageCard(d.usage)}
        <h2 class="group-label">Projects</h2>
        ${d.projects.map((p) => `<div class="card t-project"><div class="t-project-head"><a href="#/ideas/p/${esc(p.slug)}"><b>${esc(p.title)}</b></a><span class="muted t-small">${esc(p.repo_path)}</span></div>
          ${p.milestones.map((m) => milestone(p, m)).join("") || `<p class="muted">No open milestones. Add one in Ideas.</p>`}</div>`).join("")
          || `<p class="muted">No projects with a folder yet. In <a href="#/ideas">Ideas</a>, give a project a folder and a milestone.</p>`}
        ${d.plans.length ? `<h2 class="group-label">Plans</h2><div class="t-plans">${d.plans.map((p) => `<a class="t-row" href="#/team/p/${p.id}"><span>${esc(p.project_title || "Project")}${p.milestone_title ? ` · ${esc(p.milestone_title)}` : ""}</span>${badge(PLAN_STATUS, p.status)}${p.manager ? agent(p.manager) : ""}${tokens(p.tokens)}<span class="muted">${fmt.ago(p.updated)}</span></a>`).join("")}</div>` : ""}`;
      bindSettings(load);
    }
    $("#team").addEventListener("click", (e) => {
      const b = e.target.closest("[data-plan]");
      if (b) startPlan(...b.dataset.plan.split(":").map(Number), b.dataset.title);
    });
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

  function startPlan(projectId, milestoneId, title) {
    const dlg = $("#dialog");
    dlg.innerHTML = `<form method="dialog" id="t-start" class="t-form"><h2>Plan “${esc(title)}”</h2>
      <label>Anything the manager should know? <textarea name="request" rows="4" placeholder="Optional: goals, constraints, what matters most"></textarea></label>
      <p class="error" id="t-err"></p>
      <div class="dialog-actions"><button type="button" class="btn" id="t-cancel">Cancel</button><button class="btn primary" id="t-go">Start planning</button></div></form>`;
    dlg.showModal();
    $("#t-cancel").onclick = () => dlg.close();
    $("#t-start").addEventListener("submit", async (e) => {
      e.preventDefault();
      $("#t-go").disabled = true;
      try {
        const p = await api(`${BASE}/plans`, { method: "POST", body: { project_id: projectId, milestone_id: milestoneId, request: new FormData(e.target).get("request") || "" } });
        dlg.close();
        location.hash = `#/team/p/${p.id}`;
      } catch (err) { $("#t-err").textContent = err.message; $("#t-go").disabled = false; }
    });
  }

  /* ---------------- plan ---------------- */

  function runHtml(r) {
    const status = r.status === "running" ? `<span class="badge accent">Running</span>` : r.status === "done" ? "" : `<span class="badge ${r.status === "failed" ? "bad" : "warn"}">${esc(r.status === "limit" ? "Hit usage limit" : r.status)}</span>`;
    return `<details class="t-run" ${r.status === "running" ? "open" : ""}><summary>${agent(r.agent)} <span>${ROLE[r.role]}</span>${status}${r.reason ? `<span class="muted t-small">${esc(r.reason)}</span>` : ""}${tokens(r.tokens)}</summary>
      ${r.steps.length ? `<ul class="t-steps">${r.steps.slice(-40).map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
      ${r.result ? `<div class="t-md">${md(r.result)}</div>` : ""}${r.error ? `<p class="error">${esc(r.error)}</p>` : ""}
      ${r.session_id ? `<a class="t-small" href="#/s/${esc(r.session_id)}">Open session</a>` : ""}</details>`;
  }

  const who = (c) => c.author === "you" ? "You" : c.author === "manager" ? `Manager${c.agent ? ` (${LABEL[c.agent]})` : ""}` : c.author === "sidus" ? "Sidus" : LABEL[c.author] || c.author;
  const commentHtml = (c) => `<div class="t-comment t-by-${esc(c.author)}"><div class="t-small muted">${esc(who(c))} · ${fmt.ago(c.created)}</div><div class="t-md">${md(c.text)}</div></div>`;

  function taskEditor(t, i) {
    return `<div class="t-task edit" data-task="${t.id}">
      <div class="t-task-head"><span class="t-num">${i}</span><input class="t-title" data-f="title" value="${esc(t.title)}" maxlength="120">
        <select data-f="assignee">${["any", "claude", "codex"].map((a) => `<option value="${a}" ${t.assignee === a ? "selected" : ""}>${LABEL[a]}</option>`).join("")}</select>
        <button type="button" class="btn small ghost" data-move="${i - 1}" ${i === 1 ? "disabled" : ""} aria-label="Move up">↑</button>
        <button type="button" class="btn small ghost danger" data-del="${t.id}" aria-label="Delete task">✕</button></div>
      <textarea data-f="details" rows="3" placeholder="Details">${esc(t.details)}</textarea>
      <label class="t-done-when">Done when<input data-f="done_when" value="${esc(t.done_when)}" placeholder="Something the worker can check" maxlength="500"></label></div>`;
  }

  function taskCard(t, i, plan) {
    const comments = plan.comments.filter((c) => c.task_id === t.id);
    const runs = plan.runs.filter((r) => r.task_id === t.id);
    const actions = plan.status !== "done" && plan.status !== "cancelled" ? [
      ["failed", "skipped"].includes(t.status) ? `<button class="btn small" data-retry="${t.id}">Retry</button>` : "",
      ["todo", "failed"].includes(t.status) ? `<button class="btn small ghost" data-skip="${t.id}">Skip</button>` : "",
    ].join("") : "";
    return `<details class="t-task ${t.status}" ${["working", "review", "failed"].includes(t.status) ? "open" : ""}>
      <summary><span class="t-num">${i}</span><span class="t-task-title">${esc(t.title)}</span>${badge(TASK_STATUS, t.status)}${agent(t.worker || t.assignee)}</summary>
      <div class="t-task-body">${t.details ? `<div class="t-md">${md(t.details)}</div>` : ""}${t.done_when ? `<p class="t-small"><b>Done when:</b> ${esc(t.done_when)}</p>` : ""}
        ${t.files.length ? `<p class="t-small muted">Files: ${t.files.map(esc).join(", ")}</p>` : ""}
        ${runs.map(runHtml).join("")}${comments.map(commentHtml).join("")}
        <div class="t-row-btns">${actions}<button class="btn small ghost" data-note="${t.id}">Add a note</button></div></div></details>`;
  }

  function planView(id) {
    let disposed = false, timer = null, lastKey = "";
    H.setMain(`${H.topbar("Team")}<div class="scroll" id="t-scroll"><div class="page team" id="team"><p class="muted">Loading…</p></div></div>`);

    async function load(force = false) {
      let p;
      try { p = await api(`${BASE}/plans/${id}`); } catch (e) {
        if (!disposed) $("#team").innerHTML = `<p class="error">${esc(e.message)}. <a href="#/team">Back to Team</a></p>`;
        return;
      }
      if (disposed) return;
      schedule(p.running || p.status === "planning" ? 3000 : 12000);
      const key = JSON.stringify([p.updated, p.runs.map((r) => [r.status, r.steps.length, r.tokens])]);
      if (!force && (key === lastKey || isEditing())) return;
      lastKey = key;
      render(p);
    }

    function render(p) {
      const m = p.milestone || {};
      const actions = {
        planning: `<button class="btn" data-a="stop">Stop</button>`,
        questions: "", proposed: `<button class="btn ghost" data-a="replan">Re-plan</button><button class="btn primary" data-a="approve">Approve and start</button>`,
        running: `<button class="btn" data-a="pause">Pause</button>${p.running ? `<button class="btn" data-a="stop">Stop now</button>` : ""}`,
        paused: `<button class="btn primary" data-a="resume">Resume</button>${p.tasks.length ? "" : `<button class="btn ghost" data-a="replan">Re-plan</button>`}`,
        done: "", cancelled: "",
      }[p.status];
      const end = !["done", "cancelled"].includes(p.status) ? `<button class="btn ghost danger" data-a="cancel">Cancel plan</button>` : (p.running ? "" : `<button class="btn ghost danger" data-a="delete">Delete</button>`);
      const planNotes = p.comments.filter((c) => !c.task_id);
      $("#team").innerHTML = `<div class="t-head"><div>
          <a class="t-back" href="#/team">← Team</a>
          <h1>${esc(m.title || "Milestone")}</h1>
          <div class="muted t-small">${p.project ? `<a href="#/ideas/p/${esc(p.project.slug)}">${esc(p.project.title)}</a> · ` : ""}${esc(p.cwd)}</div>
          <div class="t-meta">${badge(PLAN_STATUS, p.status)}${p.manager ? `<span class="t-small">Manager</span>${agent(p.manager)}` : ""}${tokens(p.tokens)}</div></div>
        <div class="t-head-actions">${actions}${end}</div></div>
        ${p.waiting ? `<p class="badge warn t-waiting">${esc(p.waiting)}</p>` : ""}
        ${p.status === "questions" ? `<div class="card t-questions"><h2>The manager asks</h2><div class="t-md">${md(p.questions)}</div>
          <form id="t-answer"><textarea name="text" rows="4" required placeholder="Your answers"></textarea><button class="btn primary">Send answers</button></form></div>` : ""}
        ${p.summary && ["proposed", "done"].includes(p.status) ? `<div class="card"><div class="t-md">${md(p.summary)}</div></div>` : ""}
        <h2 class="group-label">Tasks</h2>
        ${p.status === "proposed" ? `<div id="t-edit">${p.tasks.map((t, i) => taskEditor(t, i + 1)).join("")}<button class="btn small" data-a="add">Add a task</button></div>`
          : (p.tasks.map((t, i) => taskCard(t, i + 1, p)).join("") || `<p class="muted">${p.status === "planning" ? "The manager is planning…" : "No tasks."}</p>`)}
        ${p.runs.some((r) => !r.task_id) ? `<h2 class="group-label">Manager</h2>${p.runs.filter((r) => !r.task_id).map(runHtml).join("")}` : ""}
        <h2 class="group-label">Notes</h2>${planNotes.map(commentHtml).join("") || `<p class="muted t-small">Nothing yet.</p>`}
        <form id="t-note" class="t-note"><textarea name="text" rows="2" required placeholder="Add a note for the team"></textarea><button class="btn">Add</button></form>`;
      $("#t-answer")?.addEventListener("submit", (e) => { e.preventDefault(); act(() => api(`${BASE}/plans/${id}/answer`, { method: "POST", body: { text: new FormData(e.target).get("text") } })); });
      $("#t-note").addEventListener("submit", (e) => { e.preventDefault(); act(() => api(`${BASE}/plans/${id}/comments`, { method: "POST", body: { text: new FormData(e.target).get("text") } })); });
      $("#t-edit")?.addEventListener("change", (e) => {
        const box = e.target.closest("[data-task]"), f = e.target.dataset.f;
        if (box && f) act(() => api(`${BASE}/tasks/${box.dataset.task}`, { method: "PATCH", body: { [f]: e.target.value } }), false);
      });
    }

    async function act(fn, reload = true) {
      try { await fn(); if (reload) { document.activeElement?.blur?.(); load(true); } } catch (e) { toast(e.message); }
    }
    function schedule(ms) { clearTimeout(timer); if (!disposed) timer = setTimeout(load, ms); }
    const post = (path, body) => api(`${BASE}${path}`, { method: "POST", body });

    $("#team").addEventListener("click", async (e) => {
      const t = e.target.closest("[data-a],[data-retry],[data-skip],[data-del],[data-move],[data-note]");
      if (!t) return;
      const a = t.dataset.a;
      if (a === "approve") act(() => post(`/plans/${id}/approve`));
      else if (a === "pause") act(() => post(`/plans/${id}/pause`));
      else if (a === "resume") act(() => post(`/plans/${id}/resume`));
      else if (a === "stop") act(() => post(`/plans/${id}/stop`).then(() => post(`/plans/${id}/pause`)));
      else if (a === "cancel") { if (confirm("Cancel this plan? Running work is stopped; changes already made stay.")) act(() => post(`/plans/${id}/cancel`)); }
      else if (a === "delete") { if (confirm("Delete this plan and its history? The agents' sessions stay in the sidebar.")) { try { await api(`${BASE}/plans/${id}`, { method: "DELETE" }); location.hash = "#/team"; } catch (err) { toast(err.message); } } }
      else if (a === "replan") { const note = prompt("What should the manager change? (optional)"); if (note !== null) act(() => post(`/plans/${id}/replan`, { note })); }
      else if (a === "add") act(() => post(`/plans/${id}/tasks`, { title: "New task" }));
      else if (t.dataset.retry) act(() => api(`${BASE}/tasks/${t.dataset.retry}`, { method: "PATCH", body: { status: "todo" } }));
      else if (t.dataset.skip) act(() => api(`${BASE}/tasks/${t.dataset.skip}`, { method: "PATCH", body: { status: "skipped" } }));
      else if (t.dataset.del) act(() => api(`${BASE}/tasks/${t.dataset.del}`, { method: "DELETE" }));
      else if (t.dataset.move) { const box = t.closest("[data-task]"); act(() => api(`${BASE}/tasks/${box.dataset.task}`, { method: "PATCH", body: { position: +t.dataset.move } })); }
      else if (t.dataset.note) { const text = prompt("Note for this task (the next worker reads it):"); if (text) act(() => post(`/plans/${id}/comments`, { text, task_id: t.dataset.note })); }
    });
    load(true);
    return { dispose() { disposed = true; clearTimeout(timer); } };
  }

  H.registerRoute(/^#\/team\/?$/, () => overview());
  H.registerRoute(/^#\/team\/p\/([\w-]+)$/, (m) => planView(m[1]));
})();
