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

  function headers() {
    const t = $("#tenant"), u = $("#user"), r = $("#role");
    return {
      "Content-Type": "application/json",
      "x-tenant-id": t?.value.trim() || "T001",
      "x-user-id":   u?.value.trim() || "USR00001",
      "x-role":      r?.value.trim() || "service_desk_agent",
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
  // Close on backdrop click ONLY when both mousedown AND mouseup landed
  // on the backdrop. Otherwise a text-selection drag (mousedown inside,
  // mouseup outside) would close the modal mid-copy.
  let _backdropDown = false;
  modal.addEventListener("mousedown", (e) => {
    _backdropDown = (e.target === modal);
  });
  modal.addEventListener("mouseup", (e) => {
    if (_backdropDown && e.target === modal) closeModal();
    _backdropDown = false;
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
    // Read the body ONCE as text, then try to parse as JSON. Calling
    // .json() and .text() both consumes the stream — the second call
    // throws "body stream already read".
    const raw = await res.text();
    let data;
    try { data = raw ? JSON.parse(raw) : {}; } catch { data = { _raw: raw }; }
    if (!res.ok) {
      const detailMsg = typeof data.detail === "string"
        ? data.detail
        : (Array.isArray(data.detail) && data.detail[0]?.msg) || data._raw || res.statusText;
      const e = new Error(`HTTP ${res.status}: ${detailMsg}`);
      e.status = res.status;
      e.data = data;
      throw e;
    }
    return data;
  }

  // ═════════════════════════════════════════════════════════════════
  // STEP 1 — Compose: two distinct capabilities
  //   (A) Find match — read-only catalog lookup, no DB writes
  //   (B) Auto-create SR & match — persists SR then matches
  // ═════════════════════════════════════════════════════════════════
  function renderCompose() {
    body.innerHTML = `
      <div class="uc08-step uc08-compose">
        <p class="uc08-help">
          Describe what you need in plain language. Two capabilities:
          <br/><strong>Find match</strong> — preview catalog match without creating anything.
          <br/><strong>Auto-create SR &amp; match</strong> — persists a Service Request, matches it, lets you review &amp; fulfill.
        </p>

        <label for="uc08-text" class="uc08-label">Your request</label>
        <textarea id="uc08-text" rows="5"
          placeholder="Onboard our new senior dev Maria starting Monday in the engineering team — full kit please."></textarea>

        <div class="uc08-actions">
          <button type="button" class="ghost"
            id="uc08-cancel">Cancel</button>
          <button type="button" class="ghost"
            id="uc08-find-match"
            title="Read-only catalog lookup. Nothing is persisted.">🔍 Find match</button>
          <button type="button" class="primary"
            id="uc08-create-sr"
            title="Creates a Service Request, runs catalog match, lets you review & execute.">✨ Auto-create SR &amp; match</button>
        </div>

        <div id="uc08-error" class="uc08-error hidden"></div>
      </div>
    `;
    $("#uc08-cancel").addEventListener("click", closeModal);
    $("#uc08-find-match").addEventListener("click", onFindMatch);
    $("#uc08-create-sr").addEventListener("click", onCreateSr);
    $("#uc08-text").focus();
  }

  // (A) Find match — preview only, no SR persistence.
  async function onFindMatch() {
    const text = ($("#uc08-text").value || "").trim();
    const err  = $("#uc08-error");
    err.classList.add("hidden");
    if (!text) {
      err.textContent = "Please describe what you need.";
      err.classList.remove("hidden");
      return;
    }
    const btn = $("#uc08-find-match");
    btn.disabled = true;
    btn.textContent = "⏳ Finding match…";
    try {
      // Live agent/tool panel (shared with chat), then the match result.
      // Use the raw text as both title + description — /match doesn't
      // require a persisted SR. Mark state.sr as null so the next-step
      // render knows this is a preview (no Proceed button).
      body.innerHTML = "";
      const match = await globalThis.oneopsLiveStream({
        url: "/api/uc08/match/stream",
        body: {
          sr_title: text.slice(0, 120),
          sr_description: text,
          top_k: 4,
        },
        headers: headers(), mount: body,
      });
      if (!match || match.error || match.final_status === "failed") {
        err.textContent = match?.error || "match failed";
        err.classList.remove("hidden");
        btn.disabled = false;
        btn.textContent = "🔍 Find match";
        return;
      }
      state.sr = null;          // preview mode — no SR yet
      state.match = match;
      state.previewText = text; // keep so we can promote-to-SR later
      state.step = "match";
      render();                 // replaces body with the match result view
    } catch (e) {
      err.textContent = `${e.message || e}`;
      err.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = "🔍 Find match";
    }
  }

  // (B) Auto-create SR & match — persists SR then matches.
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
    btn.textContent = "⏳ Creating SR + matching…";
    try {
      const sr = await api("/api/uc08/create-sr", { body: { user_text: text } });
      state.sr = sr;
      state.previewText = null;
      const match = await api("/api/uc08/match", {
        body: {
          sr_title: sr.title,
          sr_description: sr.description,
          top_k: 4,
        },
      });
      state.match = match;
      state.step = "match";
      render();
    } catch (e) {
      err.textContent = `${e.message || e}`;
      err.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = "✨ Auto-create SR & match";
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

    const chosen = m.auto_pick || m.candidates?.[0] || null;
    const e = m.enrichment || null;

    const isPreview = !sr;

    const cosinePct = chosen ? Math.round(chosen.cosine_score * 100) : 0;
    const confPct = m.rerank_used ? pct(m.rerank_confidence) : cosinePct;
    const confLabel = m.rerank_used ? "rerank confidence" : "cosine similarity";
    let confTone = "low";
    if (confPct >= 80) confTone = "high";
    else if (confPct >= 60) confTone = "mid";
    const judgeTone = m.judge_verdict ? m.judge_verdict.toLowerCase() : "none";
    const judgePct = pct(m.judge_confidence || 0);

    body.innerHTML = `
      <div class="uc08-step uc08-match">
        ${isPreview ? `
        <div class="uc08-banner uc08-banner-preview">
          <span class="uc08-banner-icon">🔍</span>
          <span class="uc08-banner-text">
            <strong>Preview mode</strong> — nothing saved yet.
            Click <em>Create SR &amp; proceed</em> below to persist.
          </span>
        </div>` : `
        <div class="uc08-banner uc08-banner-sr">
          <span class="uc08-banner-icon">🆕</span>
          <span class="uc08-banner-text">
            SR <strong>${escapeHtml(sr.request_id)}</strong> created
            · status <span class="uc08-chip uc08-chip-status">${escapeHtml(sr.status)}</span>
            · stage <span class="uc08-chip uc08-chip-status">${escapeHtml(sr.stage)}</span>
          </span>
        </div>`}

        ${matchCatalogCardHtml(chosen, m, confPct, confLabel, confTone, judgeTone, judgePct)}

        ${matchAlternatesHtml(m)}

        ${isPreview ? "" : renderEnrichmentForm(sr, chosen, e)}

        <div class="uc08-actions">
          <button type="button" class="ghost" id="uc08-back">← Back</button>
          ${isPreview ? `
          <button type="button" class="primary" id="uc08-promote">
            ✨ Create SR &amp; proceed
          </button>` : `
          <button type="button" class="primary" id="uc08-proceed">
            ✅ Proceed with these values
          </button>`}
        </div>

        <div id="uc08-error" class="uc08-error hidden"></div>
      </div>
    `;

    $("#uc08-back").addEventListener("click", () => {
      state.step = "compose"; render();
    });
    if (isPreview) {
      $("#uc08-promote").addEventListener("click", onPromotePreviewToSr);
    } else {
      $("#uc08-proceed").addEventListener("click", onProceed);
    }
  }

  // Hero catalog card for the chosen match — extracted from renderMatch so its
  // inline conditionals (description, rerank reasoning, judge verdict) don't
  // pile onto renderMatch's cognitive complexity (S3776).
  function matchCatalogCardHtml(chosen, m, confPct, confLabel, confTone, judgeTone, judgePct) {
    return `
        <!-- ── Catalog card (hero) ───────────────────────────── -->
        <div class="uc08-catalog-card uc08-catalog-hero">
          <div class="uc08-cat-header">
            <div class="uc08-cat-icon">📦</div>
            <div class="uc08-cat-titles">
              <div class="uc08-cat-name">${escapeHtml(chosen ? chosen.name : "—")}</div>
              <div class="uc08-cat-id-line">
                <code class="uc08-cat-id-code">${escapeHtml(chosen ? chosen.catalog_item_id : "")}</code>
                ${chosen ? `
                <span class="uc08-chip uc08-chip-category">${escapeHtml(chosen.category || "uncategorised")}</span>
                <span class="uc08-chip uc08-chip-owner">👥 ${escapeHtml(chosen.owner_group || "—")}</span>` : ""}
              </div>
            </div>
          </div>

          ${chosen?.description ? `
          <p class="uc08-cat-desc">${escapeHtml(chosen.description)}</p>` : ""}

          <!-- Confidence row -->
          <div class="uc08-conf-row">
            <div class="uc08-conf-label">
              ${m.rerank_used ? "🤖 LLM rerank" : "🎯 Embedding match"}
              <span class="uc08-conf-sub">(${confLabel})</span>
            </div>
            <div class="uc08-conf-bar-wrap">
              <div class="uc08-conf-bar uc08-conf-bar-${confTone}" style="width:${confPct}%"></div>
              <span class="uc08-conf-pct">${confPct}%</span>
            </div>
          </div>

          ${m.rerank_reasoning ? `
          <blockquote class="uc08-reasoning">
            <span class="uc08-reasoning-tag">💡 Why this catalog</span>
            ${escapeHtml(m.rerank_reasoning)}
          </blockquote>` : ""}

          ${m.judge_verdict ? `
          <div class="uc08-judge uc08-judge-${judgeTone}">
            <div class="uc08-judge-header">
              <span class="uc08-judge-badge uc08-judge-badge-${judgeTone}">
                ${ { FAITHFUL: "✓", UNFAITHFUL: "✗" }[m.judge_verdict] || "?" }
                ${escapeHtml(m.judge_verdict)}
              </span>
              <span class="uc08-judge-pct">verifier confidence ${judgePct}%</span>
            </div>
            <div class="uc08-judge-reason">${escapeHtml(m.judge_reasoning || "")}</div>
          </div>` : ""}
        </div>`;
  }

  // "Other top matches" disclosure (top 3 alternates). "" when there are none.
  function matchAlternatesHtml(m) {
    if ((m.candidates || []).length <= 1) return "";
    const alts = m.candidates.slice(1, 4);   // top 3 alternates only
    return `
        <details class="uc08-alts">
          <summary>
            <span class="uc08-alts-summary-text">Other top matches</span>
            <span class="uc08-alts-count">${alts.length}</span>
          </summary>
          <div class="uc08-alts-list">
            ${alts.map((c, i) => {
              const altPct = Math.round(c.cosine_score * 100);
              let altTone = "low";
              if (altPct >= 80) altTone = "high";
              else if (altPct >= 60) altTone = "mid";
              return `
              <div class="uc08-alt-card">
                <div class="uc08-alt-rank">#${i + 2}</div>
                <div class="uc08-alt-body">
                  <div class="uc08-alt-title-row">
                    <div class="uc08-alt-name">${escapeHtml(c.name || "—")}</div>
                    <div class="uc08-alt-score uc08-alt-score-${altTone}">${altPct}%</div>
                  </div>
                  <div class="uc08-alt-meta-row">
                    <code class="uc08-alt-id">${escapeHtml(c.catalog_item_id)}</code>
                    ${c.category ? `<span class="uc08-chip uc08-chip-category">${escapeHtml(c.category)}</span>` : ""}
                    ${c.owner_group ? `<span class="uc08-chip uc08-chip-owner">👥 ${escapeHtml(c.owner_group)}</span>` : ""}
                  </div>
                  <div class="uc08-alt-bar-wrap" title="cosine similarity ${c.cosine_score.toFixed(3)}">
                    <div class="uc08-alt-bar uc08-conf-bar-${altTone}" style="width:${altPct}%"></div>
                  </div>
                </div>
              </div>
            `;}).join("")}
          </div>
        </details>`;
  }

  // Preview → real SR. Calls /create-sr with the text the user originally
  // typed, then re-renders the match step in "post-SR" mode so the
  // enrichment form + Proceed button appear.
  async function onPromotePreviewToSr() {
    const err = $("#uc08-error");
    err.classList.add("hidden");
    const btn = $("#uc08-promote");
    btn.disabled = true;
    btn.textContent = "⏳ Creating SR…";
    try {
      const sr = await api("/api/uc08/create-sr", {
        body: { user_text: state.previewText || "" },
      });
      state.sr = sr;
      // Re-run match against the LLM-cleaned title to refresh enrichment.
      const match = await api("/api/uc08/match", {
        body: {
          sr_title: sr.title,
          sr_description: sr.description,
          top_k: 4,
        },
      });
      state.match = match;
      render();
    } catch (e) {
      err.textContent = `${e.message || e}`;
      err.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = "✨ Create SR & proceed";
    }
  }

  function renderEnrichmentForm(sr, chosen, e) {
    if (!e || !chosen) {
      return `<div class="uc08-help">
        No enrichment data — fill values manually after proceeding.
      </div>`;
    }
    const histEvidence = (h) => {
      if (!h?.evidence_label) return "";
      const tone = h.value ? "has" : "none";
      return `<span class="uc08-hist-tag uc08-hist-tag-${tone}">${escapeHtml(h.evidence_label)}</span>`;
    };
    const histValue = (h) => h?.value || "";
    const pCanon = e.priority_canonical || "Medium";
    const pLetter = e.priority_p_letter || "P3";
    const pTone = { P1: "p1", P2: "p2", P4: "p4" }[pLetter] || "p3";

    // SLA due — humanise
    const slaIso = e.sla_due_iso || "";
    let slaHuman = slaIso;
    if (slaIso) {
      try {
        const d = new Date(slaIso);
        const now = new Date();
        const hoursLeft = Math.round((d - now) / 36e5);
        slaHuman = `${slaIso.replaceAll("T", " ").slice(0, 16)} UTC · in ${hoursLeft}h`;
      } catch { /* keep ISO string if date parse fails */ }
    }

    return `
      <h4 class="uc08-section-title">Review &amp; edit values</h4>

      <div class="uc08-form-legend">
        <span class="uc08-legend-pill uc08-legend-ai">🤖 AI-derived</span>
        <span class="uc08-legend-pill uc08-legend-catalog">📦 From catalog</span>
        <span class="uc08-legend-pill uc08-legend-history">📚 From history</span>
      </div>

      <!-- ── Section A — AI-derived ───────────────────────────────── -->
      <section class="uc08-form-section uc08-section-ai">
        <header class="uc08-form-section-header">
          <span class="uc08-form-section-icon">🤖</span>
          <span class="uc08-form-section-title">AI-derived</span>
          <span class="uc08-form-section-hint">title, description, priority</span>
        </header>

        <div class="uc08-fields">
          <label class="uc08-row">
            <span class="uc08-row-label">Title</span>
            <input id="uc08-f-title" class="uc08-input" type="text" maxlength="120"
                   value="${escapeHtml(sr.title)}">
          </label>

          <label class="uc08-row">
            <span class="uc08-row-label">Description <em>(preserved verbatim)</em></span>
            <textarea id="uc08-f-description" class="uc08-input uc08-textarea" rows="2"
                      maxlength="4000">${escapeHtml(sr.description)}</textarea>
          </label>

          <div class="uc08-priority-box uc08-priority-${pTone}">
            <div class="uc08-priority-main">
              <span class="uc08-priority-pill uc08-priority-pill-${pTone}">
                ${escapeHtml(pLetter)} · ${escapeHtml(pCanon)}
              </span>
              <span class="uc08-priority-formula">
                impact <strong>${escapeHtml(e.impact || "—")}</strong>
                · urgency <strong>${escapeHtml(e.urgency || "—")}</strong>
              </span>
            </div>
            <span class="uc08-priority-source">4×4 matrix</span>
          </div>
        </div>
      </section>

      <!-- ── Section B — From catalog ─────────────────────────────── -->
      <section class="uc08-form-section uc08-section-catalog">
        <header class="uc08-form-section-header">
          <span class="uc08-form-section-icon">📦</span>
          <span class="uc08-form-section-title">From catalog</span>
          <span class="uc08-form-section-hint">deterministic — no AI</span>
        </header>

        <div class="uc08-fields uc08-fields-2col">
          <label class="uc08-row">
            <span class="uc08-row-label">Category</span>
            <input id="uc08-f-category" class="uc08-input uc08-input-readonly" type="text" readonly
                   value="${escapeHtml(e.category || "")}">
          </label>

          <label class="uc08-row">
            <span class="uc08-row-label">Assignment group</span>
            <input id="uc08-f-assignment-group" class="uc08-input" type="text"
                   value="${escapeHtml(e.assignment_group_from_catalog || "")}">
          </label>

          <label class="uc08-row uc08-row-wide">
            <span class="uc08-row-label">SLA due</span>
            <input id="uc08-f-sla-due" class="uc08-input uc08-input-readonly" type="text" readonly
                   value="${escapeHtml(slaHuman)}">
          </label>
        </div>
      </section>

      <!-- ── Section C — From history ─────────────────────────────── -->
      <section class="uc08-form-section uc08-section-history">
        <header class="uc08-form-section-header">
          <span class="uc08-form-section-icon">📚</span>
          <span class="uc08-form-section-title">From history</span>
          <span class="uc08-form-section-hint">pattern-matched from past SRs on this catalog</span>
        </header>

        <div class="uc08-fields uc08-fields-2col">
          <label class="uc08-row">
            <span class="uc08-row-label">Requested for ${histEvidence(null)}</span>
            <input id="uc08-f-requested-for" class="uc08-input" type="text"
                   placeholder="USR…"
                   value="${escapeHtml(sr.requested_for || "")}">
          </label>

          <label class="uc08-row">
            <span class="uc08-row-label">Assigned to ${histEvidence(e.assigned_to)}</span>
            <input id="uc08-f-assigned-to" class="uc08-input" type="text"
                   placeholder="USR…"
                   value="${escapeHtml(histValue(e.assigned_to))}">
          </label>

          <label class="uc08-row">
            <span class="uc08-row-label">Approved by ${histEvidence(e.approved_by)}</span>
            <input id="uc08-f-approved-by" class="uc08-input" type="text"
                   placeholder="USR…"
                   value="${escapeHtml(histValue(e.approved_by))}">
          </label>

          <label class="uc08-row">
            <span class="uc08-row-label">CI id ${histEvidence(e.ci_id)}</span>
            <input id="uc08-f-ci-id" class="uc08-input" type="text"
                   placeholder="CI…"
                   value="${escapeHtml(histValue(e.ci_id))}">
          </label>
        </div>
      </section>
    `;
  }

  function renderWrongIntent(sr, m) {
    body.innerHTML = `
      <div class="uc08-step uc08-empty">
        <div class="uc08-sr-pill">${sr ? `🆕 ${escapeHtml(sr.request_id)}` : "🔍 PREVIEW — no SR created"}</div>
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
        <div class="uc08-sr-pill">${sr ? `🆕 ${escapeHtml(sr.request_id)}` : "🔍 PREVIEW — no SR created"}</div>
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
    const chosen = m.auto_pick || m.candidates?.[0];
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
      if (term.includes(s.state)) {
        state.completion = s;
        setTimeout(() => { state.step = "completion"; render(); }, 800);
      } else {
        state.pollTimer = setTimeout(pollStatus, 2000);
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
      ${s.pending_approvals?.length ?
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
    const ok = s?.state === "fulfilled";
    const partial = s?.state === "partial" || s?.tasks_by_state?.failed > 0;
    let heading = "❌ Fulfillment failed";
    if (ok) heading = "🎉 Fulfillment complete";
    else if (partial) heading = "🟡 Partially complete";
    body.innerHTML = `
      <div class="uc08-step uc08-completion">
        <div class="uc08-sr-pill">
          🆕 ${escapeHtml(state.sr.request_id)} → RITM <strong>${escapeHtml(state.ritm.ritm_id)}</strong>
        </div>
        <h4>${heading}</h4>
        <p class="uc08-help">${escapeHtml(s?.display_text || "")}</p>
        <ul class="uc08-final-counts">
          <li>✅ Done: ${s?.tasks_by_state?.done || 0} of ${s?.tasks_total || 0}</li>
          ${s?.tasks_by_state?.failed ?
            `<li>❌ Failed: ${s.tasks_by_state.failed}</li>` : ""}
          ${s?.tasks_by_state?.skipped ?
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
