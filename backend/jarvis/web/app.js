"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "", snapshot = null, pendingGoal = null;
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function report(message) { $("message").textContent = message; $("message").hidden = false; }
async function api(path, body) {
  const response = await fetch(path, { credentials: "same-origin", headers: body === undefined ? {} : {"Content-Type":"application/json", "X-CSRF-Token": csrf}, method: body === undefined ? "GET" : "POST", body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) { $("desk").hidden = true; $("gate").hidden = false; }
    throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be verified. Refresh and try again.");
  }
  return data;
}
function taskList(target, entries) {
  target.replaceChildren();
  if (!entries.length) { const empty = node("div", undefined, "empty"); empty.append(node("h3", "A clear desk"), node("p", "Your saved tasks will appear here with their current state. Connect the agent before expecting execution.")); target.append(empty); return; }
  entries.forEach(t => {
    const card = node("article", undefined, "task"); card.append(node("h3", t.title, "task-title"));
    const meta = node("div", undefined, "task-meta"); meta.append(node("span", `${t.state.replaceAll("_", " ").toLowerCase()} · revision ${t.revision}`));
    if (t.state !== "CANCELLED") { const button = node("button", "Cancel task", "button"); button.type = "button"; button.onclick = () => run(button, async () => { await api(`/api/tasks/${encodeURIComponent(t.id)}/cancel`, {revision:t.revision}); await refresh(); }); meta.append(button); }
    card.append(meta); if (["WAITING_FOR_MODEL","WAITING_FOR_REVIEW"].includes(t.state)) card.append(node("small", "Saved in Google Sheets. Review is required; no message has been sent.")); target.append(card);
  });
}
async function refresh() {
  snapshot = await api("/api/overview");
  $("send-status").textContent = snapshot.control.paused ? "Replies paused" : "Replies enabled";
  $("rail-mode").textContent = snapshot.mode === "local-development" ? "Local development" : "Private workspace";
  $("task-count").textContent = `${snapshot.tasks.length} tasks`;
  taskList($("task-preview"), snapshot.tasks.slice(0, 5)); taskList($("all-tasks"), snapshot.tasks);
  $("readiness").replaceChildren(); $("connections").replaceChildren();
  snapshot.capabilities.forEach((c, index) => {
    const li = node("li", undefined, index < 2 ? "ready" : ""); li.append(node("strong", c.name), node("span", c.state)); $("readiness").append(li);
    const row = node("div", undefined, "connection"); row.append(node("strong", c.name), node("span", c.state)); $("connections").append(row);
  });
  $("audit").replaceChildren();
  if (!snapshot.audit.length) $("audit").append(node("p", "No activity recorded yet.", "quiet"));
  snapshot.audit.forEach(a => { const row = node("div", undefined, "audit-item"); row.append(node("span", a.kind.replaceAll(".", " ").replaceAll("_", " ")), node("time", new Date(a.time*1000).toLocaleString())); $("audit").append(row); });
  $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}
async function run(button, work) { button.disabled = true; try { await work(); } catch (e) { report(e.message); } finally { button.disabled = false; } }
document.querySelectorAll(".nav-item").forEach(button => button.onclick = () => {
  document.querySelectorAll(".view").forEach(v => v.hidden = v.id !== `view-${button.dataset.view}`);
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("selected", b === button));
  $("crumb").textContent = $("view-title").textContent = button.textContent;
});
$("goal-form").onsubmit = e => { e.preventDefault(); const button = e.submitter; const title = $("goal").value.trim(); if (title.length < 3) return; if (pendingGoal?.title !== title) pendingGoal = {title,request_key:crypto.randomUUID()}; run(button, async () => { await api("/api/tasks", pendingGoal); pendingGoal = null; $("goal").value = ""; await refresh(); report("Task saved in Google Sheets for review."); }); };
$("pause").onclick = () => run($("pause"), async () => { await api("/api/control/pause", {revision:snapshot.control.revision}); await refresh(); report("Replies paused. No new sends are permitted."); });
$("dashboard-link").onclick = () => run($("dashboard-link"), async () => { const link = await api("/api/dashboard-link", {}); report(`${link.url} — Sign-in is still required. ${link.phone_reachable ? "Ready to share after reachability testing." : "This local address cannot open this workstation from your phone."} The link has not been sent to WhatsApp.`); });
$("logout").onclick = () => run($("logout"), async () => { await api("/auth/logout", {}); location.assign("/"); });
$("reauth").onclick = () => location.assign("/auth/login");
document.querySelectorAll(".refresh").forEach(b => b.onclick = () => run(b, refresh));
(async () => {
  try {
    const access = await api("/api/access");
    $("signin").setAttribute("aria-disabled", String(!access.configured));
    if (!access.configured) { $("signin").removeAttribute("href"); $("access-status").textContent = "Access is locked. Configure the enrolled administrator and identity provider on the server before signing in."; return; }
    $("access-status").textContent = access.local_only ? "Local development. This address is available only on this computer." : "Administrator sign-in requires the configured assurance policy.";
    if (access.authenticated) { const session = await api("/api/session"); csrf = session.csrf; $("assurance").textContent = `A fresh sign-in is required for sensitive actions after ${new Date(session.fresh_until*1000).toLocaleTimeString()}.`; await refresh(); $("gate").hidden = true; $("desk").hidden = false; }
  } catch (e) { $("access-status").textContent = "The desk could not verify access. Check the service and try again."; }
})();
