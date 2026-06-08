// UC-5 Triage UI — talks to /api/uc05/{queue-summary,queue,propose,decide}
// Uses the same x-tenant-id / x-user-id / x-role headers the chat panel sends.

(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);

  // ── Reuse the global header builder from app.js ───────────────────────────
  function headers() {
    const tenantSel = $("#tenant");
    const userSel = $("#user");
    const roleSel = $("#role");
    return {
      "Content-Type": "application/json",
      "x-tenant-id": tenantSel?.value.trim() || "T001",
      "x-user-id":   userSel?.value.trim()   || "u_demo",
      "x-role":      roleSel?.value.trim()   || "service_desk_agent",
    };
  }

  // ── Vocabulary for editable dropdowns ─────────────────────────────────────
  const IMPACT_VALUES   = ["Low", "On Users", "On Department", "On Business"];
  const URGENCY_VALUES  = ["Low", "Medium", "High", "Urgent"];
  const PRIORITY_VALUES = ["Low", "Medium", "High", "Urgent"];

  // Fields each service triages (matches queue.py whitelist).
  const TRIAGE_FIELDS = {
    incident: [
      ["category",         "text"],
      ["subcategory",      "text"],
      ["assigned_to",      "text"],
      ["impact",           IMPACT_VALUES],
      ["urgency",          URGENCY_VALUES],
      ["priority",         PRIORITY_VALUES],
      ["assignment_group", "text"],
    ],
    request: [
      ["category",         "text"],
      ["assigned_to",      "text"],
      ["priority",         PRIORITY_VALUES],
      ["assignment_group", "text"],
    ],
  };

  // ── State ─────────────────────────────────────────────────────────────────
  let currentService = null;   // "incident" | "request" | null
  let currentProposal = null;  // last proposal returned by /propose

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const modal       = $("#triage-modal");
  const openBtn     = $("#open-triage");
  const closeBtn    = $("#triage-close");
  const pillTotal   = $("#triage-pill-total");
  const landing     = $("#triage-landing");
  const listPane    = $("#triage-list");
  const proposalPane = $("#triage-proposal");
  const countIncident = $("#triage-count-incident");
  const countRequest  = $("#triage-count-request");
  const rows        = $("#triage-rows");
  const listTitle   = $("#triage-list-title");
  const propTitle   = $("#triage-proposal-title");
  const propMeta    = $("#triage-meta");
  const propFields  = $("#triage-fields");
  const dupWarn     = $("#triage-dup-warning");
  const applyBtn    = $("#triage-apply");
  const discardBtn  = $("#triage-discard");
  const backToList  = $("#triage-back-to-list");
  const backToLanding = $("#triage-back-to-landing");

  // ── Open / close ──────────────────────────────────────────────────────────
  function openModal() {
    modal.classList.remove("hidden");
    showLanding();
    refreshCounts();
  }
  function closeModal() { modal.classList.add("hidden"); }
  openBtn.addEventListener("click", openModal);
  closeBtn.addEventListener("click", closeModal);

  function showLanding() {
    landing.classList.remove("hidden");
    listPane.classList.add("hidden");
    proposalPane.classList.add("hidden");
  }
  function showList() {
    landing.classList.add("hidden");
    listPane.classList.remove("hidden");
    proposalPane.classList.add("hidden");
  }
  function showProposal() {
    landing.classList.add("hidden");
    listPane.classList.add("hidden");
    proposalPane.classList.remove("hidden");
  }

  backToList.addEventListener("click", () => { if (currentService) loadList(currentService); });
  backToLanding.addEventListener("click", showLanding);

  // ── Refresh queue-summary counts ──────────────────────────────────────────
  async function refreshCounts() {
    countIncident.textContent = "…";
    countRequest.textContent = "…";
    pillTotal.textContent = "…";
    try {
      const r = await fetch("/api/uc05/queue-summary", { headers: headers() });
      if (!r.ok) throw new Error(`queue-summary ${r.status}`);
      const data = await r.json();
      const inc = data.incidents.untriaged_count;
      const req = data.requests.untriaged_count;
      countIncident.textContent = inc;
      countRequest.textContent  = req;
      pillTotal.textContent     = String(inc + req);
    } catch {
      countIncident.textContent = countRequest.textContent = "?";
      pillTotal.textContent = "?";
    }
  }

  // Wire type-card clicks
  document.querySelectorAll(".triage-type-card").forEach((card) => {
    card.addEventListener("click", () => loadList(card.dataset.svc));
  });

  // ── Load list for a service ───────────────────────────────────────────────
  async function loadList(serviceId) {
    currentService = serviceId;
    listTitle.textContent =
      serviceId === "incident" ? "Untriaged incidents" : "Untriaged requests";
    rows.innerHTML = `<div class="triage-empty">Loading…</div>`;
    showList();
    try {
      const r = await fetch(`/api/uc05/queue?service_id=${encodeURIComponent(serviceId)}`,
                             { headers: headers() });
      if (!r.ok) throw new Error(`queue ${r.status}`);
      const list = await r.json();
      if (!Array.isArray(list) || list.length === 0) {
        rows.innerHTML = `<div class="triage-empty">Nothing untriaged. 🎉</div>`;
        return;
      }
      rows.innerHTML = "";
      list.forEach((item) => {
        const row = document.createElement("div");
        row.className = "triage-row";
        row.innerHTML = `
          <div class="triage-row-title">${escapeHtml(item.ticket_id)} — ${escapeHtml(item.title || "")}</div>
          <div class="triage-row-desc">${escapeHtml(item.description_snippet || "")}</div>
          <div class="triage-row-meta">
            <span class="triage-row-miss">${item.missing_field_count} fields missing</span>
            <span>${escapeHtml(item.status || "")}</span>
            <span>${escapeHtml(item.created_at || "")}</span>
          </div>`;
        row.addEventListener("click", () => showPreview(item));
        rows.appendChild(row);
      });
    } catch (e) {
      rows.innerHTML = `<div class="triage-empty">Failed to load: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ── Stage 2.5: preview ticket + "Run AI Triage" button ──────────────────
  function showPreview(item) {
    currentProposal = null;
    propTitle.textContent = `${item.ticket_id}`;
    propMeta.innerHTML = `
      <div><b>${escapeHtml(item.title || "")}</b></div>
      <div class="muted small">${escapeHtml(item.description_snippet || "")}</div>
      <div class="muted small">Status: ${escapeHtml(item.status || "")}
        · Created: ${escapeHtml(item.created_at || "")}</div>
      <div style="margin-top:8px;"><b>${item.missing_field_count}</b> field(s) missing:
        <code>${item.missing_fields.map(escapeHtml).join(", ")}</code></div>
    `;
    dupWarn.classList.add("hidden");
    propFields.innerHTML = `
      <div class="triage-empty" style="text-align:center;padding:24px;">
        <button type="button" class="primary" id="triage-run-ai">🤖 Run AI Triage</button>
        <div class="muted small" style="margin-top:8px;">
          AI will fill the missing fields above. You review before save.
        </div>
      </div>`;
    applyBtn.style.display = "none";
    discardBtn.textContent = "← Back";
    showProposal();
    $("#triage-run-ai").addEventListener("click", () => runPropose(item));
  }

  // ── Run /propose ──────────────────────────────────────────────────────────
  async function runPropose(item) {
    propTitle.textContent = `🤖 Triaging ${item.ticket_id}`;
    propMeta.innerHTML = `<div>Title: ${escapeHtml(item.title)}</div>`;
    propFields.innerHTML = `<div class="triage-empty">Running AI… ~2s</div>`;
    dupWarn.classList.add("hidden");
    showProposal();
    try {
      // Live agent/tool panel (shared with chat) + the normal proposal card.
      propFields.innerHTML = "";
      const p = await globalThis.oneopsLiveStream({
        url: "/api/uc05/propose/stream",
        body: { ticket_id: item.ticket_id, service_id: currentService },
        headers: headers(), mount: propFields,
      });
      if (!p) return;                    // panel head already shows the error
      if (p.error || p.final_status === "failed") {
        const d = document.createElement("div");
        d.className = "triage-empty";
        d.textContent = "Propose failed: " + (p.error || "error");
        propFields.appendChild(d);
        return;
      }
      currentProposal = p;
      renderProposal(p, item);
      applyBtn.style.display = "";
      discardBtn.textContent = "❌ Cancel";
    } catch (e) {
      propFields.innerHTML = `<div class="triage-empty">Propose failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ── Render the editable proposal card ─────────────────────────────────────
  function renderProposal(p, item) {
    propMeta.innerHTML = `
      <div><b>${escapeHtml(p.ticket_id)}</b> — ${escapeHtml(item.title)}</div>
      <div>Overall confidence: <b>${p.overall_confidence_score}</b>
           · tier: <b>${escapeHtml(p.confidence_tier)}</b>
           · risk: <b>${escapeHtml(p.risk_class)}</b></div>
    `;

    if (p.duplicate_verdict === "duplicate" && p.top_duplicate_id) {
      dupWarn.classList.remove("hidden");
      dupWarn.innerHTML =
        `⚠️ Possible duplicate of <b>${escapeHtml(p.top_duplicate_id)}</b> ` +
        `(score ${p.top_duplicate_score})`;
    } else {
      dupWarn.classList.add("hidden");
    }

    const fields = TRIAGE_FIELDS[p.service_id] || [];
    let html = `<div class="triage-field-grid">`;
    for (const [col, type] of fields) {
      const value = proposalValue(p, col);
      const basis = proposalBasis(p, col);
      const inputId = `tf-${col}`;
      let inputHtml;
      if (Array.isArray(type)) {
        const opts = type.map((v) =>
          `<option value="${v}"${v === value ? " selected" : ""}>${v}</option>`).join("");
        inputHtml = `<select id="${inputId}" data-field="${col}"><option value=""></option>${opts}</select>`;
      } else {
        const v = value == null ? "" : String(value);
        inputHtml = `<input type="text" id="${inputId}" data-field="${col}" value="${escapeAttr(v)}" />`;
      }
      html += `<label>${humanLabel(col)}</label>${inputHtml}<span class="basis">${escapeHtml(basis)}</span>`;
    }
    // Tags row (read-only display)
    if (p.suggested_tags?.length) {
      html += `<label>tags</label><div>${p.suggested_tags.map(escapeHtml).join(", ")}</div><span class="basis">LLM</span>`;
    }
    html += `</div>`;
    propFields.innerHTML = html;
  }

  function proposalValue(p, col) {
    const map = {
      "category":         p.suggested_category,
      "subcategory":      p.suggested_subcategory,
      "service_name":     null, // dropped from spec but kept editable
      "catalog_item_id":  null, // dropped from spec but kept editable
      "assigned_to":      p.suggested_assigned_to,
      "ci_id":            p.suggested_ci_id,
      "impact":           p.suggested_impact,
      "urgency":          p.suggested_urgency,
      "priority":         p.suggested_priority,
      "assignment_group": p.suggested_assignment_group,
    };
    return map[col];
  }
  function proposalBasis(p, col) {
    // confidence_tier and rationale aren't per-field in the simple shortcuts; use generic
    if (["impact", "urgency", "priority"].includes(col)) {
      return p.prioritization_basis?.[col] ? p.prioritization_basis[col] : "—";
    }
    if (col === "assignment_group") return `${p.assignment_basis} (${p.assignment_confidence})`;
    return "kNN";
  }
  function humanLabel(col) { return col.replaceAll("_", " "); }

  // ── Apply / Cancel ────────────────────────────────────────────────────────
  applyBtn.addEventListener("click", () => sendDecide("yes"));
  discardBtn.addEventListener("click", () => {
    if (!currentProposal) { if (currentService) { loadList(currentService); } return; }
    sendDecide("no");
  });

  async function sendDecide(choice) {
    if (!currentProposal) return;
    const body = { proposal_id: currentProposal.proposal_id, choice: choice };
    if (choice === "yes") {
      // Collect edits — send everything the form has (server validates against
      // the per-service whitelist).
      const final = {};
      const fields = TRIAGE_FIELDS[currentProposal.service_id] || [];
      for (const [col] of fields) {
        const el = $(`#tf-${col}`);
        if (el?.value && el.value.trim()) {
          final[col] = el.value.trim();
        }
      }
      body.final_values = final;
    }
    applyBtn.disabled = true;
    discardBtn.disabled = true;
    try {
      const r = await fetch("/api/uc05/decide", {
        method: "POST", headers: headers(), body: JSON.stringify(body),
      });
      const out = await r.json();
      if (!r.ok) {
        toast(`decide failed: ${out.detail || r.status}`);
        return;
      }
      if (out.outcome === "applied") {
        toast(`✓ Ticket ${currentProposal.ticket_id} updated in database — assigned to ${final_assignment_or(out)}`);
      } else {
        toast(`Discarded. ${currentProposal.ticket_id} stays open.`);
      }
      currentProposal = null;
      refreshCounts();
      if (currentService) loadList(currentService); else showLanding();
    } catch (e) {
      toast(`decide failed: ${e.message}`);
    } finally {
      applyBtn.disabled = false;
      discardBtn.disabled = false;
    }
  }

  function final_assignment_or(out) {
    const af = out.applied_fields || {};
    return af.assignment_group || "—";
  }

  // ── Toast ─────────────────────────────────────────────────────────────────
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "triage-toast";
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4500);
  }

  // ── Tiny helpers ──────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // Refresh counts initially after a short delay (so identity-options finish loading)
  globalThis.addEventListener("load", () => setTimeout(refreshCounts, 1200));
})();
