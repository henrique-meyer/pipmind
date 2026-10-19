"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let token = "", state, preview;
  const message = value => { $("message").textContent = value; };
  async function api(path, body, method = "POST") {
    const headers = {Authorization: `Bearer ${token}`};
    let data;
    if (body instanceof ArrayBuffer) data = body;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; data = JSON.stringify(body); }
    const response = await fetch(path, {method, headers, body: data, cache: "no-store", credentials: "omit", redirect: "error"});
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || error.detail || "Operation refused");
    }
    return response;
  }
  async function action(task, success) {
    try { await task(); if (success) message(success); }
    catch (error) { message(`Could not complete: ${error.message}. Refresh a stale preview before confirming.`); }
  }
  function closePreview() { preview = null; $("preview").hidden = true; $("diff").textContent = ""; $("confirm-resume").hidden = true; }
  async function refresh() {
    state = await (await api("/robots", undefined, "GET")).json();
    const active = state.active;
    $("workspace").hidden = false; $("login").hidden = true;
    $("active-status").textContent = `Version ${active.version} · ${active.paused ? "Paused" : "Running shadow"}`;
    $("active-hash").textContent = active.sha256;
    $("auto-learn").checked = active.auto_learn;
    $("mind").textContent = Object.entries(active.content).map(([key, value]) => `${key.toUpperCase()}\n${value}`).join("\n\n");
    $("days").value = active.retention_days; $("count").value = active.retention_count;
    renderSaves();
  }
  function button(label, callback) {
    const result = document.createElement("button"); result.textContent = label;
    result.type = "button"; result.addEventListener("click", () => action(callback)); return result;
  }
  function renderSaves() {
    $("saves").replaceChildren();
    const saves = state.saves.filter(save => !$("favorites-only").checked || save.favorite);
    if (!saves.length) { const p = document.createElement("p"); p.textContent = "No saved robots here yet."; $("saves").append(p); }
    for (const save of saves) {
      const card = document.createElement("article"), title = document.createElement("h3"), detail = document.createElement("p"), note = document.createElement("p"), controls = document.createElement("div");
      title.textContent = save.name || "Untitled robot";
      detail.textContent = `${save.kind} · ${save.created} · ${save.sha256.slice(0, 12)}`; note.textContent = save.note;
      controls.className = "row";
      controls.append(
        button(save.favorite ? "Unfavorite" : "Favorite", async () => { await api(`/robots/${save.id}/favorite`, {favorite: !save.favorite}); await refresh(); }),
        button("Save a copy", async () => { await api("/robots/save", {source: save.id, name: `${save.name || "Robot"} copy`.slice(0, 120)}); await refresh(); message("Independent copy saved."); }),
        button("Export", async () => {
          const blob = await (await api(`/robots/${save.id}/export`, undefined, "GET")).blob();
          const url = URL.createObjectURL(blob), link = document.createElement("a");
          link.href = url; link.download = "robot.pipmind"; link.click(); URL.revokeObjectURL(url);
        }),
        button("Preview Load", async () => {
          preview = await (await api(`/robots/${save.id}/preview`)).json();
          $("preview-target").textContent = preview.target_sha256;
          $("effects").replaceChildren(...preview.effects.map(effect => { const li = document.createElement("li"); li.textContent = effect; return li; }));
          $("diff").textContent = preview.diff || "No content differences. Loading still creates a new active version.";
          $("preview").hidden = false; $("preview").scrollIntoView({behavior: "smooth"});
        })
      );
      card.append(title, detail, note, controls); $("saves").append(card);
    }
  }
  $("login").addEventListener("submit", event => { event.preventDefault(); token = $("token").value; $("token").value = ""; action(refresh, "Workspace unlocked."); });
  $("lock").addEventListener("click", () => { token = ""; state = null; closePreview(); $("saves").replaceChildren(); $("mind").textContent = ""; $("workspace").hidden = true; $("login").hidden = false; message("Workspace locked."); });
  $("auto-learn").addEventListener("change", () => action(async () => { try { await api("/robots/auto-learn", {enabled: $("auto-learn").checked}); closePreview(); } finally { await refresh(); } }, "Auto Learn setting saved."));
  $("save").addEventListener("submit", event => { event.preventDefault(); action(async () => { await api("/robots/save", {name: $("save-name").value, note: $("save-note").value}); await refresh(); }, "Robot copy saved."); });
  $("blank").addEventListener("click", () => action(async () => { await api("/robots/blank"); await refresh(); }, "Blank slate added. Preview Load when ready."));
  $("import").addEventListener("change", () => action(async () => {
    const file = $("import").files[0]; if (!file) return;
    if (file.size > 524288) throw new Error("robot_archive_size");
    await api("/robots/import", await file.arrayBuffer()); $("import").value = ""; await refresh(); message("Robot imported. Active robot unchanged.");
  }));
  $("favorites-only").addEventListener("change", renderSaves);
  $("confirm").addEventListener("click", () => action(async () => { if (!preview) return; await api("/robots/load", {preview_id: preview.id, target_sha256: preview.target_sha256, confirm: true}); closePreview(); await refresh(); }, "Saved mind loaded. Shadow remains paused; an undo save is available."));
  $("cancel").addEventListener("click", closePreview);
  $("retention").addEventListener("submit", event => { event.preventDefault(); action(async () => { await api("/robots/retention", {days: Number($("days").value), count: Number($("count").value)}); await refresh(); }, "Autosave limits saved."); });
  $("cleanup").addEventListener("click", () => action(async () => { const result = await (await api("/robots/cleanup")).json(); await refresh(); message(`${result.removed.length} unprotected autosaves expired.`); }));
  $("resume").addEventListener("click", () => {
    $("confirm-resume").hidden = false;
    message("Resume shadow reasoning with the displayed robot version? No orders can be sent. Confirm to continue.");
  });
  for (const [id, paused] of [["pause", true], ["confirm-resume", false]]) $(id).addEventListener("click", () => action(async () => {
    await api("/robots/pause", {paused, confirm: true, revision: state.active.revision}); closePreview(); await refresh();
  }, paused ? "Shadow paused." : "Shadow reasoning resumed."));
})();
