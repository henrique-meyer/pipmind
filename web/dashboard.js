"use strict";
(() => {
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => { byId(id).textContent = value; };
  const show = (value) => value === null || value === undefined ? "Unknown" : String(value);
  const stamp = (value) => {
    const date = new Date(value || "");
    return Number.isFinite(date.getTime()) ? date.toISOString().replace("T", " ") : "Unknown";
  };
  const age = (value) => {
    const seconds = (Date.now() - Date.parse(value || "")) / 1000;
    return !Number.isFinite(seconds) || seconds < -60 ? "Timestamp unavailable" :
      `${stamp(value)} · ${Math.max(0, Math.floor(seconds))}s ago${seconds > 120 ? " · STALE snapshot" : ""}`;
  };
  const node = (tag, value, className) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = value;
    if (className) item.className = className;
    return item;
  };
  function list(id, items, format = show) {
    const target = byId(id);
    target.replaceChildren();
    if (!Array.isArray(items)) target.append(node("li", "Unavailable in this observation."));
    else if (!items.length) target.append(node("li", "None recorded."));
    else for (const item of items) target.append(node("li", format(item)));
  }
  function runtime(data) {
    text("runtime-state", data.paused === true ? "Paused" : data.paused === false ? "Resumed" : "State unknown");
    const facts = byId("runtime-facts"); facts.replaceChildren();
    for (const [label, key] of [["Mode", "mode"], ["Pending inputs", "pending"], ["Mind revision", "revision"], ["Recorded outcomes", "outcomes"], ["Reported dispatches", "dispatches"]]) {
      facts.append(node("dt", label), node("dd", show(data[key])));
    }
    text("runtime-hold", data.reasoning_hold ? `Reasoning hold: ${show(data.reasoning_hold.reason)}` : "No reasoning hold reported in this snapshot.");
    const worker = data.reasoning_worker || {};
    text("runtime-worker", `Worker alive: ${show(worker.alive)} · Phase: ${show(worker.phase)} · Phase since: ${stamp(worker.phase_since)} · Last progress: ${stamp(data.last_progress)} · Last reported error: ${show(data.last_error)}`);
  }
  function observation(data) {
    text("memory-age", age(data.observed_at));
    text("memory-state", `${data.status === "ok" ? "Recorded SQL snapshot" : "PARTIAL observation"} · Revision ${show(data.head?.revision)}${data.cache?.stale ? " · STALE" : ""}${data.issues?.length ? ` · ${data.issues.join(", ")}` : ""}`);
    const working = data.head?.working || {};
    list("views", working.views); list("waiting", working.waiting_for);
    list("theses", working.position_theses, (item) => `${show(item.instrument)} · As of ${stamp(item.as_of)}\n${show(item.thesis)}`);
    const target = byId("outcomes"); target.replaceChildren();
    const outcomes = data.recent_outcomes;
    text("outcome-window", data.recent_window?.has_more ? "Partial history: older committed outcomes exist beyond this window." : "Bounded recent window; this is not a complete account history.");
    if (!Array.isArray(outcomes)) target.append(node("p", "Recent outcomes unavailable."));
    else if (!outcomes.length) target.append(node("p", "No outcomes recorded in this window."));
    else for (const item of outcomes) {
      const article = node("article", undefined, "outcome");
      article.append(node("h3", `${show(item.status)} · ${show(item.action)} · ${show(item.instrument)}`),
        node("p", `Event available: ${stamp(item.event_at)} · Recorded: ${stamp(item.recorded_at)}`, "note"),
        node("p", item.reasoning ?? "No reasoning retained for this outcome.", "reasoning"),
        node("p", `Tokens: ${show(item.tokens?.input_tokens)} input / ${show(item.tokens?.output_tokens)} output${item.tokens?.complete === true ? "" : " · incomplete usage"}`, "note"));
      if (item.errors?.length || item.issues?.length) article.append(node("p", [...(item.errors || []), ...(item.issues || [])].join(" · "), "notice"));
      target.append(article);
    }
  }
  function coverage(data) {
    text("feeds-age", age(data.observed_at));
    const page = data.pagination || {};
    text("feed-summary", `${show(page.returned)} of ${show(page.total_configured)} configured sources shown.${page.next_offset !== null && page.next_offset !== undefined ? " PARTIAL coverage page: additional configured sources are not shown." : ""} Freshness below describes saved checkpoints, not a live provider verification.`);
    const target = byId("sources"); target.replaceChildren();
    if (!Array.isArray(data.sources) || !data.sources.length) {
      const row = node("tr"); const cell = node("td", "No source observations returned."); cell.colSpan = 4; row.append(cell); target.append(row);
    } else for (const source of data.sources) {
      const row = node("tr");
      const identity = node("td", `${show(source.channel)} · ${show(source.source_id)}`);
      const state = node("td", show(source.read?.state));
      if (source.state_availability !== "available") state.append(node("p", "PARTIAL / unavailable saved state", "note"));
      const quality = source.canonical_page?.provider_quality;
      if (quality) state.append(node("p", `Provider-reported stale: ${show(quality.is_stale)} · Point-in-time safe: ${show(quality.point_in_time_safe)} · Source monitoring complete: ${show(quality.source_monitoring_complete)} (unverified provider metadata)`, "note"));
      if (source.channel === "social") state.append(node("p", `Role identity checked: ${stamp(source.identity?.checked_at)} · Unverified accounts: ${show(source.identity?.unverified_accounts)} · Account read coverage: ${source.account_read_bounds?.all_accounts_returned === true ? "all retained accounts shown" : "PARTIAL / unknown"}`, "note"));
      row.append(identity, state, node("td", stamp(source.read?.last_success_at)), node("td", stamp(source.read?.last_error_at)));
      target.append(row);
      if (Array.isArray(source.account_reads)) for (const account of source.account_reads) {
        const accountRow = node("tr");
        for (const value of [`↳ Account ${show(account.user_id)}`, `${show(account.read?.state)}${account.timestamp_anomaly ? " · timestamp anomaly" : ""}`, stamp(account.read?.last_success_at), stamp(account.read?.last_error_at)]) accountRow.append(node("td", value));
        target.append(accountRow);
      }
    }
  }
  const render = { status: runtime, coverage, observation };
  let busy = false;
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    const failed = [];
    await Promise.all(Object.keys(render).map(async (name) => {
      try {
        const response = await fetch(`/app/api/${name}`, { credentials: "same-origin", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(15000) });
        if (response.status === 401) { window.location.replace("/app/login"); return; }
        const payload = await response.json();
        if (!response.ok || payload.status !== "ok" || !payload.data) throw new Error("unavailable");
        render[name](payload.data);
        if (name === "status") text("runtime-age", age(payload.fetched_at));
      } catch {
        failed.push(name);
        // Remove prior data so a failed refresh cannot look like a current success.
        if (name === "status") { text("runtime-state", "Unavailable"); byId("runtime-facts").replaceChildren(); text("runtime-age", "Observation unavailable"); text("runtime-hold", "Hold state unavailable."); text("runtime-worker", ""); }
        if (name === "coverage") { byId("sources").replaceChildren(); text("feed-summary", "Feed observations unavailable."); text("feeds-age", "Observation unavailable"); }
        if (name === "observation") { for (const id of ["views", "waiting", "theses"]) list(id, null); byId("outcomes").replaceChildren(node("p", "Recent reasoning unavailable.")); text("memory-state", "Working memory unavailable."); text("memory-age", "Observation unavailable"); text("outcome-window", ""); }
      }
    }));
    text("connection", failed.length ? `PARTIAL / UNAVAILABLE: ${failed.join(", ")}. Other panels are independent saved observations. Next attempt in 30 seconds.` : `Last refresh ${stamp(new Date().toISOString())}. Panels are independent saved observations, not a single cross-service transaction.`);
    busy = false;
  }
  refresh();
  setInterval(refresh, 30000);
})();
