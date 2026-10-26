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
    linkedOutcomes = data.recent_outcomes;
    if (latestBook) books(latestBook);
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
  let linkedOutcomes = null;
  let latestBook = null;
  const money = (value) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(8).replace(/0+$/, "").replace(/\.$/, "") : "Unavailable";
  const protectionPrice = (annotation, key) => annotation?.protection === "known" ? (annotation[key] === null ? "None" : money(annotation[key])) : "Unavailable";
  function decisionLink(instrument) {
    if (!Array.isArray(linkedOutcomes)) return "Decision link unavailable.";
    const match = linkedOutcomes.find((item) => item && item.instrument === instrument && (item.action === "enter" || item.action === "add"));
    if (!match) return "No linked open decision in the recent reasoning window.";
    return `Linked by instrument in the recent window, not by a broker order id.\n${show(match.action)} · ${show(match.event_id)}\nRecorded ${stamp(match.recorded_at)}\n${match.reasoning ?? "No reasoning retained for this outcome."}`;
  }
  function books(data) {
    latestBook = data;
    const freshPositions = data.availability === "fresh";
    const card = byId("book-card");
    card.classList.toggle("stale-card", data.stale === true || data.availability === "stale" || data.orders_state === "stale");
    text("book-age", data.availability === "unavailable" ? "Book unavailable" : data.availability === "fresh" && data.orders_state !== "stale" ? `Within freshness window · ${age(data.checked_at)}` : `STALE · not a live book · ${age(data.checked_at)}`);
    text("book-state", freshPositions ? "Position snapshot is inside the freshness window. This is not a public quote." : data.availability === "stale" ? "STALE position snapshot. Figures below are retained and are not a live book." : "Position snapshot unavailable. No order was placed, modified, or cancelled.");
    const facts = byId("account-facts");
    facts.replaceChildren();
    const account = data.availability === "unavailable" ? null : data.account;
    if (!account) facts.append(node("dt", "Account"), node("dd", "Unavailable"));
    else for (const [label, key] of [["Currency", "currency"], ["Balance", "balance"], ["Equity", "equity"], ["Margin used", "margin_used"], ["Margin available", "margin_available"], ["Snapshot time", "as_of"]]) {
      const value = account[key];
      facts.append(node("dt", label), node("dd", key === "as_of" ? stamp(value) : key === "currency" ? show(value) : money(value)));
    }
    const positions = byId("positions");
    positions.replaceChildren();
    const rows = data.availability === "unavailable" ? null : data.positions?.positions;
    const marks = new Map((Array.isArray(data.marks) ? data.marks : []).map((mark) => [mark.instrument, mark]));
    if (!Array.isArray(rows)) {
      const row = node("tr"); const cell = node("td", "Open positions unavailable."); cell.colSpan = 9; row.append(cell); positions.append(row);
    } else if (!rows.length) {
      const row = node("tr"); const cell = node("td", "No open positions in this snapshot."); cell.colSpan = 9; row.append(cell); positions.append(row);
    } else rows.forEach((position, index) => {
      const annotation = Array.isArray(data.annotations) ? data.annotations[index] : null;
      const mark = marks.get(position.instrument);
      const current = !mark ? "Unavailable" : `${money(mark.bid)} / ${money(mark.ask)}${mark.stale ? " · STALE" : ""}`;
      const unrealized = !annotation || annotation.mark_stale === true ? "Hidden while the mark is missing or stale" : money(annotation.quote_pnl);
      const decision = node("td", decisionLink(position.instrument));
      const row = node("tr");
      for (const value of [show(position.instrument), show(position.side), show(position.size), money(position.average_price), current, unrealized, protectionPrice(annotation, "stop"), protectionPrice(annotation, "limit")]) row.append(node("td", value));
      row.append(decision);
      positions.append(row);
    });
    const orders = byId("orders");
    orders.replaceChildren();
    const orderLabel = data.orders_state === "fresh" ? "Working orders are inside the freshness window." : data.orders_state === "stale" ? "STALE working orders. Rows below are retained and are not a live order book." : "Working orders unavailable.";
    text("orders-state", `${orderLabel}${data.orders_partial ? " PARTIAL: additional working orders are not shown." : ""}${data.orders_scope === "broker_active_list" ? " Scope: the broker's active stop/limit list, not a full order history." : ""}`);
    const working = data.orders_state === "unavailable" ? [] : data.working_orders;
    if (!Array.isArray(working) || (data.orders_state === "unavailable")) {
      const row = node("tr"); const cell = node("td", "Working orders unavailable. No order was placed, modified, or cancelled."); cell.colSpan = 10; row.append(cell); orders.append(row);
    } else if (!working.length) {
      const row = node("tr"); const cell = node("td", "No working orders in the retained active list."); cell.colSpan = 10; row.append(cell); orders.append(row);
    } else for (const order of working) {
      const row = node("tr");
      for (const value of [show(order.instrument), show(order.side), show(order.size), show(order.status), order.stop === null ? "None" : money(order.stop), order.limit === null ? "None" : money(order.limit), money(order.trigger), order.role === "unclassified" ? "Unclassified trigger" : "If-done stop/limit", show(order.broker_order_id), stamp(order.ts)]) row.append(node("td", value));
      orders.append(row);
    }
  }
  const render = { status: runtime, coverage, observation, books };
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
        if (name === "observation") { linkedOutcomes = null; for (const id of ["views", "waiting", "theses"]) list(id, null); byId("outcomes").replaceChildren(node("p", "Recent reasoning unavailable.")); text("memory-state", "Working memory unavailable."); text("memory-age", "Observation unavailable"); text("outcome-window", ""); if (latestBook) books(latestBook); }
        if (name === "books") { latestBook = null; byId("positions").replaceChildren(); byId("orders").replaceChildren(); byId("account-facts").replaceChildren(); text("book-state", "Positions and orders unavailable. No order was placed, modified, or cancelled."); text("book-age", "Observation unavailable"); text("orders-state", "Working orders unavailable."); byId("book-card").classList.remove("stale-card"); }
      }
    }));
    text("connection", failed.length ? `PARTIAL / UNAVAILABLE: ${failed.join(", ")}. Other panels are independent saved observations. Next attempt in 30 seconds.` : `Last refresh ${stamp(new Date().toISOString())}. Panels are independent saved observations, not a single cross-service transaction.`);
    busy = false;
  }
  refresh();
  setInterval(refresh, 30000);
})();
