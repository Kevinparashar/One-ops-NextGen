// UC-2 Similar Tickets UI — POST /api/uc02/similar-tickets.
// Uses the same x-tenant-id / x-user-id / x-role headers chat uses.
// Both this button and the chat path land at find_similar() server-side,
// so the rendered result data is identical.

(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);

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

  const openBtn  = $("#open-similar");
  const modal    = $("#similar-modal");
  const closeBtn = $("#similar-close");
  const form     = $("#similar-form");
  const out      = $("#similar-results");
  if (!openBtn || !modal) return;

  function openModal()  { modal.classList.remove("hidden"); }
  function closeModal() { modal.classList.add("hidden"); out.innerHTML = ""; }
  openBtn.addEventListener("click", openModal);
  closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

  // ── helpers ────────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function titleCase(s) {
    return String(s || "").split(" ")
      .map(w => w ? w.charAt(0).toUpperCase() + w.slice(1) : "")
      .join(" ");
  }
  function fmtDate(s) {
    if (!s) return "—";
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toISOString().slice(0, 10);
  }
  function flagBadge(flag) {
    if (flag === "likely_duplicate")     return '<span class="flag-warn">⚠️ Likely Duplicate</span>';
    if (flag === "resolution_available") return '<span class="flag-ok">✅ Resolution Available</span>';
    return "";
  }

  // ── render source ticket panel (the one the user queried) ──────────────
  function renderSourcePanel(src) {
    if (!src?.ticket_id) return "";
    const status = (src.status || "open").replaceAll("_", " ");
    const meta = [
      src.category    && `<span><strong>Category:</strong> ${escapeHtml(src.category)}</span>`,
      src.service_name && `<span><strong>Service:</strong> ${escapeHtml(src.service_name)}</span>`,
      src.ci_id       && `<span><strong>CI:</strong> ${escapeHtml(src.ci_id)}</span>`,
      src.assignment_group && `<span><strong>Group:</strong> ${escapeHtml(src.assignment_group)}</span>`,
      src.priority    && `<span><strong>Priority:</strong> ${escapeHtml(src.priority)}</span>`,
      src.opened_at   && `<span><strong>Opened:</strong> ${fmtDate(src.opened_at)}</span>`,
    ].filter(Boolean).join("");
    return `
      <div class="similar-source">
        <div class="similar-source-head">YOUR TICKET</div>
        <div>
          <span class="similar-source-id">${escapeHtml(src.ticket_id)}</span>
          <span class="similar-status">${escapeHtml(titleCase(status))}</span>
        </div>
        <div class="similar-source-title">"${escapeHtml(src.title || "")}"</div>
        <div class="similar-source-meta">${meta}</div>
      </div>`;
  }

  // ── render one result row with click-to-expand ─────────────────────────
  function renderResultItem(r, i) {
    const status = (r.status || "open").replaceAll("_", " ");
    const why = (r.why_similar || []).map(s => s.replaceAll("_", " ")).join(", ");
    const details = [
      r.priority         && ["Priority",       r.priority],
      r.category         && ["Category",       r.category],
      r.subcategory      && ["Subcategory",    r.subcategory],
      r.service_name     && ["Service",        r.service_name],
      r.ci_id            && ["Configuration item", r.ci_id],
      r.assigned_to      && ["Assigned to",    r.assigned_to],
      r.assignment_group && ["Group",          r.assignment_group],
      r.opened_at        && ["Opened",         fmtDate(r.opened_at)],
      r.resolved_at      && ["Resolved",       fmtDate(r.resolved_at)],
    ].filter(Boolean);

    const detailsHtml = details
      .map(([k, v]) => `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div>`)
      .join("");

    return `
      <li class="similar-item" data-idx="${i}">
        <div class="similar-head">
          <strong>${i + 1}. ${escapeHtml(r.ticket_id)}</strong>
          <span class="similar-pct">${r.match_pct}% match</span>
          <span class="similar-status">${escapeHtml(titleCase(status))}</span>
          ${flagBadge(r.flag)}
          <span class="similar-toggle">▾ click to expand</span>
        </div>
        <div class="similar-title">"${escapeHtml(r.title || "")}"</div>
        ${r.discriminator ? `<div class="similar-discriminator">${escapeHtml(r.discriminator)}</div>` : ""}
        ${why ? `<div class="similar-common">Common: ${escapeHtml(why)}</div>` : ""}
        <div class="similar-details" hidden>${detailsHtml}</div>
      </li>`;
  }

  function renderResponse(d) {
    if (!d) return '<p class="muted">No response.</p>';
    const srcHtml = renderSourcePanel(d.source_ticket);

    if (!Array.isArray(d.results) || d.results.length === 0) {
      const msg = (d.message || "No similar tickets found.").trim();
      const cap = msg.charAt(0).toUpperCase() + msg.slice(1);
      const note = d.warning ? `<p class="muted">Note: ${escapeHtml(d.warning)}</p>` : "";
      return `${srcHtml}<p class="muted">${escapeHtml(cap)}.</p>${note}`;
    }

    const tfLabel = d.time_filter?.label
      ? ` from <em>${escapeHtml(d.time_filter.label)}</em>` : "";
    const cached = d.cached ? '<span class="similar-cache-pill">cached</span>' : "";
    const header = `<h4>Found ${d.results.length} similar ticket${d.results.length === 1 ? "" : "s"}${tfLabel} ${cached}</h4>`;
    const items = d.results.map(renderResultItem).join("");
    const warn = d.warning ? `<p class="muted">Note: ${escapeHtml(d.warning)}</p>` : "";
    return `${srcHtml}${header}<ol class="similar-list">${items}</ol>${warn}`;
  }

  // ── click-to-expand binding (event delegation) ─────────────────────────
  out.addEventListener("click", (e) => {
    const item = e.target.closest(".similar-item");
    if (!item) return;
    const details = item.querySelector(".similar-details");
    const toggle  = item.querySelector(".similar-toggle");
    if (!details) return;
    const open = details.hasAttribute("hidden");
    if (open) {
      details.removeAttribute("hidden");
      if (toggle) toggle.textContent = "▴ hide";
    } else {
      details.setAttribute("hidden", "");
      if (toggle) toggle.textContent = "▾ click to expand";
    }
  });

  // ── submit ─────────────────────────────────────────────────────────────
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    out.innerHTML = '<p class="muted">Searching…</p>';

    const body = {
      ticket_id:           $("#similar-ticket-id").value.trim(),
      max_results:         Number.parseInt($("#similar-max-results").value, 10) || 5,
      same_category_only:  $("#similar-same-cat").checked,
      prefer_status:       $("#similar-prefer-status").value || "any",
    };
    const svc = $("#similar-service-id").value;
    if (svc) body.service_id = svc;
    // Convert hours-dropdown to structured TimeFilter (relative_days).
    // The dropdown values are pre-canned multiples of 24h for chat parity.
    const winH = $("#similar-window").value;
    if (winH) {
      const hours = Number.parseInt(winH, 10);
      const days = Math.max(1, Math.round(hours / 24));
      const labelEl = $("#similar-window").selectedOptions[0];
      body.time_filter = {
        relative_days: days,
        label: labelEl?.textContent || `last ${days} days`,
      };
    }

    try {
      // Live agent/tool panel (shared with chat) + the normal results view.
      out.innerHTML = "";
      const data = await globalThis.oneopsLiveStream({
        url: "/api/uc02/similar-tickets/stream",
        body, headers: headers(), mount: out,
      });
      if (!data) return;                 // panel head already shows the error
      if (data.error || data.final_status === "failed") {
        const p = document.createElement("p");
        p.className = "error";
        p.textContent = "Error: " + (data.error || "request failed");
        out.appendChild(p);
        return;
      }
      const results = document.createElement("div");
      results.innerHTML = renderResponse(data);
      out.appendChild(results);
    } catch (err) {
      out.innerHTML = `<p class="error">Network error: ${escapeHtml(err.message || String(err))}</p>`;
    }
  });
})();
