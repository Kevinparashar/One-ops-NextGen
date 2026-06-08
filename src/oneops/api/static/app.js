// OneOps AI · ITSM Assistant frontend.
//
// Two doors share one session: chat (NL) and fast-path buttons (UI-declared
// intent). Each turn gets its own request_id ⇒ its own checkpoint thread —
// safe to submit Q2 before Q1 returns. The session_id is shared so both
// doors append to one conversation and references resolve across them.

(() => {
  const $ = (sel) => document.querySelector(sel);
  const conv = $("#conversation");
  const status = $("#status");
  const sessionLabel = $("#session-id");
  const fastPathActions = $("#fast-path-actions");
  const statusStrip = $("#status-strip");
  const turnCountEl = $("#turn-count");
  const cacheHitsEl = $("#cache-hits");
  const lastLatencyEl = $("#last-latency");

  // Server-owned session lifecycle. The browser caches the active
  // session_id in localStorage but the SERVER is the source of truth:
  //   * On load: GET /api/sessions/{cached}. If 404 → POST mints a fresh
  //     one. Stale ids can never silently rehydrate dead state.
  //   * + New chat: POST /api/sessions, switch active.
  //   * Sidebar lists this user's recent sessions via GET /api/sessions.
  //   * Trash icon → DELETE /api/sessions/{id}.
  //   * Server slides a 30-min idle TTL on every chat turn.
  const LS_KEY = "oneops.session_id";
  let sessionId = "";                       // resolved in bootstrap()
  let counters = { turns: 0, cacheHits: 0 };
  bootstrap();

  function renderSession() {
    sessionLabel.textContent = sessionId || "—";
  }

  function loadStoredSessionId() {
    try {
      const v = globalThis.localStorage.getItem(LS_KEY);
      return (typeof v === "string" && v.startsWith("sess_")) ? v : null;
    } catch { return null; }
  }
  function saveSessionId(id) {
    try { globalThis.localStorage.setItem(LS_KEY, id); } catch { /* ignore */ }
  }
  function clearStoredSessionId() {
    try { globalThis.localStorage.removeItem(LS_KEY); } catch { /* ignore */ }
  }

  // POST /api/sessions — mint a fresh server-side session.
  async function mintServerSession() {
    try {
      const res = await fetch("/api/sessions", {
        method: "POST", headers: envelopeHeaders(),
      });
      if (!res.ok) return null;
      const payload = await res.json();
      return payload?.session_id || null;
    } catch { return null; }
  }

  // GET /api/session/{id}/history — true iff Postgres has any durable
  // events for this id (used to recover a chat whose Dragonfly lifecycle
  // metadata has expired but whose transcript is still safe in Postgres).
  async function _cachedSessionHasDurableHistory(id) {
    if (!id) return false;
    try {
      const res = await fetch(
        `/api/session/${encodeURIComponent(id)}/history`,
        { headers: envelopeHeaders() });
      if (!res.ok) return false;
      const payload = await res.json();
      return Array.isArray(payload.events) && payload.events.length > 0;
    } catch { return false; }
  }

  // GET /api/sessions/{id} — true iff server still has this session active.
  async function isServerSessionAlive(id) {
    if (!id) return false;
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(id)}`,
                              { headers: envelopeHeaders() });
      return res.ok;
    } catch { return false; }
  }

  // Boot resolution: validate cached id; otherwise mint a new one.
  async function bootstrap() {
    const cached = loadStoredSessionId();
    const aliveCheck = cached ? await isServerSessionAlive(cached) : false;
    console.info("[oneops] bootstrap", {
      cached, alive: aliveCheck, ls_available: _lsAvailable(),
    });
    if (cached && aliveCheck) {
      sessionId = cached;
    } else if (cached && await _cachedSessionHasDurableHistory(cached)) {
      // Lifecycle metadata expired (Dragonfly TTL) but events are still
      // in Postgres — keep the cached id so restoreConversation() replays
      // the transcript. The next chat turn will re-create lifecycle meta
      // server-side via the normal touch path.
      sessionId = cached;
      console.info("[oneops] bootstrap.resurrected_from_durable_events",
                   { sessionId });
    } else {
      const fresh = await mintServerSession();
      sessionId = fresh || (cached || ("sess_" + Math.random().toString(36).slice(2, 14)));
      console.info("[oneops] bootstrap.minted_or_fallback",
                   { sessionId, mintedFromServer: !!fresh });
      if (cached && cached !== sessionId) clearStoredSessionId();
    }
    saveSessionId(sessionId);
    renderSession();
    // Identity options must populate BEFORE the history fetch — otherwise
    // a tenant/user mismatch can silently return empty events. Await it.
    await loadIdentityOptions();
    loadStatusStrip();
    refreshThreadList();
    await restoreConversation();
  }

  function _lsAvailable() {
    try {
      globalThis.localStorage.setItem("_oneops_probe", "1");
      globalThis.localStorage.removeItem("_oneops_probe");
      return true;
    } catch { return false; }
  }

  // Rehydrate the conversation panel from the server's session log when
  // the page loads with a known session_id. Best-effort: a failure does
  // not block the user — they can still chat from scratch.
  async function restoreConversation() {
    try {
      const headers = envelopeHeaders();
      const url = `/api/session/${encodeURIComponent(sessionId)}/history`;
      console.info("[oneops] restoreConversation.fetch",
                   { sessionId, headers });
      const res = await fetch(url, { headers });
      if (!res.ok) {
        console.warn("[oneops] restoreConversation.http_not_ok",
                     { status: res.status });
        return;
      }
      const payload = await res.json();
      const events = payload.events || [];
      console.info("[oneops] restoreConversation.events", {
        count: events.length, sessionId,
      });
      if (!events.length) return;
      // Hard clear the conversation panel before replaying, so a stale
      // empty placeholder or earlier debug content never overlays the
      // replayed turns.
      conv.innerHTML = "";
      for (const ev of events) {
        if (ev.role === "user") {
          addUserBubble({ door: "chat", text: ev.content });
        } else {
          // Restored assistant turn — we don't have the original
          // structured step output, so render the final_response text.
          addAssistantBubble({
            door: "chat",
            meta: ["restored"],
            text: ev.content,
            markdown: true,
          });
        }
      }
      // Keep the live turn-counter in sync so the chip in the header
      // reflects the restored conversation length (so refresh doesn't
      // show "0 turns" with a chat panel of restored bubbles).
      const userTurns = events.filter((e) => e.role === "user").length;
      counters.turns = userTurns;
      renderCounters();
      setStatus(`Restored ${events.length} turn(s) from this session.`);
    } catch (err) {
      console.warn("[oneops] restoreConversation.error", err);
    }
  }

  $("#new-session").addEventListener("click", async () => {
    const fresh = await mintServerSession();
    if (fresh) {
      sessionId = fresh;
    } else {
      // Lifecycle endpoint unreachable — fall back to a transient client id
      // so the user is never stuck. The chat path also tolerates this.
      sessionId = "sess_" + Math.random().toString(36).slice(2, 14);
    }
    saveSessionId(sessionId);
    renderSession();
    conv.innerHTML = "";
    counters = { turns: 0, cacheHits: 0 };
    renderCounters();
    addAssistantBubble({
      door: "system", text: "New session started. Conversation cleared.",
    });
    setStatus("Ready.");
    refreshThreadList();
  });

  // ── thread list (sidebar — server-driven) ───────────────────────────
  // The threads section lists this user's recent sessions. Server is
  // authoritative; we re-pull after every turn so titles + ordering
  // reflect actual activity. Clicking switches active session;
  // trash icon DELETEs.
  async function refreshThreadList() {
    const list = $("#thread-list");
    if (!list) return;
    let rows = [];
    try {
      const res = await fetch("/api/sessions?limit=25",
                              { headers: envelopeHeaders() });
      if (res.ok) {
        const payload = await res.json();
        rows = payload.sessions || [];
      }
    } catch { /* fall through with empty rows */ }
    list.innerHTML = "";
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "thread-item active";
      empty.innerHTML = `<span id="session-id">${sessionId || "—"}</span>
                         <span class="muted small">current</span>`;
      list.appendChild(empty);
      return;
    }
    for (const r of rows) {
      const item = document.createElement("div");
      item.className = "thread-item" + (r.session_id === sessionId ? " active" : "");
      item.title = `${r.turn_count} turn(s) · last active ${
        new Date(r.last_active_at_unix_ms).toLocaleString()}`;
      const title = (r.title || "(new conversation)").replace(/\s+/g, " ").slice(0, 60);
      const idSuffix = r.session_id.slice(-6);
      item.innerHTML = `
        <div class="thread-line">
          <span class="thread-title">${escapeHtml(title)}</span>
          <button class="thread-trash" data-sid="${r.session_id}" title="delete">×</button>
        </div>
        <span class="muted small">${idSuffix} · ${r.turn_count} turn(s)</span>`;
      // Switch on click anywhere except the trash button.
      item.addEventListener("click", (ev) => {
        if (ev.target.closest(".thread-trash")) return;
        switchSession(r.session_id);
      });
      list.appendChild(item);
    }
    // Re-pin the live label to whichever element holds the active session.
    const active = list.querySelector(".thread-item.active .thread-title");
    if (active && active.id !== "session-id") {
      // Keep the original sessionLabel reference valid for renderSession()
      // by ensuring SOME node carries id=session-id at all times.
      const sentinel = document.getElementById("session-id");
      if (!sentinel) {
        const span = document.createElement("span");
        span.id = "session-id";
        span.style.display = "none";
        span.textContent = sessionId;
        list.appendChild(span);
      }
    }
    list.querySelectorAll(".thread-trash").forEach((b) => {
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        await deleteSession(b.dataset.sid);
      });
    });
  }

  async function switchSession(id) {
    if (!id || id === sessionId) return;
    sessionId = id;
    saveSessionId(id);
    renderSession();
    conv.innerHTML = "";
    counters = { turns: 0, cacheHits: 0 };
    renderCounters();
    await restoreConversation();
    refreshThreadList();
  }

  async function deleteSession(id) {
    if (!id) return;
    try {
      await fetch(`/api/sessions/${encodeURIComponent(id)}`, {
        method: "DELETE", headers: envelopeHeaders(),
      });
    } catch { /* ignore — refresh will reflect server reality */ }
    if (id === sessionId) {
      // Deleted the active one — mint a fresh and clear the panel.
      const fresh = await mintServerSession();
      sessionId = fresh || "";
      saveSessionId(sessionId);
      renderSession();
      conv.innerHTML = "";
      counters = { turns: 0, cacheHits: 0 };
      renderCounters();
    }
    refreshThreadList();
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function renderCounters() {
    turnCountEl.textContent = counters.turns;
    cacheHitsEl.textContent = counters.cacheHits;
  }
  renderCounters();

  // ── identity dropdowns + status strip ───────────────────────────────

  async function loadIdentityOptions() {
    try {
      const opts = await fetch("/api/identity-options").then((r) => r.json());
      fillSelect("#tenant", opts.tenants, opts.defaults.tenant);
      fillSelect("#user",   opts.users,   opts.defaults.user);
      fillSelect("#role",   opts.roles,   opts.defaults.role);
    } catch (err) {
      setStatus("Failed to load identity options: " + err, "error");
    }
  }
  function fillSelect(sel, values, defaultValue) {
    const el = $(sel);
    el.innerHTML = "";
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      if (v === defaultValue) opt.selected = true;
      el.appendChild(opt);
    }
  }

  // Per-chip descriptor builders — each returns {label, state, value}. Split
  // out of loadStatusStrip so each chip's state/value logic stays small (S3776).
  function cacheChip(cfg) {
    return {
      label: "Cache",
      state: cfg.cache.enabled ? "on" : "off",
      value: cfg.cache.backend.replace(/^In/, "").replace(/SummaryCacheStore$/, ""),
    };
  }
  function otelChip(cfg) {
    let state = "off";
    if (cfg.otel.in_memory_spans) state = cfg.otel.enabled ? "on" : "warn";
    let value = cfg.otel.in_memory_spans ? "in-memory only" : "off";
    if (cfg.otel.endpoint) value = "exporter live";
    return { label: "OTel", state, value };
  }
  function llmChip(cfg) {
    let state = "off";
    if (cfg.llm_gateway.summarizer_wired) state = "on";
    else if (cfg.llm_gateway.configured) state = "warn";
    let value = "not configured";
    if (cfg.llm_gateway.summarizer_wired) value = "wired";
    else if (cfg.llm_gateway.configured) value = "gateway up · summarizer pending";
    return { label: "LLM", state, value };
  }
  function dbChip(cfg) {
    let state = "off";
    if (cfg.postgres.configured) {
      state = cfg.postgres.backend_in_use.startsWith("Postgres") ? "on" : "warn";
    }
    return { label: "DB", state, value: cfg.postgres.backend_in_use };
  }
  function natsChip(cfg) {
    let state = "off";
    if (cfg.nats.configured) state = cfg.nats.wired_into_ingress ? "on" : "warn";
    let value = "not configured";
    if (cfg.nats.wired_into_ingress) value = "wired";
    else if (cfg.nats.configured) value = "configured · ingress in-process";
    return { label: "NATS", state, value };
  }
  function sessionChip(cfg) {
    let value = "not wired";
    if (cfg.session?.wired) {
      value = cfg.session.durable_across_reload
        ? "durable · " + cfg.session.backend.replace(/^In/, "")
        : cfg.session.backend;
    }
    return { label: "Session", state: cfg.session?.wired ? "on" : "off", value };
  }

  async function loadStatusStrip() {
    try {
      const cfg = await fetch("/api/config").then((r) => r.json());
      statusStrip.innerHTML = "";
      [cacheChip, otelChip, llmChip, dbChip, natsChip, sessionChip]
        .forEach((mk) => addChip(mk(cfg)));
    } catch (err) {
      statusStrip.innerHTML = `<span class="status-chip off">status load failed: ${err}</span>`;
    }
  }
  function addChip({ label, state, value }) {
    const chip = document.createElement("span");
    chip.className = "status-chip " + state;
    const dot = document.createElement("span"); dot.className = "dot";
    const lbl = document.createElement("span"); lbl.className = "label"; lbl.textContent = label;
    const val = document.createElement("span"); val.className = "value"; val.textContent = value;
    chip.appendChild(dot); chip.appendChild(lbl); chip.appendChild(val);
    statusStrip.appendChild(chip);
  }

  function envelopeHeaders() {
    return {
      "Content-Type": "application/json",
      "x-tenant-id": $("#tenant").value.trim() || "T001",
      "x-user-id":   $("#user").value.trim() || "u_demo",
      "x-role":      $("#role").value.trim() || "service_desk_agent",
    };
  }

  function setStatus(text, cls = "") {
    status.textContent = text;
    status.className = (cls ? cls + " " : "") + "small muted";
  }

  // ── conversation rendering ──────────────────────────────────────────

  function addUserBubble({ door, text }) {
    const turn = document.createElement("div");
    turn.className = "turn user";
    const meta = document.createElement("div");
    meta.className = "meta";
    const chip = document.createElement("span");
    chip.className = "door-chip " + door;
    chip.textContent = door;
    meta.appendChild(chip);
    turn.appendChild(meta);
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    turn.appendChild(bubble);
    conv.appendChild(turn);
    conv.scrollTop = conv.scrollHeight;
  }

  function addAssistantBubble({ door, text, meta, content, error, markdown }) {
    const turn = document.createElement("div");
    turn.className = "turn " + (error ? "error" : "assistant");
    const metaRow = document.createElement("div");
    metaRow.className = "meta";
    const chip = document.createElement("span");
    chip.className = "door-chip " + (door || "");
    chip.textContent = door || (error ? "error" : "assistant");
    metaRow.appendChild(chip);
    (meta || []).forEach((m) => {
      const s = document.createElement("span");
      // Meta items can be plain strings (the common case) or
      // `{ html: "<span>..." }` objects when the metadata needs to
      // render a clickable link (e.g. the trace_id → Grafana Tempo
      // deep-link). The renderer fans out on shape so existing
      // call-sites that pass strings stay unchanged.
      if (m && typeof m === "object" && typeof m.html === "string") {
        s.innerHTML = m.html;
      } else {
        s.textContent = m;
      }
      metaRow.appendChild(s);
    });
    turn.appendChild(metaRow);
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (content) {
      bubble.appendChild(content);
    } else if (markdown && typeof marked !== "undefined") {
      bubble.classList.add("md");
      bubble.innerHTML = renderMarkdown(text || "");
    } else {
      bubble.textContent = text;
    }
    turn.appendChild(bubble);
    conv.appendChild(turn);
    conv.scrollTop = conv.scrollHeight;
    return turn;
  }

  // Markdown renderer. `marked` strips no HTML by default — for an
  // untrusted-text future we'd add DOMPurify. The current input is the
  // engine's own `final_response`, generated server-side, so the trust
  // boundary is the LLM Gateway (already enforced by the policy engine).
  function renderMarkdown(src) {
    try {
      marked.setOptions({ breaks: true, gfm: true });
      return marked.parse(String(src));
    } catch {
      return escapeHtml(src);
    }
  }

  function addPending({ door, text }) {
    const turn = document.createElement("div");
    turn.className = "turn pending";
    const meta = document.createElement("div");
    meta.className = "meta";
    const chip = document.createElement("span");
    chip.className = "door-chip " + door;
    chip.textContent = door;
    meta.appendChild(chip);
    turn.appendChild(meta);
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    turn.appendChild(bubble);
    conv.appendChild(turn);
    conv.scrollTop = conv.scrollHeight;
    return turn;
  }

  // ── response shape ──────────────────────────────────────────────────
  //
  // When E2 (LLM gateway) is wired, the handler will produce:
  //   step.output.summary = {
  //     summary: "<paragraph>",
  //     key_details: { Status: ..., Criticality: ..., ... },
  //     model: "...", usage: { ... }
  //   }
  //   step.output.record  = {<raw record fields, policy-filtered>}
  //
  // Until then we still try to render whatever fields the step returned,
  // so the substrate progress is visible at every stage.

  function renderResponseContent(payload) {
    const wrapper = document.createElement("div");
    wrapper.className = "response-card";

    const rawSteps = payload.step_results || [];

    // Dedup identical step messages (e.g. "summarize MISSING and find KB
    // for MISSING" produces the same not-found text twice — the user
    // should see it once).
    const seenKeys = new Set();
    const steps = rawSteps.filter((s) => {
      const msg = s?.output?.message || "";
      const key = msg.replace(/\s+/g, " ").trim();
      if (!key) return true;            // keep empty-message steps (full summaries)
      if (seenKeys.has(key)) return false;
      seenKeys.add(key);
      return true;
    });

    // Multi-step turn — render each step as its own card so multi-sub-query
    // turns ("summarize INC0001001 and INC0001002", "priority of X and
    // status of Y") show ALL answers, not just the first.
    if (steps.length > 1) {
      steps.forEach((s, idx) => {
        const sub = renderSingleStep(payload, s);
        if (idx > 0) {
          const sep = document.createElement("hr");
          sep.className = "step-sep";
          wrapper.appendChild(sep);
        }
        // The sub-renderer returns its own response-card wrapper; lift
        // its children up so we keep ONE outer wrapper.
        while (sub.firstChild) wrapper.appendChild(sub.firstChild);
      });
    } else {
      // Single-step (or empty) path — original behavior.
      const single = renderSingleStep(payload, steps[0] || {});
      while (single.firstChild) wrapper.appendChild(single.firstChild);
    }

    // ── execution trace — which agents + tools handled this turn ──────
    // One panel per turn, identical for chat AND button (both doors render
    // through this function). Built from the RAW step_results so it shows
    // every executed step, not the deduped answer view.
    const trace = buildExecutionTrace(payload);
    if (trace) wrapper.appendChild(trace);
    return wrapper;
  }

  // Collapsible "How this was answered" panel: agent → tool → status →
  // latency for each executed step, with a deep-link to the full Tempo
  // trace. Returns null when the turn has no steps (e.g. a cached turn).
  function buildExecutionTrace(payload) {
    const steps = payload.step_results || [];
    if (!steps.length) return null;

    const agentLabel = (id) =>
      String(id || "").replace(/^uc\d+_/, "").replaceAll("_", " ")
        .replace(/\b\w/g, (c) => c.toUpperCase()) || "agent";

    const agents = new Set();
    const tools = new Set();
    steps.forEach((s) => {
      if (s.agent_id) agents.add(s.agent_id);
      if (s.tool_id) tools.add(s.tool_id);
    });

    const details = document.createElement("details");
    details.className = "exec-trace";

    const summary = document.createElement("summary");
    const total = (payload.latency_ms == null)
      ? "" : " · " + (payload.latency_ms / 1000).toFixed(1) + "s";
    const plural = (n) => (n === 1 ? "" : "s");
    summary.textContent =
      `How this was answered — ${agents.size} agent${plural(agents.size)}` +
      (tools.size ? ` · ${tools.size} tool${plural(tools.size)}` : "") + total;
    details.appendChild(summary);

    const ul = document.createElement("ul");
    ul.className = "exec-trace-rows";
    steps.forEach((s) => {
      const li = document.createElement("li");
      const ok = s.status === "success";
      const failed = s.status === "failed";
      let cls = "neutral";
      let badge = "•";
      if (ok) { cls = "ok"; badge = "✓"; }
      else if (failed) { cls = "bad"; badge = "✗"; }
      li.className = "exec-trace-row " + cls;
      const tool = s.tool_id ? " → " + s.tool_id : "";
      const lat = (s.latency_ms == null) ? "" : "  ·  " + s.latency_ms + " ms";
      li.textContent = `${badge}  ${agentLabel(s.agent_id)}${tool}  (${s.status})${lat}`;
      ul.appendChild(li);
    });
    details.appendChild(ul);

    if (payload.trace_id) {
      const url = "http://localhost:3041/explore?orgId=1&left=" +
        encodeURIComponent(JSON.stringify({
          datasource: "Tempo",
          queries: [{ query: payload.trace_id, queryType: "traceql" }],
          range: { from: "now-1h", to: "now" },
        }));
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.className = "exec-trace-link";
      a.textContent = "Open full trace in Tempo →";
      details.appendChild(a);
    }
    return details;
  }

  // A transparent markdown bubble — the canonical "rendered text" element
  // used by every text path in renderSingleStep (error message, display_text,
  // summary paragraph, plain message).
  function appendMarkdownBubble(wrapper, md) {
    const p = document.createElement("div");
    p.className = "summary-text bubble md";
    p.style.background = "transparent";
    p.style.border = "0";
    p.style.padding = "0";
    p.innerHTML = renderMarkdown(md);
    wrapper.appendChild(p);
  }
  function appendSectionTitle(wrapper, text) {
    const h = document.createElement("div");
    h.className = "section-title";
    h.textContent = text;
    wrapper.appendChild(h);
  }
  // A successful summary (vs an error/denial/handler-outcome that already
  // carries a friendly final_response). When false, render the message
  // verbatim — no empty Summary / Key Details sections.
  function isSuccessfulSummary(stepStatus, handlerOutcome) {
    return stepStatus === "success" &&
      handlerOutcome !== "not_found" &&
      handlerOutcome !== "invalid_request" &&
      handlerOutcome !== "llm_unavailable" &&
      handlerOutcome !== "denied";
  }
  function appendEntityHeader(wrapper, record, step, out, summaryBlock) {
    const entityId = record?.incident_id || record?.request_id ||
                     record?.problem_id || record?.change_id ||
                     record?.asset_id || record?.ci_id || record?.kb_id;
    const entityTitle = record?.title || record?.asset_name ||
                        record?.ci_name || record?.summary;
    const serviceId = step.parameters?.service_id || out.service_id ||
                      summaryBlock?.service_id;
    if (!(entityId || entityTitle)) return;
    const header = document.createElement("div");
    header.className = "entity-header";
    const part = (cls, val) => {
      if (!val) return;
      const span = document.createElement("span");
      span.className = cls;
      span.textContent = val;
      header.appendChild(span);
    };
    part("entity-id", entityId);
    part("entity-title", entityTitle);
    part("entity-service", serviceId);
    wrapper.appendChild(header);
  }
  // Build the key/value map for the Key Details block. The full-summary
  // outcome deliberately HIDES the raw list (the compact grounded summary
  // already weaves in the fields); otherwise prefer the LLM's labelled
  // key_details, falling back to a humanised projection of the raw record.
  function computeKeyDetails(record, keyDetails, handlerOutcome) {
    let kv = (handlerOutcome === "summarized") ? null : keyDetails;
    if (!kv && record && handlerOutcome !== "summarized") {
      kv = {};
      for (const [k, v] of Object.entries(record)) {
        if (k.startsWith("_") || v == null) continue;
        if (Array.isArray(v) && v.length === 0) continue;
        if (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) continue;
        kv[humanise(k)] = v;
      }
    }
    return kv;
  }
  function appendKeyDetails(wrapper, kv) {
    if (!(kv && Object.keys(kv).length)) return;
    appendSectionTitle(wrapper, "Key Details");
    const dl = document.createElement("dl");
    dl.className = "key-details";
    for (const [k, v] of Object.entries(kv)) {
      const dt = document.createElement("dt");
      dt.textContent = humanise(k);
      dl.appendChild(dt);
      const dd = document.createElement("dd");
      dd.appendChild(renderValue(v));
      dl.appendChild(dd);
    }
    wrapper.appendChild(dl);
  }

  function renderSingleStep(payload, step) {
    const wrapper = document.createElement("div");
    wrapper.className = "response-card";
    const out = step.output || {};
    const stepStatus = (step.status || "").toLowerCase();
    const handlerOutcome = out?.outcome ? String(out.outcome).toLowerCase() : "";

    // Error / denial / non-success outcome → render the friendly message
    // verbatim (preferring the per-step message over the aggregated one).
    if (!isSuccessfulSummary(stepStatus, handlerOutcome)) {
      appendMarkdownBubble(
        wrapper, out?.message || payload.final_response || "(no response)");
      return wrapper;
    }
    // Canonical chat-ready string (UC-2 ranked list, UC-3 composed answer, …)
    // takes precedence over the UC-1 Summary/Key Details shape.
    if (typeof out.display_text === "string" && out.display_text.trim()) {
      appendMarkdownBubble(wrapper, out.display_text);
      return wrapper;
    }

    // Success path — Summary + Key Details (UC-1 shape).
    const summaryBlock = out.summary || null;
    const record = out.record || summaryBlock?.record || null;
    const keyDetails = summaryBlock?.key_details || out.key_details || null;

    appendEntityHeader(wrapper, record, step, out, summaryBlock);

    const summaryText = (typeof summaryBlock === "string")
      ? summaryBlock : summaryBlock?.summary;
    if (summaryText) {
      // Field-read answers are one-liners — the "Summary" header is
      // misleading, so omit it for that outcome.
      if (handlerOutcome !== "field_read") appendSectionTitle(wrapper, "Summary");
      appendMarkdownBubble(wrapper, summaryText);
    } else if (out?.message) {
      appendSectionTitle(wrapper, "Response");
      appendMarkdownBubble(wrapper, out.message);
    }

    appendKeyDetails(wrapper, computeKeyDetails(record, keyDetails, handlerOutcome));
    return wrapper;
  }

  function renderValue(v) {
    if (v == null) {
      const s = document.createElement("span");
      s.className = "muted small";
      s.textContent = "—";
      return s;
    }
    if (Array.isArray(v)) {
      const span = document.createElement("span");
      if (v.every((x) => typeof x !== "object")) {
        span.textContent = v.join(", ");
      } else {
        span.textContent = JSON.stringify(v);
      }
      return span;
    }
    if (typeof v === "object") {
      const span = document.createElement("span");
      // Render "a: 1, b: 2, c: 3" for shallow dicts — matches the reference.
      const parts = Object.entries(v).map(([k, val]) =>
        `${k}: ${typeof val === "object" ? JSON.stringify(val) : val}`);
      span.textContent = parts.join(", ");
      return span;
    }
    if (typeof v === "boolean") {
      const code = document.createElement("code");
      code.textContent = String(v);
      return code;
    }
    const span = document.createElement("span");
    span.textContent = String(v);
    return span;
  }

  function humanise(key) {
    return String(key).replaceAll("_", " ").replace(/^(\w)/, (m) => m.toUpperCase());
  }
  function humaniseUcId(ucId) {
    // uc01_summarization -> "Summarization"
    const tail = String(ucId).split("_").slice(1).join(" ");
    return tail ? tail.replace(/^(\w)/, (m) => m.toUpperCase()) : ucId;
  }

  // Display label for a use case. Prefers the registry-sourced `display_name`
  // returned by /api/fast/{uc_id}/spec (single source of truth); falls back to
  // deriving it from the uc_id. Never shows the raw `ucNN_` wire id to a user.
  function ucLabel(spec) {
    return spec?.display_name || humaniseUcId(spec?.uc_id);
  }

  // ── chat door ────────────────────────────────────────────────────────

  // Shared live-streaming turn driver — used by BOTH the chat door and the
  // fast-path buttons. Renders a live "working" panel whose rows light up as
  // each agent/tool runs (Claude-style), then the final answer + trace panel.
  // ── shared live-stream helpers (streamTurnInto + oneopsLiveStream) ──────
  const liveAgentName = (id) => String(id || "")
    .replace(/^uc\d+_/, "").replaceAll("_", " ")
    .replace(/\b\w/g, (c) => c.toUpperCase()) || "agent";
  const liveKeyOf = (ev) => ev.step_id || (ev.agent_id + "::" + ev.tool_id);
  function toolStartHtml(ev) {
    return '<div class="live-line1"><span class="live-spin">⏳</span> <b>' +
      liveAgentName(ev.agent_id) + " agent</b> is running " +
      '<span class="live-tool">' + (ev.tool_id || "tool") + "</span></div>" +
      (ev.action ? '<div class="live-action">↳ ' + ev.action + "…</div>" : "");
  }
  function toolDoneHtml(ev) {
    const ok = ev.status === "success";
    return '<div class="live-line1">' + (ok ? "✓" : "✗") + " <b>" +
      liveAgentName(ev.agent_id) + " agent</b> · " +
      '<span class="live-tool">' + ev.tool_id + "</span>" +
      ' <span class="live-lat">' +
      (ev.latency_ms == null ? "" : ev.latency_ms + " ms") +
      "</span></div>";
  }
  // Read an NDJSON stream line-by-line; dispatch each non-final event to
  // onEvent; return the `final` payload (or null).
  async function readNdjsonStream(res, onEvent) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let finalPayload = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) {
          let ev;
          try { ev = JSON.parse(line); } catch { continue; }
          if (ev.type === "final") finalPayload = ev.payload;
          else onEvent(ev);
        }
      }
    }
    return finalPayload;
  }

  async function streamTurnInto({ url, body, door, statusLabel }) {
    const pending = addPending({ door, text: "Working on it…" });
    setStatus(statusLabel || "Working…", "busy");

    const bubble = pending.querySelector(".bubble");
    bubble.textContent = "";
    const head = document.createElement("div");
    head.className = "live-head";
    head.textContent = "Working on it…";
    bubble.appendChild(head);
    const list = document.createElement("ul");
    list.className = "live-activity";
    bubble.appendChild(list);

    // Immediate "routing" row so the panel is never blank during the
    // decompose/route/disambiguate phase (which precedes the first tool).
    const routingRow = document.createElement("li");
    routingRow.className = "live-row running";
    routingRow.innerHTML =
      '<span class="live-spin">🧭</span> ' +
      '<span class="live-action">Understanding your request &amp; selecting agents…</span>';
    list.appendChild(routingRow);
    let routingCleared = false;
    // Keep the live panel on screen for at least this long so the
    // "agents + tools working" phase is always perceptible — even when a
    // cached result makes the tools finish in milliseconds.
    const panelStart = performance.now();
    const MIN_PANEL_MS = 1100;

    const rows = {};
    const onEvent = (ev) => {
      if (ev.type === "tool_start") {
        if (!routingCleared) { routingRow.remove(); routingCleared = true; }
        const li = document.createElement("li");
        li.className = "live-row running";
        li.innerHTML = toolStartHtml(ev);
        list.appendChild(li);
        rows[liveKeyOf(ev)] = li;
        conv.scrollTop = conv.scrollHeight;
      } else if (ev.type === "tool_done") {
        const li = rows[liveKeyOf(ev)];
        if (li) {
          li.className = "live-row " + (ev.status === "success" ? "done" : "failed");
          li.innerHTML = toolDoneHtml(ev);
        }
      }
    };

    try {
      const res = await fetch(url, {
        method: "POST", headers: envelopeHeaders(),
        body: JSON.stringify(body),
      });
      if (!res.ok || !res.body) {
        const txt = await res.text().catch(() => res.statusText);
        pending.remove();
        addAssistantBubble({ door, error: true,
          text: `HTTP ${res.status}: ${txt}` });
        setStatus("Turn failed.", "error");
        return null;
      }
      // Read the NDJSON stream, updating the live panel as events arrive.
      const finalPayload = await readNdjsonStream(res, onEvent);
      // Hold the live panel briefly so the working phase is always seen.
      const shownFor = performance.now() - panelStart;
      if (shownFor < MIN_PANEL_MS) {
        await new Promise((r) => setTimeout(r, MIN_PANEL_MS - shownFor));
      }
      pending.remove();
      if (finalPayload) {
        addAssistantBubble({
          door,
          meta: metaForPayload(finalPayload),
          content: renderResponseContent(finalPayload),
        });
        sessionId = finalPayload.session_id || sessionId;
        saveSessionId(sessionId);
        renderSession();
        tallyTurn(finalPayload);
        setStatus("Ready.");
        refreshThreadList();
        return finalPayload;
      }
      addAssistantBubble({ door, error: true,
        text: "No final response received from the stream." });
      setStatus("Turn incomplete.", "error");
      return null;
    } catch (err) {
      pending.remove();
      addAssistantBubble({ door, error: true, text: String(err) });
      setStatus("Error: " + err, "error");
      return null;
    }
  }

  // Exposed for the bespoke UC button UIs (uc02/uc05/uc08 *.js) so they show
  // the SAME live agent/tool panel. Renders the panel into `mount`, streams
  // NDJSON from `url`, and returns the final payload (the UC's own response)
  // for the caller to render its existing results view beneath the panel.
  globalThis.oneopsLiveStream = async function ({ url, body, headers, mount }) {
    const head = document.createElement("div");
    head.className = "live-head";
    head.textContent = "Working on it…";
    const list = document.createElement("ul");
    list.className = "live-activity";
    mount.appendChild(head);
    mount.appendChild(list);

    const routingRow = document.createElement("li");
    routingRow.className = "live-row running";
    routingRow.innerHTML =
      '<span class="live-spin">🧭</span> <span class="live-action">' +
      "Understanding your request &amp; selecting agents…</span>";
    list.appendChild(routingRow);
    let routingCleared = false;

    const rows = {};
    const onEvent = (ev) => {
      if (ev.type === "tool_start") {
        if (!routingCleared) { routingRow.remove(); routingCleared = true; }
        const li = document.createElement("li");
        li.className = "live-row running";
        li.innerHTML = toolStartHtml(ev);
        list.appendChild(li);
        rows[liveKeyOf(ev)] = li;
      } else if (ev.type === "tool_done") {
        const li = rows[liveKeyOf(ev)];
        if (li) {
          li.className = "live-row " + (ev.status === "success" ? "done" : "failed");
          li.innerHTML = toolDoneHtml(ev);
        }
      }
    };

    let finalPayload = null;
    try {
      const res = await fetch(url, {
        method: "POST", headers: headers || {}, body: JSON.stringify(body),
      });
      if (!res.ok || !res.body) {
        head.textContent = "Request failed (" + res.status + ")";
        return null;
      }
      finalPayload = await readNdjsonStream(res, onEvent);
    } catch (err) {
      head.textContent = "Error: " + err;
      return null;
    }
    head.textContent = "Done";
    return finalPayload;
  };

  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    addUserBubble({ door: "chat", text: msg });
    await streamTurnInto({
      url: "/api/chat/stream",
      body: { message: msg, session_id: sessionId },
      door: "chat",
      statusLabel: "Working…",
    });
  });

  function metaForPayload(p) {
    const out = [];
    if (p.latency_ms != null) out.push(p.latency_ms + " ms");
    if (p.request_id) out.push(p.request_id);
    if (p.trace_id) {
      // Emit the FULL 32-char W3C trace ID — the same value Tempo
      // stores. Pasting it into Grafana → Explore → Tempo (TraceQL
      // search by Trace ID) resolves directly. The previous 10-char
      // slice was a copy-paste-broken truncation.
      // Also render as a clickable deep-link into the local Grafana
      // Tempo Explore view so the user can jump to the trace in one
      // click. Grafana on :3001, Tempo datasource pre-wired.
      const tid = p.trace_id;
      const grafanaUrl = "http://localhost:3001/explore?orgId=1&left=" +
        encodeURIComponent(JSON.stringify({
          datasource: "Tempo",
          queries: [{ query: tid, queryType: "traceql" }],
          range: { from: "now-1h", to: "now" },
        }));
      out.push({
        html: 'trace <a href="' + grafanaUrl + '" target="_blank" ' +
              'rel="noopener" title="Open in Grafana Tempo" ' +
              'class="trace-link">' + tid + '</a>',
      });
    }
    if (p.final_status) out.push(p.final_status);
    const cacheStatus = detectCacheStatus(p);
    if (cacheStatus) out.push(cacheStatus);
    return out;
  }

  // The cache-aside flow (UC-1 E3) wraps the LLM call. Each step's
  // `output.cache_hit` carries the hit/miss signal; on hit, `cache_age_s`
  // is the cached entry's age. We also still recognise the bare cache-tool
  // outcomes (`outcome="hit"|"miss"`) for direct cache-tool invocations.
  function detectCacheStatus(p) {
    for (const r of p.step_results || []) {
      const o = r.output || {};
      if (o.cache_hit === true) {
        const age = (o.cache_age_s == null) ? "?" : o.cache_age_s;
        return `cache hit · age ${age}s`;
      }
      if (o.cache_hit === false) return "cache miss";
      if (o.outcome === "hit") return `cache hit · age ${o.age_s ?? "?"}s`;
      if (o.outcome === "miss") return "cache miss";
    }
    return null;
  }

  // Counters update at the END of every turn — call this from chat + fast-path.
  function tallyTurn(p) {
    counters.turns += 1;
    if (detectCacheStatus(p)?.startsWith("cache hit")) counters.cacheHits += 1;
    lastLatencyEl.textContent = (p.latency_ms == null ? "—" : p.latency_ms + " ms");
    renderCounters();
  }

  // ── fast-path button door (top-right + inline action chips) ─────────

  async function loadFastPathActions() {
    try {
      const h = await fetch("/api/health").then((r) => r.json());
      const ucs = h.fast_path_eligible || [];
      const specs = await Promise.all(
        ucs.map((id) => fetch(`/api/fast/${encodeURIComponent(id)}/spec`)
                         .then((r) => r.json())));
      fastPathActions.innerHTML = "";
      specs.forEach((spec) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "action-btn";
        btn.innerHTML = `<span class="icon">●</span>${ucLabel(spec)}`;
        btn.addEventListener("click", () => openFastPathModal(spec, {}));
        fastPathActions.appendChild(btn);
      });
    } catch (err) {
      fastPathActions.innerHTML = "";
      setStatus("Failed to load fast-path UCs: " + err, "error");
    }
  }

  function openFastPathModal(spec, prefill) {
    const modal = $("#fast-path-modal");
    const title = $("#fp-title");
    const form = $("#fp-form");
    title.textContent = "Run " + ucLabel(spec);
    form.innerHTML = "";

    // Only show fields the user must supply. Fields with auto-derived
    // service ids etc. are hidden from the UI by design (the dispatcher
    // fills them in).
    const visibleFields = spec.input_fields.filter((f) => !f.auto_derive_from);

    const inputs = {};
    visibleFields.forEach((f) => {
      const lab = document.createElement("label");
      lab.textContent = `${humanise(f.name)} (${f.type})${f.required ? " *" : ""}`;
      const inp = document.createElement("input");
      inp.name = f.name;
      inp.required = f.required;
      inp.placeholder = f.description.slice(0, 80);
      if (prefill?.[f.name]) inp.value = prefill[f.name];
      lab.appendChild(inp);
      form.appendChild(lab);
      inputs[f.name] = inp;
    });

    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "submit";
    submit.textContent = "Run";
    form.appendChild(submit);

    form.onsubmit = async (e) => {
      e.preventDefault();
      const inputsValues = {};
      for (const [k, el] of Object.entries(inputs)) inputsValues[k] = el.value;
      modal.classList.add("hidden");
      await runFastPath(spec, inputsValues);
    };

    modal.classList.remove("hidden");
    setTimeout(() => {
      const first = form.querySelector("input");
      if (first) first.focus();
    }, 50);
  }
  $("#fp-close").addEventListener("click", () => {
    $("#fast-path-modal").classList.add("hidden");
  });

  async function runFastPath(spec, inputsValues) {
    // Render the fast-path turn as a natural user-said-this message
    // (matches the server-side `_humanise_fast_path_request` shape). The
    // first input value is the entity id (`ticket_id`, `article_id`, …);
    // the verb comes from the UC label.
    const firstValue = Object.values(inputsValues)[0] || "";
    const userText = `${ucLabel(spec)} "${firstValue}"`;
    addUserBubble({ door: "fast_path", text: userText });
    // Live streaming door — same panel as chat, so the button shows its
    // agents + tools working in real time too.
    await streamTurnInto({
      url: `/api/fast/${encodeURIComponent(spec.uc_id)}/stream`,
      body: { inputs: inputsValues, session_id: sessionId },
      door: "fast_path",
      statusLabel: `Running ${ucLabel(spec)}…`,
    });
  }

  loadFastPathActions();
})();
