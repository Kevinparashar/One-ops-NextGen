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
  let fastPathSpecs = [];                   // populated by loadFastPathActions
  let counters = { turns: 0, cacheHits: 0 };
  bootstrap();

  function renderSession() {
    sessionLabel.textContent = sessionId || "—";
  }

  function loadStoredSessionId() {
    try {
      const v = window.localStorage.getItem(LS_KEY);
      return (typeof v === "string" && v.startsWith("sess_")) ? v : null;
    } catch (_e) { return null; }
  }
  function saveSessionId(id) {
    try { window.localStorage.setItem(LS_KEY, id); } catch (_e) { /* ignore */ }
  }
  function clearStoredSessionId() {
    try { window.localStorage.removeItem(LS_KEY); } catch (_e) { /* ignore */ }
  }

  // POST /api/sessions — mint a fresh server-side session.
  async function mintServerSession() {
    try {
      const res = await fetch("/api/sessions", {
        method: "POST", headers: envelopeHeaders(),
      });
      if (!res.ok) return null;
      const payload = await res.json();
      return (payload && payload.session_id) || null;
    } catch (_e) { return null; }
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
    } catch (_e) { return false; }
  }

  // GET /api/sessions/{id} — true iff server still has this session active.
  async function isServerSessionAlive(id) {
    if (!id) return false;
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(id)}`,
                              { headers: envelopeHeaders() });
      return res.ok;
    } catch (_e) { return false; }
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
      window.localStorage.setItem("_oneops_probe", "1");
      window.localStorage.removeItem("_oneops_probe");
      return true;
    } catch (_e) { return false; }
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
    if (!fresh) {
      // Lifecycle endpoint unreachable — fall back to a transient client id
      // so the user is never stuck. The chat path also tolerates this.
      sessionId = "sess_" + Math.random().toString(36).slice(2, 14);
    } else {
      sessionId = fresh;
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
    } catch (_e) { /* fall through with empty rows */ }
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
    } catch (_e) { /* ignore — refresh will reflect server reality */ }
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

  async function loadStatusStrip() {
    try {
      const cfg = await fetch("/api/config").then((r) => r.json());
      statusStrip.innerHTML = "";
      addChip({
        label: "Cache",
        state: cfg.cache.enabled ? "on" : "off",
        value: cfg.cache.backend.replace(/^In/, "").replace(/SummaryCacheStore$/, ""),
      });
      addChip({
        label: "OTel",
        state: cfg.otel.in_memory_spans ? (cfg.otel.enabled ? "on" : "warn") : "off",
        value: cfg.otel.endpoint
                 ? "exporter live"
                 : (cfg.otel.in_memory_spans ? "in-memory only" : "off"),
      });
      addChip({
        label: "LLM",
        state: cfg.llm_gateway.summarizer_wired
                 ? "on"
                 : (cfg.llm_gateway.configured ? "warn" : "off"),
        value: cfg.llm_gateway.summarizer_wired
                 ? "wired"
                 : (cfg.llm_gateway.configured ? "gateway up · summarizer pending"
                                              : "not configured"),
      });
      addChip({
        label: "DB",
        state: cfg.postgres.configured
                 ? (cfg.postgres.backend_in_use.startsWith("Postgres") ? "on" : "warn")
                 : "off",
        value: cfg.postgres.backend_in_use,
      });
      addChip({
        label: "NATS",
        state: cfg.nats.configured
                 ? (cfg.nats.wired_into_ingress ? "on" : "warn")
                 : "off",
        value: cfg.nats.wired_into_ingress ? "wired" : (cfg.nats.configured
                 ? "configured · ingress in-process"
                 : "not configured"),
      });
      addChip({
        label: "Session",
        state: cfg.session?.wired ? "on" : "off",
        value: cfg.session?.wired
                 ? (cfg.session.durable_across_reload
                      ? "durable · " + cfg.session.backend.replace(/^In/, "")
                      : cfg.session.backend)
                 : "not wired",
      });
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
    } catch (_e) {
      return escapeHtml(src);
    }
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;",
    }[c]));
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
      const msg = (s && s.output && s.output.message) || "";
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
      return wrapper;
    }

    // Single-step (or empty) path — original behavior.
    const single = renderSingleStep(payload, steps[0] || {});
    while (single.firstChild) wrapper.appendChild(single.firstChild);
    return wrapper;
  }

  function renderSingleStep(payload, step) {
    const wrapper = document.createElement("div");
    wrapper.className = "response-card";
    const out = step.output || {};

    // ── error / denial / handler-outcome path ─────────────────────────
    // When the step failed OR the handler returned a non-success outcome,
    // the engine's `final_response` already carries the friendly message
    // (built by `friendly_step_response` in the aggregator). Render it
    // verbatim — no empty Summary / Key Details sections.
    const stepStatus = (step.status || "").toLowerCase();
    const handlerOutcome = (out && out.outcome) ? String(out.outcome).toLowerCase() : "";
    const isSuccessfulSummary =
      stepStatus === "success" &&
      handlerOutcome !== "not_found" &&
      handlerOutcome !== "invalid_request" &&
      handlerOutcome !== "llm_unavailable" &&
      handlerOutcome !== "denied";
    if (!isSuccessfulSummary) {
      // Prefer the per-step message so multi-step turns don't duplicate
      // the entire aggregated final_response on every card.
      const stepMessage = (out && out.message) || payload.final_response || "(no response)";
      const p = document.createElement("div");
      p.className = "summary-text bubble md";
      p.style.background = "transparent";
      p.style.border = "0";
      p.style.padding = "0";
      p.innerHTML = renderMarkdown(stepMessage);
      wrapper.appendChild(p);
      return wrapper;
    }

    // ── success path ── display_text first (canonical chat-ready string) ─
    // Any UC tool whose response shape is opinionated (UC-2 ranked similar
    // tickets, UC-3 KB answer composed verbatim, future UCs) emits
    // `out.display_text`. The frontend renders it as markdown and stops —
    // the tool already composed the user-facing text and we surface it
    // verbatim. Matches the executor's `friendly_step_response` contract
    // (see oneops.executor.nodes — display_text takes precedence over the
    // UC-1-specific Summary/Key Details blocks).
    if (typeof out.display_text === "string" && out.display_text.trim()) {
      const p = document.createElement("div");
      p.className = "summary-text bubble md";
      p.style.background = "transparent";
      p.style.border = "0";
      p.style.padding = "0";
      p.innerHTML = renderMarkdown(out.display_text);
      wrapper.appendChild(p);
      return wrapper;
    }

    // ── success path — render Summary + Key Details (UC-1 shape) ──────
    const summaryBlock = out.summary || null;
    const record = out.record || (summaryBlock && summaryBlock.record) || null;
    const keyDetails =
      (summaryBlock && summaryBlock.key_details) ||
      out.key_details ||
      null;

    // ── entity header (if we have a record) ──────────────────────────
    const entityId = record?.incident_id || record?.request_id ||
                     record?.problem_id || record?.change_id ||
                     record?.asset_id || record?.ci_id || record?.kb_id;
    const entityTitle = record?.title || record?.asset_name ||
                        record?.ci_name || record?.summary;
    const serviceId = step.parameters?.service_id || out.service_id ||
                      summaryBlock?.service_id;
    if (entityId || entityTitle) {
      const header = document.createElement("div");
      header.className = "entity-header";
      if (entityId) {
        const e = document.createElement("span");
        e.className = "entity-id";
        e.textContent = entityId;
        header.appendChild(e);
      }
      if (entityTitle) {
        const t = document.createElement("span");
        t.className = "entity-title";
        t.textContent = entityTitle;
        header.appendChild(t);
      }
      if (serviceId) {
        const s = document.createElement("span");
        s.className = "entity-service";
        s.textContent = serviceId;
        header.appendChild(s);
      }
      wrapper.appendChild(header);
    }

    // ── summary paragraph (LLM-generated, rendered as markdown) ─────
    const summaryText = (typeof summaryBlock === "string")
      ? summaryBlock
      : summaryBlock?.summary;
    if (summaryText) {
      // Field-read responses are one-line answers; the "Summary" header
      // is misleading and the Key Details box below would just repeat
      // the same value. Render as a plain answer line.
      if (handlerOutcome !== "field_read") {
        const h = document.createElement("div");
        h.className = "section-title";
        h.textContent = "Summary";
        wrapper.appendChild(h);
      }
      const p = document.createElement("div");
      p.className = "summary-text bubble md";
      p.style.background = "transparent";
      p.style.border = "0";
      p.style.padding = "0";
      p.innerHTML = renderMarkdown(summaryText);
      wrapper.appendChild(p);
    } else if (out && out.message) {
      const h = document.createElement("div");
      h.className = "section-title";
      h.textContent = "Response";
      wrapper.appendChild(h);
      const p = document.createElement("div");
      p.className = "summary-text bubble md";
      p.style.background = "transparent";
      p.style.border = "0";
      p.style.padding = "0";
      p.innerHTML = renderMarkdown(out.message);
      wrapper.appendChild(p);
    }

    // ── key details (every field of the record, human-readable) ──────
    // Preferred: the LLM summariser already produced `key_details` with
    // service-aware labels (e.g. "Incident ID", "Assigned Group"). Fall
    // back to raw record + naive humanisation when the LLM half hasn't
    // populated yet (e.g. cache miss with gateway down).
    let kv = keyDetails;
    if (!kv && record) {
      kv = {};
      for (const [k, v] of Object.entries(record)) {
        if (k.startsWith("_") || v == null) continue;
        if (Array.isArray(v) && v.length === 0) continue;
        if (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) continue;
        kv[humanise(k)] = v;
      }
    }
    if (kv && Object.keys(kv).length) {
      const h = document.createElement("div");
      h.className = "section-title";
      h.textContent = "Key Details";
      wrapper.appendChild(h);
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

    // (No inline action chips — fast-path buttons live in the workspace
    // header, not under every assistant message. Keeps the conversation
    // readable.)

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
    return String(key).replace(/_/g, " ").replace(/^(\w)/, (m) => m.toUpperCase());
  }
  function humaniseUcId(ucId) {
    // uc01_summarization -> "Summarization"
    const tail = String(ucId).split("_").slice(1).join(" ");
    return tail ? tail.replace(/^(\w)/, (m) => m.toUpperCase()) : ucId;
  }

  // ── chat door ────────────────────────────────────────────────────────

  $("#chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    addUserBubble({ door: "chat", text: msg });
    const pending = addPending({ door: "chat", text: "thinking…" });
    setStatus("Submitting chat turn…", "busy");
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: envelopeHeaders(),
        body: JSON.stringify({ message: msg, session_id: sessionId }),
      });
      const payload = await res.json();
      pending.remove();
      if (!res.ok) {
        addAssistantBubble({ door: "chat", error: true,
          text: `HTTP ${res.status}: ${payload.detail || res.statusText}` });
        setStatus("Chat turn failed.", "error");
      } else {
        addAssistantBubble({
          door: "chat",
          meta: metaForPayload(payload),
          content: renderResponseContent(payload),
        });
        sessionId = payload.session_id || sessionId;
        saveSessionId(sessionId);
        renderSession();
        tallyTurn(payload);
        setStatus("Ready.");
        refreshThreadList();
      }
    } catch (err) {
      pending.remove();
      addAssistantBubble({ door: "chat", error: true, text: String(err) });
      setStatus("Chat error: " + err, "error");
    }
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
        const age = (o.cache_age_s != null) ? o.cache_age_s : "?";
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
    lastLatencyEl.textContent = (p.latency_ms != null ? p.latency_ms + " ms" : "—");
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
      fastPathSpecs = specs;
      fastPathActions.innerHTML = "";
      specs.forEach((spec) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "action-btn";
        btn.innerHTML = `<span class="icon">●</span>${humaniseUcId(spec.uc_id)}`;
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
    title.textContent = "Run " + humaniseUcId(spec.uc_id);
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
      if (prefill && prefill[f.name]) inp.value = prefill[f.name];
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
    const userText = `${humaniseUcId(spec.uc_id)} "${firstValue}"`;
    addUserBubble({ door: "fast_path", text: userText });
    const pending = addPending({ door: "fast_path", text: "running…" });
    setStatus(`Running ${spec.uc_id}…`, "busy");
    try {
      const res = await fetch(`/api/fast/${encodeURIComponent(spec.uc_id)}`, {
        method: "POST", headers: envelopeHeaders(),
        body: JSON.stringify({ inputs: inputsValues, session_id: sessionId }),
      });
      const payload = await res.json();
      pending.remove();
      if (!res.ok) {
        addAssistantBubble({ door: "fast_path", error: true,
          text: `HTTP ${res.status}: ${payload.detail || res.statusText}` });
        setStatus(spec.uc_id + " failed.", "error");
      } else {
        addAssistantBubble({
          door: "fast_path",
          meta: metaForPayload(payload),
          content: renderResponseContent(payload),
        });
        sessionId = payload.session_id || sessionId;
        saveSessionId(sessionId);
        renderSession();
        tallyTurn(payload);
        setStatus("Ready.");
        refreshThreadList();
      }
    } catch (err) {
      pending.remove();
      addAssistantBubble({ door: "fast_path", error: true, text: String(err) });
      setStatus("Error: " + err, "error");
    }
  }

  loadFastPathActions();
})();
