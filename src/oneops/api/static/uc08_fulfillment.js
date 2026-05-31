// UC-8 Catalog Fulfillment UI — production-grade button mode.
//
// Endpoints:
//   POST /api/uc08/create-sr   → mints SR id, LLM-extracts title+desc
//   POST /api/uc08/match       → semantic + reranker + enrichment
//   POST /api/uc08/fulfill     → starts the workflow
//   GET  /api/uc08/status/{id} → progress polling
//
// Single-modal, step-driven UI. Each step replaces the modal body.
// Steps: 1.compose 2.match 3.progress 4.completion 5.error
//
// Mirrors the structure of uc02_similar.js + uc05_triage.js.

(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

  function headers() {
    const t = $("#tenant"), u = $("#user"), r = $("#role");
    return {
      "Content-Type": "application/json",
      "x-tenant-id": (t && t.value.trim()) || "T001",
      "x-user-id":   (u && u.value.trim()) || "USR00001",
      "x-role":      (r && r.value.trim()) || "service_desk_agent",
    };
  }

  // ── Mount points ────────────────────────────────────────────────────
  const openBtn  = $("#open-fulfillment");
  const modal    = $("#fulfillment-modal");
  const body     = $("#fulfillment-body");
  const closeBtn = $("#fulfillment-close");
  if (!openBtn || !modal || !body) return;

  // ── State ──────────────────────────────────────────────────────────
  const state = {
    step: "compose",
    sr:        null,   // { request_id, title, description, ... }
    match:     null,   // /match response
    ritm:      null,   // { ritm_id, run_id, ... }
    pollTimer: null,
  };

  function reset() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    state.step = "compose";
    state.sr = null;
    state.match = null;
    state.ritm = null;
    render();
  }

  function openModal()  { modal.classList.remove("hidden"); reset(); }
  function closeModal() { modal.classList.add("hidden"); reset(); }
  openBtn.addEventListener("click", openModal);
  closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  // ── Tiny helpers ───────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function pct(v) { return Math.round(v * 100); }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "POST",
      headers: headers(),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data;
    try { data = await res.json(); } catch (_) { data = { _raw: await res.text() }; }
    if (!res.ok) {
      const e = new Error(`HTTP ${res.status}: ${data.detail || data._raw || ""}`);
      e.status = res.status;
      e.data = data;
      throw e;
    }
    return data;
  }

  // ═════════════════════════════════════════════════════════════════
  // STEP 1 — Compose: textarea + Auto-create SR
  // ═════════════════════════════════════════════════════════════════
  function renderCompose() {
    body.innerHTML = `
      <div class="uc08-step uc08-compose">
        <p class="uc08-help">
          Describe what you need in plain language. The system will:
          (1) create a Service Request,
          (2) match it to a catalog item,
          (3) let you review &amp; edit values,
          (4) execute the fulfillment workflow.
        </p>

        <label for="uc08-text" class="uc08-label">Your request</label>
        <textarea id="uc08-text" rows="5"
          placeholder="Onboard our new senior dev Maria starting Monday in the engineering team — full kit please."></textarea>

        <div class="uc08-actions">
          <button type="button" class="ghost"
            id="uc08-cancel">Cancel</button>
          <button type="button" class="primary"
            id="uc08-create-sr">✨ Auto-create SR &amp; find match</button>
        </div>

        <div id="uc08-error" class="uc08-error hidden"></div>
      </div>
    `;
    $("#uc08-cancel").addEventListener("click", closeModal);
    $("#uc08-create-sr").addEventListener("click", onCreateSr);
    $("#uc08-text").focus();
  }

  async function onCreateSr() {
    const text = ($("#uc08-text").value || "").trim();
    const err  = $("#uc08-error");
    err.classList.add("hidden");
    if (!text) {
      err.textContent = "Please describe what you need.";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("#uc08-create-sr");
    btn.disabled = true;
    btn.textContent = "⏳ Creating SR + finding match…";
    try {
      const sr = await api("/api/uc08/create-sr", { body: { user_text: text } });
      state.sr = sr;
      // chain straight into match
      const match = await api("/api/uc08/match", {
        body: {
          sr_title: sr.title,
          sr_description: sr.description,
          top_k: 5,
        },
      });
      state.match = match;
      state.step = "match";
      render();
    } catch (e) {
      err.textContent = `${e.message || e}`;
      err.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = "✨ Auto-create SR & find match";
    }
  }

  // ═════════════════════════════════════════════════════════════════
  // STEP 2 — Match suggestion + editable variables form
  // ═════════════════════════════════════════════════════════════════
  function renderMatch() {
    const sr = state.sr;
    const m  = state.match;
    if (m.verdict === "WRONG_INTENT") return renderWrongIntent(sr, m);
    if (m.verdict === "NO_MATCH" || (m.candidates || []).length === 0) {
      return renderNoMatch(sr, m);
    }

    const chosen = m.auto_pick || (m.candidates && m.candidates[0]) || null;
    const e = m.enrichment || null;

    body.innerHTML = `
      <div class="uc08-step uc08-match">
        <div class="uc08-sr-pill">
          🆕 ${escapeHtml(sr.request_id)} &nbsp;|&nbsp;
          status: <strong>${escapeHtml(sr.status)}</strong>
          / stage: <strong>${escapeHtml(sr.stage)}</strong>
        </div>

        <h4>${m.rerank_used ? "🤖 Best match" : "🎯 Best match"}
          <span class="uc08-confidence">
            ${m.rerank_used ?
              `(rerank confidence ${pct(m.rerank_confidence)}%)` :
              `(cosine ${chosen ? chosen.cosine_score.toFixed(2) : "—"})`}
          </span>
        </h4>

        <div class="uc08-catalog-card">
          <div class="uc08-cat-id">📦 ${escapeHtml(chosen ? chosen.catalog_item_id : "")}</div>
          <div class="uc08-cat-name">${escapeHtml(chosen ? chosen.name : "")}</div>
          <div class="uc08-cat-desc">${escapeHtml(chosen ? chosen.description : "")}</div>
          <div class="uc08-cat-meta">
            Category: <strong>${escapeHtml(chosen ? chosen.category : "")}</strong>
            &nbsp;·&nbsp; Owner: <strong>${escapeHtml(chosen ? chosen.owner_group : "")}</strong>
          </div>
          ${m.rerank_reasoning ? `
          <div class="uc08-reasoning">
            <em>Reasoning:</em> ${escapeHtml(m.rerank_reasoning)}
          </div>` : ""}
        </div>

        ${(m.candidates || []).length > 1 ? `
        <details class="uc08-alts">
          <summary>Other candidates (${m.candidates.length - 1})</summary>
          <ul>
            ${m.candidates.slice(1).map(c => `
              <li>
                <code>${escapeHtml(c.catalog_item_id)}</code>
                — ${escapeHtml(c.name)}
                <span class="uc08-cos">${c.cosine_score.toFixed(2)}</span>
              </li>
            `).join("")}
          </ul>
        </details>` : ""}

        ${renderEnrichmentForm(sr, chosen, e)}

        <div class="uc08-actions">
          <button type="button" class="ghost" id="uc08-back">← Back</button>
          <button type="button" class="primary" id="uc08-proceed">
            ✅ Proceed with these values
          </button>
        </div>

        <div id="uc08-error" class="uc08-error hidden"></div>
      </div>
    `;

    $("#uc08-back").addEventListener("click", () => {
      state.step = "compose"; render();
    });
    $("#uc08-proceed").addEventListener("click", onProceed);
  }

  function renderEnrichmentForm(sr, chosen, e) {
    if (!e || !chosen) {
      return `<div class="uc08-help">
        No enrichment data — fill values manually after proceeding.
      </div>`;
    }
    const hist = (h) => {
      if (!h) return "";
      const val = h.value || "";
      const label = h.evidence_label || "";
      return val
        ? `<span class="uc08-hist-evidence">📚 ${escapeHtml(label)}</span>`
        : `<span class="uc08-hist-evidence uc08-no-history">📚 ${escapeHtml(label)}</span>`;
    };

    return `
      <h4>Review &amp; edit values</h4>
      <p class="uc08-help">
        🤖 AI-derived · 📚 Suggested from history · 📅 Computed from catalog
      </p>

      <div class="uc08-fields">
        <label class="uc08-row">
          <span class="uc08-row-label">Title (AI)</span>
          <input id="uc08-f-title" type="text" maxlength="120"
                 value="${escapeHtml(sr.title)}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">Description (AI verbatim)</span>
          <textarea id="uc08-f-description" rows="2"
                    maxlength="4000">${escapeHtml(sr.description)}</textarea>
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">Requested for</span>
          <input id="uc08-f-requested-for" type="text"
                 placeholder="USR00007" value="${escapeHtml(sr.requested_for || "")}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">📅 Category (from catalog)</span>
          <input id="uc08-f-category" type="text" readonly
                 value="${escapeHtml(e.category || "")}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">📅 Assignment group (catalog)</span>
          <input id="uc08-f-assignment-group" type="text"
                 value="${escapeHtml(e.assignment_group_from_catalog || "")}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">
            📚 Assigned to ${hist(e.assigned_to)}
          </span>
          <input id="uc08-f-assigned-to" type="text"
                 placeholder="USR…"
                 value="${escapeHtml((e.assigned_to && e.assigned_to.value) || "")}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">
            📚 Approved by ${hist(e.approved_by)}
          </span>
          <input id="uc08-f-approved-by" type="text"
                 placeholder="USR…"
                 value="${escapeHtml((e.approved_by && e.approved_by.value) || "")}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">
            📚 CI id ${hist(e.ci_id)}
          </span>
          <input id="uc08-f-ci-id" type="text"
                 placeholder="CI…"
                 value="${escapeHtml((e.ci_id && e.ci_id.value) || "")}">
        </label>

        <label class="uc08-row">
          <span class="uc08-row-label">📅 SLA due</span>
          <input id="uc08-f-sla-due" type="text" readonly
                 value="${escapeHtml(e.sla_due_iso || "")}">
        </label>

        <div class="uc08-priority-row">
          <span class="uc08-row-label">🤖 Priority (from matrix)</span>
          <span class="uc08-priority-pill">${escapeHtml(e.priority_p_letter || "")}
            (${escapeHtml(e.priority_canonical || "")})</span>
          <span class="uc08-priority-inputs">
            impact: <strong>${escapeHtml(e.impact || "")}</strong>
            · urgency: <strong>${escapeHtml(e.urgency || "")}</strong>
          </span>
        </div>
      </div>
    `;
  }

  function renderWrongIntent(sr, m) {
    body.innerHTML = `
      <div class="uc08-step uc08-empty">
        <div class="uc08-sr-pill">🆕 ${escapeHtml(sr.request_id)}</div>
        <h4>This doesn't look like a catalog request</h4>
        <p>${escapeHtml(m.rerank_reasoning || "The reranker classified this as a non-fulfilment intent.")}</p>
        <p>Did you mean to:</p>
        <ul>
          <li>Search for a knowledge article? Use the chat → KB lookup</li>
          <li>Report an incident? Use the Triage AI button</li>
        </ul>
        <div class="uc08-actions">
          <button type="button" class="ghost" id="uc08-back">← Back</button>
          <button type="button" class="primary" id="uc08-close">Done</button>
        </div>
      </div>
    `;
    $("#uc08-back").addEventListener("click", () => { state.step="compose"; render(); });
    $("#uc08-close").addEventListener("click", closeModal);
  }

  function renderNoMatch(sr, m) {
    body.innerHTML = `
      <div class="uc08-step uc08-empty">
        <div class="uc08-sr-pill">🆕 ${escapeHtml(sr.request_id)}</div>
        <h4>No matching catalog item</h4>
        <p>The catalog doesn't have an item that matches your request. The SR
           was saved so service desk can pick it up manually.</p>
        <div class="uc08-actions">
          <button type="button" class="ghost" id="uc08-back">← Try again</button>
          <button type="button" class="primary" id="uc08-close">Done</button>
        </div>
      </div>
    `;
    $("#uc08-back").addEventListener("click", () => { state.step="compose"; render(); });
    $("#uc08-close").addEventListener("click", closeModal);
  }

  // ═════════════════════════════════════════════════════════════════
  // STEP 3 — Proceed → fulfill → progress polling
  // ═════════════════════════════════════════════════════════════════
  async function onProceed() {
    const sr = state.sr;
    const m  = state.match;
    const chosen = m.auto_pick || (m.candidates && m.candidates[0]);
    if (!chosen) return;

    // Variables collected from the form go into the fulfill payload.
    const vars = {
      title:           $("#uc08-f-title").value.trim(),
      description:     $("#uc08-f-description").value.trim(),
      requested_for:   $("#uc08-f-requested-for").value.trim(),
      assignment_group: $("#uc08-f-assignment-group").value.trim(),
      assigned_to:     $("#uc08-f-assigned-to").value.trim(),
      approved_by:     $("#uc08-f-approved-by").value.trim(),
      ci_id:           $("#uc08-f-ci-id").value.trim(),
    };
    Object.keys(vars).forEach(k => { if (!vars[k]) delete vars[k]; });

    const err = $("#uc08-error");
    err.classList.add("hidden");
    const btn = $("#uc08-proceed");
    btn.disabled = true; btn.textContent = "⏳ Starting fulfillment…";

    try {
      const resp = await api("/api/uc08/fulfill", { body: {
        request_id:      sr.request_id,
        catalog_item_id: chosen.catalog_item_id,
        variables:       vars,
        requested_for:   vars.requested_for || undefined,
      }});
      state.ritm = resp;
      state.step = "progress";
      render();
    } catch (e) {
      err.textContent = `${e.message || e}`;
      err.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = "✅ Proceed with these values";
    }
  }

  function renderProgress() {
    const r = state.ritm;
    body.innerHTML = `
      <div class="uc08-step uc08-progress">
        <div class="uc08-sr-pill">
          🆕 ${escapeHtml(state.sr.request_id)} → RITM <strong>${escapeHtml(r.ritm_id)}</strong>
        </div>
        <h4>Fulfillment running</h4>
        <p class="uc08-help">${escapeHtml(r.display_text || "Workflow started.")}</p>
        <div id="uc08-tasks" class="uc08-tasks">
          <div class="uc08-spinner">⏳ Loading status…</div>
        </div>
        <div class="uc08-actions">
          <button type="button" class="ghost" id="uc08-close">
            Close &amp; continue in background
          </button>
        </div>
      </div>
    `;
    $("#uc08-close").addEventListener("click", closeModal);
    pollStatus();
  }

  async function pollStatus() {
    const r = state.ritm;
    if (!r) return;
    try {
      const s = await api(`/api/uc08/status/${encodeURIComponent(r.ritm_id)}`,
                         { method: "GET" });
      renderTaskRows(s);
      // Continue polling unless terminal
      const term = ["fulfilled", "failed", "cancelled", "partial"];
      if (!term.includes(s.state)) {
        state.pollTimer = setTimeout(pollStatus, 2000);
      } else {
        state.completion = s;
        setTimeout(() => { state.step = "completion"; render(); }, 800);
      }
    } catch (e) {
      const out = $("#uc08-tasks");
      if (out) out.innerHTML = `<div class="uc08-error">${escapeHtml(e.message)}</div>`;
      // Retry after a longer pause on transient error
      state.pollTimer = setTimeout(pollStatus, 5000);
    }
  }

  function renderTaskRows(s) {
    const out = $("#uc08-tasks");
    if (!out) return;
    const tasksByState = s.tasks_by_state || {};
    const done = tasksByState.done || 0;
    const total = s.tasks_total || 0;
    const inProgress = tasksByState.in_progress || 0;
    const failed = tasksByState.failed || 0;
    const blocked = tasksByState.blocked || 0;
    out.innerHTML = `
      <div class="uc08-progress-summary">
        <div class="uc08-bar">
          <div class="uc08-bar-fill" style="width: ${total ? (done/total*100) : 0}%"></div>
        </div>
        <div class="uc08-counts">
          ✅ ${done}/${total} done
          ${inProgress ? ` · ⏳ ${inProgress} in progress` : ""}
          ${blocked ? ` · ⏸ ${blocked} blocked` : ""}
          ${failed ? ` · ❌ ${failed} failed` : ""}
        </div>
      </div>
      <div class="uc08-state-label">RITM state: <strong>${escapeHtml(s.state)}</strong></div>
      ${s.pending_approvals && s.pending_approvals.length ?
        `<div class="uc08-approvals">⚠️ Awaiting approval(s): ${
          s.pending_approvals.map(a => `<code>${escapeHtml(a)}</code>`).join(", ")
        }</div>` : ""}
    `;
  }

  // ═════════════════════════════════════════════════════════════════
  // STEP 4 — Completion
  // ═════════════════════════════════════════════════════════════════
  function renderCompletion() {
    const s = state.completion;
    const ok = s && s.state === "fulfilled";
    const partial = s && (s.state === "partial" || (s.tasks_by_state && s.tasks_by_state.failed > 0));
    body.innerHTML = `
      <div class="uc08-step uc08-completion">
        <div class="uc08-sr-pill">
          🆕 ${escapeHtml(state.sr.request_id)} → RITM <strong>${escapeHtml(state.ritm.ritm_id)}</strong>
        </div>
        <h4>${ok ? "🎉 Fulfillment complete" :
                  partial ? "🟡 Partially complete" :
                  "❌ Fulfillment failed"}</h4>
        <p class="uc08-help">${escapeHtml((s && s.display_text) || "")}</p>
        <ul class="uc08-final-counts">
          <li>✅ Done: ${(s && s.tasks_by_state && s.tasks_by_state.done) || 0} of ${(s && s.tasks_total) || 0}</li>
          ${(s && s.tasks_by_state && s.tasks_by_state.failed) ?
            `<li>❌ Failed: ${s.tasks_by_state.failed}</li>` : ""}
          ${(s && s.tasks_by_state && s.tasks_by_state.skipped) ?
            `<li>⏸ Skipped: ${s.tasks_by_state.skipped}</li>` : ""}
        </ul>
        <div class="uc08-actions">
          <button type="button" class="primary" id="uc08-done">Done</button>
        </div>
      </div>
    `;
    $("#uc08-done").addEventListener("click", closeModal);
  }

  // ── Dispatcher ──────────────────────────────────────────────────
  function render() {
    if (state.step === "compose")     return renderCompose();
    if (state.step === "match")       return renderMatch();
    if (state.step === "progress")    return renderProgress();
    if (state.step === "completion")  return renderCompletion();
    renderCompose();
  }
})();
